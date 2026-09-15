"""
Kijun Pullback strategy for XAUUSD M5.

Layers:
    1. Kijun pullback
    2. Tenkan momentum, optional
    3. SP2L swing-point liquidity sweep, optional

All Ichimoku values are calculated on the current bar without the traditional
26-bar forward shift. Signal generation uses the last closed candle by default.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.indicators import IndicatorSpec, adx, atr, ema
from loguru import logger
from strategies.base import BaseStrategy, Signal


class KijunPullbackStrategy(BaseStrategy):
    """Kijun pullback plus optional Tenkan and SP2L momentum layers."""

    name = "kijun_pullback"
    oscillator = "none"
    bar_minutes = 5   # دقیقه‌های هر کندلِ استراتژی (برای برچسب cooldown)

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {
            # Core indicators.
            "ema_period": 200,
            "atr_period": 14,
            "min_atr": 0.20,

            # Unshifted Ichimoku periods.
            "tenkan_period": 9,
            "kijun_period": 26,
            "senkou_b_period": 52,

            # Trend & Momentum Filters
            "kijun_slope_lookback": 3,
            "kijun_slope_min_atr": 0.05,
            "use_h1_filter": True,
            "h1_ema_period": 50,

            # Regime filter (H1-based): trade only when the market is TRENDING.
            # Blocks all layers when daily-grade trend strength is absent, so
            # the strategy sits out range-bound phases instead of bleeding.
            "use_regime_filter": False,
            "regime_adx_period": 14,       # ADX computed on H1 bars
            "regime_adx_min": 22.0,
            "regime_ema_period": 120,      # H1 bars (~5 trading days)
            "regime_slope_lookback": 24,   # H1 bars (~1 trading day)
            "regime_slope_min_atr": 1.0,   # |EMA slope| over lookback, in H1-ATR units

            # Base Kijun pullback (Asymmetric R:R).
            "kijun_near_atr": 0.12,
            "kijun_sl_atr": 1.20,
            "kijun_tp_sl_multiplier": 2.50,

            # Optional Tenkan momentum layer.
            "enable_tenkan": True,
            # Side gates (IMPROVE 2026-09-15): default True = legacy behavior.
            # Set False to trade the layer long-only (evidence: SELL side
            # −$204.56 kijun / −$45.34 tenkan over 5y at spread 20).
            "enable_kijun_short": True,
            "enable_tenkan_short": True,
            "tenkan_near_atr": 0.20,
            "tenkan_min_distance_atr": 0.40,
            "tenkan_max_distance_atr": 1.80,
            "tenkan_sl_atr": 1.20,
            "tenkan_tp_sl_multiplier": 2.50,
            "tenkan_min_body_fraction": 0.30,
            "tenkan_atr_rising_lookback": 5,

            # Extension (anti-chase) filter: سیگنال تنکان وقتی قیمت بیش از
            # N×ATR از کف روز (برای خرید) یا سقف روز (برای فروش) فاصله دارد
            # بلاک می‌شود — «دنبال اتوبوسی که رفته ن دوید». پیش‌فرض خاموش؛
            # سوییچ لحظه‌ای از تلگرام: /extension
            "extension_filter_enabled": False,
            "extension_max_atr": 4.0,
            # FIX(NFP): گیت هوشمند فیلتر کشش — فقط وقتی ATR جاری ≥ این
            # نسبت× میانگین ۵۰ کندلی باشد فیلتر اعمال شود. صفر = همیشه
            # (رفتار قدیمی). مقدار پیشنهادی: 1.8
            "extension_atr_ratio_gate": 0.0,

            # Optional SP2L layer.
            "enable_sp2l": True,
            "sp2l_swing_lookback": 20,
            "sp2l_buffer": 0.30,
            "sp2l_min_sl_distance": 0.50,
            # FIX (#13): SL مطلق خیلی تنگ/باز می‌شد؛ حالا حداقل بر اساس ATR هم
            "sp2l_min_sl_atr_floor": 0.60,
            "sp2l_tp_sl_multiplier": 3.00,
            "sp2l_min_body_fraction": 0.30,

            # Prevent repeated entries on consecutive candles.
            "cooldown_bars": 6,

            # Price rounding is intentionally disabled by default. The
            # execution layer should round to the broker's symbol specification.
            "price_decimals": None,
        }

    def __init__(
        self,
        params: Optional[Dict[str, Any]] = None,
        atr_period: int = 14,
    ) -> None:
        merged = self.default_params()
        merged.update(params or {})

        if "atr_period" not in (params or {}):
            merged["atr_period"] = int(atr_period)

        super().__init__(
            params=merged,
            atr_period=int(merged["atr_period"]),
        )

    @property
    def indicator_spec(self) -> IndicatorSpec:
        """
        The framework requires an IndicatorSpec. The strategy computes its
        custom Ichimoku columns directly inside prepare().
        """
        return IndicatorSpec(
            ema_periods=(),
            rsi_period=None,
            macd_params=None,
            bb_params=None,
            atr_period=self.pi("atr_period", self.atr_period),
            adx_period=None,
        )

    def min_bars(self) -> int:
        return max(
            self.pi("ema_period", 200) + 5,
            self.pi("senkou_b_period", 52) + 5,
            self.pi("sp2l_swing_lookback", 20) + 5,
            self.pi("atr_period", self.atr_period)
            + self.pi("tenkan_atr_rising_lookback", 5)
            + 5,
            260,
        )

    def ema_plot_columns(self) -> List[str]:
        return []

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _validate_frame(frame: pd.DataFrame) -> None:
        if frame is None or frame.empty:
            raise ValueError("M5 OHLCV data is required")

        if not isinstance(frame.index, pd.DatetimeIndex):
            raise TypeError(
                "M5 OHLCV data must use a Pandas DatetimeIndex"
            )

        required = {"open", "high", "low", "close"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(
                f"M5 OHLCV frame is missing columns: {sorted(missing)}"
            )

    def _round_price(self, value: float) -> float:
        decimals = self.params.get("price_decimals")

        if decimals is None:
            return float(value)

        return round(float(value), int(decimals))

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            converted = float(value)
        except (TypeError, ValueError):
            return None

        return converted if np.isfinite(converted) else None

    @staticmethod
    def _bool_series(
        value: Any,
        index: pd.Index,
    ) -> pd.Series:
        if isinstance(value, pd.Series):
            return value.reindex(index).fillna(False).astype(bool)

        return pd.Series(bool(value), index=index, dtype=bool)

    # --------------------------------------------------------------- indicators

    def _calculate_indicators(
        self,
        frame: pd.DataFrame,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        out = frame.copy()

        high = out["high"].astype("float64")
        low = out["low"].astype("float64")
        close = out["close"].astype("float64")

        atr_period = self.pi("atr_period", self.atr_period)
        ema_period = self.pi("ema_period", 200)
        tenkan_period = self.pi("tenkan_period", 9)
        kijun_period = self.pi("kijun_period", 26)
        senkou_b_period = self.pi("senkou_b_period", 52)
        slope_lookback = self.pi("kijun_slope_lookback", 3)

        out["atr"] = atr(
            high,
            low,
            close,
            atr_period,
        )

        out["ema_200"] = ema(
            close,
            ema_period,
        )

        # Custom no-shift Ichimoku. No forward displacement is applied.
        tenkan_high = high.rolling(
            tenkan_period,
            min_periods=tenkan_period,
        ).max()

        tenkan_low = low.rolling(
            tenkan_period,
            min_periods=tenkan_period,
        ).min()

        kijun_high = high.rolling(
            kijun_period,
            min_periods=kijun_period,
        ).max()

        kijun_low = low.rolling(
            kijun_period,
            min_periods=kijun_period,
        ).min()

        senkou_b_high = high.rolling(
            senkou_b_period,
            min_periods=senkou_b_period,
        ).max()

        senkou_b_low = low.rolling(
            senkou_b_period,
            min_periods=senkou_b_period,
        ).min()

        out["tenkan"] = (tenkan_high + tenkan_low) / 2.0
        out["kijun"] = (kijun_high + kijun_low) / 2.0
        out["senkou_a"] = (out["tenkan"] + out["kijun"]) / 2.0
        out["senkou_b"] = (senkou_b_high + senkou_b_low) / 2.0

        out["cloud_top"] = out[["senkou_a", "senkou_b"]].max(axis=1)
        out["cloud_bottom"] = out[["senkou_a", "senkou_b"]].min(axis=1)

        # Momentum: Kijun-sen Slope
        out["kijun_slope"] = out["kijun"].diff(slope_lookback)

        # Multi-timeframe H1 Trend alignment
        out["h1_trend_bull"] = True
        out["h1_trend_bear"] = True

        if self.pb("use_h1_filter", True) and h1 is not None and not h1.empty:
            try:
                h1_frame = h1.copy()
                if not isinstance(h1_frame.index, pd.DatetimeIndex):
                    raise TypeError("H1 frame must use a DatetimeIndex")
                h1_frame = h1_frame.sort_index()
                h1_ema_p = self.pi("h1_ema_period", 50)
                h1_frame["h1_ema"] = ema(
                    h1_frame["close"].astype("float64"),
                    h1_ema_p,
                )

                # FIX (anti-look-ahead, v2): shift(1) BEFORE align is mandatory.
                # An H1 candle labelled L only closes at L+1h; without the
                # shift, an M5 bar inside hour L would see hour L's FINAL
                # close — its own future (intra-hour leak, backtest only).
                # shift(1) shows every M5 bar the last FULLY CLOSED H1 candle,
                # both in backtest and live; no forming-candle drop needed.
                aligned_h1 = (
                    h1_frame[["close", "h1_ema"]]
                    .shift(1)
                    .reindex(out.index.union(h1_frame.index))
                    .sort_index()
                    .ffill()
                    .reindex(out.index)
                )

                h1_valid = (
                    aligned_h1["close"].notna()
                    & aligned_h1["h1_ema"].notna()
                )

                bull_raw = (
                    aligned_h1["close"] >= aligned_h1["h1_ema"]
                )
                bear_raw = (
                    aligned_h1["close"] <= aligned_h1["h1_ema"]
                )

                out["h1_trend_bull"] = (
                    bull_raw.fillna(True) & h1_valid.fillna(False)
                ) | ~h1_valid.fillna(False)
                out["h1_trend_bear"] = (
                    bear_raw.fillna(True) & h1_valid.fillna(False)
                ) | ~h1_valid.fillna(False)

                out["h1_trend_bull"] = out["h1_trend_bull"].astype(bool)
                out["h1_trend_bear"] = out["h1_trend_bear"].astype(bool)

                # Regime filter (trend-strength gate on H1 data). Same
                # shift(1)+ffill alignment discipline as the H1 filter above:
                # every M5 bar only sees the last FULLY CLOSED H1 bar.
                if self.pb("use_regime_filter", False):
                    adx_p = self.pi("regime_adx_period", 14)
                    adx_df = adx(
                        h1_frame["high"].astype("float64"),
                        h1_frame["low"].astype("float64"),
                        h1_frame["close"].astype("float64"),
                        adx_p,
                    )
                    reg_ema_p = self.pi("regime_ema_period", 120)
                    slope_lb = self.pi("regime_slope_lookback", 24)
                    slope_min = self.pf("regime_slope_min_atr", 1.0)
                    reg_ema = ema(h1_frame["close"].astype("float64"), reg_ema_p)
                    slope = reg_ema.diff(slope_lb)
                    h1_atr = atr(
                        h1_frame["high"].astype("float64"),
                        h1_frame["low"].astype("float64"),
                        h1_frame["close"].astype("float64"),
                        14,
                    )
                    trending_raw = (adx_df["adx"] >= self.pf("regime_adx_min", 22.0)) & (
                        slope.abs() >= slope_min * h1_atr
                    )
                    regime = (
                        trending_raw.astype("float64")
                        .shift(1)
                        .reindex(out.index.union(h1_frame.index))
                        .ffill()
                        .reindex(out.index)
                    )
                    out["regime_trending"] = regime.fillna(0.0) > 0.0
            except Exception as exc:
                # FIX (#6): خطاها بی‌صدا قورت داده نمی‌شوند.
                logger.warning(
                    "H1 filter disabled due to error: %s",
                    exc,
                )
                out["h1_trend_bull"] = True
                out["h1_trend_bear"] = True

        return out

    def _calculate_swing_levels(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        lookback = self.pi("sp2l_swing_lookback", 20)

        # Shift first so the current candle cannot define the level it sweeps.
        out["swing_high"] = (
            out["high"]
            .rolling(lookback, min_periods=lookback)
            .max()
            .shift(1)
        )

        out["swing_low"] = (
            out["low"]
            .rolling(lookback, min_periods=lookback)
            .min()
            .shift(1)
        )

        return out

    # -------------------------------------------------------------- raw layers

    def _base_layer(
        self,
        frame: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series]:
        close = frame["close"]
        low = frame["low"]
        high = frame["high"]

        atr_value = frame["atr"]
        kijun = frame["kijun"]
        ema_200 = frame["ema_200"]
        cloud_top = frame["cloud_top"]
        cloud_bottom = frame["cloud_bottom"]
        kijun_slope = frame.get("kijun_slope", pd.Series(0.0, index=frame.index))
        h1_bull = frame.get("h1_trend_bull", pd.Series(True, index=frame.index))
        h1_bear = frame.get("h1_trend_bear", pd.Series(True, index=frame.index))

        near_buffer = (
            self.pf("kijun_near_atr", 0.12)
            * atr_value
        )
        min_slope = (
            self.pf("kijun_slope_min_atr", 0.05)
            * atr_value
        )

        valid = (
            atr_value.notna()
            & (atr_value >= self.pf("min_atr", 0.20))
            & ema_200.notna()
            & kijun.notna()
            & cloud_top.notna()
            & cloud_bottom.notna()
            & kijun_slope.notna()
        )

        long_signal = (
            valid
            & (close > ema_200)
            & (close > cloud_top)
            & (kijun_slope > min_slope)
            & h1_bull
            & (low <= kijun + near_buffer)
            & (close >= kijun)
        )

        short_signal = (
            valid
            & (close < ema_200)
            & (close < cloud_bottom)
            & (kijun_slope < -min_slope)
            & h1_bear
            & (high >= kijun - near_buffer)
            & (close <= kijun)
        )

        # IMPROVE (2026-09-15): side gate for the kijun pullback layer.
        # Evidence (5y M15 backtest, sp20, $482.53, instrumented baseline):
        # kijun SELL = 548 trades, net −$204.56, PF 0.793, negative in 4 of 5
        # years (2022..2025) while kijun BUY ≈ breakeven. Corroborated by the
        # 22-year daily study (h5): gold SHORT side structurally toxic.
        # Default True = legacy behavior (base version unchanged).
        if not self.pb("enable_kijun_short", True):
            short_signal = pd.Series(False, index=frame.index, dtype=bool)

        return long_signal, short_signal

    def _tenkan_layer(
        self,
        frame: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series]:
        index = frame.index

        if not self.pb("enable_tenkan", True):
            disabled = pd.Series(False, index=index, dtype=bool)
            return disabled, disabled.copy()

        open_price = frame["open"]
        close = frame["close"]
        high = frame["high"]
        low = frame["low"]

        atr_value = frame["atr"]
        tenkan = frame["tenkan"]
        kijun = frame["kijun"]
        ema_200 = frame["ema_200"]
        cloud_top = frame["cloud_top"]
        cloud_bottom = frame["cloud_bottom"]
        h1_bull = frame.get("h1_trend_bull", pd.Series(True, index=frame.index))
        h1_bear = frame.get("h1_trend_bear", pd.Series(True, index=frame.index))

        candle_range = (high - low).clip(
            lower=0.01,
        )

        atr_lookback = self.pi(
            "tenkan_atr_rising_lookback",
            5,
        )

        previous_atr_mean = (
            atr_value
            .shift(1)
            .rolling(
                atr_lookback,
                min_periods=atr_lookback,
            )
            .mean()
        )

        atr_rising = (
            atr_value.notna()
            & previous_atr_mean.notna()
            & (atr_value > previous_atr_mean)
        )

        distance_long = close - kijun
        distance_short = kijun - close

        min_distance = (
            self.pf("tenkan_min_distance_atr", 0.40)
            * atr_value
        )
        max_distance = (
            self.pf("tenkan_max_distance_atr", 1.80)
            * atr_value
        )

        body_fraction = self.pf(
            "tenkan_min_body_fraction",
            0.30,
        )

        valid = (
            atr_value.notna()
            & (atr_value >= self.pf("min_atr", 0.20))
            & tenkan.notna()
            & kijun.notna()
            & ema_200.notna()
            & cloud_top.notna()
            & cloud_bottom.notna()
            & atr_rising
        )

        long_signal = (
            valid
            & (close > cloud_top)
            & (close > ema_200)
            & (tenkan > kijun)
            & h1_bull
            & (distance_long >= min_distance)
            & (distance_long <= max_distance)
            & (
                low
                <= tenkan
                + self.pf("tenkan_near_atr", 0.20) * atr_value
            )
            & (close > tenkan)
            & ((close - open_price) > body_fraction * candle_range)
        )

        short_signal = (
            valid
            & (close < cloud_bottom)
            & (close < ema_200)
            & (tenkan < kijun)
            & h1_bear
            & (distance_short >= min_distance)
            & (distance_short <= max_distance)
            & (
                high
                >= tenkan
                - self.pf("tenkan_near_atr", 0.20) * atr_value
            )
            & (close < tenkan)
            & ((open_price - close) > body_fraction * candle_range)
        )

        # IMPROVE (2026-09-15): side gate for the tenkan momentum layer.
        # Evidence (same instrumented baseline): tenkan SELL = 267 trades,
        # net −$45.34, PF 0.894; ALL SELL trades (both layers) = −$249.90,
        # i.e. 80% of the total −$313 hole. Default True = legacy behavior.
        if not self.pb("enable_tenkan_short", True):
            short_signal = pd.Series(False, index=frame.index, dtype=bool)

        return long_signal, short_signal

    def _sp2l_layer(
        self,
        frame: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series]:
        index = frame.index

        if not self.pb("enable_sp2l", True):
            disabled = pd.Series(False, index=index, dtype=bool)
            return disabled, disabled.copy()

        open_price = frame["open"]
        close = frame["close"]
        high = frame["high"]
        low = frame["low"]

        swing_high = frame["swing_high"]
        swing_low = frame["swing_low"]
        h1_bull = frame.get("h1_trend_bull", pd.Series(True, index=frame.index))
        h1_bear = frame.get("h1_trend_bear", pd.Series(True, index=frame.index))

        candle_range = (high - low).clip(
            lower=0.01,
        )

        body_fraction = self.pf(
            "sp2l_min_body_fraction",
            0.30,
        )

        valid = (
            frame["atr"].notna()
            & (frame["atr"] >= self.pf("min_atr", 0.20))
            & swing_high.notna()
            & swing_low.notna()
        )

        long_signal = (
            valid
            & h1_bull
            & (low <= swing_low)
            & (close > swing_low)
            & (close > open_price)
            & ((close - open_price) > body_fraction * candle_range)
        )

        short_signal = (
            valid
            & h1_bear
            & (high >= swing_high)
            & (close < swing_high)
            & (close < open_price)
            & ((open_price - close) > body_fraction * candle_range)
        )

        return long_signal, short_signal

    # --------------------------------------------------------------- cooldown

    def _apply_cooldown(
        self,
        frame: pd.DataFrame,
        base_long: pd.Series,
        base_short: pd.Series,
        tenkan_long: pd.Series,
        tenkan_short: pd.Series,
        sp2l_long: pd.Series,
        sp2l_short: pd.Series,
    ) -> pd.DataFrame:
        out = frame.copy()
        n = len(out)

        accepted_long = np.zeros(n, dtype=bool)
        accepted_short = np.zeros(n, dtype=bool)
        accepted_layer = np.array([""] * n, dtype=object)

        cooldown = self.pi("cooldown_bars", 6)
        last_signal_pos = -9999

        # FIX (#1): ترتیب صحیح اولویت‌بندی — ابتدا لایه، سپس جهت.
        # قبلاً همه long ها قبل از short ها چک می‌شدند که باعث می‌شد
        # اولویت واقعی لایه‌ها بین جهت‌ها به‌هم بریزد.
        layers = [
            ("kijun_pullback", base_long, base_short),
            ("tenkan_momentum", tenkan_long, tenkan_short),
            ("sp2l", sp2l_long, sp2l_short),
        ]

        for position in range(n):
            if (position - last_signal_pos) < cooldown:
                continue

            for layer_name, long_arr, short_arr in layers:
                raw_long = bool(long_arr.iloc[position])
                raw_short = bool(short_arr.iloc[position])

                # FIX (#2): تداخل همزمان Long/Short در یک کندل → سیگنال نامعتبر.
                if raw_long and raw_short:
                    continue

                if raw_long:
                    accepted_long[position] = True
                    accepted_layer[position] = layer_name
                    last_signal_pos = position
                    break

                if raw_short:
                    accepted_short[position] = True
                    accepted_layer[position] = layer_name
                    last_signal_pos = position
                    break

        out["long_signal"] = accepted_long
        out["short_signal"] = accepted_short
        out["signal"] = np.where(
            accepted_long,
            1,
            np.where(accepted_short, -1, 0),
        ).astype("int8")
        out["signal_layer"] = accepted_layer

        return out

    # --------------------------------------------------------------- metadata

    def _attach_trade_metadata(
        self,
        frame: pd.DataFrame,
    ) -> pd.DataFrame:
        out = frame.copy()

        n = len(out)
        invalidation = np.full(n, np.nan)
        target = np.full(n, np.nan)
        sl_distance_arr = np.full(n, np.nan)
        tp_distance_arr = np.full(n, np.nan)
        reason_arr = np.array([""] * n, dtype=object)

        signal_col = out["signal"].to_numpy()
        close_arr = out["close"].to_numpy(dtype="float64")
        atr_arr = out["atr"].to_numpy(dtype="float64")
        swing_high_arr = out["swing_high"].to_numpy(dtype="float64")
        swing_low_arr = out["swing_low"].to_numpy(dtype="float64")

        # FIX (#7): حلقه روی آرایه‌های numpy به جای iloc — بسیار سریع‌تر.
        for position in range(n):
            direction = int(signal_col[position])
            if direction == 0:
                continue

            side = "BUY" if direction > 0 else "SELL"
            layer = str(out["signal_layer"].iloc[position])
            close = float(close_arr[position])
            atr_value = float(atr_arr[position])

            if layer == "kijun_pullback":
                sl_distance = (
                    self.pf("kijun_sl_atr", 1.20)
                    * atr_value
                )
                tp_distance = (
                    self.pf("kijun_tp_sl_multiplier", 2.50)
                    * sl_distance
                )

                if side == "BUY":
                    inv = close - sl_distance
                    tgt = close + tp_distance
                else:
                    inv = close + sl_distance
                    tgt = close - tp_distance

                reason = "Kijun pullback"

            elif layer == "tenkan_momentum":
                sl_distance = (
                    self.pf("tenkan_sl_atr", 1.20)
                    * atr_value
                )
                tp_distance = (
                    self.pf("tenkan_tp_sl_multiplier", 2.50)
                    * sl_distance
                )

                if side == "BUY":
                    inv = close - sl_distance
                    tgt = close + tp_distance
                else:
                    inv = close + sl_distance
                    tgt = close - tp_distance

                reason = "Tenkan momentum"

            elif layer == "sp2l":
                buffer_price = self.pf(
                    "sp2l_buffer",
                    0.30,
                )
                minimum_sl = self.pf(
                    "sp2l_min_sl_distance",
                    0.50,
                )
                # FIX (#13): کف SL بر اساس ATR تا در بازار پرنوسان،
                # استاپ غیرمنطقی تنگ نشود.
                sl_atr_floor = (
                    self.pf("sp2l_min_sl_atr_floor", 0.60)
                    * atr_value
                )

                if side == "BUY":
                    swing = float(swing_low_arr[position])
                    sl_distance = max(
                        close - swing + buffer_price,
                        minimum_sl,
                        sl_atr_floor,
                    )
                    inv = close - sl_distance
                    tp_distance = (
                        self.pf("sp2l_tp_sl_multiplier", 3.00)
                        * sl_distance
                    )
                    tgt = close + tp_distance
                else:
                    swing = float(swing_high_arr[position])
                    sl_distance = max(
                        swing - close + buffer_price,
                        minimum_sl,
                        sl_atr_floor,
                    )
                    inv = close + sl_distance
                    tp_distance = (
                        self.pf("sp2l_tp_sl_multiplier", 3.00)
                        * sl_distance
                    )
                    tgt = close - tp_distance

                reason = "SP2L swing-point liquidity sweep"

            else:
                continue

            invalidation[position] = self._round_price(inv)
            target[position] = self._round_price(tgt)
            sl_distance_arr[position] = float(sl_distance)
            tp_distance_arr[position] = float(tp_distance)
            reason_arr[position] = reason

        out["invalidation_price"] = invalidation
        out["target_price"] = target
        out["sl_distance"] = sl_distance_arr
        out["tp_distance"] = tp_distance_arr
        out["signal_reason"] = reason_arr

        return out

    # --------------------------------------------------------------- interface

    def prepare(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Prepare M5 data with optional H1 confluence.

        Note: ``m5`` must contain only CLOSED candles. If the caller passes
        a forming candle as the last row, its signal would be unstable.
        """
        self._validate_frame(m5)

        frame = self._calculate_indicators(m5, h1=h1)
        frame = self._calculate_swing_levels(frame)

        base_long, base_short = self._base_layer(frame)
        tenkan_long, tenkan_short = self._tenkan_layer(frame)

        # Extension (anti-chase) filter — فقط لایهٔ تنکان را می‌بندد؛ ورودی‌های
        # کیجون ساختاراً نزدیک کیجون هستند و «کشیده» نمی‌شوند. کف/سقف سشن =
        # اقصای تجمعی همان روز تقویمی (بدون look-ahead: لو/های کندلِ خودش
        # در بسته‌شدن معلوم است). خاموش = رفتار بیت‌به‌بیت یکسان با قبل.
        tenkan_ext_blocked = pd.Series(False, index=frame.index)
        if self.pb("extension_filter_enabled", False):
            max_ext = self.pf("extension_max_atr", 4.0) * frame["atr"]
            session_low = frame["low"].groupby(frame.index.normalize()).cummin()
            session_high = frame["high"].groupby(frame.index.normalize()).cummax()
            long_ok = (frame["close"] - session_low) <= max_ext
            short_ok = (session_high - frame["close"]) <= max_ext
            # FIX(NFP) — گیت هوشمند: فیلتر کشش فقط وقتی اعمال شود که رژیم
            # نوسان غیرعادی باشد (ATR جاری ≥ N× میانگین ۵۰ کندل اخیر).
            # در روزهای عادی (نسبت ~1) فیلتر عملاً خاموش است و رفتار با
            # نسخهٔ بدون فیلر یکسان می‌ماند (A/B ۱۳ ماهه: فیلتر همیشگی
            # −$220..−$348 ضرر داشت؛ این گیت فقط روزهای طوفانی را می‌گیرد).
            gate = self.pf("extension_atr_ratio_gate", 0.0)
            if gate > 0:
                atr_baseline = frame["atr"].rolling(50, min_periods=10).mean()
                regime_abnormal = (frame["atr"] / atr_baseline) >= gate
                long_ok = long_ok | ~regime_abnormal
                short_ok = short_ok | ~regime_abnormal
            tenkan_ext_blocked = (tenkan_long & ~long_ok) | (tenkan_short & ~short_ok)
            tenkan_long = tenkan_long & long_ok
            tenkan_short = tenkan_short & short_ok

        sp2l_long, sp2l_short = self._sp2l_layer(frame)

        # Regime gate: sit out when trend strength is absent.
        if self.pb("use_regime_filter", False) and "regime_trending" in frame.columns:
            trending = frame["regime_trending"]
            base_long = base_long & trending
            base_short = base_short & trending
            tenkan_long = tenkan_long & trending
            tenkan_short = tenkan_short & trending
            sp2l_long = sp2l_long & trending
            sp2l_short = sp2l_short & trending

        # Optional trading-hours window on the data clock (server time).
        # None disables the filter; wrap-around windows (start > end) allowed.
        start_h = self.params.get("trade_start_hour")
        end_h = self.params.get("trade_end_hour")
        if start_h is not None and end_h is not None:
            hours = pd.Series(frame.index.hour, index=frame.index)
            if int(start_h) <= int(end_h):
                hours_ok = (hours >= int(start_h)) & (hours < int(end_h))
            else:
                hours_ok = (hours >= int(start_h)) | (hours < int(end_h))
            base_long = base_long & hours_ok
            base_short = base_short & hours_ok
            tenkan_long = tenkan_long & hours_ok
            tenkan_short = tenkan_short & hours_ok
            sp2l_long = sp2l_long & hours_ok
            sp2l_short = sp2l_short & hours_ok

        frame = self._apply_cooldown(
            frame=frame,
            base_long=base_long,
            base_short=base_short,
            tenkan_long=tenkan_long,
            tenkan_short=tenkan_short,
            sp2l_long=sp2l_long,
            sp2l_short=sp2l_short,
        )

        frame["base_long_raw"] = base_long
        frame["base_short_raw"] = base_short
        frame["tenkan_long_raw"] = tenkan_long
        frame["tenkan_short_raw"] = tenkan_short
        frame["sp2l_long_raw"] = sp2l_long
        frame["sp2l_short_raw"] = sp2l_short
        # تشخیص: سیگنال تنکانی که فیلتر کشش بلاکش کرده (برای لاگ/شمارنده)
        frame["tenkan_ext_blocked"] = tenkan_ext_blocked

        # FIX (#7): دیگر نیازی به پاس دادن لایه‌های خام نیست؛
        # ستون‌های signal/signal_layer از قبل موجودند.
        return self._attach_trade_metadata(frame=frame)

    def _rules(
        self,
        df: pd.DataFrame,
            ) -> Tuple[pd.Series, pd.Series]:
        """
        Required by BaseStrategy.
        """
        return (
            df.get(
                "long_signal",
                pd.Series(False, index=df.index),
            ),
            df.get(
                "short_signal",
                pd.Series(False, index=df.index),
            ),
        )

    def generate_signal(
        self,
        frame: pd.DataFrame,
        index: int = -1,
    ) -> Optional[Signal]:
        """
        Convert one prepared candle into the shared Signal dataclass.

        FIX (#12): default index از -2 به -1 تغییر کرد تا با evaluate()
        سازگار باشد. قرارداد: ورودی prepare() فقط باید شامل کندل‌های
        closed باشد و index=-1 به آخرین کندلِ closed اشاره می‌کند.
        """
        if frame is None or frame.empty:
            return None

        position = index if index >= 0 else len(frame) + index

        if position < 0 or position >= len(frame):
            return None

        row = frame.iloc[position]
        direction = int(row.get("signal", 0))

        if direction == 0:
            return None

        atr_value = self._safe_float(row.get("atr"))
        invalidation = self._safe_float(
            row.get("invalidation_price")
        )
        target = self._safe_float(
            row.get("target_price")
        )
        sl_distance = self._safe_float(
            row.get("sl_distance")
        )
        tp_distance = self._safe_float(
            row.get("tp_distance")
        )

        if atr_value is None or atr_value <= 0:
            return None

        if (
            invalidation is None
            or target is None
            or sl_distance is None
            or tp_distance is None
        ):
            return None

        side = "BUY" if direction > 0 else "SELL"
        layer = str(row.get("signal_layer", ""))
        reason = str(
            row.get(
                "signal_reason",
                layer or "kijun pullback",
            )
        )

        return Signal(
            side=side,
            strategy=self.name,
            reason=reason,
            atr=atr_value,
            ref_time=frame.index[position],
            ref_close=float(row["close"]),
            oscillator=self.oscillator,
            meta={
                "layer": layer,
                "invalidation_price": invalidation,
                "target_price": target,
                "sl_distance": sl_distance,
                "tp_distance": tp_distance,

                # Raw structural context.
                "ema_200": self._safe_float(row.get("ema_200")),
                "tenkan": self._safe_float(row.get("tenkan")),
                "kijun": self._safe_float(row.get("kijun")),
                "kijun_slope": self._safe_float(row.get("kijun_slope")),
                "senkou_a": self._safe_float(row.get("senkou_a")),
                "senkou_b": self._safe_float(row.get("senkou_b")),
                "cloud_top": self._safe_float(row.get("cloud_top")),
                "cloud_bottom": self._safe_float(
                    row.get("cloud_bottom")
                ),

                # SP2L context.
                "swing_high": self._safe_float(
                    row.get("swing_high")
                ),
                "swing_low": self._safe_float(
                    row.get("swing_low")
                ),

                # Raw layer diagnostics.
                "base_long_raw": bool(
                    row.get("base_long_raw", False)
                ),
                "base_short_raw": bool(
                    row.get("base_short_raw", False)
                ),
                "tenkan_long_raw": bool(
                    row.get("tenkan_long_raw", False)
                ),
                "tenkan_short_raw": bool(
                    row.get("tenkan_short_raw", False)
                ),
                "sp2l_long_raw": bool(
                    row.get("sp2l_long_raw", False)
                ),
                "sp2l_short_raw": bool(
                    row.get("sp2l_short_raw", False)
                ),
            },
        )

    def evaluate(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> Optional[Signal]:
        """ارزیابی زنده روی آخرین کندل بسته‌شده."""
        prepared = self.prepare(m5, m15, h1)
        return self.generate_signal(prepared, index=-1)

    def describe(self) -> str:
        enabled_layers = ["Kijun pullback"]

        if self.pb("enable_tenkan", True):
            enabled_layers.append("Tenkan momentum")

        if self.pb("enable_sp2l", True):
            enabled_layers.append("SP2L sweep")

        return (
            "XAUUSD M5 "
            + " + ".join(enabled_layers)
            + " with unshifted Ichimoku, Kijun slope momentum, EMA200, "
            "H1 macro confluence, ATR sizing, structural metadata, and active cooldown"
        )
