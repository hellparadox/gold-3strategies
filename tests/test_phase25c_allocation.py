"""PHASE 2.5c correction — independent tests for the two review demands.

1. PORTFOLIO IDENTITY (T6): in independent-accounts mode the engine's two-bot
   equity must equal the SUM of the two single-bot equities at EVERY bar
   timestamp (Case C = A + B); min(C) >= min(A) + min(B); DD(C) <= DD(A)+DD(B);
   trade/guard counters additive. Catches the original release bug where C was
   built as `A + B - CAP` (whole series shifted down by one base).
2. M15 SIGNAL TIMING (T7): no entry may occur before its source M15 bar CLOSES
   (label + 15min); each signal bar is consumed at most once per run. The
   scenario encodes exactly the old phase-2.5 lookahead bug (signal consumed
   10 minutes inside the M15 window) — the corrected core must NOT reproduce it.

All money assertions are hand-calculated in the comments.
"""
import unittest

import numpy as np
import pandas as pd

from _phase25c_core import SimConfig, simulate
from core.risk_manager import RiskConfig


def rc(**kw):
    base = dict(risk_percent=1.0, sl_atr_multiplier=2.0, tp_atr_multiplier=3.0,
                min_lot=0.01, max_lot=0.1, min_sl_points=1.0,
                commission_per_lot=0.0, max_forced_risk_percent=0.0)
    base.update(kw)
    return RiskConfig(**base)


def flat_frame(n, start="2024-01-02 09:00", spikes=None):
    """n flat M5 bars (100, 100.5, 99.5, 100) with optional {index: (o,h,l,c)} spikes."""
    idx = pd.date_range(start, periods=n, freq="5min")
    rows = [(100, 100.5, 99.5, 100)] * n
    for i, r in (spikes or {}).items():
        rows[i] = r
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def bot_ctx(name, sig_vals, rc_, start="2024-01-02 09:00"):
    """Signal series on the 5min grid (sig_close defaults to label+5min)."""
    idx = pd.date_range(start, periods=len(sig_vals), freq="5min")
    return {name: {"sig": pd.Series(sig_vals, index=idx, dtype=int),
                   "atr": pd.Series([1.0] * len(sig_vals), index=idx),
                   "rc": rc_, "news": None, "sess": None}}


class T6IndependentPortfolioIdentity(unittest.TestCase):
    def test_equity_of_two_independent_accounts_is_exact_sum(self):
        # 40 M5 bars; spike at i=20 (high 104 -> BUY TP 103.20 hit; also SELL SL
        # 102 hit via ask 104.20). orb: BUY sig at bar2 (entry bar3 open 100.20,
        # TP at i=20), second BUY sig at bar25 (entry bar26, open at end).
        # ichi: SELL sig at bar7 (entry bar8 open 100.00, SL at i=20).
        # Hand-calc (lot 0.05 everywhere: budget $10 / loss-per-lot $200):
        #   orb trade 1: TP (103.20-100.20)/0.01*1*0.05 = +$15.00
        #   ichi trade 1: SL (100.00-102.00)/0.01*1*0.05 = -$10.00
        #   orb open at end: floating (100-100.20)/0.01*1*0.05 = -$1.00
        # final A = 1000+15-1 = 1014.00; final B = 1000-10 = 990.00
        # final C = 2000+15-10-1 = 2004.00; min A = 999, min B = 990 -> minC >= 1989
        n = 40
        m5 = flat_frame(n, spikes={20: (100, 104.0, 99.5, 103.5)})
        sig_orb = [0] * n; sig_orb[2] = 1; sig_orb[25] = 1
        sig_ichi = [0] * n; sig_ichi[7] = -1
        cfg = dict(capital=1000.0, guard_pct=0)
        r_a = simulate(m5, None, None, bot_ctx("orb", sig_orb, rc()),
                       SimConfig(bots=["orb"], shared=False, **cfg))
        r_b = simulate(m5, None, None, bot_ctx("ichi", sig_ichi, rc()),
                       SimConfig(bots=["ichi"], shared=False, **cfg))
        r_c = simulate(m5, None, None, {**bot_ctx("orb", sig_orb, rc()),
                                        **bot_ctx("ichi", sig_ichi, rc())},
                       SimConfig(bots=["orb", "ichi"], shared=False, **cfg))
        # --- identity at EVERY timestamp ---
        self.assertTrue(r_c.equity.index.equals(r_a.equity.index))
        self.assertTrue(r_c.equity.index.equals(r_b.equity.index))
        self.assertLess(float((r_c.equity - (r_a.equity + r_b.equity)).abs().max()), 1e-8)
        # --- mathematical consequences ---
        self.assertGreaterEqual(float(r_c.equity.min()),
                                float(r_a.equity.min() + r_b.equity.min()) - 1e-8)
        self.assertLessEqual(r_c.dd["dd_amount_usd"],
                             r_a.dd["dd_amount_usd"] + r_b.dd["dd_amount_usd"] + 1e-6)
        # --- counters additive, hand-checked money ---
        self.assertEqual(len(r_c.trades), len(r_a.trades) + len(r_b.trades))
        self.assertEqual(r_c.guard_blocks, r_a.guard_blocks + r_b.guard_blocks)
        self.assertAlmostEqual(r_a.trades[0]["pnl"], 15.00, places=6)
        self.assertAlmostEqual(r_b.trades[0]["pnl"], -10.00, places=6)
        self.assertAlmostEqual(float(r_a.equity.iloc[-1]), 1014.00, places=4)
        self.assertAlmostEqual(float(r_b.equity.iloc[-1]), 990.00, places=4)
        self.assertAlmostEqual(float(r_c.equity.iloc[-1]), 2004.00, places=4)
        # final C == 2*capital + netA + netB (equity-delta nets)
        self.assertAlmostEqual(float(r_c.equity.iloc[-1]),
                               2000.0 + (float(r_a.equity.iloc[-1]) - 1000.0)
                               + (float(r_b.equity.iloc[-1]) - 1000.0), places=6)


class T7M15SignalTiming(unittest.TestCase):
    def test_no_entry_before_source_bar_close_and_single_consumption(self):
        # M15 grid: bar0 09:30 (sig 0), bar1 09:45 (BUY, closes 10:00),
        #           bar2 10:00 (sig 0, closes 10:15), bar3 10:15 (SELL, closes 10:30).
        # OLD PHASE-2.5 BUG: the 09:45 signal was consumed at 09:50/09:55 — ten
        # minutes INSIDE the M15 window. Corrected core: earliest possible entry
        # is the M5 bar labeled 10:00 (opens exactly when the M15 bar closes).
        # Hand-calc (lot 0.05):
        #   BUY  entry 10:00 open 100.20, TP 103.20 hit at 10:05 (H 104)  -> +$15.00
        #   SELL entry 10:30 open 100.00, TP 97.00 hit at 10:35
        #        (ask = low 96.50 + 0.20 = 96.70 <= 97)                   -> +$15.00
        # bar2 (sig 0) must be consumed silently at 10:15 (no entry, no retry).
        n = 17
        m5 = flat_frame(n, start="2024-01-02 09:30",
                        spikes={7: (100, 104.0, 99.5, 103.5),
                                13: (100, 100.5, 96.5, 97.0)})
        m15_idx = pd.date_range("2024-01-02 09:30", periods=4, freq="15min")
        sig = pd.Series([0, 1, 0, -1], index=m15_idx, dtype=int)
        atr = pd.Series([1.0] * 4, index=m15_idx)
        ctx = {"ichi": {"sig": sig, "atr": atr, "rc": rc(), "news": None, "sess": None,
                        "sig_close": m15_idx + pd.Timedelta(minutes=15)}}
        r = simulate(m5, None, None, ctx,
                     SimConfig(capital=1000.0, bots=["ichi"], shared=False, guard_pct=0))
        self.assertEqual(len(r.trades), 2)
        t0, t1 = r.trades
        self.assertEqual((t0["side"], t0["entry_time"]), ("BUY", "2024-01-02 10:00:00"))
        self.assertAlmostEqual(t0["pnl"], 15.00, places=6)
        self.assertEqual((t1["side"], t1["entry_time"]), ("SELL", "2024-01-02 10:30:00"))
        self.assertAlmostEqual(t1["pnl"], 15.00, places=6)
        # the old lookahead bug would have entered at 09:50/09:55 (and 10:20):
        for t_ in r.trades:
            et = pd.Timestamp(t_["entry_time"])
            self.assertFalse(pd.Timestamp("2024-01-02 09:45") <= et < pd.Timestamp("2024-01-02 10:00"))
            self.assertFalse(pd.Timestamp("2024-01-02 10:15") <= et < pd.Timestamp("2024-01-02 10:30"))
        # --- independent recomputation of the consumption rule ---
        sc = m15_idx + pd.Timedelta(minutes=15)
        seen = set()
        for t_ in r.trades:
            et = pd.Timestamp(t_["entry_time"])
            k = int(sc.searchsorted(et, side="right")) - 1   # last CLOSED signal bar
            self.assertGreaterEqual(k, 1)                     # bar 0 never consumed
            self.assertLessEqual(sc[k], et)                   # source closed at/before entry
            self.assertNotIn(k, seen); seen.add(k)            # consumed at most once
            s = int(sig.iloc[k])
            self.assertNotEqual(s, 0)
            self.assertEqual(t_["side"], "BUY" if s > 0 else "SELL")
        # both trades closed, nothing open -> final equity = 1000 + 15 + 15
        self.assertAlmostEqual(float(r.equity.iloc[-1]), 1030.00, places=4)


if __name__ == "__main__":
    unittest.main()
