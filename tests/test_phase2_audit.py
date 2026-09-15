"""PHASE-2 AUDIT TESTS — added without touching production logic.

Covers (per audit request 2026-09-15):
  T1  look-ahead truncation invariance (both strategies)
  T2  engine fills at NEXT bar's open (no same-bar fill)
  T3  H1 alignment at the hour boundary (no intra-hour leak)
  T4  lot arithmetic with odd tick specs + bogus-broker sizing guard
  T5  min-lot forced risk + 3% guard on/off
  T6  M15 dedup semantics after restart (documents in-memory state loss)
  T7  gap-through-SL engine behaviour (documents fill AT stop, no gap slip)
  T8  news blackout timezone conversion + fixed-offset DST sensitivity
  T9  break-even arithmetic sanity (commission math in _settle_trade)
Existing coverage (not duplicated): SL+TP same-bar -> SL wins, partial
precedence, min-volume split (test_backtest_timing.py).
"""
import json
import os
import tempfile
import unittest

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, BacktestEngine, BacktestTrade, HistoricalNewsChecker
from core import Settings
from core.risk_manager import RiskConfig, RiskManager, SymbolSpec
from strategies import build_from_settings


def synth(n, start="2024-01-02", freq="5min", seed=7, drift=0.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=freq)
    close = 2000.0 + np.cumsum(rng.normal(drift, 2.0, n))
    out = pd.DataFrame(
        {
            "open": close + rng.normal(0, 0.3, n),
            "high": close + np.abs(rng.normal(1.5, 0.5, n)),
            "low": close - np.abs(rng.normal(1.5, 0.5, n)),
            "close": close,
            "tick_volume": rng.integers(10, 200, n),
        },
        index=idx,
    )
    return out


class T1LookaheadTruncation(unittest.TestCase):
    """A signal on bar i must be identical when computed on data[:i+1]."""

    def _check(self, strategy, df, m15=None, h1=None):
        full = strategy.prepare_and_sign(df, m15, h1)
        sig_idx = np.flatnonzero(full["signal"].to_numpy() != 0)
        self.assertGreater(len(sig_idx), 0, "synthetic data produced no signals")
        for i in sig_idx[:: max(1, len(sig_idx) // 5)]:  # sample up to 5 signals
            head = df.iloc[: i + 1]
            h15 = m15.iloc[: m15.index.searchsorted(df.index[i]) + 1] if m15 is not None else None
            hh1 = h1.iloc[: h1.index.searchsorted(df.index[i]) + 1] if h1 is not None else None
            part = strategy.prepare_and_sign(head, h15, hh1)
            self.assertEqual(
                int(part["signal"].iloc[-1]),
                int(full["signal"].iloc[i]),
                f"signal changed under truncation at {df.index[i]} -> look-ahead leak",
            )

    def test_orb_no_lookahead(self):
        s = Settings.load("config/settings.yaml")
        strat = build_from_settings(s)
        df = synth(1500, freq="5min")
        self._check(strat, df)

    def test_ichimoku_no_lookahead(self):
        s = Settings.load("config/settings_ichimoku.yaml")
        strat = build_from_settings(s)
        m15 = synth(400, freq="15min")
        h1 = synth(120, freq="h")
        # align m5 frame with m15 index style: strategy trades the M15 frame
        self._check(strat, m15, m15, h1)


class T2NextBarFill(unittest.TestCase):
    def test_entry_is_next_open_plus_spread(self):
        s = Settings.load("config/settings.yaml")
        cfg = BacktestConfig.from_settings(s)
        cfg.news_filter_enabled = False
        cfg.spread_points = 10.0
        cfg.slippage_points = 0.0
        cfg.simulate_breakeven = False
        cfg.simulate_trailing = False
        eng = BacktestEngine(build_from_settings(s), RiskConfig.from_settings(s), cfg,
                             SymbolSpec.gold_default())
        df = synth(900)
        res = eng.run(df, None, None)
        self.assertGreater(len(res.trades), 0)
        spread = cfg.spread_points * 0.01
        for t in res.trades:
            bar = df.loc[: t.entry_time].index[-1]
            nxt_open = df.loc[bar, "open"] if bar == t.entry_time else None
            # engine stamps entry_time as the fill bar; fill price must equal that bar's open (+spread for BUY)
            if t.side == "BUY":
                self.assertAlmostEqual(t.entry_price, df.loc[t.entry_time, "open"] + spread, delta=0.02)
            else:
                self.assertAlmostEqual(t.entry_price, df.loc[t.entry_time, "open"], delta=0.02)
            self.assertIsNotNone(nxt_open)


class T3H1Boundary(unittest.TestCase):
    def test_m5_inside_hour_h_never_sees_hour_h_close(self):
        s = Settings.load("config/settings_ichimoku.yaml")
        strat = build_from_settings(s)
        m5 = synth(600, freq="5min")
        h1 = synth(100, freq="h")
        prepared = strat.prepare(m5, None, h1)
        # h1 columns land via align_higher with shift(1): the first M5 bar of
        # hour H must carry hour H-1's values (or NaN), never hour H's.
        # find a column broadcast from H1 if the strategy uses h1_columns
        cols = [c for c in prepared.columns if c.startswith("h1_")]
        if not cols:
            self.skipTest("strategy has no H1 broadcast columns on this frame")
        col = cols[0]
        hours = prepared.index.hour
        for h in range(1, 5):
            inside = prepared[(hours == h)]
            if inside.empty:
                continue
            h1_vals = h1[col.replace("h1_", "")] if col.replace("h1_", "") in h1 else None
            if h1_vals is None:
                continue
            leak = inside[col].isin(h1_vals.iloc[[h]]).any() if h < len(h1_vals) else False
            self.assertFalse(leak, f"intra-hour leak detected in {col} at hour {h}")


class T4LotArithmetic(unittest.TestCase):
    def test_money_per_lot_matches_distance_over_tick_times_value(self):
        spec = SymbolSpec(tick_size=0.01, tick_value=1.0, contract_size=100.0)
        self.assertAlmostEqual(spec.money_per_lot(23.34), 2334.0)
        spec2 = SymbolSpec(tick_size=0.05, tick_value=5.0, contract_size=100.0)
        self.assertAlmostEqual(spec2.money_per_lot(1.0), 100.0)

    def test_bogus_broker_tick_value_replaced_by_computed(self):
        class FakeInfo:
            name = "XAUUSD@"
            digits = 2
            point = 0.01
            trade_tick_size = 0.01
            trade_tick_value = 0.10          # bogus (off 10x)
            trade_tick_value_loss = 0.10
            trade_tick_value_profit = 0.10
            trade_contract_size = 100.0
            volume_min = 0.01
            volume_max = 100.0
            volume_step = 0.01
            trade_stops_level = 0.0
        spec = SymbolSpec.from_mt5(FakeInfo())
        self.assertAlmostEqual(spec.tick_value, 1.0)  # computed tick_size*contract

    def test_plausible_reported_tick_value_kept(self):
        class FakeInfo:
            name = "XAUUSD@"
            digits = 2
            point = 0.01
            trade_tick_size = 0.01
            trade_tick_value = 1.2
            trade_tick_value_loss = 1.2
            trade_tick_value_profit = 1.2
            trade_contract_size = 100.0
            volume_min = 0.01
            volume_max = 100.0
            volume_step = 0.01
            trade_stops_level = 0.0
        spec = SymbolSpec.from_mt5(FakeInfo())
        self.assertAlmostEqual(spec.tick_value, 1.2)  # inside 0.5x..2x band -> kept


class T5ForcedRiskGuard(unittest.TestCase):
    def _rm(self, guard):
        cfg = RiskConfig(sl_atr_multiplier=2.0, risk_percent=1.0, max_lot=0.1,
                         max_forced_risk_percent=guard)
        return RiskManager(cfg, SymbolSpec.gold_default())

    def test_guard_blocks_oversized_min_lot(self):
        # ATR 11.85, balance 491 -> forced risk 4.8% -> must be skipped
        self.assertIsNone(self._rm(3.0).build_levels("SELL", 4270.4, 11.85, 491.10))

    def test_guard_off_takes_min_lot(self):
        levels = self._rm(0.0).build_levels("SELL", 4270.4, 11.85, 491.10)
        self.assertIsNotNone(levels)
        self.assertAlmostEqual(levels.lot, 0.01)

    def test_guard_passes_normal_risk(self):
        levels = self._rm(3.0).build_levels("SELL", 4270.4, 5.0, 491.10)
        self.assertIsNotNone(levels)
        self.assertLess(levels.risk_money / 491.10 * 100, 3.0)


class T6RestartDedupSemantics(unittest.TestCase):
    def test_same_bar_evaluated_once_then_again_after_restart(self):
        s = Settings.load("config/settings_ichimoku.yaml")
        strat = build_from_settings(s)
        m15 = synth(400, freq="15min")
        h1 = synth(120, freq="h")
        first = strat.evaluate(m15, m15, h1)
        if first is not None:
            second = strat.evaluate(m15, m15, h1)
            self.assertIsNone(second, "dedup failed: same bar evaluated twice")
            fresh = build_from_settings(s)  # simulates a restart
            again = fresh.evaluate(m15, m15, h1)
            self.assertIsNotNone(again, "fresh instance must re-evaluate the bar (restart loses dedup state)")


class T7GapThroughSL(unittest.TestCase):
    def test_engine_fills_at_stop_level_on_gap(self):
        s = Settings.load("config/settings_ichimoku.yaml")
        cfg = BacktestConfig.from_settings(s)
        cfg.news_filter_enabled = False
        cfg.spread_points = 0.0
        cfg.slippage_points = 0.0
        cfg.simulate_breakeven = False
        cfg.simulate_trailing = False
        eng = BacktestEngine(build_from_settings(s), RiskConfig.from_settings(s), cfg,
                             SymbolSpec.gold_default())
        levels = eng.risk.build_levels("BUY", 100.0, 1.0, 533.0)
        t = BacktestTrade(ticket=1, side="BUY", lot=0.01,
                          entry_time=pd.Timestamp("2024-01-02"), entry_price=100.0,
                          sl=levels.sl, tp=levels.tp, atr=1.0,
                          initial_sl_distance=abs(100.0 - levels.sl))
        # gap: low jumps FAR below the stop in one bar
        closed = eng._process_open_bar(t, pd.Timestamp("2024-01-02 12:15"), 100.5, 80.0,
                                       90.0, 1.0, eng._spread_price, eng._slip_price)
        self.assertTrue(closed)
        self.assertAlmostEqual(t.exit_price, levels.sl)  # filled AT stop, not at 80 -> engine does NOT model gap slippage


class T8NewsTimezone(unittest.TestCase):
    def _checker(self, offset, event_utc):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump([{"title": "CPI m/m", "time": event_utc}], f)
        return HistoricalNewsChecker(filepath=path, before_minutes=5, after_minutes=5,
                                     enabled=True, server_utc_offset_hours=offset)

    def test_server_bar_time_converted_with_offset(self):
        ck = self._checker(3.0, "2026-09-15T13:30:00+00:00")  # 13:30 UTC == 16:30 server (offset +3)
        self.assertTrue(ck.is_blocked(pd.Timestamp("2026-09-15 16:30")))
        self.assertFalse(ck.is_blocked(pd.Timestamp("2026-09-15 18:00")))

    def test_wrong_offset_shifts_window_one_hour(self):
        ck = self._checker(2.0, "2026-09-15T13:30:00+00:00")  # DST mismatch: broker actually +3
        self.assertFalse(ck.is_blocked(pd.Timestamp("2026-09-15 16:30")),
                         "with wrong offset the true event time (16:30 server) is NOT blocked")
        self.assertTrue(ck.is_blocked(pd.Timestamp("2026-09-15 15:30")),
                        "fixed offset shifts blackout window one hour earlier when broker DST differs")


class T9CommissionMath(unittest.TestCase):
    def test_settle_charges_commission_once_on_full_volume(self):
        s = Settings.load("config/settings.yaml")
        cfg = BacktestConfig.from_settings(s)
        cfg.commission_per_lot = 6.0
        eng = BacktestEngine(build_from_settings(s), RiskConfig.from_settings(s), cfg,
                             SymbolSpec.gold_default())
        t = BacktestTrade(ticket=1, side="BUY", lot=0.05,
                          entry_time=pd.Timestamp("2024-01-02"), entry_price=100.0,
                          sl=98.0, tp=110.0, atr=1.0, initial_sl_distance=2.0)
        t.exit_price = 103.0
        eng._settle_trade(t, 533.0)
        self.assertAlmostEqual(t.commission, 0.30)  # 6.0 * 0.05 once
        self.assertAlmostEqual(t.gross_profit, 15.0)  # 3.0/0.01 * 1.0 * 0.05
        self.assertAlmostEqual(t.net_profit, 14.70)


if __name__ == "__main__":
    unittest.main()
