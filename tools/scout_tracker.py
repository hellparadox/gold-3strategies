"""Pure helpers for Scout's position tracking (no MT5, no Telegram) — unit-testable.

Stop policy (owner's rules, 2026-10-02):
- The signal stop-loss is capped (scout.sl_cap_usd) and, by default, it is a real broker stop:
  a position is never allowed past it unless the owner said «hold».
- «Hold» (per position, or the global away mode «hold») moves the broker stop out to the
  emergency level (scout.emergency_sl_mult × distance, capped at scout.emergency_cap_usd).
- Away mode «ai»: broker stop at the emergency level; when price reaches the signal stop the
  model decides HOLD or CLOSE (anything but a confident HOLD closes).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# fields persisted per open ticket (JSON-safe scalars only)
STATE_KEYS = ("setup", "side", "entry", "sl", "emerg", "risk", "r1", "warned", "opened",
              "alert", "status_mid", "below", "hold", "adopted", "approach", "ai_next", "ai_note",
              # قیمت خروج پوزیشن نگه‌داشته (TP/SL روی بروکر)، پیام سؤالش، و برای کارت پایان معامله
              "xtp", "xsl", "xprompt", "tp_owned", "by", "why")
POLICIES = ("close", "hold", "ai")
POLICY_FA = {"close": "در حد ضرر بسته شود", "hold": "نگه داشته شود", "ai": "هوش مصنوعی تصمیم بگیرد"}


def emergency_sl(side: str, entry: float, soft_sl: float, mult: float,
                 cap_dist: Optional[float] = None) -> float:
    """Broker-side stop ``mult`` × the signal's stop distance, at most ``cap_dist`` from entry
    (never tighter than the signal stop)."""
    base = abs(float(entry) - float(soft_sl))
    dist = base * max(float(mult), 1.0)
    if cap_dist is not None and cap_dist > 0:
        dist = max(min(dist, float(cap_dist)), base)
    return round(float(entry) - dist if side == "BUY" else float(entry) + dist, 2)


def capped_sl(side: str, entry: float, soft_sl: float, cap_dist: Optional[float]) -> float:
    """Signal stop moved closer to entry when its distance exceeds ``cap_dist``."""
    dist = abs(float(entry) - float(soft_sl))
    if cap_dist is not None and cap_dist > 0 and dist > cap_dist:
        dist = float(cap_dist)
    return round(float(entry) - dist if side == "BUY" else float(entry) + dist, 2)


def effective_policy(info: Dict[str, Any], mode: str) -> str:
    """Per-position «hold» always wins; otherwise the global away mode."""
    return "hold" if info.get("hold") else (mode if mode in POLICIES else "close")


def desired_broker_sl(info: Dict[str, Any], mode: str) -> float:
    if info.get("hold") and info.get("xsl"):          # قیمت خروج پایینِ خود مالک
        return float(info["xsl"])
    pol = effective_policy(info, mode)
    return float(info["sl"]) if pol == "close" else float(info.get("emerg") or info["sl"])


FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫٠١٢٣٤٥٦٧٨٩", "0123456789.0123456789")
_NUMS = re.compile(r"\d+(?:\.\d+)?")


def parse_numbers(text: str) -> List[float]:
    """«۴۱۲۰ 4,095.5» → [4120.0, 4095.5]؛ اعداد فارسی/عربی هم پذیرفته می‌شود (ویرگول = جداکنندهٔ هزارگان)."""
    raw = re.sub(r"(?<=\d)[,،٬](?=\d)", "", str(text or "").translate(FA_DIGITS))
    return [float(m) for m in _NUMS.findall(raw)]


def reached(side: str, price: float, level: float, upper: bool) -> bool:
    """upper=True: سطح «بالا»ی فهرست خروج (برای BUY بالاتر از قیمت، برای SELL پایین‌تر)."""
    if upper:
        return price >= level if side == "BUY" else price <= level
    return price <= level if side == "BUY" else price >= level


def classify_exits(side: str, price: float, nums: List[float]) -> Tuple[Optional[float], Optional[float]]:
    """یک یا دو عدد → (xtp, xsl) نسبت به قیمت فعلی بستن. BUY: بالاتر = xtp، پایین‌تر = xsl؛
    SELL برعکس. خطا (ValueError) با پیام فارسی برای کاربر."""
    if not nums or len(nums) > 2:
        raise ValueError("یک یا دو عدد بنویسید، مثل: 4120 4095")
    tp = sl = None
    for v in nums:
        v = round(float(v), 2)
        if price > 0 and abs(v - price) > 0.2 * price:
            raise ValueError(f"{v:.2f} خیلی از قیمت فعلی ({price:.2f}) دور است؛ عدد را دوباره نگاه کنید")
        if abs(v - price) < 0.01:
            raise ValueError(f"{v:.2f} همان قیمت فعلی است؛ عددی بالاتر یا پایین‌تر بنویسید")
        upper = (v > price) if side == "BUY" else (v < price)
        if upper:
            if tp is not None:
                raise ValueError("هر دو عدد یک طرف قیمت فعلی‌اند؛ یکی بالاتر و یکی پایین‌تر بنویسید")
            tp = v
        else:
            if sl is not None:
                raise ValueError("هر دو عدد یک طرف قیمت فعلی‌اند؛ یکی بالاتر و یکی پایین‌تر بنویسید")
            sl = v
    return tp, sl


def adverse_fraction(side: str, entry: float, price: float, risk: float) -> float:
    """How much of the planned risk is already lost (0 = at entry, 1 = at the stop)."""
    if risk <= 0:
        return 0.0
    return -signed_move(side, entry, price) / float(risk)


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
                open_seconds: Optional[float], server_now: Optional[float],
                mode: Optional[str] = None) -> str:
    side, entry, soft, risk = info["side"], float(info["entry"]), float(info["sl"]), float(info["risk"])
    move = signed_move(side, entry, price)
    r_mult = move / risk if risk > 0 else 0.0
    to_sl = signed_move(side, soft, price)            # >0: still above (BUY) / below (SELL) the stop
    if beyond_soft_sl(side, price, soft):
        state = "⏸ <b>زیر حد ضرر سیگنال — نگه داشته‌اید</b>" if info.get("hold") else \
                "⚠️ <b>از حد ضرر سیگنال رد شده</b>"
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
    if mode is not None:
        pol = effective_policy(info, mode)
        why = "نگه دار (دستی)" if info.get("hold") else POLICY_FA.get(pol, pol)
        lines.append(f"در حد ضرر: <b>{why}</b>")
    if info.get("hold") and (info.get("xtp") or info.get("xsl")):
        parts = []
        if info.get("xtp"):
            parts.append(f"⬆️ {float(info['xtp']):.2f}")
        if info.get("xsl"):
            parts.append(f"⬇️ {float(info['xsl']):.2f}")
        lines.append("🎯 قیمت خروج شما: " + " · ".join(parts))
    if info.get("ai_note"):
        lines.append(f"🤖 {info['ai_note']}")
    return "\n".join(lines)


def save_mode(path: Path, mode: str) -> None:
    if mode not in POLICIES:
        raise ValueError(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    os.replace(tmp, path)


def load_mode(path: Path, default: str = "close") -> str:
    try:
        mode = json.loads(path.read_text(encoding="utf-8")).get("mode")
    except (OSError, ValueError, AttributeError):
        return default
    return mode if mode in POLICIES else default


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
