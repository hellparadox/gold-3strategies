"""EMA 21/50 trend structure with RSI acting as a momentum band filter."""
from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from core.indicators import IndicatorSpec, crossed_above, crossed_below
from strategies.base import BaseStrategy, Side


class EmaRsiStrategy(BaseStrategy):
    name = "ema_rsi"
    oscillator = "rsi"

    @property
    def indicator_spec(self) -> IndicatorSpec:
        return IndicatorSpec(
            ema_periods=(self.pi("fast_ema", 21), self.pi("slow_ema", 50)),
            rsi_period=self.pi("rsi_period", 14),
            macd_params=(12, 26, 9),
            bb_params=None,
            atr_period=self.atr_period,
            adx_period=self.pi("adx_period", 14),
        )

    @property
    def m15_spec(self) -> Optional[IndicatorSpec]:
        return IndicatorSpec(
            ema_periods=(self.pi("m15_ema", 21),),
            rsi_period=None, macd_params=None, bb_params=None,
            atr_period=None, adx_period=None,
        )

    @property
    def h1_spec(self) -> Optional[IndicatorSpec]:
        return IndicatorSpec(
            ema_periods=(self.pi("h1_ema", 200),),
            rsi_period=None, macd_params=None, bb_params=None,
            atr_period=None, adx_period=None,
        )

    @property
    def m15_columns(self) -> Tuple[str, ...]:
        return (f"ema_{self.pi('m15_ema', 21)}", "close")

    @property
    def h1_columns(self) -> Tuple[str, ...]:
        return (f"ema_{self.pi('h1_ema', 200)}", "close")

    def min_bars(self) -> int:
        return max(super().min_bars(), 260)

    def _rules(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        fast = df[f"ema_{self.pi('fast_ema', 21)}"]
        slow = df[f"ema_{self.pi('slow_ema', 50)}"]
        rsi_v = df["rsi"]
        m15_ema = df[f"m15_ema_{self.pi('m15_ema', 21)}"]
        h1_ema = df[f"h1_ema_{self.pi('h1_ema', 200)}"]
        adx_ok = df["adx"] >= self.pf("min_adx", 20.0)

        stack_up = fast > slow
        stack_dn = fast < slow
        # pullback entry: price closes back above the fast EMA inside an uptrend
        pull_up = crossed_above(df["close"], fast) | crossed_above(fast, slow)
        pull_dn = crossed_below(df["close"], fast) | crossed_below(fast, slow)

        rsi_long = rsi_v.between(self.pf("rsi_long_min", 50.0), self.pf("rsi_long_max", 72.0))
        rsi_short = rsi_v.between(self.pf("rsi_short_min", 28.0), self.pf("rsi_short_max", 50.0))

        long_s = (
            stack_up & pull_up & rsi_long & adx_ok
            & (df["m15_close"] > m15_ema) & (df["h1_close"] > h1_ema)
        )
        short_s = (
            stack_dn & pull_dn & rsi_short & adx_ok
            & (df["m15_close"] < m15_ema) & (df["h1_close"] < h1_ema)
        )
        return long_s, short_s

    def explain(self, row: pd.Series, side: Side) -> str:
        return (
            f"EMA{self.pi('fast_ema', 21)}/{self.pi('slow_ema', 50)} "
            f"{'bullish' if side == 'BUY' else 'bearish'} stack | RSI {row['rsi']:.1f} "
            f"in band | ADX {row['adx']:.1f} | M15+H1 trend aligned"
        )

    def describe(self) -> str:
        return (
            f"Trend-following: EMA {self.pi('fast_ema', 21)}/{self.pi('slow_ema', 50)} stack, "
            f"RSI({self.pi('rsi_period', 14)}) band filter, H1 EMA{self.pi('h1_ema', 200)} bias"
        )
