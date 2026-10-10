"""برچسب بالای هشدار: «همراه شکست» / «بدون شکست» / «حد ضرر نزدیک» با درصدِ کارنامهٔ ۱۴ روزه.
فقط اطلاع است — هشدار، دکمه‌ها و تصمیم مالک مثل قبل‌اند."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from tests.test_scout_tracker import make_scout
from tools import scout_dashboard as dash
from tools import scout_signal_audit as sa


def m1(path, start="2026-10-06 09:30"):
    idx = pd.date_range(start, periods=len(path), freq="1min")
    c = np.array(path, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c}, index=idx)


UP = list(np.linspace(4100, 4115, 61)) + [4115.0] * 200


def alerts(rows):
    return pd.DataFrame([{"ts": "t", "decision": "taken", "bar": "2026-10-06 09:15:00", "price": 4100.0, **r}
                         for r in rows])


class Groups(unittest.TestCase):
    def test_label_group(self):
        self.assertEqual(sa.label_group("pullback_resume,range_break", 8.0), "break")
        self.assertEqual(sa.label_group("cloud_break", 10.0), "break")
        self.assertEqual(sa.label_group("engulfing", 8.0), "plain")
        self.assertEqual(sa.label_group("pullback_resume+kijun_pullback", 9.0), "plain")
        self.assertEqual(sa.label_group("range_break", 6.0), "tight")          # حد ضرر نزدیک اول می‌آید
        self.assertEqual(sa.label_group("engulfing", 5.2), "tight")
        self.assertEqual(sa.label_group("engulfing", 7.0, tight=7.5), "tight")
        self.assertEqual(sa.label_group("engulfing", None), "plain")

    def test_report_has_label_rows(self):
        a = alerts([{"setup": "range_break", "side": "BUY", "sl": 4090.0},      # همراه شکست، بالا رفت ← +1R
                    {"setup": "engulfing", "side": "SELL", "sl": 4110.0},       # بدون شکست، خلاف ← -1R
                    {"setup": "tk_cross", "side": "BUY", "sl": 4095.0}])        # حد ضرر ۵ ← نزدیک
        rep = sa.audit_report(sa.audit(a, m1(UP)))
        lab = {x["key"]: x for x in rep["label"]}
        self.assertEqual([x["key"] for x in rep["label"]], ["break", "plain", "tight"])
        self.assertEqual((lab["break"]["n"], lab["break"]["first"]), (1, 1.0))
        self.assertEqual((lab["plain"]["n"], lab["plain"]["first"]), (1, 0.0))
        self.assertEqual(lab["tight"]["n"], 1)
        self.assertEqual(lab["tight"]["fa"], "حد ضرر نزدیک")
        self.assertEqual(rep["tight"], 6.0)
        self.assertIn("== BY ALERT LABEL", sa.summarize(sa.audit(a, m1(UP))))
        self.assertEqual(sa.audit_report(sa.audit(a.iloc[0:0], m1(UP)))["label"][0]["n"], 0)


def lab(key, plus, minus, n):
    return {"key": key, "n": n, "plus": plus, "minus": minus, "both": 0,
            "first": round(plus / (plus + minus), 3)}


def row(setup="engulfing", sl_dist=8.0):
    return pd.Series({"setup": setup, "side": "BUY", "ref_close": 4100.0, "sl_dist": sl_dist,
                      "tp_dist": 2 * sl_dist, "atr": 5.0, "bar": pd.Timestamp("2026-10-06 09:15")})


class AlertLine(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.sc = make_scout(Path(self._d.name))
        self.sc.admins = {1}
        self.sc.client.positions.return_value = []
        self.sc.client.get_tick.return_value = NS(time=0, bid=4100.0, ask=4100.3)
        self.sc.audit_days, self.sc.audit_min_n = 14, 5
        self.sc._audit_labels = {"break": lab("break", 55, 35, 95),           # همان اعداد واقعی ۱۴ روز
                                 "plain": lab("plain", 79, 78, 166),
                                 "tight": lab("tight", 8, 24, 33)}

    def tearDown(self):
        self._d.cleanup()

    def test_three_labels(self):
        self.assertEqual(self.sc.label_line("pullback_resume,range_break", 8.0),
                         "✅ <b>خوب</b> — همراه شکست (61٪ اول ‎+1R در 14 روز، 95 سیگنال)")
        self.assertIn("⚪ <b>شانسی</b> — بدون شکست (50٪", self.sc.label_line("engulfing", 8.0))
        self.assertIn("⚠️ <b>ضعیف</b> — حد ضرر نزدیک (5.20) (25٪", self.sc.label_line("tk_cross", 5.2))

    def test_mark_follows_live_numbers(self):
        self.sc._audit_labels["plain"] = lab("plain", 60, 100, 170)            # ۳۸٪ با نمونهٔ زیاد
        self.assertTrue(self.sc.label_line("engulfing", 8.0).startswith("⚠️ <b>ضعیف</b> — بدون شکست"))
        self.sc._audit_labels["plain"] = lab("plain", 110, 60, 175)            # ۶۵٪ با نمونهٔ زیاد
        self.assertTrue(self.sc.label_line("engulfing", 8.0).startswith("✅ <b>خوب</b>"))

    def test_no_claim_without_confidence(self):
        self.sc._audit_labels["break"] = lab("break", 7, 3, 10)                # ۷۰٪ ولی فقط ۱۰ نمونه
        self.assertTrue(self.sc.label_line("range_break", 8.0).startswith("⚪ <b>هنوز معلوم نیست</b> — همراه شکست (70٪"))
        self.sc._audit_labels["tight"] = lab("tight", 3, 7, 10)                # ۳۰٪ ولی فقط ۱۰ نمونه
        self.assertTrue(self.sc.label_line("engulfing", 5.0).startswith("⚪ <b>هنوز معلوم نیست</b>"))
        self.sc._audit_labels["plain"] = lab("plain", 26, 24, 50)              # ۵۲٪
        self.assertTrue(self.sc.label_line("engulfing", 8.0).startswith("⚪ <b>شانسی</b>"))

    def test_wilson(self):
        lo, hi = sa.wilson(55, 90)
        self.assertGreater(lo, 0.5)
        self.assertAlmostEqual(lo, 0.508, places=2)
        lo, hi = sa.wilson(8, 32)
        self.assertLess(hi, 0.5)
        self.assertEqual(sa.wilson(0, 0), (0.0, 1.0))

    def test_quiet_without_data_or_small_sample_or_disabled(self):
        self.sc._audit_labels["break"] = lab("break", 3, 0, 3)
        self.assertEqual(self.sc.label_line("range_break", 8.0), "")
        self.sc._audit_labels = {}
        self.assertEqual(self.sc.label_line("engulfing", 8.0), "")
        self.sc._audit_labels = {"plain": lab("plain", 25, 25, 50)}
        self.sc.label_enabled = False
        self.assertEqual(self.sc.label_line("engulfing", 8.0), "")

    def test_alert_first_line_and_unchanged_buttons(self):
        self.sc.alert(row("engulfing", 8.0), 27.0)
        text, buttons = self.sc.tg.sent[-1][0], self.sc.tg.sent[-1][1]
        self.assertTrue(text.startswith("⚪ <b>شانسی</b> — بدون شکست"))
        self.assertIn("🟢 BUY", text.splitlines()[1])
        self.assertEqual(buttons[0][0]["callback_data"], "a|1|y")
        self.assertEqual(len(self.sc.pending), 1)
        self.sc.client.send_market_order.assert_not_called()

    def test_alert_without_report_has_no_label(self):
        self.sc._audit_labels = {}
        self.sc.alert(row("engulfing", 8.0), 27.0)
        self.assertTrue(self.sc.tg.sent[-1][0].startswith("<b>🟢 BUY</b>"))

    def test_audit_tick_fills_labels(self):
        import csv
        from datetime import datetime
        from tools.scout_bot import JOURNAL_FIELDS
        self.sc._audit_labels = {}
        self.sc.audit_enabled, self.sc.audit_every, self.sc.audit_min_n = True, 3600.0, 1
        self.sc._audit, self.sc._audit_setups, self.sc._audit_t = None, {}, 0.0
        now = datetime.now().isoformat(timespec="seconds")
        with self.sc.journal.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
            w.writeheader()
            w.writerow({"ts": now, "event": "alert", "alert": "1", "setup": "range_break", "side": "BUY",
                        "bar": "2026-10-06 09:15:00", "price": "4100", "sl": "4090"})
        self.sc.client.get_history_bars.return_value = m1(UP)
        self.sc.audit_tick(now=10_000.0)
        self.assertEqual(self.sc._audit_labels["break"]["first"], 1.0)
        self.assertEqual(self.sc._audit["label"][0]["key"], "break")
        self.assertIn("⚪ <b>هنوز معلوم نیست</b> — همراه شکست (100٪", self.sc.label_line("cloud_break", 9.0))


class Page(unittest.TestCase):
    def test_dashboard_label_section(self):
        for part in ('id="aulabel"', "a.label", 'id="autight"', "برچسب هشدارها"):
            self.assertIn(part, dash.PAGE)


if __name__ == "__main__":
    unittest.main()
