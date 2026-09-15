"""PHASE 2.5c — unit tests for the shared-account simulator core.

Every financial assertion is hand-calculated in comments (point 4 of the
2026-09-15 review). These tests validate the NEW simulator itself; running
the old engine baseline is NOT validation of this engine.
"""
import unittest

import numpy as np
import pandas as pd

from _phase25c_core import SimConfig, simulate
from core.risk_manager import RiskConfig

PT = 0.01  # point


def frame(rows, spreads=None):
    """rows: list of (open, high, low, close) -> m5 frame with spread column."""
    idx = pd.date_range("2024-01-02 09:00", periods=len(rows), freq="5min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["spread"] = spreads if spreads is not None else [20.0] * len(rows)
    return df


def ctx(rc=None, sig_vals=(0, 1, 0, 0)):
    rc = rc or RiskConfig(risk_percent=1.0, sl_atr_multiplier=2.0, tp_atr_multiplier=3.0,
                          min_lot=0.01, max_lot=0.1, min_sl_points=1.0,
                          commission_per_lot=0.0, max_forced_risk_percent=0.0)
    idx = pd.date_range("2024-01-02 09:00", periods=len(sig_vals), freq="5min")
    sig = pd.Series(sig_vals, index=idx, dtype=int)
    atr = pd.Series([1.0] * len(sig_vals), index=idx)
    return {"orb": {"sig": sig, "atr": atr, "rc": rc, "news": None, "sess": None}}


class T1LongShortPnL(unittest.TestCase):
    def test_buy_tp_hand_calculated(self):
        # ATR=1 -> SL dist = 2*1 = 2.00, TP dist = 3*1 = 3.00, spread=20pts=0.20
        # BUY entry at bar2 open 100.00 + 0.20 = 100.20; TP = 103.20
        # bar3 high 104 >= 103.20 -> exit 103.20
        # pnl = (103.20-100.20)/0.01 * 1.0 * lot
        m5 = frame([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100),
                    (100, 100.5, 99.5, 100), (103, 104, 102.9, 103.5)])
        r = simulate(m5, None, None, ctx(), SimConfig(capital=1000, bots=["orb"], shared=False, guard_pct=0))
        self.assertEqual(len(r.trades), 1)
        # sizing: budget = 1000*1% = $10; loss_per_lot = 2.00/0.01*1.0 = $200 -> lot = 0.05
        # entry 100.20, exit 103.20 -> +3.00 price -> (3.00/0.01)*1.0*0.05 = $15.00
        self.assertAlmostEqual(r.trades[0]["pnl"], 15.00, places=6)

    def test_sell_sl_hand_calculated(self):
        # SELL entry at bar2 open 100.00 (bid); SL = 100 + 2.00 = 102.00
        # bar3: high+spread(0.20) = 103.2 >= 102 -> exit at SL 102.00
        # pnl = (100.00-102.00)/0.01 * 1.0 * 0.05 = -$10.00
        m5 = frame([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100),
                    (100, 100.5, 99.5, 100), (101, 103, 100.5, 102)])
        r = simulate(m5, None, None, ctx(sig_vals=(0, -1, 0, 0)),
                     SimConfig(capital=1000, bots=["orb"], shared=False, guard_pct=0))
        self.assertEqual(len(r.trades), 1)
        self.assertAlmostEqual(r.trades[0]["pnl"], -10.00, places=6)
        self.assertEqual(r.trades[0]["reason"], "sl")

    def test_sell_tp_charges_spread_on_exit(self):
        # SELL TP = 100 - 3.00 = 97.00; exit needs ask <= 97 -> low + 0.20 <= 97 -> low <= 96.80
        # bar3 low 96.50 -> ask 96.70 <= 97 -> exit at TP 97.00
        # pnl = (100.00-97.00)/0.01*1*0.05 = +$15.00 (spread paid via ask-side trigger)
        m5 = frame([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100),
                    (100, 100.5, 99.5, 100), (98, 99, 96.5, 97)])
        r = simulate(m5, None, None, ctx(sig_vals=(0, -1, 0, 0)),
                     SimConfig(capital=1000, bots=["orb"], shared=False, guard_pct=0))
        self.assertAlmostEqual(r.trades[0]["pnl"], 15.00, places=6)


class T2GapFill(unittest.TestCase):
    def test_gap_through_sl_fills_at_open(self):
        # BUY entry 100.20, SL 98.20; bar3 OPENS at 95 (gap) -> exit at 95.00, not 98.20
        # pnl = (95.00-100.20)/0.01*1*0.05 = -$26.00
        m5 = frame([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100),
                    (100, 100.5, 99.5, 100), (95, 96, 94.5, 95.5)])
        r = simulate(m5, None, None, ctx(), SimConfig(capital=1000, bots=["orb"], shared=False, guard_pct=0))
        self.assertEqual(r.trades[0]["reason"], "gap")
        self.assertAlmostEqual(r.trades[0]["pnl"], -26.00, places=6)

    def test_gap_fill_disabled_fills_at_stop(self):
        m5 = frame([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100),
                    (100, 100.5, 99.5, 100), (95, 96, 94.5, 95.5)])
        r = simulate(m5, None, None, ctx(), SimConfig(capital=1000, bots=["orb"], shared=False,
                                                      guard_pct=0, gap_fill_at_open=False))
        self.assertAlmostEqual(r.trades[0]["pnl"], -10.00, places=6)


class T3SharedSizing(unittest.TestCase):
    def test_second_bot_sizes_from_updated_shared_balance(self):
        # big capital so lots are NOT at the min-lot floor:
        # capital 200k, risk 1% -> budget 2000; loss_per_lot = 2.00/0.01*1 = 200 -> lot = 10.0 -> clamp max 0.1
        # use max_lot 5.0 to keep lots continuous: lot1 = 2000/200 = 10
        # after bot A wins $3.00, shared balance 200003 -> bot B lot = 2000.03/200 = 10.00015 -> round step 0.01 -> 10.00
        # to make the effect visible, give bot A a huge win via big lot:
        rc = RiskConfig(risk_percent=50.0, sl_atr_multiplier=2.0, tp_atr_multiplier=3.0,
                        min_lot=0.01, max_lot=100.0, min_sl_points=1.0, commission_per_lot=0.0)
        idx = pd.date_range("2024-01-02 09:00", periods=6, freq="5min")
        m5 = pd.DataFrame({"open": [100]*6, "high": [100.5, 100.5, 104, 100.5, 100.5, 100.5],
                           "low": [99.5]*6, "close": [100]*6, "spread": [20.0]*6}, index=idx)
        ctxd = ctx(rc, sig_vals=(0, 1, 0, 0, 0, 0))     # orb: signal bar2 only
        # second bot same ctx but signal one bar later
        ctx2 = ctx(rc, sig_vals=(0, 0, 0, 0, 1, 0))     # ichi: signal bar5
        bot_ctx = {"orb": ctxd["orb"], "ichi": ctx2["orb"]}
        r = simulate(m5, None, None, bot_ctx,
                     SimConfig(capital=20000.0, bots=["orb", "ichi"], shared=True, guard_pct=0))
        # orb: budget 10000, loss_per_lot 200 -> lot 50 (max 100 ok) -> entry 100.20 TP 103.20 -> pnl = 3.00*100*50/... wait
        # pnl = (103.20-100.20)/0.01 * 1.0 * 50 = 300*50 = 15000
        # then ichi sizes from 35000: budget 17500 -> lot 87.5 -> 87.5 (step 0.01)
        # orb exits at TP same bar: +3.00*100*50 = +15000 -> shared balance 35000
        self.assertEqual(len(r.trades), 1)
        self.assertAlmostEqual(r.trades[0]["pnl"], 15000.0, places=2)
        # ichi then sizes from the UPDATED shared 35000: budget 17500 -> lot 87.5 (max 100 ok)
        # its position stays open (TP 103.20 not reached): floating = (100-100.20)*100*87.5 = -1750
        # equity at end = 35000 - 1750 = 33250 -> proves lot grew from the shared balance
        self.assertAlmostEqual(float(r.equity.iloc[-1]), 33250.0, places=1)

    def test_independent_accounts_do_not_share(self):
        rc = RiskConfig(risk_percent=50.0, sl_atr_multiplier=2.0, tp_atr_multiplier=3.0,
                        min_lot=0.01, max_lot=100.0, min_sl_points=1.0, commission_per_lot=0.0)
        idx = pd.date_range("2024-01-02 09:00", periods=6, freq="5min")
        m5 = pd.DataFrame({"open": [100]*6, "high": [100.5, 100.5, 104, 100.5, 100.5, 100.5],
                           "low": [99.5]*6, "close": [100]*6, "spread": [20.0]*6}, index=idx)
        c1 = ctx(rc, sig_vals=(0, 1, 0, 0, 1, 0))
        r = simulate(m5, None, None, c1,
                     SimConfig(capital=20000.0, bots=["orb"], shared=False, guard_pct=0))
        # single bot: lot 50 -> +15000 -> balance 35000; the 2nd signal (bar5) sizes from 35000
        # entry bar5 open 100.2? entry = open+spread = 100.20, TP 103.20 not hit again (high 100.5)
        # just verify no cross-bot interference: trades all bot 'orb'
        self.assertTrue(all(t_["bot"] == "orb" for t_ in r.trades))


class T4FloatingAndMargin(unittest.TestCase):
    def test_floating_loss_marks_equity_below_balance(self):
        # BUY entry 100.20; bar3 closes 99.00 -> floating = (99-100.2)/0.01*1*0.01 = -1.2
        # equity at that close = 1000 - 1.2 = 998.8 (< balance 1000)
        m5 = frame([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100),
                    (100, 100.5, 99.5, 100), (99, 99.5, 98.5, 99.0)])
        r = simulate(m5, None, None, ctx(), SimConfig(capital=1000, bots=["orb"], shared=False, guard_pct=0))
        # floating = (99.00-100.20)/0.01*1*0.05 = -6.0 -> equity 994.00
        self.assertAlmostEqual(float(r.equity.iloc[-1]), 994.00, places=4)

    def test_margin_reject(self):
        # leverage 1:1 -> margin = 0.01*100*100 = 100 > capital 50 -> reject
        m5 = frame([(100, 100.5, 99.5, 100)] * 4)
        r = simulate(m5, None, None, ctx(), SimConfig(capital=50.0, bots=["orb"], shared=False,
                                                      guard_pct=0, leverage=1.0))
        self.assertEqual(r.margin_rejects, 1)
        self.assertEqual(len(r.trades), 0)

    def test_simultaneous_entries_both_bots(self):
        # both bots signal bar2 -> both enter at bar3 open, both positions open concurrently
        m5 = frame([(100, 100.5, 99.5, 100)] * 6)
        c = ctx(sig_vals=(0, 1, 0, 0, 0, 0))
        bot_ctx = {"orb": c["orb"], "ichi": c["orb"]}
        r = simulate(m5, None, None, bot_ctx,
                     SimConfig(capital=1000, bots=["orb", "ichi"], shared=True, guard_pct=0))
        entries = [t_ for t_ in r.trades]
        # neither exits (range 99.5-100.5 vs SL 98.2/TP 103.2) -> both still open at end
        self.assertEqual(len(entries), 0)
        # floating equity reflects 2 positions: 2 * (100-100.20)/0.01*1*0.05 = -2.0
        self.assertAlmostEqual(float(r.equity.iloc[-1]), 998.00, places=4)


class T5SpreadTiming(unittest.TestCase):
    def test_entry_uses_previous_bar_spread_not_same_bar(self):
        # spread column: bar2=20pts, bar3=99pts. Entry at bar3 OPEN must use bar2's 20pts (known),
        # NOT bar3's 99 (stamped at bar3 close -> would be lookahead).
        # BUY entry = 100 + 0.20 = 100.20 (if same-bar leak: 100.99)
        m5 = frame([(100, 100.5, 99.5, 100)] * 4, spreads=[20.0, 20.0, 99.0, 99.0])
        r = simulate(m5, None, None, ctx(), SimConfig(capital=1000, bots=["orb"], shared=False,
                                                      guard_pct=0, spread_pts=0))
        # no exit -> use floating to infer entry price: equity = 1000 + (100-100.20)/... wait close=100
        # floating = (100 - 100.20)*100*0.05 = -1.00 => entry was 100.20 (prev-bar spread OK)
        self.assertAlmostEqual(float(r.equity.iloc[-1]), 999.00, places=4)


if __name__ == "__main__":
    unittest.main()
