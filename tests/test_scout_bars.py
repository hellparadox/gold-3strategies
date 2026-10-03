"""هیچ سیگنالی گم نشود: کندل فقط بعد از پردازش موفق «دیده‌شده» است، کندل‌های جاافتاده (تا ۴) با برچسب
«دیرهنگام» بررسی می‌شوند، هشدار تکراری هرگز (حتی بعد از ری‌استارت)؛ به‌علاوهٔ شمارهٔ پوزیشن، ادمین بسته
و تلاش دوبارهٔ بستن دستی."""
import csv
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

from tests.test_scout_tracker import make_scout
from tools.scout_bot import JOURNAL_FIELDS

IDX = pd.date_range("2026-10-05 09:00", periods=12, freq="15min")
FRAME = pd.DataFrame({"close": 4150.0, "tenkan": 4149.0}, index=IDX)


def sigs(*bars, side="BUY", setup="cloud_break"):
    return pd.DataFrame([{"bar": b, "setup": setup, "side": side, "ref_close": 4150.0, "atr": 5.0,
                          "sl_dist": 6.0, "tp_dist": 12.0} for b in bars])


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.paused, self.sc.merge_setups, self.sc.last_bar = False, True, None
        self.sc.backfill_bars, self.sc._bar_fail, self.sc._seen_keys = 4, {}, None
        self.sc.client.spread_points.return_value = 27.0
        self.sc.client.positions.return_value = []

    def tearDown(self):
        self._d.cleanup()

    def alerts(self):
        return [t for t, _ in self.sc.tg.sent if "cloud_break" in t or "exhaustion" in t]

    def run_bars(self, frame, signals):
        with unittest.mock.patch("tools.scout_bot.detect", return_value=signals):
            self.sc.process_new_bars(frame)


class Bars(Base):
    def test_new_bar_once_and_dedup(self):
        self.sc.last_bar = IDX[-2]
        self.run_bars(FRAME, sigs(IDX[-1]))
        self.run_bars(FRAME, sigs(IDX[-1]))                      # همان کندل، حلقهٔ بعد
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.sc.last_bar, IDX[-1])
        self.assertNotIn("دیرهنگام", self.alerts()[0])

    def test_failure_is_retried_without_duplicate(self):
        self.sc.last_bar = IDX[-2]
        real_log, calls = self.sc.log, {"n": 0}

        def flaky(**row):                                       # ژورنال قفل (مثلاً CSV باز در Excel)
            if row.get("event") == "alert" and calls["n"] == 0:
                calls["n"] += 1
                raise PermissionError("journal locked")
            return real_log(**row)
        self.sc.log = flaky
        with self.assertRaises(PermissionError):
            self.run_bars(FRAME, sigs(IDX[-1]))
        self.assertEqual(self.sc.last_bar, IDX[-2])              # کندل «دیده‌شده» حساب نشد
        self.run_bars(FRAME, sigs(IDX[-1]))
        self.assertEqual(self.sc.last_bar, IDX[-1])
        self.assertEqual(len(self.alerts()), 1)                  # هشدار دوباره فرستاده نشد

    def test_gives_up_after_three_failures_with_notice(self):
        self.sc.last_bar = IDX[-2]
        with unittest.mock.patch.object(self.sc, "process_bar", side_effect=ValueError("boom")):
            for _ in range(2):
                with self.assertRaises(ValueError):
                    self.run_bars(FRAME, sigs(IDX[-1]))
            self.run_bars(FRAME, sigs(IDX[-1]))
        self.assertEqual(self.sc.last_bar, IDX[-1])
        self.assertIn("سه بار خطا داد", self.sc.tg.sent[-1][0])

    def test_stalled_loop_backfills_up_to_four_with_late_label(self):
        self.sc.last_bar = IDX[-7]                              # ۶ کندل جاافتاده
        self.run_bars(FRAME, sigs(*IDX[-6:]))
        a = self.alerts()
        self.assertEqual(len(a), 4)                              # فقط یک ساعت اخیر
        self.assertEqual(sum("دیرهنگام" in t for t in a), 3)     # جدیدترین برچسب ندارد
        self.assertIn("3 کندل قبل", a[0])

    def test_restart_checks_recent_bars_but_never_repeats(self):
        with self.sc.journal.open("w", newline="", encoding="utf-8") as fh:   # قبل از ری‌استارت فرستاده شده
            w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
            w.writeheader()
            w.writerow({"ts": "2026-10-05T11:31:00", "event": "alert", "alert": "1", "setup": "cloud_break",
                        "side": "BUY", "bar": str(IDX[-2])})
        self.sc.market_open = lambda: True
        self.run_bars(FRAME, sigs(IDX[-2], IDX[-1]))
        a = self.alerts()
        self.assertEqual(len(a), 1)                              # فقط کندل جدید
        self.assertNotIn("دیرهنگام", a[0])

    def test_restart_on_weekend_sends_nothing_old(self):
        self.sc.market_open = lambda: False
        self.run_bars(FRAME, sigs(*IDX[-4:]))
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.sc.last_bar, IDX[-1])

    def test_paused_and_quiet_still_recorded_not_lost(self):
        self.sc.last_bar = IDX[-2]
        self.sc.paused = True
        self.run_bars(FRAME, sigs(IDX[-1]))
        with self.sc.journal.open(encoding="utf-8") as fh:
            self.assertEqual([r["event"] for r in csv.DictReader(fh)], ["suppressed"])


class TicketAdminClose(Base):
    def test_position_ticket_from_deal(self):
        self.sc.client.position_id_for_deal.return_value = 777
        self.assertEqual(self.sc.position_ticket(NS(position=0, deal=55, order=100)), 777)
        self.assertEqual(self.sc.position_ticket(NS(position=900, deal=55, order=100)), 900)
        self.sc.client.position_id_for_deal.side_effect = RuntimeError("deal not in history yet")
        self.assertEqual(self.sc.position_ticket(NS(position=0, deal=55, order=100)), 100)
        self.sc.client.position_id_for_deal.side_effect = None
        self.sc.client.position_id_for_deal.return_value = None
        self.assertEqual(self.sc.position_ticket(NS(position=0, deal=0, order=100)), 100)

    def test_approval_tracks_position_ticket(self):
        self.sc.client.get_tick.return_value = NS(time=0, bid=4150.0, ask=4150.3)
        self.sc.client.send_market_order.return_value = NS(ok=True, position=0, deal=55, order=100,
                                                           price=4150.3, comment="")
        self.sc.client.position_id_for_deal.return_value = 777
        self.sc.alert(sigs(IDX[-1]).iloc[0], 27.0)
        self.sc.decide(str(self.sc.n), True, "cb")
        self.assertIn(777, self.sc.open)
        self.assertNotIn(100, self.sc.open)

    def test_admin_fail_closed(self):
        upd = {"callback_query": {"from": {"id": 5}, "data": "k|restart", "id": "q"}}
        self.sc.admins = set()
        self.assertFalse(self.sc.is_admin(upd))
        self.sc.admins = {5}
        self.assertTrue(self.sc.is_admin(upd))

    def test_failed_manual_close_can_be_retried_immediately(self):
        self.sc.open[5] = {"setup": "x", "side": "BUY", "entry": 4150.0, "sl": 4144.0}
        self.sc.client.close_position.return_value = NS(ok=False, price=0.0, comment="off quotes")
        self.sc.close(5, "cb1")
        self.sc.client.close_position.return_value = NS(ok=True, price=4151.0, comment="")
        self.sc.close(5, "cb2")
        self.assertEqual(self.sc.client.close_position.call_count, 2)
        self.sc.close(5, "cb3")                                  # بعد از موفقیت، کلیک تکراری نادیده
        self.assertEqual(self.sc.client.close_position.call_count, 2)


if __name__ == "__main__":
    unittest.main()
