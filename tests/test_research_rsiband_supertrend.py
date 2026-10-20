"""تست اسکریپت پژوهشی Supertrend + RSI Bands: فرمول اندیکاتورها، نبودن نگاه به آینده، و شبیه‌ساز معامله."""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from tools import research_rsiband_supertrend as rs


def walk(n=3000, seed=1, drift=0.0, vol=0.6):
    rng = np.random.default_rng(seed)
    c = 4000 + np.cumsum(rng.normal(drift, vol, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.2, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 0.2, n))
    idx = pd.date_range("2026-01-05", periods=n, freq="1min")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "spread": 20.0}, index=idx)


class Indicators(unittest.TestCase):
    def test_rsi_bands_price_really_gives_that_rsi(self):
        """قیمت باند: اگر کندل بعد همان‌جا ببندد، RSI وایلدر دقیقاً برابر سطح (۷۰ یا ۳۰) می‌شود."""
        df = walk(800, seed=3)
        c = df["close"].to_numpy()
        b = rs.rsi_bands(c, 14, 70.0, 30.0)
        for i in (100, 250, 399, 600):
            for level, key in ((70.0, "ub"), (30.0, "lb")):
                ext = pd.Series(np.r_[c[:i + 1], b[key][i]])
                up = ext.diff().clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
                dn = (-ext.diff()).clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
                rsi = 100 * up.iloc[-1] / (up.iloc[-1] + dn.iloc[-1])
                self.assertAlmostEqual(rsi, level, places=6, msg=f"i={i} {key}")
            ext = pd.Series(np.r_[c[:i + 1], b["rsi50"][i]])
            up = ext.diff().clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
            dn = (-ext.diff()).clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
            self.assertAlmostEqual(100 * up.iloc[-1] / (up.iloc[-1] + dn.iloc[-1]), 50.0, places=6)
        self.assertTrue(np.all(b["ub"][50:] >= b["lb"][50:]))
        np.testing.assert_allclose(b["mid"], (b["ub"] + b["lb"]) / 2)

    def test_supertrend_flips_follow_the_move_and_never_double(self):
        c = np.r_[np.full(40, 4000.0), np.linspace(4000, 3940, 50), np.linspace(3940, 4040, 70),
                  np.linspace(4040, 3960, 70)]
        h, l = c + 0.5, c - 0.5
        st = rs.supertrend(h, l, c, 9, 3.9)
        self.assertTrue(st["buy"].any() and st["sell"].any())
        self.assertLess(np.flatnonzero(st["sell"])[0], np.flatnonzero(st["buy"])[0])        # اول فروش، بعد خرید
        self.assertFalse((st["buy"] & st["sell"]).any())
        flips = np.flatnonzero(st["buy"] | st["sell"])
        sides = np.where(st["buy"][flips], 1, -1)
        self.assertTrue(np.all(sides[1:] != sides[:-1]))                                     # خرید و فروش متناوب
        self.assertTrue(np.all(st["trend"][np.flatnonzero(st["buy"])] == 1))
        self.assertTrue(np.all(st["trend"][np.flatnonzero(st["sell"])] == -1))

    def test_no_lookahead_prefix_values_do_not_change(self):
        df = walk(1500, seed=5)
        full = rs.supertrend(df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy(), 9, 3.9)
        cut = rs.supertrend(df.high.to_numpy()[:900], df.low.to_numpy()[:900], df.close.to_numpy()[:900], 9, 3.9)
        for k in ("trend", "buy", "sell"):
            np.testing.assert_array_equal(full[k][:900], cut[k], err_msg=k)
        np.testing.assert_allclose(full["line"][20:900], cut["line"][20:], equal_nan=True)
        bf = rs.rsi_bands(df.close.to_numpy(), 14, 70, 30)
        bc = rs.rsi_bands(df.close.to_numpy()[:900], 14, 70, 30)
        np.testing.assert_allclose(bf["mid"][:900], bc["mid"])

    def test_entry_modes(self):
        df = walk(4000, seed=9, drift=0.02)
        c = df.close.to_numpy()
        st = rs.supertrend(df.high.to_numpy(), df.low.to_numpy(), c, 9, 3.9)
        bands = rs.rsi_bands(c, 14, 70, 30)
        b0, s0 = rs.entry_signals("st", st, bands, c)
        b1, s1 = rs.entry_signals("st_mid", st, bands, c)
        self.assertTrue(np.all(b0[b1]) and np.all(s0[s1]))                                   # st_mid زیرمجموعهٔ st
        self.assertTrue(np.all(c[b1] > bands["mid"][b1]) and np.all(c[s1] < bands["mid"][s1]))
        b2, s2 = rs.entry_signals("mid_cross", st, bands, c)
        self.assertTrue(np.all(st["trend"][b2] == 1) and np.all(st["trend"][s2] == -1))
        with self.assertRaises(ValueError):
            rs.entry_signals("nope", st, bands, c)


def frame(o, h, l, c, spread=0.0):
    n = len(o)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "spread": spread},
                        index=pd.date_range("2026-01-05", periods=n, freq="1min"))


def fake_st(n, line, buy_at=(), sell_at=()):
    buy, sell = np.zeros(n, bool), np.zeros(n, bool)
    buy[list(buy_at)] = True
    sell[list(sell_at)] = True
    return {"buy": buy, "sell": sell, "line": np.full(n, float(line)), "trend": np.ones(n, int),
            "atr": np.ones(n), "up": np.full(n, line), "dn": np.full(n, line)}


class Simulator(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(rs, "WARMUP", 0)
        p.start()
        self.addCleanup(p.stop)

    def sim(self, df, st, buy_at=(), sell_at=(), exit_mode="sl2r", horizon=50, spread=0.0, **kw):
        n = len(df)
        b, s = np.zeros(n, bool), np.zeros(n, bool)
        b[list(buy_at)] = True
        s[list(sell_at)] = True
        return rs.simulate(df, b, s, st, exit_mode, horizon, spread_default=spread, **kw)

    def test_buy_enters_next_open_plus_spread_and_hits_target(self):
        n = 12
        o = np.full(n, 4000.0); c = o.copy(); h = o + 0.1; l = o - 0.1
        h[5] = 4010.0                                                # بالاتر از هدف 2R (ریسک ۲ → هدف +۴)
        df = frame(o, h, l, c, spread=20.0)                          # اسپرد ۰٫۲۰
        tr = self.sim(df, fake_st(n, 3998.0), buy_at=[1])           # خط 3998 → فاصله ۲ (حداقل)
        self.assertEqual(len(tr), 1)
        r = tr.iloc[0]
        self.assertAlmostEqual(r.entry, 4000.20)                      # open بار ۲ + اسپرد
        self.assertEqual(r.why, "TP")
        self.assertAlmostEqual(r.pnl, 4.0, places=6)                  # هدف = ۲×dist
        self.assertAlmostEqual(r.R, 2.0, places=6)

    def test_stop_wins_when_both_hit_in_same_bar(self):
        n = 8
        o = np.full(n, 4000.0); c = o.copy(); h = o + 0.1; l = o - 0.1
        h[4], l[4] = 4020.0, 3990.0
        tr = self.sim(frame(o, h, l, c), fake_st(n, 3998.0), buy_at=[1])
        self.assertEqual(tr.iloc[0].why, "SL")
        self.assertAlmostEqual(tr.iloc[0].R, -1.0, places=6)

    def test_sell_uses_ask_for_stop(self):
        n = 8
        o = np.full(n, 4000.0); c = o.copy(); h = o + 0.1; l = o - 0.1
        h[4] = 4001.9                                              # bid بالا نرسیده به 4002، ولی ask = +۰٫۲۰ رسیده
        df = frame(o, h, l, c, spread=20.0)
        tr = self.sim(df, fake_st(n, 4002.0), sell_at=[1])         # فروش، حد ضرر = 4000 + 2
        self.assertEqual(tr.iloc[0].why, "SL")
        h[4] = 4001.5                                              # ask = 4001.7 < 4002 → نمی‌خورد
        tr = self.sim(frame(o, h, l, c, spread=20.0), fake_st(n, 4002.0), sell_at=[1])
        self.assertNotEqual(tr.iloc[0].why, "SL")

    def test_flip_exit_runs_to_opposite_flip_and_one_position_at_a_time(self):
        n = 40
        o = np.linspace(4000, 4020, n); c = o + 0.2; h = o + 0.3; l = o - 0.1
        st = fake_st(n, 3990.0, buy_at=[3, 6, 9], sell_at=[20])
        tr = self.sim(frame(o, h, l, c), st, buy_at=[3, 6, 9], exit_mode="flip")
        self.assertEqual(len(tr), 1)                               # ۶ و ۹ وقتی پوزیشن باز است نادیده‌اند
        r = tr.iloc[0]
        self.assertEqual(r.why, "FLIP")
        self.assertAlmostEqual(r.exit, o[21])                      # اجرا در open کندل بعد از flip
        self.assertEqual(r.bars, 21 - 4)

    def test_flip_then_new_signal_same_bar_reverses(self):
        n = 40
        o = np.full(n, 4000.0); c = o.copy(); h = o + 0.3; l = o - 0.3
        st = fake_st(n, 3995.0, buy_at=[3], sell_at=[15])
        tr = self.sim(frame(o, h, l, c), st, buy_at=[3], sell_at=[15], exit_mode="flip")
        self.assertEqual(list(tr.side), ["BUY", "SELL"])
        self.assertEqual(tr.iloc[0].exit, tr.iloc[1].entry - 0.0)     # خروج و ورود هر دو open بار ۱۶

    def test_be_flip_moves_stop_to_entry_after_1r(self):
        n = 30
        o = np.full(n, 4000.0); c = o.copy(); h = o + 0.1; l = o - 0.1
        h[5] = 4003.0                                              # +۳ ≥ ۱R (۲) → حد ضرر روی ورود
        l[7] = 3999.0                                              # برگشت به زیر ورود، ولی هنوز بالاتر از حد ضرر اصلی
        l[7] = 3999.9
        tr = self.sim(frame(o, h, l, c), fake_st(n, 3998.0, sell_at=[25]), buy_at=[1], exit_mode="be_flip")
        self.assertEqual(tr.iloc[0].why, "BE")
        self.assertAlmostEqual(tr.iloc[0].pnl, 0.0)

    def test_time_exit_and_contra_is_mirror_side(self):
        n = 80
        o = np.full(n, 4000.0); c = o.copy(); h = o + 0.1; l = o - 0.1
        tr = self.sim(frame(o, h, l, c), fake_st(n, 3998.0), buy_at=[1], horizon=10)
        self.assertEqual(tr.iloc[0].why, "TIME")
        ctr = self.sim(frame(o, h, l, c), fake_st(n, 3998.0), buy_at=[1], horizon=10, contra=True)
        self.assertEqual(ctr.iloc[0].side, "SELL")

    def test_spread_stress_only_hurts(self):
        df = walk(6000, seed=11, drift=0.03)
        c = df.close.to_numpy()
        st = rs.supertrend(df.high.to_numpy(), df.low.to_numpy(), c, 9, 3.9)
        b, s = rs.entry_signals("st", st, rs.rsi_bands(c, 14, 70, 30), c)
        a = rs.simulate(df, b, s, st, "flip", 720, 0.2, 1.0)
        x = rs.simulate(df, b, s, st, "flip", 720, 0.2, 1.5)
        self.assertEqual(len(a), len(x))
        self.assertLess(x["usd"].sum(), a["usd"].sum())


class Stats(unittest.TestCase):
    def test_stats_and_verdict(self):
        rng = np.random.default_rng(2)
        r = rng.normal(0.3, 1.0, 600)
        tr = pd.DataFrame({"R": r, "usd": r * 4, "side": np.where(r > 0.3, "BUY", "SELL"), "bars": 10})
        s = rs.stats(tr)
        self.assertEqual(s["n"], 600)
        self.assertGreater(s["ciLo"], 0)
        self.assertLess(s["ciLo"], s["avgR"])
        self.assertLess(s["avgR"], s["ciHi"])
        self.assertTrue(rs.verdict(s, {"n": 600, "avgR": 0.1}))
        self.assertFalse(rs.verdict(s, {"n": 600, "avgR": -0.01}))             # با اسپرد بیشتر منفی
        self.assertFalse(rs.verdict(dict(s, n=100), {"n": 100, "avgR": 0.1}))   # کم‌نمونه
        self.assertFalse(rs.verdict(dict(s, t2=-0.1), {"n": 600, "avgR": 0.1}))  # یک ثلث منفی
        self.assertEqual(rs.stats(pd.DataFrame()), {"n": 0})

    def test_end_to_end_on_random_walk_finds_no_edge(self):
        """بازار بدون الگو + اسپرد: هیچ ترکیبی نباید «قبول» شود (کنترل خطای مثبت کاذب)."""
        df = walk(60000, seed=21)
        rows, trades = rs.run_one(df, "M1", 0.2)
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(trades), 9)
        for r in rows:
            self.assertIn(r["entry"], rs.ENTRIES)
            self.assertIn(r["exit"], rs.EXITS)
            self.assertGreater(r["n"], 50)
        self.assertFalse([r["key"] for r in rows if r["PASS"]])
        self.assertTrue(rs.fmt_table(rows))

    def test_end_to_end_detects_a_planted_trend_edge(self):
        """بازار با روندهای بلند (لبهٔ واقعی): تست باید حداقل یک ترکیب را قبول کند (کنترل توان تشخیص)."""
        rng = np.random.default_rng(5)
        n = 60000
        drift, i, sgn = np.zeros(n), 0, 1
        while i < n:
            ln = int(rng.integers(150, 500))
            drift[i:i + ln] = sgn * 0.12
            sgn, i = -sgn, i + ln
        c = 4000 + np.cumsum(drift + rng.normal(0, 0.6, n))
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) + np.abs(rng.normal(0, 0.2, n))
        l = np.minimum(o, c) - np.abs(rng.normal(0, 0.2, n))
        df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "spread": 20.0},
                          index=pd.date_range("2026-01-05", periods=n, freq="1min"))
        rows, _ = rs.run_one(df, "M1", 0.2)
        self.assertTrue([r["key"] for r in rows if r["PASS"]])
        flip = [r for r in rows if r["exit"] == "flip" and r["entry"] == "st"][0]
        self.assertGreater(flip["avgR"], 0.5)


if __name__ == "__main__":
    unittest.main()
