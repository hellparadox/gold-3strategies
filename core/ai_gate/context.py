"""Build the model's input from data that existed when the signal bar closed.

Look-ahead policy (enforced, see tests):
* trigger frame: only rows with ``index <= signal.ref_time``; the last row must
  BE the signal bar, otherwise :class:`ContextError`;
* H1 frame: the live feed includes the forming candle; only candles whose
  close (``open + 1h``) is at or before the signal bar's close are kept;
* recent trades: only trades closed before the signal bar's close.

Privacy policy: nothing about the account (login, server, balance, equity,
tokens, chat ids) is ever placed in the context.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd

CONTEXT_SCHEMA = "ai_gate_ctx/1"
_INDICATOR_COLUMNS = (
    "atr", "tenkan", "kijun", "senkou_a", "senkou_b", "cloud_top", "cloud_bottom",
    "ema_200", "kijun_slope", "h1_trend_bull", "h1_trend_bear", "regime_trending",
    "swing_high", "swing_low",
)


class ContextError(ValueError):
    """Inputs do not allow a look-ahead-free context."""


@dataclass(frozen=True)
class TradePlan:
    side: str
    entry: float
    sl: float
    tp: float
    atr: float
    risk_percent: float
    be_trigger_atr: float
    be_offset_points: float
    trail_trigger_atr: float
    trail_dist_atr: float
    spread_points: float
    point: float = 0.01


def server_offset_hours(tick_time: float, now_epoch: float) -> Optional[float]:
    """Broker server clock offset vs UTC, rounded to 30 minutes; ``None`` if implausible.

    MT5 tick timestamps are the server's wall clock expressed as epoch seconds,
    so the difference to the real epoch is the server's UTC offset (handles the
    broker's DST switch without configuration).  The last tick is only fresh
    while the market trades: over a weekend it can be ~2 days old and the
    difference is meaningless, so anything outside UTC-12..UTC+14 is rejected
    and the caller falls back to :func:`eet_offset_hours`.
    """
    offset = round((float(tick_time) - float(now_epoch)) / 1800.0) * 0.5
    return offset if -12.0 <= offset <= 14.0 else None


def _last_sunday(year: int, month: int) -> datetime:
    day = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def eet_offset_hours(now_utc: datetime) -> float:
    """UTC offset of a broker on EET/EEST (UTC+2 winter, UTC+3 summer, EU DST rules).

    EU summer time runs from 01:00 UTC on the last Sunday of March to 01:00 UTC on
    the last Sunday of October.  Used only as a fallback when the tick is stale.
    """
    now = now_utc.replace(tzinfo=None)
    start = _last_sunday(now.year, 3) + timedelta(hours=1)
    end = _last_sunday(now.year, 10) + timedelta(hours=1)
    return 3.0 if start <= now < end else 2.0


def session_name(hour_utc: int) -> str:
    if hour_utc < 7:
        return "asia"
    if hour_utc < 12:
        return "london"
    if hour_utc < 16:
        return "overlap"
    if hour_utc < 21:
        return "newyork"
    return "late"


def _num(value: Any, digits: int = 2) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def _flag(value: Any) -> Optional[bool]:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    return bool(value)


def _iso(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%MZ")


def _bars(frame: pd.DataFrame, offset: timedelta) -> List[List[Any]]:
    vol_col = "tick_volume" if "tick_volume" in frame.columns else None
    out = []
    for ts, row in frame.iterrows():
        out.append([
            _iso(pd.Timestamp(ts) - offset),
            _num(row.get("open")), _num(row.get("high")), _num(row.get("low")), _num(row.get("close")),
            int(row[vol_col]) if vol_col and not pd.isna(row[vol_col]) else None,
        ])
    return out


def _perf(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    profits = [float(r.get("profit") or 0.0) for r in rows]
    return {
        "n": len(rows),
        "wins": sum(1 for p in profits if p >= 0.5),
        "losses": sum(1 for p in profits if p <= -0.5),
        "be": sum(1 for p in profits if -0.5 < p < 0.5),
        "net_usd": round(sum(profits), 2),
    }


def build_context(
    *,
    strategy: str,
    layer: str,
    ref_time: Any,
    trigger: pd.DataFrame,
    trigger_minutes: int,
    h1: Optional[pd.DataFrame],
    plan: TradePlan,
    offset_hours: float,
    decision_time_utc: datetime,
    trigger_bars: int = 48,
    h1_bars: int = 48,
    next_news: Optional[Mapping[str, Any]] = None,
    recent_trades: Optional[Iterable[Mapping[str, Any]]] = None,
    recent_limit: int = 20,
    base_rates: Optional[Mapping[str, Any]] = None,
    symbol: str = "XAUUSD",
) -> Dict[str, Any]:
    """Return the ``ai_gate_ctx/1`` dict (see module docstring for guarantees)."""
    if trigger is None or trigger.empty:
        raise ContextError("empty trigger frame")
    ref = pd.Timestamp(ref_time)
    if ref.tzinfo is not None:
        raise ContextError("frames and ref_time must be naive broker-server time")
    offset = timedelta(hours=float(offset_hours))
    bar_len = timedelta(minutes=int(trigger_minutes))
    side = str(plan.side).upper()
    direction = 1.0 if side == "BUY" else -1.0

    past = trigger.loc[trigger.index <= ref]
    if past.empty or pd.Timestamp(past.index[-1]) != ref:
        raise ContextError(f"signal bar {ref} is not the last closed trigger bar")
    row = past.iloc[-1]
    atr = float(plan.atr) if plan.atr else float(row.get("atr") or 0.0)
    if atr <= 0:
        raise ContextError("ATR must be positive")
    close = float(row["close"])
    signal_close_server = ref + bar_len
    signal_close_utc = signal_close_server - offset

    def rel(value: Any) -> Optional[float]:
        v = _num(value, 6)
        return None if v is None else round((close - v) / atr, 3)

    ind: Dict[str, Any] = {"close": _num(close)}
    for col in _INDICATOR_COLUMNS:
        value = row.get(col) if col in past.columns else None
        if col in ("h1_trend_bull", "h1_trend_bear", "regime_trending"):
            ind[col] = _flag(value) if col in past.columns else None
        elif col == "kijun_slope":
            ind[col] = _num(value, 4)
        else:
            ind[col] = _num(value)
    ind["atr"] = _num(atr, 3)
    ind["close_minus_kijun_atr"] = rel(ind.get("kijun"))
    ind["close_minus_tenkan_atr"] = rel(ind.get("tenkan"))
    ind["close_minus_ema200_atr"] = rel(ind.get("ema_200"))
    top, bottom = ind.get("cloud_top"), ind.get("cloud_bottom")
    if top is not None and bottom is not None:
        if close > top:
            ind["close_vs_cloud_atr"] = round((close - top) / atr, 3)
        elif close < bottom:
            ind["close_vs_cloud_atr"] = round((close - bottom) / atr, 3)
        else:
            ind["close_vs_cloud_atr"] = 0.0
        ind["cloud_thickness_atr"] = round((top - bottom) / atr, 3)
    else:
        ind["close_vs_cloud_atr"] = None
        ind["cloud_thickness_atr"] = None
    target_level = ind.get("swing_high") if side == "BUY" else ind.get("swing_low")
    ind["room_to_swing_atr"] = (None if target_level is None
                                else round((target_level - plan.entry) * direction / atr, 3))

    window = past.tail(int(trigger_bars))
    ranges = (past["high"] - past["low"]).tail(20)
    median_range = float(ranges.median()) if len(ranges) else 0.0
    spread_hist = past["spread"].tail(20) if "spread" in past.columns else pd.Series(dtype=float)
    spread_median = float(spread_hist.median()) if len(spread_hist) else 0.0
    sl_dist = abs(plan.entry - plan.sl)
    tp_dist = abs(plan.tp - plan.entry)

    day_rows = past.loc[pd.DatetimeIndex(past.index).normalize() == ref.normalize()]
    day_open = float(day_rows["open"].iloc[0]) if len(day_rows) else None
    day = {
        "server_day_open": _num(day_open),
        "high_so_far": _num(day_rows["high"].max()) if len(day_rows) else None,
        "low_so_far": _num(day_rows["low"].min()) if len(day_rows) else None,
        "range_atr": (round(float(day_rows["high"].max() - day_rows["low"].min()) / atr, 3)
                      if len(day_rows) else None),
        "move_from_open_atr": None if day_open is None else round((close - day_open) / atr, 3),
    }

    h1_rows: List[List[Any]] = []
    if h1 is not None and not h1.empty and int(h1_bars) > 0:
        closed = h1.loc[pd.DatetimeIndex(h1.index) + timedelta(hours=1) <= signal_close_server]
        h1_rows = _bars(closed.tail(int(h1_bars)), offset)

    news = None
    if next_news:
        minutes = _num(next_news.get("time_until_minutes"), 0)
        if minutes is not None and 0 <= minutes <= 24 * 60:
            news = {"title": str(next_news.get("title", ""))[:80], "minutes_until": int(minutes)}

    perf = None
    if recent_trades is not None:
        cutoff = signal_close_utc.to_pydatetime().replace(tzinfo=None)
        done = []
        for t in recent_trades:
            closed_at = t.get("closed_at")
            if not closed_at or t.get("profit") is None:
                continue
            try:
                when = datetime.strptime(str(closed_at)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if when < cutoff:
                done.append((when, t))
        done.sort(key=lambda x: x[0])
        done = [t for _, t in done][-int(recent_limit):] if recent_limit else []
        same = [t for t in done if str(t.get("side", "")).upper() == side
                and str(t.get("reason", "")) == str(layer)]
        perf = {"same_side_layer": _perf(same), "all": _perf(done)}

    return {
        "schema": CONTEXT_SCHEMA,
        "symbol": symbol,
        "strategy": strategy,
        "layer": str(layer)[:60],
        "side": side,
        "trigger_tf": f"M{int(trigger_minutes)}",
        "signal_bar_close_utc": _iso(signal_close_utc),
        "decision_time_utc": decision_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session_name(int(signal_close_utc.hour)),
        "plan": {
            "entry": _num(plan.entry), "sl": _num(plan.sl), "tp": _num(plan.tp),
            "sl_dist": _num(sl_dist), "tp_dist": _num(tp_dist),
            "sl_atr": round(sl_dist / atr, 3), "tp_atr": round(tp_dist / atr, 3),
            "risk_percent": _num(plan.risk_percent),
            "be_trigger_atr": _num(plan.be_trigger_atr, 3),
            "be_offset_points": _num(plan.be_offset_points, 1),
            "trail_trigger_atr": _num(plan.trail_trigger_atr, 3),
            "trail_dist_atr": _num(plan.trail_dist_atr, 3),
            "spread_points": _num(plan.spread_points, 1),
            "spread_vs_median": (round(plan.spread_points / spread_median, 3)
                                 if spread_median > 0 else None),
            "sl_vs_median_bar_range": (round(sl_dist / median_range, 3)
                                       if median_range > 0 else None),
        },
        "indicators_at_signal": ind,
        "trigger_bars": _bars(window, offset),
        "h1_bars": h1_rows,
        "day": day,
        "news": {"next_high_impact_usd": news},
        "recent_performance": perf,
        "base_rates": dict(base_rates or {}),
    }
