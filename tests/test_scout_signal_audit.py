"""بررسی فقط‌خواندنی سیگنال‌ها: بعد از هر هشدار، بازار در جهت سیگنال رفت یا خلاف؟"""
import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from tools import scout_signal_audit as sa
from tools.scout_bot import JOURNAL_FIELDS


def m1(path, start="2026-10-06 09:30"):
    idx = pd.date_range(start, periods=len(path), freq="1min")
    c = np.array(path, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c}, index=idx)


def alerts(rows):
    return pd.DataFrame([{"ts": "2026-10-06T00:00:00", "setup": "cloud_break", "decision": "taken", **r} for r in rows])


class Journal(unittest.TestCase):
    def test_decisions_follow_latest_alert_with_same_number(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.csv"
            rows = [("2026-10-04T10:00:00", "alert", "1", "BUY"), ("2026-10-04T10:01:00", "opened", "1", "BUY"),
                    ("2026-10-05T10:00:00", "alert", "1", "SELL"),                      # بعد از ری‌استارت
                    ("2026-10-05T10:00:30", "alert", "2", "BUY"), ("2026-10-05T10:05:00", "blocked_limit", "2", "BUY"),
                    ("2026-10-05T10:30:00", "expired", "1", "SELL"), ("2026-10-05T11:00:00", "alert", "3", "BUY")]
            with p.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
                w.writeheader()
                for ts, ev, aid, side in rows:
                    w.writerow({"ts": ts, "event": ev, "alert": aid, "side": side, "setup": "x",
                                "bar": "2026-10-05 09:00:00", "price": "4100", "sl": "4090"})
            df = sa.read_alerts(p)
            self.assertEqual(list(df.decision), ["taken", "expired", "blocked", "pending"])
            self.assertEqual(len(sa.read_alerts(p, since="2026-10-05")), 3)
            self.assertEqual(len(sa.read_alerts(p, since="2026-10-05", until="2026-10-05T10:30")), 2)


class Audit(unittest.TestCase):
    def test_buy_moves_with_signal(self):
        a = alerts([{"side": "BUY", "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4090.0}])
        path = list(np.linspace(4100, 4115, 61)) + [4115.0] * 200                   # بالا رفت
        r = sa.audit(a, m1(path)).iloc[0]
        self.assertEqual(r.status, "ok")
        self.assertGreater(r.move15, 0)
        self.assertAlmostEqual(r.r60, 1.5, places=1)
        self.assertEqual(r["first"], "+1R")
        self.assertGreater(r.mfe_r, 1.4)
        self.assertLess(r.mae_r, 0.05)

    def test_sell_against_signal(self):
        a = alerts([{"side": "SELL", "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4108.0}])
        path = list(np.linspace(4100, 4110, 61)) + [4110.0] * 200                   # فروش؛ قیمت بالا رفت
        r = sa.audit(a, m1(path)).iloc[0]
        self.assertLess(r.move30, 0)
        self.assertEqual(r["first"], "-1R")
        self.assertGreater(r.mae_r, 1.2)

    def test_ignores_bars_before_alert_candle_closes_and_both(self):
        a = alerts([{"side": "BUY", "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4095.0}])
        f = m1([4080.0] * 10, start="2026-10-06 09:20")                           # قبل از 09:30 حساب نیست
        f = pd.concat([f, m1([4100.0] * 70)])
        f.loc[pd.Timestamp("2026-10-06 09:35")] = [4100, 4106, 4094, 4100]          # هر دو در یک کندل
        r = sa.audit(a, f.sort_index()).iloc[0]
        self.assertEqual(r["first"], "both")
        self.assertAlmostEqual(r.mae_r, 1.2, places=1)

    def test_no_data_and_partial(self):
        a = alerts([{"side": "BUY", "bar": "2026-10-09 09:15:00", "price": 4100.0, "sl": 4090.0},
                    {"side": "BUY", "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4090.0}])
        r = sa.audit(a, m1([4101.0] * 20))
        self.assertEqual(list(r.status), ["no_data", "partial"])
        self.assertTrue(np.isnan(r.iloc[1].r60))
        self.assertEqual(r.iloc[1]["first"], "none")
        self.assertEqual(sa.audit(a, None).status.tolist(), ["no_data", "no_data"])


class Summary(unittest.TestCase):
    def test_sections_and_tehran_hour(self):
        a = alerts([{"side": "BUY", "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4090.0,
                     "setup": "cloud_break,engulfing"},
                    {"side": "SELL", "bar": "2026-10-06 09:15:00", "price": 4100.0, "sl": 4110.0,
                     "setup": "tk_cross"}])
        res = sa.audit(a, m1(list(np.linspace(4100, 4115, 61)) + [4115.0] * 200))
        txt = sa.summarize(res)
        for part in ("== ALL ==", "== BY SIDE ==", "== BY YOUR DECISION ==", "== BY SETUP", "== BY TEHRAN HOUR",
                     "cloud_break", "engulfing", "tk_cross", "first +1R 1 / -1R 1"):
            self.assertIn(part, txt)
        self.assertEqual(sa.tehran_hour("2026-10-06 09:15:00", 30), 10)          # 09:30 سرور = 10:00 تهران
        self.assertEqual(sa.tehran_hour("2026-10-06 23:30:00", 30), 0)


if __name__ == "__main__":
    unittest.main()
