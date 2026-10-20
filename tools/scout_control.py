"""Scout control panel helpers (pure / OS-level, no Telegram) — unit-testable.

- InstanceLock: one Scout process per lock file (an OS file lock, released by the OS
  if the process dies), so a restart or a second launch can never run two Scouts.
- Flags: small persisted switches (alerts paused).
- market_open_utc: XAUUSD trading hours (New York based, US daylight saving aware) for the feed watchdog.
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


def _nth_sunday_utc(year: int, month: int, n: int, hour_utc: int) -> datetime:
    d = datetime(year, month, 1, hour_utc, tzinfo=timezone.utc)
    d += timedelta(days=(6 - d.weekday()) % 7)            # اولین یکشنبه
    return d + timedelta(weeks=n - 1)


def new_york_offset_hours(now_utc: datetime) -> int:
    """اختلاف ساعت نیویورک با UTC (‎-4 تابستان، ‎-5 زمستان) با قاعدهٔ آمریکا؛ بدون نیاز به tzdata روی ویندوز.
    تابستانی: یکشنبهٔ دوم مارس ساعت 2 بامداد محلی (07:00 UTC) تا یکشنبهٔ اول نوامبر ساعت 2 (06:00 UTC)."""
    y = now_utc.year
    start, end = _nth_sunday_utc(y, 3, 2, 7), _nth_sunday_utc(y, 11, 1, 6)
    return -4 if start <= now_utc < end else -5


def market_open_utc(now: datetime) -> bool:
    """Gold CFD hours (New York based, follows US daylight saving):
    Sun 18:00 → Fri 17:00 New York, daily break 17:00–18:00 New York.
    Summer = Sun 22:00 → Fri 21:00 UTC, break 21–22 UTC; winter = one hour later."""
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    ny = now + timedelta(hours=new_york_offset_hours(now))
    wd, h = ny.weekday(), ny.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 18
    if h == 17:
        return False
    return not (wd == 4 and h >= 17)


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
    {"command": "buy", "description": "سفارش دستی خرید (اول کارت تأیید)"},
    {"command": "sell", "description": "سفارش دستی فروش (اول کارت تأیید)"},
    {"command": "settings", "description": "تنظیمات (لات، سقف‌ها، مهلت هشدار)"},
    {"command": "restart", "description": "ری‌استارت ربات"},
    {"command": "yes", "description": "تأیید هشدار با متن (مثلاً /yes 7)"},
    {"command": "no", "description": "رد هشدار با متن (مثلاً /no 7)"},
    {"command": "hold", "description": "نگه داشتن پوزیشن (/hold تیکت)"},
    {"command": "unhold", "description": "لغو نگه داشتن (/unhold تیکت)"},
    {"command": "lock", "description": "بی‌ضرر کردن پوزیشن (/lock تیکت)"},
    {"command": "exit", "description": "قیمت خروج (/exit تیکت 4120 4095)"},
    {"command": "set", "description": "تنظیمات با متن (/set lot 0.02)"},
    {"command": "cmds", "description": "فهرست دستورهای متنی (برای ربات‌ها)"},
    {"command": "alert", "description": "هشدار قیمت (مثلاً /alert 4150)"},
    {"command": "alerts", "description": "فهرست و حذف هشدارهای قیمت"},
    {"command": "help", "description": "راهنمای دکمه‌ها"},
]

HELP_TEXT = """❓ <b>راهنمای اسکات</b>

<b>زیر هر سیگنال</b>
✅ <b>تأیید</b> — با قیمت همین لحظه وارد می‌شود (حجم ۰.۰۱).
❌ <b>رد</b> — سیگنال کنار گذاشته می‌شود. بی‌جواب ماندن = بعد از ۳۰ دقیقه منقضی.
🔄 <b>خلاف جهت</b> — همان سیگنال را برعکس باز می‌کند (خرید به‌جای فروش یا برعکس) با همان فاصلهٔ حد ضرر و هدف 2R؛ اول سطح‌ها را نشان می‌دهد و تأیید می‌گیرد. در آمار جدا با برچسب «خلاف سیگنال» ثبت می‌شود.
🤖 <b>تحلیل</b> — نظر کوتاه هوش مصنوعی: ورود یا صبر، اطمینان، دلیل‌ها و یک نکتهٔ مدیریت. فقط راهنماست؛ تصمیم با شماست.

<b>زیر هر پوزیشن باز</b>
🔻 <b>بستن</b> — پوزیشن همین الان بسته می‌شود (یک بار زدن کافی است).
⏸ <b>نگه دار</b> — در حد ضرر بسته نمی‌شود؛ حد ضرر بروکر به سطح اضطراری می‌رود (حداکثر {em:.0f}$ ضرر).
▶️ <b>لغو نگه داشتن</b> — برمی‌گردد به حالت عادی (حالت /mode).
🎯 <b>قیمت خروج</b> (روی پوزیشن نگه‌داشته) — روی پیام سؤال ریپلای کنید، مثل <code>4120 4095</code>: اگر برگشت به عدد بالا یا بدتر شد به عدد پایین، بسته می‌شود. هر دو روی بروکر گذاشته می‌شوند (حتی با VPS خاموش)؛ بیش از {em:.0f}$ ضرر = اول می‌پرسد.
🔒 <b>بی‌ضرر کن</b> (زیر پیام «به ۱ برابر ریسک رسید») — با یک کلیک حد ضرر روی قیمت ورود می‌رود (روی بروکر). هدف نمی‌گذارد: اگر قیمت برگردد سر به سر بسته می‌شود، وگرنه سود باز می‌ماند و بستن با شماست.
🃏 <b>کارت پایان معامله</b> — بعد از بسته شدن هر پوزیشن: نمودار ورود تا خروج، نتیجه به دلار و R، مدت، چطور بسته شد و چه کسی تأیید کرده بود.

<b>دکمه‌های پایین صفحه</b>
📋 <b>پوزیشن‌ها</b> — پوزیشن‌های باز با سود/زیان و دکمهٔ بستن.
💰 <b>قیمت</b> — قیمت لحظه‌ای، اسپرد و تغییر امروز.
📊 <b>وضعیت</b> — سلامت ربات، اتصال MT5، موجودی و خلاصهٔ ۲۴ ساعت.
🎛 <b>منو</b> — پنل کامل (پایین). ❓ <b>راهنما</b> — همین پیام.

<b>برای ربات‌ها</b> — همهٔ دکمه‌ها دستور متنی هم دارند: /cmds

<b>سفارش دستی</b>
🟢 <code>/buy</code> · 🔴 <code>/sell</code> (یا دکمه‌های «خرید/فروش دستی» در منو) — کارتی با قیمت، حد ضرر (همیشه سقف تنظیمات) و ریسک می‌آید؛ فقط با «✅ تأیید» باز می‌شود. سقف تعداد/ریسک و پوزیشن خلاف جهت مثل سیگنال‌ها پرسیده می‌شود. ۵ دقیقه فرصت دارد.

🤖 <b>بات ادمین</b> (مثل Muse، اگر شمارهٔ بات در admin_ids باشد): <code>/buy@HellTreid_bot</code> · <code>/sell@HellTreid_bot</code> · <code>/close تیکت</code> — چون بات دکمه نمی‌زند، بدون کارت تأیید و از همان مسیر سقف‌ها باز می‌شود؛ جواب‌ها ریپلای به پیام خود بات است و در ژورنال با نام بات ثبت می‌شود.

<b>پنل منو</b>
⚙️ <b>حالت حد ضرر</b> (/mode) — وقتی گرفتارید در حد ضرر چه شود:
  ⛔ ببند (پیش‌فرض) · ⏸ نگه دار تا خودتان بگویید · 🤖 هوش مصنوعی تصمیم بگیرد (شک یا بی‌جواب = بستن)
⏸ <b>توقف هشدارها</b> / ▶️ <b>ادامه</b> — سیگنال جدید نمی‌آید؛ پوزیشن‌های باز مثل قبل مدیریت می‌شوند.
🛠 <b>تنظیمات</b> — لات، سقف‌های حد ضرر، تعداد پوزیشن، سقف ریسک کل و مهلت هشدار با ➖/➕ (فقط ادمین). از سیگنال بعدی اعمال می‌شود، ری‌استارت لازم نیست؛ تغییر لات تأیید می‌خواهد و هر تغییر در گروه اعلام می‌شود.
🔄 <b>ری‌استارت</b> — با تأیید؛ پوزیشن‌ها بسته نمی‌شوند.

<b>همراه هشدارها</b>
📊 <b>نمودار</b> زیر هر هشدار: 🟩 محدودهٔ سود تا هدف 2R · 🟥 محدودهٔ ضرر تا حد ضرر · 🟨 محدودهٔ خنثی (بستن آن‌جا تقریباً سر به سر است).
🔮 <b>اگر گرفته بودید…</b> — هشداری که رد کنید یا منقضی شود، بی‌صدا دنبال می‌شود و بعداً می‌گوید به هدف رسید یا حد ضرر خورد، و چرا.

<b>ابزارهای شما</b>
🔔 <b>هشدار قیمت</b>: <code>/alert 4150</code> (با یادداشت: <code>/alert 4150 مقاومت</code>) · فهرست و حذف: /alerts
🏁 <b>جدول امتیاز</b>: هر شب ساعت ۲۳:۵۵ تهران خودکار می‌آید (شما + اسکات در برابر ORB و ایچیموکو)؛ هر وقت خواستید از منو.

<b>پیام‌های خودکار</b>
📊 وضعیت زندهٔ هر پوزیشن هر {status} دقیقه · ⚠️ نزدیک حد ضرر (۷۰٪ ریسک) · 🎯 رسیدن به ۱ برابر ریسک · ⚠️ شکست ساختار · ✅ «سالمم» روزانه · ⚠️ قطع داده از MT5

<b>قواعد ریسک</b>
حد ضرر هر سیگنال حداکثر {sl:.0f}$ · حداکثر {max_open} پوزیشن هم‌زمان · ریسک کل حداکثر {risk:.0f}$ — اگر با تأیید جدید پر شود، اسکات ضرر احتمالی کل را می‌گوید و با «✅ می‌دانم، باز کن» از شما می‌پرسد · سیگنال خلاف جهت پوزیشن باز: با تأیید می‌پرسد «🔄 ببند و جهت را عوض کن»، «✅ هر دو بمانند» یا «❌ رد».

<b>دستورها</b>: /menu /price /status /positions /buy /sell /mode /pause /resume /settings /restart /alert /alerts /help — /close شمارهٔ_تیکت"""


# ----------------------------------------------------------------- دستورهای متنی (بات ادمین مثل Muse)
# یک بات نمی‌تواند دکمهٔ اینلاین بزند؛ برای هر دکمه یک دستور متنی هم هست و جواب‌هایی که Scout فقط با
# ویرایش کارت می‌داد (که بات نمی‌بیند) به‌صورت پیام جدید هم فرستاده می‌شوند.
_A_CMDS = {"y": "/yes {}", "n": "/no {}", "r": "/yes {} reverse", "b": "/yes {} both",
           "o": "/yes {} force", "q": "/yes {} both force", "x": "/yes {} reverse force"}


def button_command(data: Any) -> Optional[str]:
    """callback_data یک دکمه → دستور متنی معادلش (None = معادلی ندارد)."""
    p = str(data or "").split("|")
    k, n = p[0], len(p)
    try:
        if k == "a" and n == 3:
            return _A_CMDS[p[2]].format(p[1])
        if k == "x" and n == 2:
            return f"/analyze {p[1]}"
        if k == "v" and n == 2:
            return f"/contra {p[1]}"
        if k == "v" and n == 3 and p[2] in ("y", "n"):
            return f"/contra {p[1]} " + ("yes" if p[2] == "y" else "no")
        if k == "c" and n == 2:
            return f"/close {p[1]}"
        if k == "h" and n == 2:
            return f"/hold {p[1]}"
        if k == "u" and n == 2:
            return f"/unhold {p[1]}"
        if k == "z" and n == 2:
            return f"/lock {p[1]}"
        if k == "e" and n == 2:
            return f"/exit {p[1]} <قیمت بالا> <قیمت پایین>"
        if k == "e" and n == 3 and p[2] == "d":
            return f"/exitdel {p[1]}"
        if k == "e" and n == 3 and p[2] in ("y", "n"):
            return f"/exit{'ok' if p[2] == 'y' else 'no'} {p[1]}"
        if k == "m" and n == 2:
            return f"/mode {p[1]}"
        if k == "p" and n == 2:
            return f"/alertdel {p[1]}"
    except KeyError:
        return None
    return None


def buttons_as_commands(buttons: Any) -> str:
    """ردیف دکمه‌ها → یک خط «دکمه ← دستور» برای باتی که دکمه نمی‌بیند. بدون معادل = خط خالی."""
    import html as _html
    out = []
    for row in buttons or []:
        for b in row:
            cmd = button_command((b or {}).get("callback_data"))
            if cmd:
                out.append(f"{_html.escape(str(b.get('text', '')))} ← <code>{_html.escape(cmd)}</code>")
    return ("🤖 با متن:\n" + "\n".join(out)) if out else ""


def echo_text(text: str, buttons: Any = None, limit: int = 3500) -> str:
    """کارتی که فقط ویرایش شد را برای بات پیام تازه کن: فقط بند آخر (چیزی که اضافه شده) + دستورهای معادل دکمه‌ها."""
    t = str(text or "")
    if "\n\n" in t:
        t = t.rsplit("\n\n", 1)[-1]
    hint = buttons_as_commands(buttons)
    return (t[:limit] + (("\n\n" + hint) if hint else "")).strip()


HELP_CMDS = """🤖 <b>دستورهای متنی اسکات</b> (برای ربات ادمین مثل Muse؛ آدم‌ها هم می‌توانند)
اگر در گروه چند ربات هست، بعد از اسم دستور بنویسید: <code>/yes@HellTreid_bot 7</code>

<b>هشدار سیگنال</b> (شمارهٔ هشدار زیر هر کارت است؛ اگر فقط یک هشدار باز باشد شماره لازم نیست)
<code>/yes 7</code> تأیید · <code>/no 7</code> رد · <code>/analyze 7</code> تحلیل هوش مصنوعی
اگر پوزیشن خلاف جهت باز بود و اسکات پرسید: <code>/yes 7 reverse</code> (ببند و جهت را عوض کن) یا <code>/yes 7 both</code> (هر دو بمانند).
اگر سقف تعداد/ریسک پر بود و پرسید: <code>/yes 7 force</code> (می‌دانم، باز کن) — با reverse یا both هم ترکیب می‌شود.
خلاف جهت سیگنال: <code>/contra 7</code> سطح‌ها را نشان می‌دهد، <code>/contra 7 yes</code> باز می‌کند، <code>/contra 7 no</code> برمی‌گردد.

<b>سفارش دستی</b>
<code>/buy</code> · <code>/sell</code> — ربات ادمین مستقیم باز می‌کند.

<b>پوزیشن باز</b> (تیکت را از /positions بخوانید؛ اگر فقط یکی باز است تیکت لازم نیست)
<code>/close 123</code> بستن · <code>/hold 123</code> نگه دار · <code>/unhold 123</code> لغو نگه داشتن
<code>/lock 123</code> بی‌ضرر (حد ضرر روی ورود، بدون هدف)
<code>/exit 123 4120 4095</code> قیمت خروج بالا و/یا پایین (فقط روی پوزیشن نگه‌داشته) · اگر بیش از سقف ضرر بود: <code>/exitok 123</code> یا <code>/exitno 123</code> · <code>/exitdel 123</code> حذف قیمت‌های خروج

<b>تنظیمات</b>
<code>/settings</code> مقدارها · <code>/set lot 0.02</code> · کلیدها: lot, sl_cap, emerg_cap, max_open, max_risk, expiry_min
لات و تغییر بیش از دو برابر اول می‌پرسد: <code>/setok</code> تأیید · <code>/set reset</code> برگشت به پیش‌فرض (آن هم با /setok)
<code>/mode close</code> · <code>/mode hold</code> · <code>/mode ai</code> — در حد ضرر چه شود

<b>بقیه</b>
<code>/status</code> <code>/positions</code> <code>/price</code> <code>/pause</code> <code>/resume</code> <code>/score</code>
<code>/alert 4150 یادداشت</code> · <code>/alerts</code> · <code>/alertdel شماره</code>"""
