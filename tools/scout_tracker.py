"""Pure helpers for Scout's position tracking (no MT5, no Telegram) — unit-testable.

Soft stop (owner's rule, 2026-09-30): the signal's stop-loss is only a *warning*.
The position is closed only by the owner (button or /close).  A far "emergency"
stop stays on the broker so a VPS/internet outage can never leave the loss uncapped.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# fields persisted per open ticket (JSON-safe scalars only)
STATE_KEYS = ("setup", "side", "entry", "sl", "emerg", "risk", "r1", "warned", "opened",
              "alert", "status_mid", "below", "hold", "adopted")


def emergency_sl(side: str, entry: float, soft_sl: float, mult: float) -> float:
    """Broker-side stop ``mult`` × the signal's stop distance away from entry."""
    dist = abs(float(entry) - float(soft_sl)) * max(float(mult), 1.0)
    return round(float(entry) - dist if side == "BUY" else float(entry) + dist, 2)


def signed_move(side: str, entry: float, price: float) -> float:
    """Favourable move in price units (+ = profit direction)."""
    return (price - entry) if side == "BUY" else (entry - price)


def beyond_soft_sl(side: str, price: float, soft_sl: float) -> bool:
    """True when the (close-side) price has reached the signal's stop-loss."""
    return price <= soft_sl if side == "BUY" else price >= soft_sl


def recovered(side: str, price: float, soft_sl: float, risk: float, frac: float = 0.25) -> bool:
    """Price back on the safe side by ``frac``×risk — re-arms the soft-stop alarm (anti-flap)."""
    margin = max(float(risk), 0.0) * frac
    return price >= soft_sl + margin if side == "BUY" else price <= soft_sl - margin


def fmt_duration(seconds: float) -> str:
    s = max(int(seconds), 0)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return (f"{d} روز " if d else "") + f"{h}:{m:02d}"


def status_text(tk: int, info: Dict[str, Any], price: float, profit: float, swap: float,
                open_seconds: Optional[float], server_now: Optional[float]) -> str:
    side, entry, soft, risk = info["side"], float(info["entry"]), float(info["sl"]), float(info["risk"])
    move = signed_move(side, entry, price)
    r_mult = move / risk if risk > 0 else 0.0
    to_sl = signed_move(side, soft, price)            # >0: still above (BUY) / below (SELL) the stop
    if beyond_soft_sl(side, price, soft):
        state = "⏸ <b>زیر حد ضرر سیگنال — نگه داشته‌اید</b>" if info.get("hold") else \
                "⚠️ <b>از حد ضرر سیگنال رد شده — تصمیم با شماست</b>"
    elif info.get("warned"):
        state = "⚠️ ساختار شکسته (بسته شدن خلاف تنکان)"
    else:
        state = "✅ سالم"
    emerg = info.get("emerg")
    lines = [
        f"📊 <b>#{tk} · {'🟢 BUY' if side == 'BUY' else '🔴 SELL'}</b> · <code>{info.get('setup', '')}</code>",
        f"ورود {entry:.2f} → الان <b>{price:.2f}</b>",
        f"سود/زیان: <b>{profit:+.2f}$</b> ({r_mult:+.2f}R)" + (f" · سواپ {swap:+.2f}$" if swap else ""),
        f"حد ضرر سیگنال {soft:.2f} (فاصله {to_sl:+.2f})"
        + (f" · اضطراری روی بروکر {float(emerg):.2f}" if emerg else ""),
    ]
    tail = []
    if open_seconds is not None:
        tail.append(f"باز از {fmt_duration(open_seconds)}")
    if server_now:
        tail.append("به‌روزرسانی " + datetime.fromtimestamp(float(server_now), timezone.utc).strftime("%H:%M")
                    + " سرور")
    if tail:
        lines.append(" · ".join(tail))
    lines.append("وضعیت: " + state)
    return "\n".join(lines)


def save_state(path: Path, open_positions: Dict[int, Dict[str, Any]]) -> None:
    """Atomic JSON write of the tracked positions."""
    data = {str(tk): {k: info.get(k) for k in STATE_KEYS} for tk, info in open_positions.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_state(path: Path) -> Dict[int, Dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    for k, v in (raw or {}).items():
        try:
            out[int(k)] = dict(v)
        except (TypeError, ValueError):
            continue
    return out
