"""AO/AC: فرمول‌ها، سیگنال‌ها، شبیه‌ساز (اسپرد، حد ضرر قبل از هدف) و اجرای کامل روی CSV ساختگی."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from tools import research_ao_ac as ra


def frame(close, spread=0.0, start="2026-01-05 00:00", freq="15min", wick=0.5):
    c = np.asarray(close, float)
    o = np.r_[c[0], c[:-1]]
    d = pd.DataFrame({"open": o, "high": np.maximum(o, c) + wick, "low": np.minimum(o, c) - wick, "close": c,
                      "spread": spread * 100.0}, index=pd.date_range(start, periods=len(c), freq=freq))
    return d


class Formulas(unittest.TestCase):
    def test_ao_ac_match_definition(self):
        rng = np.random.default_rng(1)
        h = 100 + np.cumsum(rng.normal(0, 1, 200)) + 1
        l = h - 2
        ao, ac = ra.ao_ac(h, l)
        med = (h + l) / 2
        i = 120
        want_ao = med[i - 4:i + 1].mean() - med[i - 33:i + 1].mean()
        self.assertAlmostEqual(ao[i], want_ao, places=9)
        self.assertAlmostEqual(ac[i], ao[i] - ao[i - 4:i + 1].mean(), places=9)
        self.assertTrue(np.isnan(ao[32]) and not np.isnan(ao[33]))

    def test_signals(self):
        ao = np.array([np.nan, -1, -0.5, 0.2, 0.5, 0.3, 0.4, -0.1])
        ac = np.array([np.nan, -0.2, -0.1, 0.1, 0.2, -0.1, 0.05, -0.3])
        b, s = ra.signals("ao_zero", ao, ac)
        self.assertEqual(list(np.flatnonzero(b)), [3])
        self.assertEqual(list(np.flatnonzero(s)), [7])
        b, s = ra.signals("ao_ac", ao, ac)
        self.assertEqual(list(np.flatnonzero(b)), [3, 6])                # AO>0 و AC از صفر بالا رفت
        b, s = ra.signals("saucer", ao, ac)
        self.assertEqual(list(np.flatnonzero(b)), [6])                   # 0.5 → 0.3 (قرمز) → 0.4 (سبز)
        b, s = ra.signals("ac_2", ao, ac)
        self.assertEqual(list(np.flatnonzero(b)), [3])                   # فقط اولین کندل از دو میلهٔ صعودی
        with self.assertRaises(ValueError):
            ra.signals("x", ao, ac)


class Simulator(unittest.TestCase):
    def setUp(self):
        self.n = 700
        self.at = np.full(self.n, 2.0)                                   # فاصلهٔ حد ضرر = 1.5×2 = 3
        self.ac = np.zeros(self.n)

    def sig_at(self, t, buy=True):
        b, s = np.zeros(self.n, bool), np.zeros(self.n, bool)
        (b if buy else s)[t] = True
        return b, s

    def test_buy_hits_target_and_pays_spread(self):
        c = np.r_[np.full(650, 100.0), np.linspace(100, 150, 50)]
        df = frame(c, spread=0.3, wick=0.0)
        b, s = self.sig_at(640)
        tr = ra.simulate(df, b, s, self.ac, self.at, "r1", 16)
        self.assertEqual(len(tr), 1)
        r = tr.iloc[0]
        self.assertAlmostEqual(r.entry, 100.3)                            # خرید با ask
        self.assertEqual(r.why, "TP")
        self.assertAlmostEqual(r.R, 1.0)

    def test_stop_before_target_in_same_bar(self):
        c = np.full(self.n, 100.0)
        df = frame(c, wick=10.0)                                          # هر کندل هم بالا هم پایین را می‌زند
        b, s = self.sig_at(640)
        tr = ra.simulate(df, b, s, self.ac, self.at, "r2", 16)
        self.assertEqual(tr.iloc[0].why, "SL")
        self.assertAlmostEqual(tr.iloc[0].R, -1.0)

    def test_contra_reverses_and_time_exit(self):
        c = np.full(self.n, 100.0)
        df = frame(c, wick=0.0)
        b, s = self.sig_at(640)
        tr = ra.simulate(df, b, s, self.ac, self.at, "r2", 16, contra=True)
        self.assertEqual((tr.iloc[0].side, tr.iloc[0].why), ("SELL", "TIME"))

    def test_acflip_exit(self):
        c = np.full(self.n, 100.0)
        df = frame(c, wick=0.0)
        ac = np.ones(self.n)
        ac[645:] = -1.0                                                   # شتاب برعکس شد
        b, s = self.sig_at(640)
        tr = ra.simulate(df, b, s, ac, self.at, "acflip", 16)
        self.assertEqual(tr.iloc[0].why, "FLIP")
        self.assertEqual(tr.iloc[0].bars, 5)

    def test_one_position_at_a_time(self):
        c = np.full(self.n, 100.0)
        df = frame(c, wick=0.0)
        b = np.zeros(self.n, bool)
        b[[640, 645, 670]] = True
        tr = ra.simulate(df, b, np.zeros(self.n, bool), self.ac, self.at, "r2", 16)
        self.assertEqual(len(tr), 2)                                      # 645 وسط معاملهٔ اول است


class EndToEnd(unittest.TestCase):
    def test_random_walk_csv_runs_and_does_not_pass(self):
        rng = np.random.default_rng(5)
        c = 4000 + np.cumsum(rng.normal(0, 1.2, 4000))
        df = frame(c, spread=0.4).reset_index().rename(columns={"index": "time"})
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bars.csv"
            df.to_csv(p, index=False)
            with mock.patch.object(ra, "lower_priority"), mock.patch("builtins.print"):
                rc = ra.main(["--csv", str(p), "--tf", "M15", "--out", str(Path(d) / "out")])
            self.assertEqual(rc, 0)
            txt = (Path(d) / "out" / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("M15|ao_ac|r2", txt)
            self.assertIn("PASS: none", txt)                              # گام تصادفی نباید قبول شود


if __name__ == "__main__":
    unittest.main()
