"""تنظیمات اسکات از داخل تلگرام (فقط ادمین) — منطق خالص، بدون تلگرام و MT5.

- مقدارهای پیش‌فرض همان config/settings_scout.yaml است؛ تغییرهای تلگرام جدا در
  data/scout_overrides.json ذخیره می‌شود (YAML دست نمی‌خورد) و با ری‌استارت می‌ماند.
- هر تنظیم حد پایین/بالا و قدم ثابت دارد؛ عدد بیرون از بازه پذیرفته نمی‌شود.
- فایل خراب یا ناسازگار: نادیده گرفته می‌شود و پیش‌فرض‌ها می‌مانند (ربات خاموش نمی‌شود).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ترتیب همین ترتیب نمایش است
SPECS: Dict[str, Dict[str, Any]] = {
    "lot":        {"fa": "لات", "step": 0.01, "lo": 0.01, "hi": 0.10, "nd": 2, "unit": ""},
    "sl_cap":     {"fa": "سقف حد ضرر سیگنال", "step": 1.0, "lo": 3.0, "hi": 30.0, "nd": 0, "unit": "$"},
    "emerg_cap":  {"fa": "سقف حد ضرر اضطراری", "step": 1.0, "lo": 5.0, "hi": 50.0, "nd": 0, "unit": "$"},
    "max_open":   {"fa": "پوزیشن هم‌زمان", "step": 1.0, "lo": 1.0, "hi": 5.0, "nd": 0, "unit": ""},
    "max_risk":   {"fa": "سقف ریسک کل", "step": 5.0, "lo": 5.0, "hi": 100.0, "nd": 0, "unit": "$"},
    "expiry_min": {"fa": "مهلت جواب هشدار", "step": 5.0, "lo": 5.0, "hi": 120.0, "nd": 0, "unit": " دقیقه"},
}
INT_KEYS = {"max_open", "expiry_min"}


def norm(key: str, value: Any) -> Any:
    """گرد کردن به دقت همان تنظیم (لات دو رقم اعشار، بقیه عدد صحیح)."""
    v = round(float(value), SPECS[key]["nd"])
    return int(v) if key in INT_KEYS else v


def fmt(key: str, value: Any) -> str:
    sp = SPECS[key]
    v = float(value)
    txt = f"{v:.2f}" if sp["nd"] == 2 else f"{v:.0f}"
    return (sp["unit"] + txt) if sp["unit"] == "$" else (txt + sp["unit"])


def step(key: str, current: Any, direction: str) -> Any:
    """یک قدم بالا (+) یا پایین (−)، داخل بازه. مقدار بیرون از بازه اول به نزدیک‌ترین لبه می‌آید."""
    sp = SPECS[key]
    cur = float(current)
    if cur < sp["lo"] - 1e-9:
        return norm(key, sp["lo"])
    if cur > sp["hi"] + 1e-9:
        return norm(key, sp["hi"])
    new = cur + sp["step"] if direction == "+" else cur - sp["step"]
    return norm(key, min(sp["hi"], max(sp["lo"], new)))


def in_range(key: str, value: Any) -> bool:
    sp = SPECS[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return sp["lo"] - 1e-9 <= v <= sp["hi"] + 1e-9


MIN_STOP_PRICE = 2.0     # حد ضرر سیگنال حداقل این‌قدر فاصلهٔ قیمت (دلار طلا) داشته باشد؛ کمتر = داخل اسپرد و نوسان


def problems(values: Dict[str, Any]) -> List[str]:
    """ناسازگاری بین تنظیم‌ها (به فارسی)."""
    out = []
    sl, em, risk = values.get("sl_cap"), values.get("emerg_cap"), values.get("max_risk")
    lot = values.get("lot")
    if sl and lot and float(sl) / (100.0 * float(lot)) < MIN_STOP_PRICE - 1e-9:
        out.append(f"با لات {fmt('lot', lot)}، سقف {fmt('sl_cap', sl)} یعنی حد ضرر فقط "
                   f"{float(sl) / (100.0 * float(lot)):.2f} دلار فاصلهٔ قیمت (حداقل {MIN_STOP_PRICE:.0f}) — "
                   "اول سقف حد ضرر سیگنال را بالا ببرید")
    if sl and em and float(em) < float(sl):
        out.append(f"سقف اضطراری ({fmt('emerg_cap', em)}) نباید کمتر از سقف حد ضرر سیگنال ({fmt('sl_cap', sl)}) باشد")
    if sl and risk and float(risk) < float(sl):
        out.append(f"سقف ریسک کل ({fmt('max_risk', risk)}) نباید کمتر از سقف حد ضرر یک سیگنال ({fmt('sl_cap', sl)}) باشد")
    return out


def needs_confirm(key: str, old: Any, new: Any) -> bool:
    """لات همیشه؛ بقیه فقط اگر بیشتر از دو برابر شود."""
    if key == "lot":
        return True
    try:
        return float(old) > 0 and float(new) > 2.0 * float(old) + 1e-9
    except (TypeError, ValueError):
        return True


def sanitize(raw: Any, base: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """مقدارهای فایل → فقط کلیدهای شناخته‌شده و داخل بازه. اگر چیزی خراب یا ناسازگار بود،
    هیچ‌کدام اعمال نمی‌شود و متن خطا برمی‌گردد."""
    if raw in (None, {}):
        return {}, None
    if not isinstance(raw, dict):
        return {}, "فایل تنظیمات تلگرام خراب است"
    clean: Dict[str, Any] = {}
    for k, v in raw.items():
        if k not in SPECS:
            continue
        if not in_range(k, v):
            return {}, f"مقدار {SPECS[k]['fa']} در فایل تنظیمات بیرون از بازه است ({v})"
        clean[k] = norm(k, v)
    bad = problems(dict(base, **clean))
    if bad:
        return {}, bad[0]
    return clean, None


def load(path: Path, base: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    p = Path(path)
    if not p.exists():
        return {}, None
    try:
        raw = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}, "فایل تنظیمات تلگرام خوانده نشد"
    return sanitize(raw, base)


def save(path: Path, values: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def diff(values: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """فقط چیزهایی که با پیش‌فرض فرق دارند در فایل می‌رود."""
    return {k: values[k] for k in SPECS if k in values and abs(float(values[k]) - float(defaults.get(k, 0))) > 1e-9}
