"""سقف تعداد/ریسک پر است: اسکات رد نمی‌کند، از مالک می‌پرسد («✅ می‌دانم، باز کن»). پوزیشن خلاف جهت مثل قبل."""
import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_scout_multi import row
from tests.test_scout_tracker import make_scout


class Override(unittest.TestCase):
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

    def tearDown(self):
        self._d.cleanup()

    def approve(self, r):
        self.sc.alert(r, 27.0)
        aid = str(self.sc.n)
        self.sc.decide(aid, True, "cb")
        return aid

    def tap(self, data):
        self.sc.handle_update({"callback_query": {"from": {"id": 1}, "data": data, "id": "q"}})

    def events(self):
        with self.sc.journal.open(encoding="utf-8") as fh:
            return [(r["event"], r["note"]) for r in csv.DictReader(fh)]

    def test_risk_cap_asks_then_owner_opens(self):
        self.approve(row(sl_dist=10.0))                       # 10$
        self.approve(row(sl_dist=10.0))                       # 20$ = سقف
        aid = self.approve(row(sl_dist=4.0))                  # سومی: هم سقف تعداد هم ریسک
        self.assertEqual(len(self.sc.open), 2)
        self.assertIn(aid, self.sc.pending)                   # رد نشد؛ منتظر تصمیم شما
        text, buttons = self.sc.tg.edits[-1][1], self.sc.tg.edits[-1][2]
        self.assertIn("باز هم باز کنم؟", text)
        self.assertIn("24.00$", text)                         # 20 + 4
        self.assertEqual([b["callback_data"] for b in buttons[0]], [f"a|{aid}|o", f"a|{aid}|n"])
        self.assertIn("limit_confirm", [e for e, _ in self.events()])
        self.tap(f"a|{aid}|o")
        self.assertEqual(len(self.sc.open), 3)
        self.assertNotIn(aid, self.sc.pending)
        self.assertIn("over_cap=owner", [n for e, n in self.events() if e == "opened"][-1])

    def test_hold_mode_scenario_second_position_asks(self):
        self.sc.mode = "hold"                                 # مثل امروز: یک پوزیشن، اضطراری تا 20$
        self.approve(row(sl_dist=6.67))
        self.assertGreater(self.sc.open_risk_usd(), 19.9)
        aid = self.approve(row(sl_dist=5.0))
        self.assertEqual(len(self.sc.open), 1)
        self.assertIn("باز هم باز کنم؟", self.sc.tg.edits[-1][1])
        self.tap(f"a|{aid}|o")
        self.assertEqual(len(self.sc.open), 2)

    def test_reject_after_ask_and_expiry_are_followed(self):
        self.sc.whatif_enabled = True
        self.approve(row(sl_dist=10.0))
        self.approve(row(sl_dist=10.0))
        aid = self.approve(row(sl_dist=4.0))
        self.assertEqual(self.sc._whatif, [])                 # هنوز تصمیم نگرفته‌اید
        self.tap(f"a|{aid}|n")
        self.assertEqual(self.sc._whatif[-1]["decision"], "rejected")
        aid2 = self.approve(row(sl_dist=4.0))
        self.sc.pending[aid2]["t"] -= 10_000
        self.sc.expire()
        self.assertEqual(self.sc._whatif[-1]["decision"], "expired")
        self.assertEqual(len(self.sc.open), 2)

    def test_opposite_side_still_not_opened_without_asking(self):
        self.approve(row(side="BUY"))
        self.sc.alert(row(side="SELL"), 27.0)
        self.assertIn("فعلاً قابل باز شدن نیست", self.sc.tg.sent[-1][0])
        aid = str(self.sc.n)
        self.sc.decide(aid, True, "cb")
        self.assertNotIn(aid, self.sc.pending)
        self.assertIn("خلاف جهت", self.sc.tg.edits[-1][1])
        self.tap(f"a|{aid}|o")                                # دکمه‌ای نبود؛ حتی اگر بیاید اثری ندارد
        self.assertEqual(len(self.sc.open), 1)

    def test_alert_text_says_it_will_ask(self):
        self.approve(row(sl_dist=10.0))
        self.approve(row(sl_dist=10.0))
        self.sc.alert(row(sl_dist=4.0), 27.0)
        self.assertIn("از شما می‌پرسد", self.sc.tg.sent[-1][0])
        self.assertNotIn("فعلاً قابل باز شدن نیست", self.sc.tg.sent[-1][0])


if __name__ == "__main__":
    unittest.main()
