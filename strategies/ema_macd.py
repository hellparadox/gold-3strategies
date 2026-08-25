"""EMA 9/21 crossover + MACD confirmation, gated by M15 momentum and H1 trend.

This is the production default: the H1 EMA200 filter kills counter-trend
noise, the M15 EMA21 slope requires the intermediate leg to agree, and MACD
confirms that the cross has actual momentum behind it.
"""
from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from core.indicators import IndicatorSpec, crossed_above, crossed_below
from strategies.base import BaseStrategy, Side


class EmaMacdStrategy(BaseStrategy):
    name = "ema_macd"
    oscillator = "macd"

    @property
    def indicator_spec(self) -> IndicatorSpec:
        return IndicatorSpec(
            ema_periods=(self.pi("fast_ema", 9), self.pi("slow_ema", 21)),
            rsi_period=14,
            macd_params=(
                self.pi("macd_fast", 12),
                self.pi("macd_slow", 26),
                self.pi("macd_signal", 9),
            ),
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
        fast = df[f"ema_{self.pi('fast_ema', 9)}"]
        slow = df[f"ema_{self.pi('slow_ema', 21)}"]
        m15_ema = df[f"m15_ema_{self.pi('m15_ema', 21)}"]
        h1_ema = df[f"h1_ema_{self.pi('h1_ema', 200)}"]
        macd_line, macd_sig = df["macd"], df["macd_signal"]
        adx_ok = df["adx"] >= self.pf("min_adx", 18.0)

        # cross must be fresh: fired on this bar or the previous 2
        cross_up = crossed_above(fast, slow).rolling(3, min_periods=1).max().astype(bool)
        cross_dn = crossed_below(fast, slow).rolling(3, min_periods=1).max().astype(bool)

        # M15 momentum: price above its EMA21 AND that EMA rising
        m15_slope_up = m15_ema.diff() > 0
        m15_slope_dn = m15_ema.diff() < 0
        m15_bull = (df["m15_close"] > m15_ema) & m15_slope_up
        m15_bear = (df["m15_close"] < m15_ema) & m15_slope_dn

        # H1 macro trend
        h1_bull = df["h1_close"] > h1_ema
        h1_bear = df["h1_close"] < h1_ema

        macd_bull = macd_line > macd_sig
        macd_bear = macd_line < macd_sig
        if self.pb("require_macd_above_zero", False):
            macd_bull &= macd_line > 0
            macd_bear &= macd_line < 0

        long_s = cross_up & (fast > slow) & macd_bull & m15_bull & h1_bull & adx_ok
        short_s = cross_dn & (fast < slow) & macd_bear & m15_bear & h1_bear & adx_ok
        return long_s, short_s

    def explain(self, row: pd.Series, side: Side) -> str:
        f, s = self.pi("fast_ema", 9), self.pi("slow_ema", 21)
        arrow = "↑" if side == "BUY" else "↓"
        return (
            f"EMA{f}{arrow}EMA{s} cross | MACD {row['macd']:.3f} vs signal "
            f"{row['macd_signal']:.3f} | ADX {row['adx']:.1f} | "
            f"M15 EMA{self.pi('m15_ema', 21)} aligned | "
            f"H1 EMA{self.pi('h1_ema', 200)} macro {'bullish' if side == 'BUY' else 'bearish'}"
        )

    def describe(self) -> str:
        return (
            f"EMA {self.pi('fast_ema', 9)}/{self.pi('slow_ema', 21)} cross + MACD"
            f"({self.pi('macd_fast', 12)},{self.pi('macd_slow', 26)},{self.pi('macd_signal', 9)})"
            f" + M15 EMA{self.pi('m15_ema', 21)} + H1 EMA{self.pi('h1_ema', 200)}"
            f" + ADX>={self.pf('min_adx', 18.0):.0f}"
        )
