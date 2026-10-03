"""Scout control panel helpers (pure / OS-level, no Telegram) — unit-testable.

- InstanceLock: one Scout process per lock file (an OS file lock, released by the OS
  if the process dies), so a restart or a second launch can never run two Scouts.
- Flags: small persisted switches (alerts paused).
- market_open_utc: XAUUSD trading hours (approximate, UTC) for the feed watchdog.
- journal_summary / status texts for the /status and daily health messages.
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self, wait: float = 0.0, poll: float = 0.5) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(float(wait), 0.0)
        while True:
            fh = open(self.path, "a+")
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                if time.time() >= deadline:
                    return False
                time.sleep(poll)
                continue
            self._fh = fh
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()))
                fh.flush()
            except OSError:
                pass
            return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def load_flags(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_flags(path: Path, flags: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(flags), encoding="utf-8")
    os.replace(tmp, path)


def market_open_utc(now: datetime) -> bool:
    """Gold CFD hours, approximate: Sun 22:00 → Fri 21:00 UTC, daily break 21:00–22:00 UTC."""
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    wd, h = now.weekday(), now.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 22
    if h == 21:
        return False
    return not (wd == 4 and h >= 21)


def journal_summary(path: Path, since: datetime) -> Dict[str, Any]:
    """Counts and closed P/L from the Scout journal since ``since`` (journal ts = local time)."""
    out = {"alerts": 0, "opened": 0, "closed": 0, "profit": 0.0, "wins": 0}
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            rows: Iterable[Dict[str, str]] = list(csv.DictReader(fh))
    except OSError:
        return out
    key = since.strftime("%Y-%m-%dT%H:%M:%S")
    for r in rows:
        if str(r.get("ts", "")) < key:
            continue
        ev = r.get("event")
        if ev == "alert":
            out["alerts"] += 1
        elif ev == "opened":
            out["opened"] += 1
        elif ev == "gone":
            out["closed"] += 1
            try:
                p = float(r.get("profit") or "nan")
            except ValueError:
                continue
            if p == p:
                out["profit"] += p
                out["wins"] += int(p > 0)
    out["profit"] = round(out["profit"], 2)
    return out


def fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s} ثانیه"
    if s < 5400:
        return f"{s // 60} دقیقه"
    if s < 172800:
        return f"{s // 3600} ساعت"
    return f"{s // 86400} روز"


def summary_line(js: Dict[str, Any]) -> str:
    return (f"هشدار {js['alerts']} · باز {js['opened']} · بسته {js['closed']}"
            f" (برد {js['wins']}) · نتیجه {js['profit']:+.2f}$")


MENU_COMMANDS: List[Dict[str, str]] = [
    {"command": "menu", "description": "پنل کنترل"},
    {"command": "price", "description": "قیمت لحظه‌ای طلا"},
    {"command": "status", "description": "سلامت ربات و حساب"},
    {"command": "positions", "description": "پوزیشن‌های باز"},
    {"command": "mode", "description": "در حد ضرر چه شود (وقتی گرفتارم)"},
    {"command": "pause", "description": "توقف هشدارهای جدید"},
    {"command": "resume", "description": "ادامهٔ هشدارها"},
    {"command": "restart", "description": "ری‌استارت ربات"},
    {"command": "help", "description": "راهنمای دکمه‌ها"},
]

HELP_TEXT = """❓ <b>راهنمای اسکات</b>

<b>زیر هر سیگنال</b>
✅ <b>تأیید</b> — با قیمت همین لحظه وارد می‌شود (حجم ۰.۰۱).
❌ <b>رد</b> — سیگنال کنار گذاشته می‌شود. بی‌جواب ماندن = بعد از ۳۰ دقیقه منقضی.
🤖 <b>تحلیل</b> — نظر کوتاه هوش مصنوعی: ورود یا صبر، اطمینان، دلیل‌ها و یک نکتهٔ مدیریت. فقط راهنماست؛ تصمیم با شماست.

<b>زیر هر پوزیشن باز</b>
🔻 <b>بستن</b> — پوزیشن همین الان بسته می‌شود (یک بار زدن کافی است).
⏸ <b>نگه دار</b> — در حد ضرر بسته نمی‌شود؛ حد ضرر بروکر به سطح اضطراری می‌رود (حداکثر {em:.0f}$ ضرر).
▶️ <b>لغو نگه داشتن</b> — برمی‌گردد به حالت عادی (حالت /mode).

<b>دکمه‌های پایین صفحه</b>
📋 <b>پوزیشن‌ها</b> — پوزیشن‌های باز با سود/زیان و دکمهٔ بستن.
💰 <b>قیمت</b> — قیمت لحظه‌ای، اسپرد و تغییر امروز.
📊 <b>وضعیت</b> — سلامت ربات، اتصال MT5، موجودی و خلاصهٔ ۲۴ ساعت.
🎛 <b>منو</b> — پنل کامل (پایین). ❓ <b>راهنما</b> — همین پیام.

<b>پنل منو</b>
⚙️ <b>حالت حد ضرر</b> (/mode) — وقتی گرفتارید در حد ضرر چه شود:
  ⛔ ببند (پیش‌فرض) · ⏸ نگه دار تا خودتان بگویید · 🤖 هوش مصنوعی تصمیم بگیرد (شک یا بی‌جواب = بستن)
⏸ <b>توقف هشدارها</b> / ▶️ <b>ادامه</b> — سیگنال جدید نمی‌آید؛ پوزیشن‌های باز مثل قبل مدیریت می‌شوند.
🔄 <b>ری‌استارت</b> — با تأیید؛ پوزیشن‌ها بسته نمی‌شوند.

<b>پیام‌های خودکار</b>
📊 وضعیت زندهٔ هر پوزیشن هر {status} دقیقه · ⚠️ نزدیک حد ضرر (۷۰٪ ریسک) · 🎯 رسیدن به ۱ برابر ریسک · ⚠️ شکست ساختار · ✅ «سالمم» روزانه · ⚠️ قطع داده از MT5

<b>قواعد ریسک</b>
حد ضرر هر سیگنال حداکثر {sl:.0f}$ · حداکثر {max_open} پوزیشن هم‌زمان · ریسک کل حداکثر {risk:.0f}$ · سیگنال خلاف جهت پوزیشن باز فقط نمایش داده می‌شود.

<b>دستورها</b>: /menu /price /status /positions /mode /pause /resume /restart /help — /close شمارهٔ_تیکت"""
