"""
Conditioned multi-session liquidity sweep strategy for XAUUSD M15.

Primary frame:
    m5 argument from BaseStrategy is treated as M15 OHLCV data.

Higher-timeframe bias:
    h1 argument is treated as a supplied H4 or Daily OHLCV frame.
    The caller must pass the desired higher timeframe explicitly.

Design:
    - London trades sweeps of the completed Asian range.
    - New York trades sweeps of completed Asian or London ranges.
    - Longs require bullish HTF bias and sweep lows.
    - Shorts require bearish HTF bias and sweep highs.
    - Stop reference is the sweep wick plus a configurable buffer.
    - Target reference is the opposing session liquidity pool.
    - One long and one short maximum per UTC date/session.
    - All signals are generated from closed candles only.

Important integration note:
    Signal.meta contains "invalidation_price" and "target_price".
    The execution/risk layer must consume those values. If RiskManager
    continues to build ATR-only levels, the structural stop/target will
    not be used in live or backtest execution.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.indicators import IndicatorSpec, atr, ema
from strategies.base import BaseStrategy, Signal


class SessionSweepStrategy(BaseStrategy):
    """HTF-conditioned London/New York liquidity sweep strategy."""

    name = "session_sweep"
    oscillator = "none"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {
            # UTC session windows.
            "asian_start_hour": 0.0,
            "asian_end_hour": 6.0,
            "london_start_hour": 7.0,
            "london_end_hour": 12.0,
            "new_york_start_hour": 12.0,
            "new_york_end_hour": 19.0,

            # XAUUSD price point. 25 points = $0.25 with point=0.01.
            "price_point": 0.01,
            "sweep_buffer_points": 5.0,
            "min_rejection_points": 2.0,

            # Session range filters.
            "asian_min_range_points": 40.0,
            "asian_max_range_points": 900.0,
            "london_min_range_points": 40.0,
            "london_max_range_points": 1200.0,

            # Candle-quality filter.
            "use_displacement_filter": True,
            "min_body_atr": 0.15,

            # HTF directional filter.
            "require_htf_bias": True,
            "htf_fast_ema": 20,
            "htf_slow_ema": 50,
            "htf_slope_lookback": 1,
            "htf_min_ema_separation_atr": 0.0,

            # Optional structure filter.
            "require_htf_structure": False,
            "htf_structure_lookback": 3,

            # Target construction.
            "target_mode": "opposing_extreme",
            "target_buffer_points": 0.0,
            "min_target_atr": 0.50,
            "max_target_atr": 8.0,

            # Signal controls.
            "allow_london_sweeps": True,
            "allow_ny_asian_sweeps": True,
            "allow_ny_london_sweeps": True,
            "max_trades_per_session_direction": 1,

            # ATR.
            "atr_period": 14,
        }

    def __init__(
        self,
        params: Optional[Dict[str, Any]] = None,
        atr_period: int = 14,
    ) -> None:
        merged = self.default_params()
        merged.update(params or {})
        merged["atr_period"] = int(merged.get("atr_period", atr_period))

        super().__init__(
            params=merged,
            atr_period=int(merged["atr_period"]),
        )

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
        return max(
            100,
            self.pi("atr_period", self.atr_period) + 5,
        )

    def ema_plot_columns(self) -> List[str]:
        return []

    # ------------------------------------------------------------------ time

    @staticmethod
    def _utc_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
        if not isinstance(index, pd.DatetimeIndex):
            raise TypeError("OHLCV data must use a Pandas DatetimeIndex")

        if index.tz is None:
            return index.tz_localize("UTC")

        return index.tz_convert("UTC")

    @staticmethod
    def _hours(index: pd.DatetimeIndex) -> pd.Series:
        return pd.Series(
            index.hour
            + index.minute / 60.0
            + index.second / 3600.0,
            index=index,
            dtype="float64",
        )

    @staticmethod
    def _window(
        hours: pd.Series,
        start: float,
        end: float,
    ) -> pd.Series:
        if end > start:
            return (hours >= start) & (hours < end)

        return (hours >= start) | (hours < end)

    # --------------------------------------------------------------- sessions

    def _session_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out.index = self._utc_index(out.index)
        out = out.sort_index()

        hours = self._hours(out.index)

        out["_date"] = pd.Series(
            out.index.date,
            index=out.index,
            dtype="object",
        )
        out["_hour"] = hours

        out["_is_asian"] = self._window(
            hours,
            self.pf("asian_start_hour", 0.0),
            self.pf("asian_end_hour", 6.0),
        )

        out["_is_london"] = self._window(
            hours,
            self.pf("london_start_hour", 7.0),
            self.pf("london_end_hour", 12.0),
        )

        out["_is_new_york"] = self._window(
            hours,
            self.pf("new_york_start_hour", 12.0),
            self.pf("new_york_end_hour", 19.0),
        )

        return out

    @staticmethod
    def _completed_session_levels(
        frame: pd.DataFrame,
        mask_column: str,
    ) -> pd.DataFrame:
        """
        Computes completed session levels per UTC date.

        Since London only reads Asian levels and New York only reads Asian or
        London levels, the grouped values are available before they are used.
        The current candle is never included in a session level that it tests.
        """
        session_high = frame["high"].where(frame[mask_column])
        session_low = frame["low"].where(frame[mask_column])

        high = session_high.groupby(frame["_date"]).transform("max")
        low = session_low.groupby(frame["_date"]).transform("min")

        return pd.DataFrame(
            {
                "high": high,
                "low": low,
            },
            index=frame.index,
        )

    def _build_levels(self, frame: pd.DataFrame) -> pd.DataFrame:
        asian = self._completed_session_levels(
            frame,
            "_is_asian",
        )
        london = self._completed_session_levels(
            frame,
            "_is_london",
        )

        out = frame.copy()

        out["asian_high"] = asian["high"]
        out["asian_low"] = asian["low"]
        out["london_high"] = london["high"]
        out["london_low"] = london["low"]

        out["asian_range"] = (
            out["asian_high"] - out["asian_low"]
        )
        out["london_range"] = (
            out["london_high"] - out["london_low"]
        )

        return out

    # --------------------------------------------------------- HTF condition

    def _prepare_htf_bias(
        self,
        primary: pd.DataFrame,
        higher: Optional[pd.DataFrame],
    ) -> pd.DataFrame:
        """
        Merge the last fully closed HTF candle onto the primary frame.

        The supplied higher frame should be H4 or Daily. The method shifts the
        higher-timeframe values by one candle before merge_asof, preventing a
        forming H4/Daily candle from influencing an M15 signal.
        """
        out = primary.copy()

        out["htf_bias"] = 0
        out["htf_close"] = np.nan
        out["htf_fast_ema"] = np.nan
        out["htf_slow_ema"] = np.nan
        out["htf_atr"] = np.nan

        if higher is None or higher.empty:
            return out

        required = {"high", "low", "close"}
        missing = required.difference(higher.columns)
        if missing:
            raise ValueError(
                "Higher-timeframe frame is missing: "
                f"{sorted(missing)}"
            )

        htf = higher.copy()
        htf.index = self._utc_index(htf.index)
        htf = htf.sort_index()

        fast_period = self.pi("htf_fast_ema", 20)
        slow_period = self.pi("htf_slow_ema", 50)
        atr_period = self.pi("atr_period", self.atr_period)

        htf["__fast"] = ema(htf["close"], fast_period)
        htf["__slow"] = ema(htf["close"], slow_period)
        htf["__atr"] = atr(
            htf["high"],
            htf["low"],
            htf["close"],
            atr_period,
        )

        lookback = self.pi("htf_slope_lookback", 1)
        htf["__fast_slope"] = (
            htf["__fast"] - htf["__fast"].shift(lookback)
        )

        htf["__bull"] = (
            (htf["close"] > htf["__fast"])
            & (htf["__fast"] > htf["__slow"])
            & (htf["__fast_slope"] > 0)
        )

        htf["__bear"] = (
            (htf["close"] < htf["__fast"])
            & (htf["__fast"] < htf["__slow"])
            & (htf["__fast_slope"] < 0)
        )

        separation = (
            (htf["__fast"] - htf["__slow"]).abs()
            / htf["__atr"].replace(0.0, np.nan)
        )

        min_separation = self.pf(
            "htf_min_ema_separation_atr",
            0.0,
        )

        if min_separation > 0:
            strong_enough = separation >= min_separation
            htf["__bull"] &= strong_enough
            htf["__bear"] &= strong_enough

        if self.pb("require_htf_structure", False):
            structure_lookback = self.pi(
                "htf_structure_lookback",
                3,
            )

            prior_high = htf["high"].rolling(
                structure_lookback,
                min_periods=structure_lookback,
            ).max().shift(1)

            prior_low = htf["low"].rolling(
                structure_lookback,
                min_periods=structure_lookback,
            ).min().shift(1)

            htf["__bull"] &= htf["close"] > prior_high
            htf["__bear"] &= htf["close"] < prior_low

        htf["__bias"] = np.where(
            htf["__bull"],
            1,
            np.where(htf["__bear"], -1, 0),
        ).astype("int8")

        # Only the previous completed HTF candle is visible.
        right = htf[
            [
                "close",
                "__fast",
                "__slow",
                "__atr",
                "__bias",
            ]
        ].shift(1)

        right = right.rename(
            columns={
                "close": "htf_close",
                "__fast": "htf_fast_ema",
                "__slow": "htf_slow_ema",
                "__atr": "htf_atr",
                "__bias": "htf_bias",
            }
        )

        left = out.reset_index()
        left = left.rename(
            columns={left.columns[0]: "__time"}
        )

        right = right.reset_index()
        right = right.rename(
            columns={right.columns[0]: "__time"}
        )

        left = left.sort_values("__time")
        right = right.sort_values("__time")

        merged = pd.merge_asof(
            left,
            right,
            on="__time",
            direction="backward",
        )

        merged = merged.set_index("__time")
        merged.index.name = out.index.name or "time"

        for column in (
            "htf_bias",
            "htf_close",
            "htf_fast_ema",
            "htf_slow_ema",
            "htf_atr",
        ):
            if column in merged:
                out[column] = merged[column].reindex(out.index)

        out["htf_bias"] = (
            out["htf_bias"]
            .fillna(0)
            .astype("int8")
        )

        return out

    # --------------------------------------------------------------- filters

    def _valid_range(
        self,
        value: pd.Series,
        minimum_points: float,
        maximum_points: float,
    ) -> pd.Series:
        point = self.pf("price_point", 0.01)
        width_points = value / point

        return (
            value.notna()
            & (width_points >= minimum_points)
            & (width_points <= maximum_points)
        )

    def _body_ok(self, frame: pd.DataFrame) -> pd.Series:
        if not self.pb("use_displacement_filter", True):
            return pd.Series(True, index=frame.index)

        body = (frame["close"] - frame["open"]).abs()
        threshold = frame["atr"] * self.pf(
            "min_body_atr",
            0.15,
        )

        return body >= threshold

    def _bearish_sweep(
        self,
        frame: pd.DataFrame,
        level: pd.Series,
    ) -> pd.Series:
        rejection = (
            level - frame["close"]
        ) >= (
            self.pf("min_rejection_points", 2.0)
            * self.pf("price_point", 0.01)
        )

        return (
            level.notna()
            & (frame["high"] > level)
            & (frame["close"] < level)
            & rejection
        )

    def _bullish_sweep(
        self,
        frame: pd.DataFrame,
        level: pd.Series,
    ) -> pd.Series:
        rejection = (
            frame["close"] - level
        ) >= (
            self.pf("min_rejection_points", 2.0)
            * self.pf("price_point", 0.01)
        )

        return (
            level.notna()
            & (frame["low"] < level)
            & (frame["close"] > level)
            & rejection
        )

    # --------------------------------------------------------------- targets

    def _choose_target(
        self,
        frame: pd.DataFrame,
        long_mask: pd.Series,
        short_mask: pd.Series,
        source: pd.Series,
        session: pd.Series,
    ) -> pd.Series:
        """
        Long targets:
            London Asian high
            New York London high when sweeping London low
            otherwise Asian high

        Short targets:
            London Asian low
            New York London low when sweeping London high
            otherwise Asian low
        """
        target = pd.Series(np.nan, index=frame.index)

        long_london = long_mask & (session == "london")
        short_london = short_mask & (session == "london")

        long_ny_london = (
            long_mask
            & (session == "new_york")
            & (source == "london")
        )
        short_ny_london = (
            short_mask
            & (session == "new_york")
            & (source == "london")
        )

        long_ny_asian = (
            long_mask
            & (session == "new_york")
            & (source == "asian")
        )
        short_ny_asian = (
            short_mask
            & (session == "new_york")
            & (source == "asian")
        )

        target.loc[long_london] = frame.loc[
            long_london,
            "asian_high",
        ]
        target.loc[short_london] = frame.loc[
            short_london,
            "asian_low",
        ]

        target.loc[long_ny_london] = frame.loc[
            long_ny_london,
            "london_high",
        ]
        target.loc[short_ny_london] = frame.loc[
            short_ny_london,
            "london_low",
        ]

        target.loc[long_ny_asian] = frame.loc[
            long_ny_asian,
            "asian_high",
        ]
        target.loc[short_ny_asian] = frame.loc[
            short_ny_asian,
            "asian_low",
        ]

        return target

    def _target_filter(
        self,
        frame: pd.DataFrame,
        target: pd.Series,
        long_mask: pd.Series,
        short_mask: pd.Series,
    ) -> pd.Series:
        point = self.pf("price_point", 0.01)
        buffer_price = (
            self.pf("target_buffer_points", 0.0)
            * point
        )

        long_distance = target - frame["close"] - buffer_price
        short_distance = frame["close"] - target + buffer_price

        min_distance = frame["atr"] * self.pf(
            "min_target_atr",
            0.50,
        )
        max_distance = frame["atr"] * self.pf(
            "max_target_atr",
            8.0,
        )

        valid_long = (
            long_mask
            & target.notna()
            & (long_distance >= min_distance)
            & (long_distance <= max_distance)
        )

        valid_short = (
            short_mask
            & target.notna()
            & (short_distance >= min_distance)
            & (short_distance <= max_distance)
        )

        return valid_long | valid_short

    # ---------------------------------------------------------- throttle fix

    def _apply_one_trade_per_direction(
        self,
        frame: pd.DataFrame,
        long_signal: pd.Series,
        short_signal: pd.Series,
        session_name: pd.Series,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Apply the trade limit independently per UTC date and session.

        Correct grouping:
            2026-08-17_london
            2026-08-17_new_york
            2026-08-18_london
            2026-08-18_new_york

        Empty sessions are excluded.
        """
        max_trades = max(
            1,
            self.pi("max_trades_per_session_direction", 1),
        )

        session_name = session_name.astype(str).str.strip()
        valid_session = session_name.ne("")

        session_id = (
            frame["_date"].astype(str)
            + "_"
            + session_name
        ).where(valid_session)

        allowed_long = pd.Series(
            False,
            index=frame.index,
            dtype=bool,
        )
        allowed_short = pd.Series(
            False,
            index=frame.index,
            dtype=bool,
        )

        valid_ids = session_id.dropna()

        for _, positions in valid_ids.groupby(valid_ids):
            group_index = positions.index

            long_positions = group_index[
                long_signal.loc[group_index].to_numpy(dtype=bool)
            ]
            short_positions = group_index[
                short_signal.loc[group_index].to_numpy(dtype=bool)
            ]

            allowed_long.loc[
                long_positions[:max_trades]
            ] = True

            allowed_short.loc[
                short_positions[:max_trades]
            ] = True

        return allowed_long, allowed_short

    # ----------------------------------------------------------- signal build

    def _build_signals(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()

        out["atr"] = atr(
            out["high"],
            out["low"],
            out["close"],
            self.pi("atr_period", self.atr_period),
        )

        out["body_ok"] = self._body_ok(out)

        asian_valid = self._valid_range(
            out["asian_range"],
            self.pf("asian_min_range_points", 40.0),
            self.pf("asian_max_range_points", 900.0),
        )

        london_valid = self._valid_range(
            out["london_range"],
            self.pf("london_min_range_points", 40.0),
            self.pf("london_max_range_points", 1200.0),
        )

        htf_available = out["htf_bias"].notna()
        htf_bull = out["htf_bias"] == 1
        htf_bear = out["htf_bias"] == -1

        if self.pb("require_htf_bias", True):
            long_bias_ok = htf_available & htf_bull
            short_bias_ok = htf_available & htf_bear
        else:
            long_bias_ok = pd.Series(True, index=out.index)
            short_bias_ok = pd.Series(True, index=out.index)

        # --------------------------------------------------------- raw sweeps

        london_session = pd.Series(
            np.where(out["_is_london"], "london", ""),
            index=out.index,
            dtype="object",
        )

        ny_session = pd.Series(
            np.where(out["_is_new_york"], "new_york", ""),
            index=out.index,
            dtype="object",
        )

        london_bull_asian = self._bullish_sweep(
            out,
            out["asian_low"],
        )
        london_bear_asian = self._bearish_sweep(
            out,
            out["asian_high"],
        )

        ny_bull_asian = self._bullish_sweep(
            out,
            out["asian_low"],
        )
        ny_bear_asian = self._bearish_sweep(
            out,
            out["asian_high"],
        )

        ny_bull_london = self._bullish_sweep(
            out,
            out["london_low"],
        )
        ny_bear_london = self._bearish_sweep(
            out,
            out["london_high"],
        )

        london_long = (
            out["_is_london"]
            & self.pb("allow_london_sweeps", True)
            & asian_valid
            & out["body_ok"]
            & long_bias_ok
            & london_bull_asian
        )

        london_short = (
            out["_is_london"]
            & self.pb("allow_london_sweeps", True)
            & asian_valid
            & out["body_ok"]
            & short_bias_ok
            & london_bear_asian
        )

        ny_long = (
            out["_is_new_york"]
            & out["body_ok"]
            & long_bias_ok
            & (
                (
                    self.pb("allow_ny_asian_sweeps", True)
                    & asian_valid
                    & ny_bull_asian
                )
                | (
                    self.pb("allow_ny_london_sweeps", True)
                    & london_valid
                    & ny_bull_london
                )
            )
        )

        ny_short = (
            out["_is_new_york"]
            & out["body_ok"]
            & short_bias_ok
            & (
                (
                    self.pb("allow_ny_asian_sweeps", True)
                    & asian_valid
                    & ny_bear_asian
                )
                | (
                    self.pb("allow_ny_london_sweeps", True)
                    & london_valid
                    & ny_bear_london
                )
            )
        )

        raw_long = london_long | ny_long
        raw_short = london_short | ny_short

        session_name = pd.Series(
            np.where(
                out["_is_london"],
                "london",
                np.where(out["_is_new_york"], "new_york", ""),
            ),
            index=out.index,
            dtype="object",
        )

        # Source is needed for target selection.
        source = pd.Series("", index=out.index, dtype="object")

        source.loc[
            london_long
            | london_short
            | ny_bull_asian
            | ny_bear_asian
        ] = "asian"

        source.loc[
            ny_bull_london
            | ny_bear_london
        ] = "london"

        # A New York candle can sweep both source pools. Prefer the London
        # pool when it is explicitly swept, otherwise use Asian.
        source.loc[
            ny_bull_london | ny_bear_london
        ] = "london"

        target = self._choose_target(
            out,
            raw_long,
            raw_short,
            source,
            session_name,
        )

        target_ok = self._target_filter(
            out,
            target,
            raw_long,
            raw_short,
        )

        accepted_long = raw_long & target_ok
        accepted_short = raw_short & target_ok

        accepted_long, accepted_short = (
            self._apply_one_trade_per_direction(
                frame=out,
                long_signal=accepted_long,
                short_signal=accepted_short,
                session_name=session_name,
            )
        )

        out["long_signal"] = accepted_long
        out["short_signal"] = accepted_short
        out["signal"] = np.where(
            accepted_long,
            1,
            np.where(accepted_short, -1, 0),
        ).astype("int8")

        # -------------------------------------------------------- metadata

        out["sweep_session"] = session_name.where(
            accepted_long | accepted_short,
            "",
        )
        out["sweep_source"] = source.where(
            accepted_long | accepted_short,
            "",
        )
        out["sweep_level"] = np.nan
        out["sweep_extreme"] = np.nan
        out["invalidation_price"] = np.nan
        out["target_price"] = np.nan

        accepted = accepted_long | accepted_short

        asian_long = accepted_long & (
            out["sweep_source"] == "asian"
        )
        asian_short = accepted_short & (
            out["sweep_source"] == "asian"
        )
        london_long = accepted_long & (
            out["sweep_source"] == "london"
        )
        london_short = accepted_short & (
            out["sweep_source"] == "london"
        )

        out.loc[asian_long, "sweep_level"] = out.loc[
            asian_long,
            "asian_low",
        ]
        out.loc[asian_short, "sweep_level"] = out.loc[
            asian_short,
            "asian_high",
        ]
        out.loc[london_long, "sweep_level"] = out.loc[
            london_long,
            "london_low",
        ]
        out.loc[london_short, "sweep_level"] = out.loc[
            london_short,
            "london_high",
        ]

        out.loc[accepted_long, "sweep_extreme"] = out.loc[
            accepted_long,
            "low",
        ]
        out.loc[accepted_short, "sweep_extreme"] = out.loc[
            accepted_short,
            "high",
        ]

        buffer_price = (
            self.pf("sweep_buffer_points", 5.0)
            * self.pf("price_point", 0.01)
        )

        out.loc[accepted_long, "invalidation_price"] = (
            out.loc[accepted_long, "sweep_extreme"]
            - buffer_price
        )

        out.loc[accepted_short, "invalidation_price"] = (
            out.loc[accepted_short, "sweep_extreme"]
            + buffer_price
        )

        out.loc[accepted, "target_price"] = target.loc[accepted]

        ny_open_start = self.pf(
            "new_york_start_hour",
            12.0,
        )
        ny_open_end = (
            ny_open_start
            + self.pf("ny_open_grace_minutes", 60.0) / 60.0
        )

        out["ny_open_fakeout"] = (
            (out["_hour"] >= ny_open_start)
            & (out["_hour"] < ny_open_end)
            & accepted
        )

        return out

    # --------------------------------------------------------------- interface

    def prepare(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Prepare the primary M15 frame.

        The existing engine names the primary argument m5. For this strategy,
        pass M15 data there and pass H4 or Daily data through h1.
        """
        if m5 is None or m5.empty:
            raise ValueError("M15 OHLCV data is required")

        required = {"open", "high", "low", "close"}
        missing = required.difference(m5.columns)
        if missing:
            raise ValueError(
                "Primary OHLCV frame is missing: "
                f"{sorted(missing)}"
            )

        frame = self._session_frame(m5)
        frame = self._build_levels(frame)
        frame = self._prepare_htf_bias(frame, h1)
        return self._build_signals(frame)

    def _rules(
        self,
        frame: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series]:
        return (
            frame.get(
                "long_signal",
                pd.Series(False, index=frame.index),
            ),
            frame.get(
                "short_signal",
                pd.Series(False, index=frame.index),
            ),
        )

    def generate_signal(
        self,
        frame: pd.DataFrame,
        index: int = -2,
    ) -> Optional[Signal]:
        """
        Convert a prepared closed candle into the shared Signal dataclass.
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

        if atr_value is None or atr_value <= 0:
            return None

        if invalidation is None or target is None:
            return None

        side = "BUY" if direction > 0 else "SELL"
        session = str(row.get("sweep_session", "") or "")
        source = str(row.get("sweep_source", "") or "")
        htf_bias = int(row.get("htf_bias", 0))

        return Signal(
            side=side,
            strategy=self.name,
            reason=(
                f"{session} {side.lower()} conditioned liquidity sweep "
                f"of {source} level, HTF bias={htf_bias:+d}"
            ),
            atr=atr_value,
            ref_time=frame.index[position],
            ref_close=float(row["close"]),
            oscillator=self.oscillator,
            meta={
                "session": session,
                "sweep_source": source,
                "htf_bias": htf_bias,
                "htf_close": self._safe_float(
                    row.get("htf_close")
                ),
                "htf_fast_ema": self._safe_float(
                    row.get("htf_fast_ema")
                ),
                "htf_slow_ema": self._safe_float(
                    row.get("htf_slow_ema")
                ),
                "sweep_level": self._safe_float(
                    row.get("sweep_level")
                ),
                "sweep_extreme": self._safe_float(
                    row.get("sweep_extreme")
                ),
                "invalidation_price": invalidation,
                "target_price": target,
                "asian_high": self._safe_float(
                    row.get("asian_high")
                ),
                "asian_low": self._safe_float(
                    row.get("asian_low")
                ),
                "london_high": self._safe_float(
                    row.get("london_high")
                ),
                "london_low": self._safe_float(
                    row.get("london_low")
                ),
                "asian_range": self._safe_float(
                    row.get("asian_range")
                ),
                "london_range": self._safe_float(
                    row.get("london_range")
                ),
                "ny_open_fakeout": bool(
                    row.get("ny_open_fakeout", False)
                ),
            },
        )

    def evaluate(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> Optional[Signal]:
        prepared = self.prepare(m5, m15, h1)
        return self.generate_signal(prepared, index=-2)

    def describe(self) -> str:
        return (
            "HTF-conditioned XAUUSD M15 liquidity sweeps with "
            "wick invalidation and opposing session-pool targets"
        )

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            converted = float(value)
        except (TypeError, ValueError):
            return None

        return converted if np.isfinite(converted) else None