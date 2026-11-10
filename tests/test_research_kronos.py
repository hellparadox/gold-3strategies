"""تست Kronos بدون torch: پیش‌بینی‌کنندهٔ ساختگی. مهم‌ترین چیز: هیچ قیمتی از آینده وارد ورودی مدل نشود."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import numpy as np
import pandas as pd

from tools import research_kronos as rk


def make_bars(n=900, start="2025-12-20 00:00", seed=1):
    rng = np.random.default_rng(seed)
    t = pd.date_range(start, periods=n, freq="15min")
    c = 4000 + np.cumsum(rng.normal(0, 2, n))
    return pd.DataFrame({"time": t, "open": c - 0.3, "high": c + 1.5, "low": c - 1.5, "close": c,
                         "volume": rng.integers(50, 500, n).astype(float), "spread": 40.0})


class Context(unittest.TestCase):
    def test_no_future_prices_in_input(self):
        b = make_bars()
        x, xt, yt = rk.context(b, 500, 400, 4)
        self.assertEqual(len(x), 400)
        self.assertEqual(xt.iloc[-1], b.time.iloc[500])                  # آخرین کندل ورودی = کندل هشدار
        self.assertEqual(x["close"].iloc[-1], b.close.iloc[500])
        self.assertTrue((yt > xt.iloc[-1]).all())
        self.assertEqual(list(yt), list(b.time.iloc[501:505]))
        self.assertEqual(list(x.columns), rk.COLS)                      # فقط قیمت/حجم؛ اسپرد و زمان جدا

    def test_future_timestamps_synthesized_at_the_end(self):
        b = make_bars()
        _, xt, yt = rk.context(b, len(b) - 2, 400, 4)
        self.assertEqual(len(yt), 4)
        self.assertEqual(yt.iloc[-1] - xt.iloc[-1], pd.Timedelta(minutes=60))

    def test_not_enough_history(self):
        with self.assertRaises(ValueError):
            rk.context(make_bars(), 10, 400, 4)


class Stats(unittest.TestCase):
    def test_wilson_and_p(self):
        lo, hi = rk.wilson(50, 100)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)
        self.assertLess(rk.two_prop_p(70, 100, 40, 100), 0.001)
        self.assertGreater(rk.two_prop_p(50, 100, 49, 100), 0.5)

    def test_direction_stats_perfect_and_cost(self):
        rows = [{"pred_move": s, "act_move": 2 * s, "mom_move": -s, "pnl": abs(2 * s) - 0.4}
                for s in ([1.0] * 150 + [-1.0] * 150)]
        st = rk.direction_stats(rows)
        self.assertEqual(st["n"], 300)
        self.assertAlmostEqual(st["hit"], 1.0)
        self.assertAlmostEqual(st["momentum_hit"], 0.0)
        self.assertAlmostEqual(st["pnl_avg"], 1.6)
        self.assertEqual(rk.verdict_direction(st), "PASS")

    def test_coin_flip_fails(self):
        rng = np.random.default_rng(3)
        rows = [{"pred_move": float(rng.choice([-1, 1])), "act_move": float(rng.choice([-1, 1])),
                 "mom_move": 1.0, "pnl": -0.4} for _ in range(400)]
        self.assertEqual(rk.verdict_direction(rk.direction_stats(rows)), "FAIL")

    def test_filter_stats(self):
        rows = []
        for h in (0, 1):
            rows += [{"agree": True, "win": i % 10 < 7, "half": h, "strength": 0.5} for i in range(100)]
            rows += [{"agree": False, "win": i % 10 < 4, "half": h, "strength": -0.5} for i in range(100)]
        st = rk.filter_stats(rows)
        self.assertAlmostEqual(st["agree"]["win"], 0.7)
        self.assertAlmostEqual(st["disagree"]["win"], 0.4)
        self.assertLess(st["p_diff"], 0.001)
        self.assertEqual(rk.verdict_filter(st), "PASS")
        same = [dict(r, win=(i % 2 == 0)) for i, r in enumerate(rows)]
        self.assertEqual(rk.verdict_filter(rk.filter_stats(same)), "FAIL")


class Points(unittest.TestCase):
    def test_raw_points_only_after_since_with_full_future(self):
        b = make_bars(start="2025-12-28 00:00")
        pts = rk.raw_points(b, "2026-01-01", 50, 400, 4)
        self.assertTrue(pts)
        self.assertTrue(all(b.time.iloc[i] >= pd.Timestamp("2026-01-01") for i in pts))
        self.assertTrue(all(i >= 399 and i + 4 < len(b) for i in pts))

    def test_align_alerts(self):
        b = make_bars()
        audit = pd.DataFrame([
            {"ts": "2026-01-02T10:00:01", "bar": str(b.time.iloc[600]), "side": "BUY", "price": 4000, "sl": 3990,
             "first": "+1R", "status": "ok", "setup": "engulfing"},
            {"ts": "x", "bar": str(b.time.iloc[601]), "side": "SELL", "price": 4000, "sl": 4010,
             "first": "none", "status": "ok", "setup": "x"},                       # بدون نتیجه: کنار
            {"ts": "y", "bar": "2020-01-01 00:00:00", "side": "SELL", "price": 1, "sl": 2,
             "first": "-1R", "status": "ok", "setup": "x"},                         # کندلش نیست: کنار
        ])
        al = rk.align_alerts(audit, b, 400)
        self.assertEqual(len(al), 1)
        self.assertEqual((al[0]["i"], al[0]["side"], al[0]["win"], al[0]["sl_dist"]), (600, "BUY", True, 10.0))


class EndToEnd(unittest.TestCase):
    def test_run_with_fake_model_resumes_and_writes_summary(self):
        b = make_bars(n=1200)
        calls = []

        def fake_predict(items):                     # «پیش‌بینی»: ادامهٔ حرکت ۴ کندل اخیر — فقط از ورودی
            calls.append(len(items))
            out = []
            for x, _, yt in items:
                drift = x["close"].iloc[-1] - x["close"].iloc[-5]
                out.append(np.array([x["close"].iloc[-1] + drift * (k + 1) / 4 for k in range(len(yt))]))
            return out

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            b.to_csv(d / "bars.csv", index=False)
            audit = pd.DataFrame([{"ts": f"2026-01-0{1 + k % 5}T{k:02d}:00:00", "bar": str(b.time.iloc[700 + 7 * k]),
                                   "side": "BUY" if k % 2 else "SELL", "price": 4000, "sl": 3992 if k % 2 else 4008,
                                   "first": "+1R" if k % 3 else "-1R", "status": "ok", "setup": "s"} for k in range(20)])
            audit.to_csv(d / "audit.csv", index=False)
            args = ["run", "--kronos-dir", str(d), "--bars-csv", str(d / "bars.csv"), "--audit-csv",
                    str(d / "audit.csv"), "--points", "40", "--since", "2026-01-01", "--out", str(d / "out")]
            with mock.patch.object(rk, "make_predictor", return_value=fake_predict), \
                    mock.patch.object(rk, "busy_guard", return_value=None), \
                    mock.patch.object(rk, "lower_priority"), mock.patch("builtins.print"):
                rk.main(args)
                first = sum(calls)
                rk.main(args)                         # بار دوم: همه‌چیز ذخیره شده، مدل صدا زده نمی‌شود
            self.assertEqual(sum(calls), first)
            self.assertEqual(first, 40 + 20)
            txt = next((d / "out").glob("summary_*.txt")).read_text(encoding="utf-8")
            self.assertIn("RAW DIRECTION", txt)
            self.assertIn("SCOUT ALERTS FILTERED BY KRONOS", txt)
            self.assertIn("VERDICT", txt)


class Export(unittest.TestCase):
    def test_export_halves_until_terminal_accepts_and_always_shuts_down(self):
        t0 = int(pd.Timestamp("2026-01-01").timestamp())
        raw = np.array([(t0 + 900 * k, 1.0, 2.0, 0.5, 1.5, 10, 40, 0) for k in range(3)],
                       dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                              ("tick_volume", "i8"), ("spread", "i8"), ("real_volume", "i8")])
        asked = []

        class FakeMT5:
            TIMEFRAME_M15 = 15
            shut = False

            def initialize(self):
                return True

            def symbol_info(self, s):
                return NS() if s == "XAUUSD@" else None

            def symbol_select(self, s, on):
                return True

            def copy_rates_from_pos(self, s, tf, start, n):
                asked.append(n)
                return raw if n <= 5000 else None

            def shutdown(self):
                FakeMT5.shut = True

            def last_error(self):
                return (0, "")

        with mock.patch("builtins.print"):
            d = rk.export_bars(FakeMT5(), 30000, None)
        self.assertEqual(asked, [30000, 15000, 7500, 3750])
        self.assertTrue(FakeMT5.shut)
        self.assertEqual(list(d.columns), ["time"] + rk.COLS + ["spread"])
        self.assertEqual(len(d), 3)


class Guard(unittest.TestCase):
    def test_market_hours(self):
        from datetime import datetime, timezone
        u = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
        self.assertFalse(rk.gold_market_closed(u(2026, 11, 10, 12)))     # سه‌شنبه
        self.assertFalse(rk.gold_market_closed(u(2026, 11, 13, 20)))     # جمعه ۲۰
        self.assertTrue(rk.gold_market_closed(u(2026, 11, 13, 21)))      # جمعه ۲۱
        self.assertTrue(rk.gold_market_closed(u(2026, 11, 14, 12)))      # شنبه
        self.assertTrue(rk.gold_market_closed(u(2026, 11, 15, 21)))      # یکشنبه ۲۱
        self.assertFalse(rk.gold_market_closed(u(2026, 11, 15, 22)))     # یکشنبه ۲۲

    def test_busy_guard(self):
        self.assertIsNotNone(rk.busy_guard(1, closed=False, force=False))   # همان VPS: نه
        self.assertIsNone(rk.busy_guard(1, closed=True, force=False))       # آخر هفته: بله
        self.assertIsNone(rk.busy_guard(1, closed=False, force=True))
        self.assertIsNone(rk.busy_guard(8, closed=False, force=False))      # کامپیوتر قوی

    def test_run_refuses_on_busy_single_core(self):
        with mock.patch.object(rk.os, "cpu_count", return_value=1), \
                mock.patch.object(rk, "gold_market_closed", return_value=False), \
                mock.patch.object(rk, "make_predictor") as mp:
            with self.assertRaises(SystemExit) as cm:
                rk.main(["run", "--kronos-dir", "x"])
        self.assertIn("هسته", str(cm.exception))
        mp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
