"""Synthetic-data tests for the staged partial-exit simulation.

Run:  python _test_partial_exit.py

Scenarios (XAUUSD, ATR=1.0, entry=100.0, SL=1.0, TP=3.0, lot=0.05):
  A  BUY  -> partial at +1.5R (bank 0.02 lots) -> remainder to TP   net = 12.00
  B  BUY  -> partial at +1.5R -> reversal to break-even stop        net =  3.30
  C  same data as A but partial disabled -> full lot TP             net = 15.00
  D  same-bar SL+partial clash -> pessimistic full SL, no partial   net = -5.00
  E  lot too small to split (0.01) -> partial skipped               net =  3.00
  F  SELL -> partial at -1.5R -> remainder to TP                    net = 12.00
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, BacktestEngine
from core.risk_manager import RiskConfig
from core.risk_manager import SymbolSpec


class StubStrategy:
    """Minimal duck-typed strategy: one BUY/SELL signal at bar 10."""

    name = "stub"
    PARTIAL_RR = 1.5
    PARTIAL_FRAC = 0.5

    def __init__(self, direction: int = 1, partial: bool = True) -> None:
        self.direction = direction
        self.partial = partial

    def pf(self, key: str, default: float) -> float:
        if not self.partial:
            return default
        if key == "partial_tp_rr":
            return self.PARTIAL_RR
        if key == "partial_frac":
            return self.PARTIAL_FRAC
        return default

    def min_bars(self) -> int:
        return 2

    def prepare_and_sign(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        df = m5.copy()
        df["atr"] = 1.0
        df["signal"] = 0
        df.iloc[10, df.columns.get_loc("signal")] = self.direction
        return df


def make_bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 00:00", periods=len(rows), freq="5min")
    return pd.DataFrame(
        rows, index=idx, columns=["open", "high", "low", "close"]
    )


def build_engine(direction: int, partial: bool) -> BacktestEngine:
    risk = RiskConfig(
        risk_percent=1.0,
        min_lot=0.01,
        max_lot=0.05,
        sl_atr_multiplier=1.0,
        tp_atr_multiplier=3.0,
        breakeven_trigger_atr=1.0,
        breakeven_buffer_points=10,
        trailing_trigger_atr=99.0,   # trailing off
        trailing_distance_atr=1.2,
        commission_per_lot=0.0,
        min_sl_points=1,
        include_commission_in_risk=False,
    )
    cfg = BacktestConfig(
        initial_balance=533.0,
        spread_points=0.0,
        slippage_points=0.0,
        commission_per_lot=0.0,
        simulate_breakeven=True,
        simulate_trailing=False,
        simulate_partial=partial,
        respect_session=False,
        news_filter_enabled=False,
        max_spread_points=999.0,
    )
    return BacktestEngine(
        strategy=StubStrategy(direction=direction, partial=partial),
        risk_config=risk,
        backtest_config=cfg,
        spec=SymbolSpec.gold_default(),
        symbol="XAUUSD",
        timeframe="M5",
    )


def run_case(name: str, direction: int, partial: bool, rows, checks) -> None:
    engine = build_engine(direction, partial)
    result = engine.run(make_bars(rows))
    assert result.trades, f"{name}: expected a trade"
    t = result.trades[0]
    for desc, actual, expected in checks(t, result):
        ok = abs(actual - expected) < 1e-9 if isinstance(expected, float) else actual == expected
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name} :: {desc}: {actual!r} (expected {expected!r})")
        if not ok:
            sys.exit(1)


FLAT = (100.0, 100.0, 100.0, 100.0)
WARMUP = [FLAT] * 10          # bars 0..9
SIGNAL_BAR = [FLAT]           # bar 10 (signal on close)
# entry fills at bar 11 open = 100.0


def main() -> None:
    # ---- A: BUY -> partial -> TP ------------------------------------------
    rows_a = WARMUP + SIGNAL_BAR + [
        (100.0, 101.6, 100.0, 101.5),   # entry bar touches +1.5R
        (101.5, 103.0, 101.0, 103.0),   # runs to TP
    ] + [FLAT] * 5
    run_case(
        "A partial->TP", 1, True, rows_a,
        lambda t, r: [
            ("partial taken", t.partial_done, True),
            ("partial lot", t.partial_lot, 0.02),
            ("partial price", t.partial_price, 101.5),
            ("partial gross", t.partial_gross, 3.0),
            ("SL moved to BE", t.sl, 100.10),
            ("exit reason", t.exit_reason, "tp"),
            ("net profit", t.net_profit, 12.0),
            ("partial metric count", r.metrics["partial_closes"], 1),
        ],
    )

    # ---- B: BUY -> partial -> reversal to break-even ----------------------
    rows_b = WARMUP + SIGNAL_BAR + [
        (100.0, 101.6, 100.0, 101.5),
        (101.0, 101.2, 100.0, 100.2),   # reverses, hits BE stop
        (100.1, 100.4, 99.9, 100.0),
    ] + [FLAT] * 5
    run_case(
        "B partial->BE", 1, True, rows_b,
        lambda t, r: [
            ("partial taken", t.partial_done, True),
            ("exit reason", t.exit_reason, "breakeven"),
            ("net profit", t.net_profit, 3.3),
        ],
    )

    # ---- C: partial disabled -> full lot to TP ----------------------------
    run_case(
        "C no-partial->TP", 1, False, rows_a,
        lambda t, r: [
            ("partial not taken", t.partial_done, False),
            ("net profit", t.net_profit, 15.0),
            ("partial metric count", r.metrics["partial_closes"], 0),
        ],
    )

    # ---- D: same bar hits SL and partial level -> pessimistic full SL -----
    rows_d = WARMUP + SIGNAL_BAR + [
        (100.0, 101.6, 98.9, 99.5),     # spike both ways
    ] + [FLAT] * 5
    run_case(
        "D same-bar SL wins", 1, True, rows_d,
        lambda t, r: [
            ("partial not taken", t.partial_done, False),
            ("exit reason", t.exit_reason, "sl"),
            ("net profit", t.net_profit, -5.0),
        ],
    )

    # ---- E: lot 0.01 cannot be split -> partial skipped -------------------
    risk_small = RiskConfig(
        risk_percent=1.0, min_lot=0.01, max_lot=0.01,
        sl_atr_multiplier=1.0, tp_atr_multiplier=3.0,
        breakeven_trigger_atr=1.0, breakeven_buffer_points=10,
        trailing_trigger_atr=99.0, trailing_distance_atr=1.2,
        commission_per_lot=0.0, min_sl_points=1, include_commission_in_risk=False,
    )
    cfg = BacktestConfig(
        initial_balance=533.0, spread_points=0.0, slippage_points=0.0,
        commission_per_lot=0.0, simulate_breakeven=True, simulate_trailing=False,
        simulate_partial=True, respect_session=False, news_filter_enabled=False,
        max_spread_points=999.0,
    )
    engine = BacktestEngine(StubStrategy(1, True), risk_small, cfg, SymbolSpec.gold_default())
    result = engine.run(make_bars(rows_a))
    t = result.trades[0]
    print(f"[{'PASS' if not t.partial_done and abs(t.net_profit - 3.0) < 1e-9 else 'FAIL'}] "
          f"E unsplittable lot: partial_done={t.partial_done} net={t.net_profit}")
    assert not t.partial_done and abs(t.net_profit - 3.0) < 1e-9

    # ---- F: SELL -> partial -> TP ------------------------------------------
    rows_f = WARMUP + [ (100.0, 100.0, 100.0, 100.0) ] + [
        (100.0, 100.0, 98.4, 98.5),     # entry bar dips to -1.5R
        (98.5, 98.8, 96.9, 97.0),       # runs down to TP
    ] + [FLAT] * 5
    # signal bar must be bar 10 -> rebuild with stub direction -1
    run_case(
        "F SELL partial->TP", -1, True, rows_f,
        lambda t, r: [
            ("partial taken", t.partial_done, True),
            ("partial lot", t.partial_lot, 0.02),
            ("partial gross", t.partial_gross, 3.0),
            ("SL moved to BE (short)", t.sl, 99.90),
            ("exit reason", t.exit_reason, "tp"),
            ("net profit", t.net_profit, 12.0),
        ],
    )

    print("\nAll partial-exit tests passed.")


if __name__ == "__main__":
    main()
