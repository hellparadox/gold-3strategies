"""دو اصلاح ایمنی:
۱) دکمهٔ هشدار قدیمی (قبل از ری‌استارت، با همان شماره) هشدار جدید را باز نمی‌کند.
۲) ساعت بازار طلا با ساعت تابستانی/زمستانی آمریکا جابه‌جا می‌شود (۱ نوامبر ۲۰۲۶ به بعد یک ساعت دیرتر)."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_control as ctl


def row(side="BUY"):
    return pd.Series({"setup": "rejection", "side": side, "ref_close": 4100.0, "sl_dist": 6.5,
                      "tp_dist": 13.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-06 09:15")})


class StaleButton(unittest.TestCase):
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

    def tap(self, data, mid):
        self.sc.handle_update({"callback_query": {"from": {"id": 1, "first_name": "H"}, "data": data, "id": "q",
                                                  "message": {"message_id": mid}}})

    def new_alert(self, side="BUY"):
        self.sc.alert(row(side), 27.0)
        return str(self.sc.n), self.sc.pending[str(self.sc.n)]["mid"]

    def test_old_message_cannot_open_new_alert_with_same_number(self):
        aid, mid = self.new_alert("SELL")                    # بعد از ری‌استارت: هشدار #1 جدید
        self.assertEqual(aid, "1")
        old_mid = mid + 500                                  # پیام هشدار #1 قبل از ری‌استارت
        self.tap("a|1|y", old_mid)
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("هشدار قدیمی", self.sc.tg.acks[-1])
        self.assertIn("1", self.sc.pending)                  # هشدار جدید سالم ماند
        with self.sc.journal.open(encoding="utf-8") as fh:
            self.assertIn("stale_tap", fh.read())
        for data in ("v|1", "v|1|y", "x|1", "a|1|n"):
            self.tap(data, old_mid)
        self.sc.client.send_market_order.assert_not_called()
        self.assertIn("1", self.sc.pending)

    def test_current_message_still_works(self):
        aid, mid = self.new_alert("BUY")
        self.tap(f"a|{aid}|y", mid)
        self.assertEqual(self.sc.client.send_market_order.call_args.args[0], "BUY")
        self.assertIn(77, self.sc.open)

    def test_contra_on_current_message_still_works(self):
        aid, mid = self.new_alert("BUY")
        self.tap(f"v|{aid}", mid)
        self.tap(f"v|{aid}|y", mid)
        self.assertEqual(self.sc.client.send_market_order.call_args.args[0], "SELL")

    def test_tap_without_message_info_keeps_old_behaviour(self):
        aid, _mid = self.new_alert("BUY")
        self.sc.handle_update({"callback_query": {"from": {"id": 1}, "data": f"a|{aid}|y", "id": "q"}})
        self.sc.client.send_market_order.assert_called_once()

    def test_expired_number_unchanged(self):
        self.tap("a|9|y", 123)
        self.assertIn("دیگر معتبر نیست", self.sc.tg.acks[-1])


class MarketHoursDST(unittest.TestCase):
    def f(self, *a):
        return ctl.market_open_utc(datetime(*a, tzinfo=timezone.utc))

    def test_offsets(self):
        off = lambda *a: ctl.new_york_offset_hours(datetime(*a, tzinfo=timezone.utc))
        self.assertEqual(off(2026, 10, 30, 12), -4)
        self.assertEqual(off(2026, 11, 1, 5, 59), -4)        # ۱ نوامبر ۲۰۲۶ ساعت ۲ بامداد نیویورک
        self.assertEqual(off(2026, 11, 1, 6, 0), -5)
        self.assertEqual(off(2027, 1, 15, 12), -5)
        self.assertEqual(off(2027, 3, 14, 6, 59), -5)        # ۱۴ مارس ۲۰۲۷
        self.assertEqual(off(2027, 3, 14, 7, 0), -4)

    def test_summer_same_as_before(self):
        self.assertTrue(self.f(2026, 10, 1, 10))
        self.assertFalse(self.f(2026, 10, 1, 21))            # استراحت روزانه 21–22 UTC
        self.assertTrue(self.f(2026, 10, 1, 22))
        self.assertFalse(self.f(2026, 10, 2, 21))            # جمعه بسته
        self.assertFalse(self.f(2026, 10, 3, 12))            # شنبه
        self.assertFalse(self.f(2026, 10, 4, 21))
        self.assertTrue(self.f(2026, 10, 4, 22))             # یکشنبه باز

    def test_winter_one_hour_later(self):
        self.assertTrue(self.f(2026, 11, 3, 21))             # حالا باز است (قبلاً «بسته» حساب می‌شد)
        self.assertFalse(self.f(2026, 11, 3, 22))            # استراحت روزانه 22–23 UTC
        self.assertTrue(self.f(2026, 11, 3, 23))
        self.assertTrue(self.f(2026, 11, 6, 21, 30))         # جمعه هنوز باز
        self.assertFalse(self.f(2026, 11, 6, 22))            # جمعه بسته
        self.assertFalse(self.f(2026, 11, 8, 22, 30))        # یکشنبه هنوز بسته
        self.assertTrue(self.f(2026, 11, 8, 23))             # یکشنبه باز

    def test_naive_datetime_is_utc(self):
        self.assertFalse(ctl.market_open_utc(datetime(2026, 11, 3, 22)))


if __name__ == "__main__":
    unittest.main()
