"""Vectorized technical indicator library.

Every function is pure and side-effect free: given a Series/DataFrame it
returns new objects and never mutates the input.  All recursive indicators use
Wilder smoothing (``alpha = 1/period``) which is what MetaTrader itself uses,
so backtest numbers line up with the terminal.

>>> ZERO-LOOKAHEAD CONTRACT <<<
Index ``-1`` of a live MT5 rates frame is the *forming* candle.  Strategy logic
must therefore only ever read index ``-2``.  Use :func:`closed_bar` /
:func:`closed_slice` instead of raw ``iloc`` so the invariant is auditable in
one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Final, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

__all__ = [
    "CLOSED_BAR_OFFSET",
    "IndicatorSpec",
    "adx",
    "atr",
    "bollinger_bands",
    "closed_bar",
    "closed_slice",
    "crossed_above",
    "crossed_below",
    "ema",
    "enrich",
    "macd",
    "rma",
    "rsi",
    "sma",
    "true_range",
]

#: ``df.iloc[-2]`` -> last fully closed bar.
CLOSED_BAR_OFFSET: Final[int] = 2

OHLC_COLUMNS: Final[Tuple[str, ...]] = ("open", "high", "low", "close")


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------
def _as_series(data: pd.Series | Sequence[float], name: str = "value") -> pd.Series:
    if isinstance(data, pd.Series):
        return data.astype("float64")
    return pd.Series(np.asarray(data, dtype="float64"), name=name)


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return _as_series(series).rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (``alpha = 2/(period+1)``, MT4/MT5 style).

    The first ``period`` values are seeded with an SMA so the curve matches
    MetaTrader's rendering rather than pandas' infinite-warmup default.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    s = _as_series(series)
    if len(s) < period:
        return pd.Series(np.nan, index=s.index, dtype="float64")
    out = s.ewm(span=period, adjust=False, min_periods=period).mean()
    seed = s.iloc[:period].mean()
    values = out.to_numpy(copy=True)
    values[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        values[i] = values[i - 1] + alpha * (s.iat[i] - values[i - 1])
    return pd.Series(values, index=s.index, name=f"ema_{period}")


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothed moving average (``alpha = 1/period``)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    s = _as_series(series)
    return s.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def crossed_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on the bar where ``fast`` crosses from <= to > ``slow``."""
    fast_s, slow_s = _as_series(fast), _as_series(slow)
    now = fast_s > slow_s
    prev = fast_s.shift(1) <= slow_s.shift(1)
    return (now & prev).fillna(False)


def crossed_below(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on the bar where ``fast`` crosses from >= to < ``slow``."""
    fast_s, slow_s = _as_series(fast), _as_series(slow)
    now = fast_s < slow_s
    prev = fast_s.shift(1) >= slow_s.shift(1)
    return (now & prev).fillna(False)


# ---------------------------------------------------------------------------
# oscillators / bands / volatility
# ---------------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index, 0..100."""
    s = _as_series(close)
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 -> pure uptrend -> RSI 100 ; avg_gain == 0 -> RSI 0
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss != 0.0)), 0.0)
    out[avg_gain.isna() | avg_loss.isna()] = np.nan
    return out.rename(f"rsi_{period}")


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    if fast >= slow:
        raise ValueError("fast period must be < slow period")
    s = _as_series(close)
    macd_line = ema(s, fast) - ema(s, slow)
    signal_line = ema(macd_line.dropna(), signal)
    signal_line = signal_line.reindex(s.index)
    hist = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_hist": hist},
        index=s.index,
    )


def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> pd.DataFrame:
    """Bollinger Bands (population standard deviation, like MT5)."""
    s = _as_series(close)
    mid = sma(s, period)
    dev = s.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + std_mult * dev
    lower = mid - std_mult * dev
    width = (upper - lower) / mid.replace(0.0, np.nan)
    return pd.DataFrame(
        {"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width},
        index=s.index,
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Classic True Range: max(H-L, |H-C_prev|, |L-C_prev|)."""
    h, l, c = _as_series(high), _as_series(low), _as_series(close)
    prev_close = c.shift(1)
    ranges = pd.concat(
        [(h - l).abs(), (h - prev_close).abs(), (l - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1).rename("true_range")


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
    method: str = "rma",
) -> pd.Series:
    """Average True Range. ``method`` in {'rma' (Wilder, default), 'sma', 'ema'}."""
    tr = true_range(high, low, close)
    method = method.lower()
    if method == "sma":
        out = sma(tr, period)
    elif method == "ema":
        out = ema(tr, period)
    elif method == "rma":
        out = rma(tr, period)
    else:
        raise ValueError(f"unknown ATR method: {method!r}")
    return out.rename(f"atr_{period}")


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.DataFrame:
    """Wilder's ADX with +DI / -DI."""
    h, l, c = _as_series(high), _as_series(low), _as_series(close)
    up_move = h.diff()
    down_move = -l.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move.to_numpy(), 0.0),
        index=h.index,
        dtype="float64",
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move.to_numpy(), 0.0),
        index=h.index,
        dtype="float64",
    )

    atr_w = rma(true_range(h, l, c), period).replace(0.0, np.nan)
    plus_di = 100.0 * rma(plus_dm, period) / atr_w
    minus_di = 100.0 * rma(minus_dm, period) / atr_w
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx_line = rma(dx.fillna(0.0), period)
    adx_line[dx.isna() & plus_di.isna()] = np.nan
    return pd.DataFrame(
        {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di},
        index=h.index,
    )


# ---------------------------------------------------------------------------
# closed-bar accessors (the anti-lookahead gate)
# ---------------------------------------------------------------------------
def closed_bar(df: pd.DataFrame, offset: int = CLOSED_BAR_OFFSET) -> pd.Series:
    """Return the last fully **closed** bar (``df.iloc[-offset]``).

    Raises ``IndexError`` when the frame is too short, so a half-warmed dataset
    can never silently produce a signal from the forming candle.
    """
    if offset < 1:
        raise ValueError("offset must be >= 1")
    if len(df) < offset:
        raise IndexError(f"need >= {offset} bars, got {len(df)}")
    return df.iloc[-offset]


def closed_slice(df: pd.DataFrame, offset: int = CLOSED_BAR_OFFSET) -> pd.DataFrame:
    """Return every bar up to and including the last closed bar."""
    if offset < 1:
        raise ValueError("offset must be >= 1")
    if offset == 1:
        return df
    return df.iloc[: -(offset - 1)]


# ---------------------------------------------------------------------------
# batch enrichment
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IndicatorSpec:
    """Declarative description of which indicator columns to materialise."""

    ema_periods: Tuple[int, ...] = (9, 21)
    rsi_period: Optional[int] = 14
    macd_params: Optional[Tuple[int, int, int]] = (12, 26, 9)
    bb_params: Optional[Tuple[int, float]] = (20, 2.0)
    atr_period: Optional[int] = 14
    atr_method: str = "rma"
    adx_period: Optional[int] = 14
    extra: Dict[str, int] = field(default_factory=dict)

    def ema_column(self, period: int) -> str:
        return f"ema_{period}"


def enrich(df: pd.DataFrame, spec: IndicatorSpec) -> pd.DataFrame:
    """Return a copy of ``df`` with every column described by ``spec`` added.

    Existing columns are recomputed (cheap, vectorized) so a live frame that is
    refreshed every bar never carries stale values.
    """
    missing = [c for c in OHLC_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"missing OHLC columns: {missing}")

    out = df.copy()
    close, high, low = out["close"], out["high"], out["low"]

    for period in sorted({int(p) for p in spec.ema_periods if p and p > 0}):
        out[spec.ema_column(period)] = ema(close, period)

    if spec.rsi_period:
        out["rsi"] = rsi(close, int(spec.rsi_period))

    if spec.macd_params:
        f, s, sig = spec.macd_params
        out[["macd", "macd_signal", "macd_hist"]] = macd(close, int(f), int(s), int(sig))

    if spec.bb_params:
        period, mult = spec.bb_params
        out[["bb_mid", "bb_upper", "bb_lower", "bb_width"]] = bollinger_bands(
            close, int(period), float(mult)
        )

    if spec.atr_period:
        out["atr"] = atr(high, low, close, int(spec.atr_period), spec.atr_method)

    if spec.adx_period:
        out[["adx", "plus_di", "minus_di"]] = adx(high, low, close, int(spec.adx_period))

    for name, period in spec.extra.items():
        out[name] = ema(close, int(period))

    return out


def warmup_bars(spec: IndicatorSpec) -> int:
    """Minimum bars required before any column in ``spec`` is trustworthy."""
    candidates: List[int] = [50]
    candidates.extend(int(p) for p in spec.ema_periods if p)
    if spec.rsi_period:
        candidates.append(int(spec.rsi_period) * 5)
    if spec.macd_params:
        candidates.append(int(spec.macd_params[1]) + int(spec.macd_params[2]))
    if spec.bb_params:
        candidates.append(int(spec.bb_params[0]))
    if spec.atr_period:
        candidates.append(int(spec.atr_period) * 5)
    if spec.adx_period:
        candidates.append(int(spec.adx_period) * 6)
    candidates.extend(int(v) for v in spec.extra.values())
    return max(candidates) + 10