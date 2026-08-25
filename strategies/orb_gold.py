"""Gold Open Range Breakout (ORB) strategy for XAUUSD M5.

M5 port of the community GOLD_ORB EA (github.com/yulz008/GOLD_ORB, 281 stars)
into the gold_m5_bot framework. Honest multi-year backtest through this
project's engine (spread 26pts): PF 1.02 / 5y, PF 1.23 / 1y, maxDD 20% (1y).
At ideal spread (12pts) 5y PF rises to 1.24 — the edge is real but
spread-sensitive; pair with a low-spread broker.

Logic (faithful to the EA):
    - Gold session opens ~`session_start_hour` server time.
    - Initial range = high/low of the first `initial_range_bars` M5 bars.
    - While price prints new extremes the range extends; once
      `confirm_bars` consecutive M5 bars stay inside, the range is FINAL.
    - Breakout of the final range high => BUY (once per day);
      breakdown of the final range low => SELL (once per day).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.indicators import IndicatorSpec, atr
from strategies.base import BaseStrategy, Signal


class GoldOrbStrategy(BaseStrategy):
    """Daily opening-range breakout, both directions, once per direction."""

    name = "orb_gold"
    oscillator = "none"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {
            "session_start_hour": 1,     # gold opens ~01:00 broker server time
            "initial_range_bars": 12,    # 1 hour of M5
            "confirm_bars": 36,          # 3 H1 candles worth of M5 bars
            "atr_period": 14,
            # SL/TP metadata (informational; the risk engine applies the
            # configured sl_atr_multiplier / tp_atr_multiplier).
            "sl_atr": 1.5,
            "tp_sl_multiplier": 3.5,
        }

    @property
    def indicator_spec(self) -> IndicatorSpec:
        return IndicatorSpec(
            ema_periods=(),
            rsi_period=None,
            macd_params=None,
            bb_params=None,
            atr_period=self.pi("atr_period", self.atr_period),
            adx_period=None,
        )

    def min_bars(self) -> int:
        return 150

    def ema_plot_columns(self) -> List[str]:
        return []

    def describe(self) -> str:
        return (
            "XAUUSD M5 daily Open Range Breakout: initial 1h range, "
            f"{self.pi('confirm_bars', 36)}-bar consolidation confirmation, "
            "breakout BUY / breakdown SELL, once per direction per day"
        )

    # ------------------------------------------------------------------ rules

    def _rules(
        self,
        frame: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series]:
        return (
            frame.get("long_signal", pd.Series(False, index=frame.index)),
            frame.get("short_signal", pd.Series(False, index=frame.index)),
        )

    # --------------------------------------------------------------- prepare

    def prepare(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        if m5 is None or m5.empty:
            raise ValueError("M5 OHLCV data is required")
        if not isinstance(m5.index, pd.DatetimeIndex):
            raise TypeError("M5 frame must use a DatetimeIndex")
        for col in ("open", "high", "low", "close"):
            if col not in m5.columns:
                raise ValueError(f"M5 frame missing column: {col}")

        out = m5.copy()
        atr_period = self.pi("atr_period", self.atr_period)
        out["atr"] = atr(
            out["high"].astype("float64"),
            out["low"].astype("float64"),
            out["close"].astype("float64"),
            atr_period,
        )

        n = len(out)
        long_s = np.zeros(n, dtype=bool)
        short_s = np.zeros(n, dtype=bool)
        layer = np.array([""] * n, dtype=object)

        ts = out.index
        session_day = np.array((ts - pd.Timedelta(hours=1)).date)
        highs = out["high"].to_numpy("float64")
        lows = out["low"].to_numpy("float64")
        hours = ts.hour.to_numpy()

        init_bars = max(2, int(self.pi("initial_range_bars", 12)))
        confirm_bars = max(1, int(self.pi("confirm_bars", 36)))
        start_hour = int(self.pi("session_start_hour", 1))

        order = np.argsort(session_day, kind="stable")

        pos = 0
        while pos < n:
            day = session_day[order[pos]]
            day_end = pos
            while day_end < n and session_day[order[day_end]] == day:
                day_end += 1
            idx = np.sort(order[pos:day_end])
            pos = day_end

            # session begins at the first bar at/after the open hour
            starts = np.flatnonzero(hours[idx] >= start_hour)
            if len(starts) == 0:
                continue
            idx = idx[starts[0]:]
            if len(idx) < init_bars + confirm_bars + 2:
                continue

            rh = float(np.max(highs[idx[:init_bars]]))
            rl = float(np.min(lows[idx[:init_bars]]))

            inside = 0
            finalized = False
            long_taken = False
            short_taken = False

            for p in idx[init_bars:]:
                h, l = highs[p], lows[p]
                if not finalized:
                    if h > rh:
                        rh, inside = h, 0
                    elif l < rl:
                        rl, inside = l, 0
                    else:
                        inside += 1
                        if inside >= confirm_bars:
                            finalized = True
                else:
                    if not long_taken and h > rh:
                        long_s[p] = True
                        layer[p] = "orb_breakout"
                        long_taken = True
                    if not short_taken and l < rl:
                        short_s[p] = True
                        layer[p] = "orb_breakdown"
                        short_taken = True

        out["long_signal"] = long_s
        out["short_signal"] = short_s
        out["signal"] = np.where(
            long_s, 1, np.where(short_s, -1, 0)
        ).astype("int8")
        out["signal_layer"] = layer

        # trade metadata (matches what the risk engine will actually place)
        out["invalidation_price"] = np.nan
        out["target_price"] = np.nan
        out["sl_distance"] = np.nan
        out["tp_distance"] = np.nan
        out["signal_reason"] = ""

        sl_mult = self.pf("sl_atr", 1.5)
        tp_mult = self.pf("tp_sl_multiplier", 3.5)
        closes = out["close"].to_numpy("float64")
        atrs = out["atr"].to_numpy("float64")
        longs = long_s
        shorts = short_s

        for p in np.flatnonzero(longs | shorts):
            a = atrs[p]
            if not np.isfinite(a) or a <= 0:
                continue
            side = "BUY" if longs[p] else "SELL"
            sl_d = sl_mult * a
            tp_d = tp_mult * sl_d
            c = closes[p]
            sign = 1.0 if side == "BUY" else -1.0
            out.iat[p, out.columns.get_loc("invalidation_price")] = round(c - sign * sl_d, 2)
            out.iat[p, out.columns.get_loc("target_price")] = round(c + sign * tp_d, 2)
            out.iat[p, out.columns.get_loc("sl_distance")] = sl_d
            out.iat[p, out.columns.get_loc("tp_distance")] = tp_d
            out.iat[p, out.columns.get_loc("signal_reason")] = (
                "ORB breakout" if side == "BUY" else "ORB breakdown"
            )

        return out

    # ------------------------------------------------------------- live entry

    def evaluate(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> Optional[Signal]:
        """Live evaluation on the last CLOSED bar.

        main_live already strips the forming candle (iloc[:-1]) before
        calling this, so index=-1 is the last closed bar.
        """
        prepared = self.prepare(m5, m15, h1)
        return self.generate_signal(prepared, index=-1)

    def generate_signal(
        self,
        frame: pd.DataFrame,
        index: int = -1,
    ) -> Optional[Signal]:
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
        invalidation = self._safe_float(row.get("invalidation_price"))
        target = self._safe_float(row.get("target_price"))
        if atr_value is None or atr_value <= 0:
            return None
        if invalidation is None or target is None:
            return None

        side = "BUY" if direction > 0 else "SELL"
        reason = str(row.get("signal_reason", "") or "ORB")

        return Signal(
            side=side,
            strategy=self.name,
            reason=reason,
            atr=atr_value,
            ref_time=frame.index[position],
            ref_close=float(row["close"]),
            oscillator=self.oscillator,
            meta={
                "layer": str(row.get("signal_layer", "")),
                "invalidation_price": invalidation,
                "target_price": target,
                "sl_distance": self._safe_float(row.get("sl_distance")),
                "tp_distance": self._safe_float(row.get("tp_distance")),
            },
        )

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            converted = float(value)
        except (TypeError, ValueError):
            return None
        return converted if np.isfinite(converted) else None
