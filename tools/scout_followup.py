"""پیگیری‌های اسکات (منطق خالص، قابل تست) — هیچ‌کدام سیگنالی را حذف یا معامله‌ای را عوض نمی‌کند.

- WhatIf: هشدارهایی که رد کردید، منقضی شدند یا باز نشدند، بی‌صدا دنبال می‌شوند (همان قاعدهٔ
  «نتیجهٔ فرضی»: ورود قیمت هشدار، حد ضرر هشدار، هدف 2R، از بسته شدن کندل هشدار روی M1،
  هر دو در یک کندل = حد ضرر) و وقتی معلوم شد، نتیجه با دلیلش گفته می‌شود.
- price alerts: «/alert 4150» → وقتی قیمت به ۴۱۵۰ رسید خبر بده.
- scoreboard: جدول امتیاز شبانه (شما + اسکات در برابر ORB و ایچیموکو).
- تاریخ شمسی برای پیام‌ها.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

DECISION_FA = {"rejected": "رد کردید", "expired": "بی‌جواب ماند و منقضی شد",
               "blocked_hedge": "باز نشد (پوزیشن خلاف جهت باز بود)",
               "blocked_limit": "باز نشد (سقف پوزیشن/ریسک پر بود)"}
WEEKDAY_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
MONTH_FA = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی",
            "بهمن", "اسفند"]


# --------------------------------------------------------------------------- jalali
def to_jalali(d: date) -> Tuple[int, int, int]:
    """تبدیل میلادی به شمسی (الگوریتم استاندارد)."""
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_label(d: date) -> str:
    jy, jm, jd = to_jalali(d)
    return f"{WEEKDAY_FA[d.weekday()]} {jd} {MONTH_FA[jm - 1]} {jy}"


def fmt_minutes(m: float) -> str:
    m = int(round(m))
    if m < 60:
        return f"{m} دقیقه"
    h, r = divmod(m, 60)
    return f"{h} ساعت" + (f" و {r} دقیقه" if r else "")


# --------------------------------------------------------------------------- what-if
def new_whatif(aid: str, setup: str, side: str, entry: float, sl: float, tp: float, bar: Any,
               decision: str, risk_usd: float, ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"aid": str(aid), "setup": str(setup), "side": side, "entry": float(entry), "sl": float(sl),
            "tp": float(tp), "bar": str(pd.Timestamp(bar)), "decision": decision, "risk_usd": float(risk_usd),
            "ctx": ctx, "mfe": 0.0, "mae": 0.0}


def context_flags(frame: Optional[pd.DataFrame], side: str) -> Dict[str, Any]:
    """وضعیت بازار در لحظهٔ هشدار، برای توضیح «چرا»."""
    out: Dict[str, Any] = {}
    if frame is None or frame.empty:
        return out
    last = frame.iloc[-1]
    buy = side == "BUY"

    def val(k):
        v = last.get(k)
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
    c, h1, top, bot, ema = val("close"), val("h1_ema"), val("cloud_top"), val("cloud_bot"), val("ema200")
    if c is not None and h1 is not None:
        out["h1_with"] = (c >= h1) if buy else (c <= h1)
    if c is not None and top is not None and bot is not None:
        out["cloud"] = "with" if ((c > top) if buy else (c < bot)) else ("inside" if bot <= c <= top else "against")
    if c is not None and ema is not None:
        out["ema_with"] = (c >= ema) if buy else (c <= ema)
    return out


def evaluate(item: Dict[str, Any], m1: Optional[pd.DataFrame], horizon_hours: float = 24.0,
             bar_minutes: int = 15) -> Optional[Dict[str, Any]]:
    """None = هنوز معلوم نیست. وگرنه {"exit": "TP"|"SL"|"OPEN", "R", "minutes", "mfe", "mae"}."""
    if m1 is None or m1.empty:
        return None
    start = pd.Timestamp(item["bar"]) + pd.Timedelta(minutes=bar_minutes)
    end = start + pd.Timedelta(hours=horizon_hours)
    seg = m1[(m1.index >= start) & (m1.index < end)]
    entry, sl, tp = item["entry"], item["sl"], item["tp"]
    risk = abs(entry - sl) or 1e-9
    buy = item["side"] == "BUY"
    mfe, mae = float(item.get("mfe", 0.0)), float(item.get("mae", 0.0))
    for ts, b in seg.iterrows():
        hi, lo = float(b["high"]), float(b["low"])
        hit_sl = lo <= sl if buy else hi >= sl
        hit_tp = hi >= tp if buy else lo <= tp
        fav = ((hi - entry) if buy else (entry - lo)) / risk
        adv = ((entry - lo) if buy else (hi - entry)) / risk
        if hit_sl:                                           # هر دو در یک کندل = محافظه‌کارانه حد ضرر
            return {"exit": "SL", "R": -1.0, "minutes": (ts - start).total_seconds() / 60.0,
                    "mfe": round(mfe, 2), "mae": 1.0}
        if hit_tp:
            return {"exit": "TP", "R": round(abs(tp - entry) / risk, 2),
                    "minutes": (ts - start).total_seconds() / 60.0, "mfe": 2.0, "mae": round(max(mae, adv), 2)}
        mfe, mae = max(mfe, fav), max(mae, adv)
    item["mfe"], item["mae"] = round(mfe, 2), round(mae, 2)
    if len(m1) and m1.index[-1] >= end:                      # افق تمام شد، هیچ‌کدام نخورد
        last = seg["close"].iloc[-1] if len(seg) else entry
        r = ((float(last) - entry) if buy else (entry - float(last))) / risk
        return {"exit": "OPEN", "R": round(r, 2), "minutes": horizon_hours * 60.0, "mfe": item["mfe"],
                "mae": item["mae"]}
    return None


def reasons(item: Dict[str, Any], res: Dict[str, Any], record: Optional[Tuple[int, float]] = None) -> List[str]:
    """دلیل‌های ساده و واقعی (از مسیر قیمت و وضعیت بازار لحظهٔ هشدار) — بدون حدس."""
    ctx = item.get("ctx") or {}
    side_fa = "خرید" if item["side"] == "BUY" else "فروش"
    out: List[str] = []
    if "h1_with" in ctx:
        out.append(f"{side_fa} {'هم‌جهت با' if ctx['h1_with'] else 'خلاف'} روند یک‌ساعته بود")
    cl = ctx.get("cloud")
    if cl:
        out.append({"with": "قیمت در سمت درست ابر ایچیموکو بود", "inside": "قیمت داخل ابر بود (بازار بی‌جهت)",
                    "against": "قیمت در سمت مخالف ابر ایچیموکو بود"}[cl])
    if ctx.get("news"):
        out.append("نزدیک یک خبر مهم دلار بود")
    t = res.get("minutes", 0.0)
    if res["exit"] == "TP":
        out.append(f"در مسیر حداکثر تا ‎−{res.get('mae', 0):.1f}R برگشت و بعد از {fmt_minutes(t)} به هدف رسید"
                   if res.get("mae", 0) >= 0.2 else f"تقریباً بدون برگشت، بعد از {fmt_minutes(t)} به هدف رسید")
    elif res["exit"] == "SL":
        out.append(f"اول تا ‎+{res.get('mfe', 0):.1f}R به نفع رفت و بعد برگشت و بعد از {fmt_minutes(t)} حد ضرر خورد"
                   if res.get("mfe", 0) >= 0.3 else f"تقریباً مستقیم، بعد از {fmt_minutes(t)} حد ضرر خورد")
    else:
        out.append(f"در ۲۴ ساعت نه به هدف رسید نه به حد ضرر؛ بیشترین سود ‎+{res.get('mfe', 0):.1f}R "
                   f"و بیشترین ضرر ‎−{res.get('mae', 0):.1f}R بود")
    if record:
        n, avg = record
        out.append(f"کارنامهٔ این ستاپ در {n} نمونهٔ آخر: میانگین {avg:+.2f}R")
    return out


def whatif_message(item: Dict[str, Any], res: Dict[str, Any], record: Optional[Tuple[int, float]] = None) -> str:
    side = "🟢 BUY" if item["side"] == "BUY" else "🔴 SELL"
    usd = item["risk_usd"] * res["R"]
    if res["exit"] == "TP":
        head = f"📈 <b>این یکی جواب می‌داد</b>: به هدف 2R رسید (‎{usd:+.2f}$)"
    elif res["exit"] == "SL":
        verdict = "✅ <b>تصمیم درستی بود</b>" if item["decision"] == "rejected" else "✅ <b>خوب شد باز نشد</b>"
        head = f"{verdict}: حد ضرر می‌خورد (‎{usd:+.2f}$)"
    else:
        head = f"⏳ <b>بعد از ۲۴ ساعت معلوم نشد</b>: الان ‎{res['R']:+.2f}R (‎{usd:+.2f}$)"
    why = "\n".join(f"• {r}" for r in reasons(item, res, record))
    return (f"🔮 <b>اگر گرفته بودید…</b> · هشدار #{item['aid']} · {side} <code>{item['setup']}</code>\n"
            f"{DECISION_FA.get(item['decision'], item['decision'])} · ورود {item['entry']:.2f} · "
            f"حد ضرر {item['sl']:.2f} · هدف {item['tp']:.2f}\n{head}\n<b>چرا:</b>\n{why}")


def outcome_kind(item: Dict[str, Any], res: Dict[str, Any]) -> str:
    """برای جدول امتیاز: good_pass = رد کردید و ضرر می‌داد، missed = رد کردید و سود می‌داد."""
    if res["exit"] == "SL":
        return "good_pass"
    if res["exit"] == "TP":
        return "missed"
    return "open"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------- price alerts
_NUM = re.compile(r"^\d{3,6}(?:[.,]\d{1,3})?$")
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫", "0123456789.")


def parse_alert(text: str) -> Optional[Tuple[float, str]]:
    """«/alert 4150 مقاومت» → (4150.0, "مقاومت")؛ اعداد فارسی هم پذیرفته می‌شود."""
    bits = str(text or "").strip().split(maxsplit=2)
    if len(bits) < 2:
        return None
    raw = bits[1].translate(FA_DIGITS).replace(",", ".")
    if not _NUM.match(raw):
        return None
    return float(raw), (bits[2].strip()[:80] if len(bits) > 2 else "")


def add_price_alert(alerts: List[Dict[str, Any]], level: float, price_now: float, note: str = "",
                    now: Optional[float] = None, max_alerts: int = 20) -> Optional[Dict[str, Any]]:
    if len(alerts) >= max_alerts:
        return None
    nid = max([int(a.get("id", 0)) for a in alerts] + [0]) + 1
    a = {"id": nid, "level": round(float(level), 2), "dir": "up" if level >= price_now else "down",
         "note": note, "created": float(now or 0.0), "from": round(float(price_now), 2)}
    alerts.append(a)
    return a


def due_price_alerts(alerts: List[Dict[str, Any]], bid: float) -> List[Dict[str, Any]]:
    return [a for a in alerts if (bid >= a["level"] if a["dir"] == "up" else bid <= a["level"])]


# --------------------------------------------------------------------------- scoreboard
def scoreboard_text(day: date, today: List[Dict[str, Any]], week: List[Dict[str, Any]],
                    alerts_today: int, taken_today: int, passes: Dict[str, int]) -> str:
    def rfmt(v):
        return "—" if v is None else f"{v:+.1f}R"
    best = max([r["sum_r"] for r in today if r.get("sum_r") is not None and r["n"]] or [None]) \
        if any(r["n"] for r in today) else None
    lines = [f"🏁 <b>جدول امتیاز امروز</b> · {jalali_label(day)}"]
    for r in today:
        crown = "🏆 " if best is not None and r["n"] and r.get("sum_r") == best else ""
        name = f"<b>{r['name']}</b>" if r.get("me") else r["name"]
        if r["n"]:
            lines.append(f"{crown}{name}: {rfmt(r.get('sum_r'))} · {r['n']} معامله، برد {r['win']}٪ · "
                         f"‎{r['pnl']:+.2f}$")
        else:
            lines.append(f"{name}: بدون معامله")
    lines.append("— ۷ روز اخیر —")
    lines.append(" · ".join(f"{r['name']} {rfmt(r.get('sum_r'))}" for r in week))
    lines.append(f"🔔 هشدارهای امروز: {alerts_today} · تأیید شما: {taken_today}")
    if passes.get("good_pass") or passes.get("missed"):
        lines.append(f"🔮 هشدارهای ردشده/منقضی امروز: {passes.get('good_pass', 0)} بار رد درست بود · "
                     f"{passes.get('missed', 0)} بار سود می‌داد")
    lines.append("<i>مقایسه با R است (سود تقسیم بر ریسک اولیه)، چون حجم ربات‌ها فرق دارد.</i>")
    return "\n".join(lines)
