"""پوزیشن خلاف جهت: اسکات رد نمی‌کند، می‌پرسد — «🔄 ببند و جهت را عوض کن»، «✅ هر دو بمانند»، «❌ رد»."""
import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_scout_multi import row
from tests.test_scout_tracker import make_scout


class Hedge(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.admins = {1}
        self.sc.client.get_tick.return_value = NS(time=0, bid=4175.4, ask=4175.69)
        self.sc.client.positions.return_value = []
        self.n = 0

        def order(side, lot, sl=None, tp=None, comment=""):
            self.n += 1
            return NS(ok=True, position=100 + self.n, order=100 + self.n, deal=0,
                      price=4175.69 if side == "BUY" else 4175.4, comment="")
        self.sc.client.send_market_order.side_effect = order
        self.sc.client.close_position.return_value = NS(ok=True, price=4175.4, comment="")
        self.sc.alert(row(side="BUY"), 27.0)
        self.sc.decide(str(self.sc.n), True, "cb")              # #101 BUY باز
        self.sc._live[101] = {"profit": 0.61}
        self.sc.alert(row(side="SELL", ref=4175.4), 27.0)
        self.aid = str(self.sc.n)
        self.sc.decide(self.aid, True, "cb")                     # سؤال خلاف جهت

    def tearDown(self):
        self._d.cleanup()

    def tap(self, code):
        self.sc.handle_update({"callback_query": {"from": {"id": 1}, "data": f"a|{self.aid}|{code}", "id": "q"}})

    def events(self):
        with self.sc.journal.open(encoding="utf-8") as fh:
            return [(r["event"], r["note"]) for r in csv.DictReader(fh)]

    def test_question_with_three_choices_and_live_pl(self):
        self.assertIn(self.aid, self.sc.pending)
        text, buttons = self.sc.tg.edits[-1][1], self.sc.tg.edits[-1][2]
        self.assertIn("پوزیشن خرید باز است", text)
        self.assertIn("#101 (+0.61$)", text)
        self.assertEqual([b[0]["callback_data"] for b in buttons],
                         [f"a|{self.aid}|r", f"a|{self.aid}|b", f"a|{self.aid}|n"])
        self.assertIn("🔄 خرید را ببند، فروش را باز کن", buttons[0][0]["text"])
        self.assertIn("hedge_confirm", [e for e, _ in self.events()])
        self.sc.client.close_position.assert_not_called()       # بدون جواب شما چیزی بسته نمی‌شود

    def test_reverse_closes_buy_then_opens_sell(self):
        self.tap("r")
        self.sc.client.close_position.assert_called_once()
        self.assertEqual(self.sc.client.close_position.call_args.args[0], 101)
        self.assertTrue(self.sc.open[101]["closing"])            # monitor نتیجه را ثبت می‌کند
        self.assertEqual(self.sc.open[102]["side"], "SELL")
        self.assertNotIn(self.aid, self.sc.pending)
        self.assertIn("hedge=reverse", [n for e, n in self.events() if e == "opened"][-1])
        self.assertIn(("closed", "reverse"), self.events())
        self.assertEqual(self.sc.opposite_of("SELL"), [])
        self.assertAlmostEqual(self.sc.open_risk_usd(), 6.0, places=1)   # فقط فروش جدید

    def test_reverse_close_failure_opens_nothing_and_keeps_alert(self):
        self.sc.client.close_position.return_value = NS(ok=False, price=0.0, comment="off quotes")
        self.tap("r")
        self.sc.client.send_market_order.assert_called_once()    # فقط همان خرید اول
        self.assertIn(self.aid, self.sc.pending)
        self.assertIn("بستن #101 انجام نشد", self.sc.tg.edits[-1][1])
        self.assertEqual(self.sc.tg.edits[-1][2][0][0]["callback_data"], f"a|{self.aid}|r")

    def test_reverse_checks_signal_still_valid_before_closing(self):
        self.sc.client.get_tick.return_value = NS(time=0, bid=4182.0, ask=4182.3)   # بالای حد ضرر فروش
        self.tap("r")
        self.sc.client.close_position.assert_not_called()       # خرید بی‌دلیل بسته نشد
        self.assertIn("پوزیشن قبلی دست نخورد", self.sc.tg.edits[-1][1])
        self.assertEqual(len(self.sc.open), 1)

    def test_keep_both(self):
        self.tap("b")
        self.sc.client.close_position.assert_not_called()
        self.assertEqual(sorted(i["side"] for i in self.sc.open.values()), ["BUY", "SELL"])
        self.assertIn("hedge=both", [n for e, n in self.events() if e == "opened"][-1])

    def test_keep_both_then_cap_question_keeps_choice(self):
        self.sc.open[101]["sl"] = self.sc.open[101]["entry"] - 16.0     # ریسک خرید 16$ → با فروش 6$ > 20$
        self.tap("b")
        self.assertIn("باز هم باز کنم؟", self.sc.tg.edits[-1][1])
        self.assertEqual(self.sc.tg.edits[-1][2][0][0]["callback_data"], f"a|{self.aid}|q")
        self.tap("q")
        self.assertEqual(len(self.sc.open), 2)

    def test_reject_is_followed(self):
        self.sc.whatif_enabled = True
        self.tap("n")
        self.assertNotIn(self.aid, self.sc.pending)
        self.assertEqual(self.sc._whatif[-1]["decision"], "rejected")
        self.assertEqual(len(self.sc.open), 1)


if __name__ == "__main__":
    unittest.main()
