"""Abstract strategy contract + multi-timeframe alignment helpers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from core.indicators import (
    IndicatorSpec,
    closed_bar,
    crossed_above,
    crossed_below,
    enrich,
    warmup_bars,
)

__all__ = ["BaseStrategy", "Signal", "align_higher"]

Side = Literal["BUY", "SELL"]


@dataclass
class Signal:
    """A validated entry intent produced from a CLOSED bar."""

    side: Side
    strategy: str
    reason: str
    atr: float
    ref_time: pd.Timestamp
    ref_close: float
    oscillator: str = "macd"
    meta: Dict[str, Any] = field(default_factory=dict)
    # filled downstream by the RiskManager / execution layer
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    lot: float = 0.0
    ticket: int = 0

    @property
    def is_long(self) -> bool:
        return self.side == "BUY"

    @property
    def emoji(self) -> str:
        return "\U0001F7E9 BUY" if self.is_long else "\U0001F7E5 SELL"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side, "strategy": self.strategy, "reason": self.reason,
            "atr": self.atr, "ref_time": str(self.ref_time), "ref_close": self.ref_close,
            "entry": self.entry, "sl": self.sl, "tp": self.tp, "lot": self.lot,
            "ticket": self.ticket, "meta": self.meta,
        }


def align_higher(
    base: pd.DataFrame,
    higher: pd.DataFrame,
    columns: Sequence[str],
    prefix: str,
) -> pd.DataFrame:
    """Broadcast closed higher-timeframe columns onto the base (M5) index.

    ``higher[columns].shift(1)`` guarantees that a base bar living *inside*
    higher-timeframe candle ``H_k`` only ever sees ``H_{k-1}`` values, i.e. the
    last candle that actually finished.  Zero lookahead by construction.
    """
    if higher is None or higher.empty:
        out = base.copy()
        for col in columns:
            out[f"{prefix}_{col}"] = np.nan
        return out

    missing = [c for c in columns if c not in higher.columns]
    if missing:
        raise KeyError(f"higher timeframe frame is missing {missing}")

    right = higher[list(columns)].shift(1).copy()
    right.columns = [f"{prefix}_{c}" for c in columns]
    right = right.reset_index()
    right = right.rename(columns={right.columns[0]: "__time"}).sort_values("__time")

    left = base.reset_index()
    left = left.rename(columns={left.columns[0]: "__time"}).sort_values("__time")

    merged = pd.merge_asof(left, right, on="__time", direction="backward")
    merged = merged.set_index("__time")
    merged.index.name = base.index.name or "time"
    return merged


class BaseStrategy(ABC):
    """Contract every strategy implements.

    Subclasses provide:
      * ``indicator_spec``      -> M5 columns to materialise
      * ``m15_spec`` / ``h1_spec`` -> higher timeframe columns (optional)
      * ``_rules(df)``          -> ``(long_series, short_series)`` booleans
    """

    name: str = "base"
    oscillator: str = "macd"      # which subpanel the chart generator draws

    def __init__(self, params: Optional[Dict[str, Any]] = None, atr_period: int = 14) -> None:
        self.params: Dict[str, Any] = dict(params or {})
        self.atr_period = int(atr_period)

    # ------------------------------------------------------------ parameters
    def p(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def pi(self, key: str, default: int) -> int:
        return int(self.params.get(key, default))

    def pf(self, key: str, default: float) -> float:
        return float(self.params.get(key, default))

    def pb(self, key: str, default: bool) -> bool:
        return bool(self.params.get(key, default))

    # -------------------------------------------------------------- contract
    @property
    @abstractmethod
    def indicator_spec(self) -> IndicatorSpec:
        """Indicators required on the M5 trigger frame."""

    @property
    def m15_spec(self) -> Optional[IndicatorSpec]:
        return None

    @property
    def h1_spec(self) -> Optional[IndicatorSpec]:
        return None

    @property
    def m15_columns(self) -> Tuple[str, ...]:
        return ()

    @property
    def h1_columns(self) -> Tuple[str, ...]:
        return ()

    @abstractmethod
    def _rules(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        """Vectorized entry rules -> (long, short) boolean Series."""

    @abstractmethod
    def describe(self) -> str:
        """Human readable one-liner for Telegram cards."""

    # ---------------------------------------------------------------- plumbing
    def min_bars(self) -> int:
        return warmup_bars(self.indicator_spec)

    def ema_plot_columns(self) -> List[str]:
        return [f"ema_{p}" for p in self.indicator_spec.ema_periods]

    def prepare(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Enrich M5 with indicators and lookahead-safe higher TF context."""
        if m5 is None or m5.empty:
            raise ValueError("M5 frame is empty")
        df = enrich(m5, self.indicator_spec)

        if self.m15_columns:
            spec = self.m15_spec
            src = enrich(m15, spec) if (spec is not None and m15 is not None and not m15.empty) else m15
            df = align_higher(df, src if src is not None else pd.DataFrame(), self.m15_columns, "m15")
        if self.h1_columns:
            spec = self.h1_spec
            src = enrich(h1, spec) if (spec is not None and h1 is not None and not h1.empty) else h1
            df = align_higher(df, src if src is not None else pd.DataFrame(), self.h1_columns, "h1")
        return df

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Attach ``long_signal`` / ``short_signal`` / ``signal`` columns."""
        long_s, short_s = self._rules(df)
        long_s = long_s.fillna(False).astype(bool)
        short_s = short_s.fillna(False).astype(bool)
        both = long_s & short_s
        if both.any():
            long_s = long_s & ~both
            short_s = short_s & ~both
        out = df.copy()
        out["long_signal"] = long_s
        out["short_signal"] = short_s
        out["signal"] = np.where(long_s, 1, np.where(short_s, -1, 0)).astype("int8")
        return out

    def prepare_and_sign(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        return self.signals(self.prepare(m5, m15, h1))

    # ------------------------------------------------------------ live entry
    def evaluate(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> Optional[Signal]:
        """Live evaluation.

        FIX(#6): قرارداد با overrideهای استراتژی‌ها یکسان شد — ``main_live``
        کندل در حال شکل‌گیری را قبل از فراخوانی حذف می‌کند، پس index ``-1``
        آخرین کندلِ بسته‌شده است. (خواندن ``iloc[-2]`` قبلی سیگنال را یک کندل
        دیر ارزیابی می‌کرد.)
        """
        if m5 is None or len(m5) < self.min_bars():
            logger.debug("{}: not enough bars ({} < {})", self.name, 0 if m5 is None else len(m5), self.min_bars())
            return None

        df = self.prepare_and_sign(m5, m15, h1)
        try:
            row = closed_bar(df, 1)   # forming candle already stripped by the caller
        except IndexError as exc:
            logger.warning("{}: {}", self.name, exc)
            return None

        atr_value = float(row.get("atr", np.nan))
        if not np.isfinite(atr_value) or atr_value <= 0:
            logger.debug("{}: ATR not ready on closed bar", self.name)
            return None

        side: Optional[Side] = None
        if bool(row.get("long_signal", False)):
            side = "BUY"
        elif bool(row.get("short_signal", False)):
            side = "SELL"
        if side is None:
            return None

        return Signal(
            side=side,
            strategy=self.name,
            reason=self.explain(row, side),
            atr=atr_value,
            ref_time=df.index[-1],
            ref_close=float(row["close"]),
            oscillator=self.oscillator,
            meta=self.snapshot(row),
        )

    # -------------------------------------------------------------- reporting
    def explain(self, row: pd.Series, side: Side) -> str:
        return f"{self.name} {side} on closed bar"

    def snapshot(self, row: pd.Series) -> Dict[str, Any]:
        keys = (
            "close", "atr", "rsi", "macd", "macd_signal", "macd_hist", "adx",
            "bb_upper", "bb_lower", "bb_mid", "m15_ema_21", "h1_ema_200",
        )
        out: Dict[str, Any] = {}
        for key in keys:
            if key in row.index and pd.notna(row[key]):
                out[key] = round(float(row[key]), 4)
        for col in self.ema_plot_columns():
            if col in row.index and pd.notna(row[col]):
                out[col] = round(float(row[col]), 4)
        return out
