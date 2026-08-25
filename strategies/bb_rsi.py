"""Bollinger Band exhaustion + RSI reversal, for range/low-ADX regimes."""
from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from core.indicators import IndicatorSpec, crossed_above, crossed_below
from strategies.base import BaseStrategy, Side


class BbRsiStrategy(BaseStrategy):
    name = "bb_rsi"
    oscillator = "rsi"

    @property
    def indicator_spec(self) -> IndicatorSpec:
        return IndicatorSpec(
            ema_periods=(21, 50),
            rsi_period=self.pi("rsi_period", 14),
            macd_params=(12, 26, 9),
            bb_params=(self.pi("bb_period", 20), self.pf("bb_std", 2.0)),
            atr_period=self.atr_period,
            adx_period=self.pi("adx_period", 14),
        )

    @property
    def h1_spec(self) -> Optional[IndicatorSpec]:
        return IndicatorSpec(
            ema_periods=(self.pi("h1_ema", 200),),
            rsi_period=None, macd_params=None, bb_params=None,
            atr_period=None, adx_period=None,
        )

    @property
    def h1_columns(self) -> Tuple[str, ...]:
        return (f"ema_{self.pi('h1_ema', 200)}", "close")

    def min_bars(self) -> int:
        return max(super().min_bars(), 260)

    def _rules(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        close = df["close"]
        rsi_v = df["rsi"]
        upper, lower, mid = df["bb_upper"], df["bb_lower"], df["bb_mid"]
        quiet = df["adx"] <= self.pf("max_adx", 28.0)

        # touched/pierced the band on a recent bar, now snapping back inside
        pierced_low = (df["low"] <= lower).rolling(3, min_periods=1).max().astype(bool)
        pierced_high = (df["high"] >= upper).rolling(3, min_periods=1).max().astype(bool)
        reclaim_low = crossed_above(close, lower)
        reclaim_high = crossed_below(close, upper)

        rsi_was_low = (rsi_v <= self.pf("rsi_oversold", 30.0)).rolling(3, min_periods=1).max().astype(bool)
        rsi_was_high = (rsi_v >= self.pf("rsi_overbought", 70.0)).rolling(3, min_periods=1).max().astype(bool)
        rsi_turning_up = rsi_v.diff() > 0
        rsi_turning_dn = rsi_v.diff() < 0

        long_s = pierced_low & reclaim_low & rsi_was_low & rsi_turning_up & quiet & (close < mid)
        short_s = pierced_high & reclaim_high & rsi_was_high & rsi_turning_dn & quiet & (close > mid)

        if self.pb("respect_macro_trend", False):
            h1_ema = df[f"h1_ema_{self.pi('h1_ema', 200)}"]
            long_s &= df["h1_close"] > h1_ema
            short_s &= df["h1_close"] < h1_ema
        return long_s, short_s

    def explain(self, row: pd.Series, side: Side) -> str:
        band = "lower" if side == "BUY" else "upper"
        level = row["bb_lower"] if side == "BUY" else row["bb_upper"]
        return (
            f"Mean reversion off {band} BB ({level:.2f}) | RSI {row['rsi']:.1f} reversing | "
            f"ADX {row['adx']:.1f} (range regime)"
        )

    def describe(self) -> str:
        return (
            f"Mean-reversion: BB({self.pi('bb_period', 20)}, {self.pf('bb_std', 2.0)}) reclaim + "
            f"RSI({self.pi('rsi_period', 14)}) {self.pf('rsi_oversold', 30.0):.0f}/"
            f"{self.pf('rsi_overbought', 70.0):.0f} reversal, ADX ≤ {self.pf('max_adx', 28.0):.0f}"
        )
