"""XAUUSD M5 first-touch FVG displacement strategy.

The implementation deliberately uses:
- Vectorized ATR, EMA, ADX, sweep, and volume calculations.
- A small linear state machine only for FVG lifecycle management because
  first-touch and full-fill invalidation are path-dependent.
- BaseStrategy.evaluate(), which reads only the last closed M5 bar.
- BaseStrategy.align_higher(), which shift(1)-aligns M15 and H1 data.

Important:
This emits a market signal after the retest candle closes because the current
execution engine fills signals on the next bar open. It does not pretend to
be a true pending limit order.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from core.indicators import IndicatorSpec
from strategies.base import BaseStrategy, Side, Signal


class FvgDisplacementStrategy(BaseStrategy):
    """MTF displacement plus first FVG retest for XAUUSD M5."""

    name = "fvg_displacement"
    oscillator = "rsi"

    # ---------------------------------------------------------------- specs

    @property
    def indicator_spec(self) -> IndicatorSpec:
        return IndicatorSpec(
            ema_periods=(
                self.pi("m5_fast_ema", 20),
                self.pi("m5_slow_ema", 50),
            ),
            rsi_period=self.pi("rsi_period", 14),
            macd_params=None,
            bb_params=None,
            atr_period=self.atr_period,
            adx_period=self.pi("adx_period", 14),
        )

    @property
    def m15_spec(self) -> Optional[IndicatorSpec]:
        return IndicatorSpec(
            ema_periods=(
                self.pi("m15_fast_ema", 20),
                self.pi("m15_slow_ema", 50),
            ),
            rsi_period=None,
            macd_params=None,
            bb_params=None,
            atr_period=None,
            adx_period=None,
        )

    @property
    def h1_spec(self) -> Optional[IndicatorSpec]:
        return IndicatorSpec(
            ema_periods=(
                self.pi("h1_fast_ema", 50),
                self.pi("h1_slow_ema", 200),
            ),
            rsi_period=None,
            macd_params=None,
            bb_params=None,
            atr_period=None,
            adx_period=None,
        )

    @property
    def m15_columns(self) -> Tuple[str, ...]:
        return (
            f"ema_{self.pi('m15_fast_ema', 20)}",
            f"ema_{self.pi('m15_slow_ema', 50)}",
            "close",
        )

    @property
    def h1_columns(self) -> Tuple[str, ...]:
        return (
            f"ema_{self.pi('h1_fast_ema', 50)}",
            f"ema_{self.pi('h1_slow_ema', 200)}",
            "close",
        )

    def min_bars(self) -> int:
        return max(super().min_bars(), 120)

    # -------------------------------------------------------------- preparation

    def prepare(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        df = super().prepare(m5, m15, h1)
        return self._add_features(df)

    def _add_features(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        open_ = pd.to_numeric(out["open"], errors="coerce")
        high = pd.to_numeric(out["high"], errors="coerce")
        low = pd.to_numeric(out["low"], errors="coerce")
        close = pd.to_numeric(out["close"], errors="coerce")
        atr = pd.to_numeric(out["atr"], errors="coerce")

        candle_range = (high - low).abs().replace(0.0, np.nan)
        body = (close - open_).abs()

        body_ratio = body / candle_range
        close_location_long = (close - low) / candle_range
        close_location_short = (high - close) / candle_range

        # The displacement candle is compared against the previous ATR,
        # preventing the impulse from inflating its own benchmark.
        reference_atr = atr.shift(1).replace(0.0, np.nan)
        range_atr = candle_range / reference_atr

        displacement_long = (
            (close > open_)
            & (body_ratio >= self.pf("min_body_ratio", 0.50))
            & (close_location_long >= self.pf("min_close_location", 0.60))
            & range_atr.between(
                self.pf("displacement_min_atr", 0.80),
                self.pf("displacement_max_atr", 2.75),
            )
        )

        displacement_short = (
            (close < open_)
            & (body_ratio >= self.pf("min_body_ratio", 0.50))
            & (close_location_short >= self.pf("min_close_location", 0.60))
            & range_atr.between(
                self.pf("displacement_min_atr", 0.80),
                self.pf("displacement_max_atr", 2.75),
            )
        )

        high_2 = high.shift(2)
        low_2 = low.shift(2)

        bullish_gap = low - high_2
        bearish_gap = low_2 - high

        bullish_seed = (
            displacement_long
            & (bullish_gap >= reference_atr * self.pf("fvg_min_gap_atr", 0.02))
            & (bullish_gap <= reference_atr * self.pf("fvg_max_gap_atr", 0.90))
        )

        bearish_seed = (
            displacement_short
            & (bearish_gap >= reference_atr * self.pf("fvg_min_gap_atr", 0.02))
            & (bearish_gap <= reference_atr * self.pf("fvg_max_gap_atr", 0.90))
        )

        (
            bullish_retest,
            bearish_retest,
            bullish_zone_low,
            bullish_zone_high,
            bearish_zone_low,
            bearish_zone_high,
            bullish_zone_age,
            bearish_zone_age,
        ) = self._resolve_zone_lifecycle(
            high=high,
            low=low,
            open_=open_,
            close=close,
            bullish_seed=bullish_seed,
            bearish_seed=bearish_seed,
            bullish_lower=high_2,
            bullish_upper=low,
            bearish_lower=high,
            bearish_upper=low_2,
        )

        out["displacement_atr"] = range_atr
        out["bullish_fvg"] = bullish_seed
        out["bearish_fvg"] = bearish_seed

        out["bull_first_retest"] = bullish_retest
        out["bear_first_retest"] = bearish_retest

        out["bull_zone_low"] = bullish_zone_low
        out["bull_zone_high"] = bullish_zone_high
        out["bear_zone_low"] = bearish_zone_low
        out["bear_zone_high"] = bearish_zone_high

        out["bull_zone_age"] = bullish_zone_age
        out["bear_zone_age"] = bearish_zone_age

        # Liquidity sweep uses only prior completed M5 bars.
        sweep_lookback = self.pi("sweep_lookback", 12)

        prior_low = (
            low.shift(1)
            .rolling(sweep_lookback, min_periods=sweep_lookback)
            .min()
        )

        prior_high = (
            high.shift(1)
            .rolling(sweep_lookback, min_periods=sweep_lookback)
            .max()
        )

        out["sweep_long"] = (low < prior_low) & (close > prior_low)
        out["sweep_short"] = (high > prior_high) & (close < prior_high)

        # Volume is optional because some MT5 brokers provide unreliable
        # real-volume fields. Tick volume is used only when enabled.
        volume = None
        for column in ("volume", "tick_volume", "real_volume"):
            if column in out.columns:
                volume = pd.to_numeric(out[column], errors="coerce")
                break

        if volume is None or not self.pb("use_volume_filter", False):
            out["volume_ok"] = True
        else:
            volume_period = self.pi("volume_period", 20)
            previous_mean = (
                volume.shift(1)
                .rolling(volume_period, min_periods=volume_period)
                .mean()
            )
            valid_mean = previous_mean.notna() & (previous_mean > 0)
            out["volume_ok"] = (
                (~valid_mean)
                | (volume >= previous_mean * self.pf("min_volume_ratio", 1.10))
            )

        return out

    def _resolve_zone_lifecycle(
        self,
        *,
        high: pd.Series,
        low: pd.Series,
        open_: pd.Series,
        close: pd.Series,
        bullish_seed: pd.Series,
        bearish_seed: pd.Series,
        bullish_lower: pd.Series,
        bullish_upper: pd.Series,
        bearish_lower: pd.Series,
        bearish_upper: pd.Series,
    ) -> Tuple[
        pd.Series,
        pd.Series,
        pd.Series,
        pd.Series,
        pd.Series,
        pd.Series,
        pd.Series,
        pd.Series,
    ]:
        """Resolve one-shot FVG zones without allowing future information.

        A zone is processed in this order on every bar:

        1. Expire old zone.
        2. Invalidate if fully filled.
        3. Consume the first touch.
        4. Register a new zone created on the current bar.

        Registering the seed last prevents an FVG from being retested by the
        candle that created it.
        """
        n = len(close)
        index = close.index
        max_age = self.pi("max_fvg_age", 8)
        reclaim_fraction = self.pf("retest_reclaim_fraction", 0.35)

        bull_signal = np.zeros(n, dtype=bool)
        bear_signal = np.zeros(n, dtype=bool)

        bull_low_out = np.full(n, np.nan, dtype="float64")
        bull_high_out = np.full(n, np.nan, dtype="float64")
        bear_low_out = np.full(n, np.nan, dtype="float64")
        bear_high_out = np.full(n, np.nan, dtype="float64")

        bull_age_out = np.full(n, np.nan, dtype="float64")
        bear_age_out = np.full(n, np.nan, dtype="float64")

        # Each tuple is (creation_index, lower_boundary, upper_boundary).
        bull_zone: Optional[Tuple[int, float, float]] = None
        bear_zone: Optional[Tuple[int, float, float]] = None

        high_values = high.to_numpy(dtype="float64")
        low_values = low.to_numpy(dtype="float64")
        open_values = open_.to_numpy(dtype="float64")
        close_values = close.to_numpy(dtype="float64")

        for i in range(n):
            # ------------------------------ existing bullish zone
            if bull_zone is not None:
                created_at, zone_low, zone_high = bull_zone
                age = i - created_at

                if age > max_age:
                    bull_zone = None
                elif low_values[i] <= zone_low:
                    # Full penetration invalidates a bullish imbalance.
                    bull_zone = None
                else:
                    bull_low_out[i] = zone_low
                    bull_high_out[i] = zone_high
                    bull_age_out[i] = age

                    touched = (
                        low_values[i] <= zone_high
                        and high_values[i] >= zone_low
                    )

                    zone_width = zone_high - zone_low
                    reclaim_level = zone_low + reclaim_fraction * zone_width

                    rejected_up = (
                        close_values[i] > open_values[i]
                        and close_values[i] >= reclaim_level
                    )

                    if touched:
                        # The first touch is consumed regardless of whether
                        # rejection succeeds. No repeated stale entries.
                        bull_signal[i] = rejected_up
                        bull_zone = None

            # ------------------------------ existing bearish zone
            if bear_zone is not None:
                created_at, zone_low, zone_high = bear_zone
                age = i - created_at

                if age > max_age:
                    bear_zone = None
                elif high_values[i] >= zone_high:
                    # Full penetration invalidates a bearish imbalance.
                    bear_zone = None
                else:
                    bear_low_out[i] = zone_low
                    bear_high_out[i] = zone_high
                    bear_age_out[i] = age

                    touched = (
                        high_values[i] >= zone_low
                        and low_values[i] <= zone_high
                    )

                    zone_width = zone_high - zone_low
                    reclaim_level = zone_high - reclaim_fraction * zone_width

                    rejected_down = (
                        close_values[i] < open_values[i]
                        and close_values[i] <= reclaim_level
                    )

                    if touched:
                        bear_signal[i] = rejected_down
                        bear_zone = None

            # ------------------------------ register current FVG last
            if bool(bullish_seed.iat[i]):
                lower = bullish_lower.iat[i]
                upper = bullish_upper.iat[i]

                if pd.notna(lower) and pd.notna(upper) and upper > lower:
                    bull_zone = (i, float(lower), float(upper))

            if bool(bearish_seed.iat[i]):
                lower = bearish_lower.iat[i]
                upper = bearish_upper.iat[i]

                if pd.notna(lower) and pd.notna(upper) and upper > lower:
                    bear_zone = (i, float(lower), float(upper))

        return (
            pd.Series(bull_signal, index=index),
            pd.Series(bear_signal, index=index),
            pd.Series(bull_low_out, index=index),
            pd.Series(bull_high_out, index=index),
            pd.Series(bear_low_out, index=index),
            pd.Series(bear_high_out, index=index),
            pd.Series(bull_age_out, index=index),
            pd.Series(bear_age_out, index=index),
        )

    # ---------------------------------------------------------------- rules

    def _rules(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        m15_fast = df[f"m15_ema_{self.pi('m15_fast_ema', 20)}"]
        m15_slow = df[f"m15_ema_{self.pi('m15_slow_ema', 50)}"]

        h1_fast = df[f"h1_ema_{self.pi('h1_fast_ema', 50)}"]
        h1_slow = df[f"h1_ema_{self.pi('h1_slow_ema', 200)}"]

        # Softer than the original version:
        # M15 requires a directional EMA stack and price on the correct side
        # of the slower EMA, not necessarily above the fast EMA.
        m15_bull = (
            (df["m15_close"] > m15_slow)
            & (m15_fast > m15_slow)
        )

        m15_bear = (
            (df["m15_close"] < m15_slow)
            & (m15_fast < m15_slow)
        )

        h1_bull = (
            (df["h1_close"] > h1_slow)
            & (h1_fast > h1_slow)
        )

        h1_bear = (
            (df["h1_close"] < h1_slow)
            & (h1_fast < h1_slow)
        )

        adx_ok = df["adx"] >= self.pf("min_adx", 13.0)

        long_signal = (
            df["bull_first_retest"]
            & m15_bull
            & h1_bull
            & adx_ok
            & df["volume_ok"]
        )

        short_signal = (
            df["bear_first_retest"]
            & m15_bear
            & h1_bear
            & adx_ok
            & df["volume_ok"]
        )

        if self.pb("require_m5_bias", False):
            m5_fast = df[f"ema_{self.pi('m5_fast_ema', 20)}"]
            m5_slow = df[f"ema_{self.pi('m5_slow_ema', 50)}"]

            long_signal &= (
                (df["close"] > m5_slow)
                & (m5_fast > m5_slow)
            )

            short_signal &= (
                (df["close"] < m5_slow)
                & (m5_fast < m5_slow)
            )

        if self.pb("require_liquidity_sweep", False):
            long_signal &= df["sweep_long"]
            short_signal &= df["sweep_short"]

        if self.pb("use_rsi_filter", False):
            rsi_value = df["rsi"]

            long_signal &= rsi_value.between(
                self.pf("rsi_long_min", 48.0),
                self.pf("rsi_long_max", 78.0),
            )

            short_signal &= rsi_value.between(
                self.pf("rsi_short_min", 22.0),
                self.pf("rsi_short_max", 52.0),
            )

        # Candidate cooldown is deliberately short. The zone itself is already
        # one-shot, so a long cooldown would recreate the low sample problem.
        cooldown = self.pi("cooldown_bars", 3)
        candidate = (long_signal | short_signal).fillna(False)

        recent_candidate = (
            candidate.shift(1)
            .rolling(cooldown, min_periods=1)
            .max()
            .fillna(0)
            .astype(bool)
        )

        long_signal &= ~recent_candidate
        short_signal &= ~recent_candidate

        return long_signal, short_signal

    # ---------------------------------------------------------------- live API

    def evaluate(
        self,
        m5_df: pd.DataFrame,
        m15_df: Optional[pd.DataFrame] = None,
        h1_df: Optional[pd.DataFrame] = None,
    ) -> Optional[Signal]:
        """Return a signal only for the last closed M5 candle."""
        return super().evaluate(m5_df, m15_df, h1_df)

    # -------------------------------------------------------------- reporting

    def explain(self, row: pd.Series, side: Side) -> str:
        direction = "bullish" if side == "BUY" else "bearish"
        age_key = "bull_zone_age" if side == "BUY" else "bear_zone_age"
        age = row.get(age_key, np.nan)

        return (
            f"MTF {direction} FVG first retest | "
            f"displacement {float(row.get('displacement_atr', np.nan)):.2f}x ATR | "
            f"ADX {float(row.get('adx', np.nan)):.1f} | "
            f"zone age {float(age):.0f} bars | "
            f"M15/H1 aligned"
        )

    def snapshot(self, row: pd.Series) -> Dict[str, Any]:
        result = super().snapshot(row)

        for key in (
            "displacement_atr",
            "bull_zone_low",
            "bull_zone_high",
            "bear_zone_low",
            "bear_zone_high",
            "bull_zone_age",
            "bear_zone_age",
        ):
            value = row.get(key, np.nan)
            if pd.notna(value):
                result[key] = round(float(value), 4)

        return result

    def describe(self) -> str:
        return (
            "M5 first-touch FVG retest | "
            f"M15 EMA{self.pi('m15_fast_ema', 20)}/"
            f"EMA{self.pi('m15_slow_ema', 50)} | "
            f"H1 EMA{self.pi('h1_fast_ema', 50)}/"
            f"EMA{self.pi('h1_slow_ema', 200)}"
        )