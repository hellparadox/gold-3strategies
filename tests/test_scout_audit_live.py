"""کارنامهٔ سیگنال‌ها داخل اسکات: هر ساعت حساب می‌شود، زیر هشدار یک خط می‌آید و در داشبورد یک تب دارد.
فقط آمار است — هیچ سیگنالی حذف یا عوض نمی‌شود."""
import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_dashboard as dash
from tools import scout_signal_audit as sa
from tools.scout_bot import JOURNAL_FIELDS


def m1(path, start="2026-10-06 09:30"):
    idx = pd.date_range(start, periods=len(path), freq="1min")
    c = np.array(path, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c}, index=idx)


UP = list(np.linspace(4100, 4115, 61)) + [4115.0] * 200


def write_journal(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


class Report(unittest.TestCase):
    def test_report_groups(self):
        a = pd.DataFrame([{"ts": "t", "setup": "tk_cross,engulfing", "decision": "taken", "side": "BUY",
                           "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4090.0},
                          {"ts": "t", "setup": "rejection", "decision": "rejected", "side": "SELL",
                           "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4110.0}])
        rep = sa.audit_report(sa.audit(a, m1(UP)))
        self.assertEqual(rep["all"]["plus"], 1)
        self.assertEqual(rep["all"]["minus"], 1)
        self.assertEqual(rep["all"]["first"], 0.5)
        keys = {x["key"]: x for x in rep["setup"]}
        self.assertEqual(keys["tk_cross"]["first"], 1.0)
        self.assertEqual(keys["rejection"]["first"], 0.0)
        self.assertEqual({x["key"] for x in rep["decision"]}, {"taken", "rejected"})
        self.assertEqual([x["fa"] for x in rep["decision"] if x["key"] == "rejected"], ["رد کردید"])
        self.assertEqual(len(rep["hour"]), 5)
        self.assertEqual(sa.group_stats(a.iloc[0:0].assign(status=[]))["n"], 0)


class Live(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.audit_enabled, self.sc.audit_days, self.sc.audit_every, self.sc.audit_min_n = True, 14, 3600.0, 1
        self.sc._audit, self.sc._audit_setups, self.sc._audit_t = None, {}, 0.0
        now = datetime.now().isoformat(timespec="seconds")
        write_journal(self.sc.journal, [
            {"ts": now, "event": "alert", "alert": "1", "setup": "tk_cross", "side": "BUY",
             "bar": "2026-10-06 09:15:00", "price": "4100", "sl": "4090"},
            {"ts": now, "event": "alert", "alert": "2", "setup": "rejection", "side": "SELL",
             "bar": "2026-10-06 09:15:00", "price": "4100", "sl": "4110"},
            {"ts": (datetime.now() - timedelta(days=30)).isoformat(), "event": "alert", "alert": "1",
             "setup": "old", "side": "BUY", "bar": "2026-09-01 09:15:00", "price": "4000", "sl": "3990"}])
        self.sc.client.get_history_bars.return_value = m1(UP)

    def tearDown(self):
        self._d.cleanup()

    def test_tick_builds_report_hourly_and_reads_only(self):
        self.sc.audit_tick(now=10_000.0)
        self.assertEqual(self.sc._audit["all"]["n"], 2)                    # ۳۰ روز پیش بیرون ماند
        self.assertEqual(self.sc._audit_setups["tk_cross"]["first"], 1.0)
        self.assertEqual(self.sc._audit["days"], 14)
        self.sc.audit_tick(now=10_100.0)                                    # هنوز یک ساعت نشده
        self.assertEqual(self.sc.client.get_history_bars.call_count, 1)
        self.sc.client.send_market_order.assert_not_called()
        self.sc.client.close_position.assert_not_called()
        self.sc.client.modify_sltp.assert_not_called()

    def test_failure_is_quiet(self):
        self.sc.client.get_history_bars.side_effect = RuntimeError("no history")
        self.sc.audit_tick(now=10_000.0)
        self.assertIsNone(self.sc._audit)
        self.sc.audit_enabled = False
        self.sc._audit_t = 0.0
        self.sc.audit_tick(now=99_999.0)
        self.assertEqual(self.sc.client.get_history_bars.call_count, 1)

    def test_alert_shows_line(self):
        self.sc.audit_tick(now=10_000.0)
        line = self.sc.audit_line("tk_cross,rejection")
        self.assertIn("<code>tk_cross</code> 100٪ (1)", line)          # یک نمونه: علامت نمی‌گیرد
        self.assertNotIn("✅", line)
        self.assertNotIn("⚠️", line)
        self.assertNotIn("اکثراً اول خلاف جهت رفته", self.sc.audit_line("rejection"))
        self.assertEqual(self.sc.audit_line("unknown"), "")
        saved = self.sc._audit_setups
        self.sc._audit_setups = {"tk_cross": {"n": 40, "plus": 30, "minus": 10, "both": 0, "first": 0.75},
                                 "rejection": {"n": 40, "plus": 10, "minus": 30, "both": 0, "first": 0.25}}
        line = self.sc.audit_line("tk_cross,rejection")                     # همان منطق بازهٔ ۹۵٪ برچسب
        self.assertIn("✅<code>tk_cross</code> 75٪ (40)", line)
        self.assertIn("⚠️<code>rejection</code> 25٪ (40)", line)
        self.assertIn("اکثراً اول خلاف جهت رفته", self.sc.audit_line("rejection"))
        self.sc._audit_setups = saved
        self.sc.audit_min_n = 5
        self.assertEqual(self.sc.audit_line("tk_cross"), "")                # نمونهٔ کم: چیزی نمی‌گوید
        self.sc.audit_min_n = 1
        self.sc.admins = {1}
        self.sc.client.positions.return_value = []
        self.sc.client.get_tick.return_value = NS(time=0, bid=4100.0, ask=4100.3)
        row = pd.Series({"setup": "tk_cross", "side": "BUY", "ref_close": 4100.0, "sl_dist": 6.0,
                         "tp_dist": 12.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-06 09:15")})
        self.sc.alert(row, 27.0)
        self.assertIn("🧭 بعد از سیگنال", self.sc.tg.sent[-1][0])
        self.assertEqual(len(self.sc.pending), 1)                           # هشدار مثل قبل آمد

    def test_alert_without_report_unchanged(self):
        row = pd.Series({"setup": "tk_cross", "side": "BUY", "ref_close": 4100.0, "sl_dist": 6.0,
                         "tp_dist": 12.0, "atr": 5.0, "bar": pd.Timestamp("2026-10-06 09:15")})
        self.sc.client.positions.return_value = []
        self.sc.alert(row, 27.0)
        self.assertNotIn("🧭", self.sc.tg.sent[-1][0])


class Page(unittest.TestCase):
    def test_dashboard_has_tab(self):
        for part in ('id="t-audit"', 'id="p-audit"', "renderAudit(d.audit)", "const PANES=['dash','trades','audit','guide']"):
            self.assertIn(part, dash.PAGE)


if __name__ == "__main__":
    unittest.main()
