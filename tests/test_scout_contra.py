"""«🔄 خلاف جهت»: مالک می‌تواند یک سیگنال را برعکس باز کند. اول سطح‌ها را نشان می‌دهد و تأیید می‌گیرد؛
سقف‌ها و سؤال پوزیشن خلاف جهت مثل همیشه؛ در ژورنال و آمار جدا («contra») ثبت می‌شود."""
import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_signal_audit as sa
from tools import scout_tracker as trk
from tools.scout_bot import JOURNAL_FIELDS

BAR = pd.Timestamp("2026-10-06 09:15")


def row(side="BUY"):
    return pd.Series({"setup": "rejection", "side": side, "ref_close": 4100.0, "sl_dist": 6.0,
                      "tp_dist": 12.0, "atr": 5.0, "bar": BAR})


class Contra(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.admins = {1}
        self.sc.client.get_tick.return_value = NS(time=0, bid=4099.8, ask=4100.1)
        self.sc.client.positions.return_value = []
        self.sc.client.send_market_order.return_value = NS(ok=True, position=77, order=77, deal=0,
                                                           price=4099.8, comment="")

    def tearDown(self):
        self._d.cleanup()

    def tap(self, data, uid=1):
        self.sc.handle_update({"callback_query": {"from": {"id": uid, "first_name": "Hamid"}, "data": data, "id": "q"}})

    def alert(self, side="BUY"):
        self.sc.alert(row(side), 27.0)
        return str(self.sc.n)

    def events(self):
        with self.sc.journal.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_alert_has_contra_button_in_second_row(self):
        aid = self.alert()
        buttons = self.sc.tg.sent[-1][1]
        self.assertEqual([b["callback_data"] for b in buttons[0]], [f"a|{aid}|y", f"a|{aid}|n", f"x|{aid}"])
        self.assertEqual(buttons[1][0]["callback_data"], f"v|{aid}")
        self.assertIn("SELL", buttons[1][0]["text"])

    def test_ask_shows_mirrored_levels_then_back(self):
        aid = self.alert()
        self.tap(f"v|{aid}")
        mid, text, buttons = self.sc.tg.edits[-1]
        self.assertIn("خلاف جهت سیگنال؟", text)
        self.assertIn("SELL به‌جای BUY", text)
        self.assertIn("حد ضرر 4106.00", text)
        self.assertIn("هدف 2R 4088.00", text)
        self.assertEqual([b["callback_data"] for b in buttons[0]], [f"v|{aid}|y", f"v|{aid}|n"])
        self.sc.client.send_market_order.assert_not_called()
        self.tap(f"v|{aid}|n")
        self.assertEqual(self.sc.tg.edits[-1][2][1][0]["callback_data"], f"v|{aid}")    # دکمه‌های اصلی برگشت
        self.assertEqual(self.sc.pending[aid]["row"].side, "BUY")
        self.assertIn("contra_confirm", [e["event"] for e in self.events()])

    def test_confirm_opens_opposite_side(self):
        aid = self.alert()
        self.tap(f"v|{aid}")
        self.tap(f"v|{aid}|y")
        args = self.sc.client.send_market_order.call_args
        self.assertEqual(args.args[0], "SELL")
        info = self.sc.open[77]
        self.assertEqual((info["side"], info["contra"]), ("SELL", True))
        self.assertAlmostEqual(info["sl"], 4099.8 + 6.0, places=2)                   # همان فاصله، طرف مقابل
        self.assertIn("خلاف سیگنال: SELL باز شد", self.sc.tg.edits[-1][1])
        opened = [e for e in self.events() if e["event"] == "opened"][-1]
        self.assertIn("contra=1", opened["note"])
        self.assertEqual(opened["side"], "SELL")
        self.assertTrue(trk.load_state(self.sc.state_path)[77]["contra"])

    def test_invalid_when_price_past_reversed_stop(self):
        aid = self.alert()
        self.sc.client.get_tick.return_value = NS(time=0, bid=4106.5, ask=4106.8)   # بالای حد ضرر فروش
        self.tap(f"v|{aid}")
        self.tap(f"v|{aid}|y")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("از حد ضرر هشدار رد شده", self.sc.tg.edits[-1][1])

    def test_hedge_question_still_asked_and_reject_tracks_original(self):
        self.sc.whatif_enabled = True
        self.sc.open[5] = {"setup": "x", "side": "BUY", "entry": 4095.0, "sl": 4089.0, "risk": 6.0, "emerg": 4077.0,
                           "hold": False, "r1": False, "warned": False, "status_mid": None}
        aid = self.alert("BUY")                                                      # سیگنال BUY؛ برعکسش SELL
        self.tap(f"v|{aid}")
        self.tap(f"v|{aid}|y")
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("چه کنم؟", self.sc.tg.edits[-1][1])                            # سؤال خلاف جهت
        self.tap(f"a|{aid}|n")
        self.assertEqual(self.sc._whatif[-1]["side"], "BUY")                         # خود سیگنال پیگیری می‌شود

    def test_non_admin_ignored(self):
        aid = self.alert()
        self.tap(f"v|{aid}", uid=9)
        self.tap(f"v|{aid}|y", uid=9)
        self.sc.client.send_market_order.assert_not_called()

    def test_expired_alert(self):
        self.tap("v|99")
        self.assertIn("دیگر معتبر نیست", self.sc.tg.acks[-1])


class AuditLabel(unittest.TestCase):
    def test_contra_decision_in_audit(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.csv"
            with p.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
                w.writeheader()
                for ev, note in (("alert", ""), ("opened", "broker_sl=1;contra=1")):
                    w.writerow({"ts": "2026-10-06T00:00:00", "event": ev, "alert": "1", "setup": "x", "side": "BUY",
                                "bar": "2026-10-06 09:15:00", "price": "4100", "sl": "4094", "note": note})
            self.assertEqual(list(sa.read_alerts(p).decision), ["contra"])
            self.assertEqual(sa.DECISION_FA["contra"], "خلاف جهت گرفتید")


if __name__ == "__main__":
    unittest.main()
