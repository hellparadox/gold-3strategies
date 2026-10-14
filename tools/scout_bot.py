"""SCOUT BOT — هر ستاپ را به تلگرام می‌فرستد و منتظر تأیید یا رد شما می‌ماند.

- خودش وارد نمی‌شود. فقط بعد از «تأیید» شما سفارش می‌گذارد.
- حد ضرر (قواعد مالک، ۲۰۲۶-۱۰-۰۲): فاصلهٔ حد ضرر سیگنال حداکثر scout.sl_cap_usd دلار است و
  به‌طور پیش‌فرض روی بروکر واقعی است — پوزیشن از حد مجاز ضرر رد نمی‌شود، مگر شما «نگه دار» زده
  باشید. «نگه دار» حد ضرر بروکر را به سطح اضطراری می‌برد (scout.emergency_sl_mult × فاصله،
  حداکثر scout.emergency_cap_usd دلار) و فقط هشدار/گزارش می‌آید. حد سود گذاشته نمی‌شود، مگر خودتان
  با «🎯 قیمت خروج» بگویید: «اگر به ۴۱۲۰ رسید ببند / اگر به ۴۰۹۵ رسید ببند» — هر دو روی بروکر (TP/SL)
  می‌روند؛ بیش از سقف اضطراری = اول از شما می‌پرسد. «لغو نگه داشتن» آن‌ها را هم برمی‌دارد.
- کارت پایان معامله (scout.trade_card.enabled، پیش‌فرض روشن): بعد از بسته شدن هر پوزیشن، عکس مسیر
  قیمت از ورود تا خروج با نتیجه، مدت، نحوهٔ بسته شدن و تأییدکننده (در نخ پس‌زمینه).
- حالت گرفتاری (/mode): وقتی نمی‌توانید سیگنال را چک کنید، تعیین می‌کنید در حد ضرر چه شود:
  close = بسته شود (پیش‌فرض) · hold = نگه داشته شود تا خودتان بگویید · ai = هوش مصنوعی
  تصمیم بگیرد (اگر جواب ندهد یا مطمئن نباشد، بسته می‌شود).
- نزدیک شدن به حد ضرر (scout.approach_frac از ریسک): یک هشدار با دکمهٔ «بستن / نگه دار» تا
  قبل از رسیدن تصمیم بگیرید.
- گزارش زنده: یک پیام وضعیت برای هر پوزیشن تأییدشده، هر scout.status_every_seconds
  (پیش‌فرض ۵ دقیقه) به‌روز می‌شود. رسیدن به ۱ برابر ریسک و شکست ساختار پیام جداگانه دارند.
- پوزیشن‌های باز در scout.state_file نگه داشته می‌شوند و بعد از ری‌استارت دوباره دنبال می‌شوند.
- بستن با دکمهٔ «بستن» یا دستور /close <ticket>.
- پنل کنترل (/menu): قیمت لحظه‌ای، سلامت ربات و حساب، پوزیشن‌ها، حالت حد ضرر، توقف/ادامهٔ
  هشدارها، ری‌استارت. فقط از admin_ids پذیرفته می‌شود. یک پیام «سالمم» روزانه
  (scout.health_daily_utc_hour) و هشدار خودکار وقتی داده/قیمت از MT5 نمی‌رسد.
- فقط یک نسخه از اسکات اجرا می‌شود (قفل فایل scout.lock_file).
- زیر هر هشدار «🤖 تحلیل»: نظر کوتاه هوش مصنوعی (ورود/صبر، اطمینان، دلیل‌ها، نکتهٔ مدیریت)
  قبل از تصمیم؛ فقط راهنماست. «❓ راهنما» (/help) کار همهٔ دکمه‌ها را توضیح می‌دهد.
- مهلت پاسخ به هر هشدار: scout.expiry_seconds (پیش‌فرض ۳۰ دقیقه).
- در لحظهٔ تأیید، ورود با قیمت همان لحظه است و حد ضرر با همان فاصلهٔ هشدار از این قیمت
  گذاشته می‌شود؛ اگر قیمت از حد ضرر هشدار رد شده باشد، سفارشی ثبت نمی‌شود.
- هشدار با پوزیشن باز (scout.alert_while_open، پیش‌فرض روشن، قاعدهٔ مالک ۲۰۲۶-۱۰-۰۲): همهٔ
  هشدارها می‌آیند، با وضعیت پوزیشن‌های باز. تأیید فقط وقتی باز می‌شود که: پوزیشن خلاف جهت باز
  نباشد، تعداد پوزیشن‌ها کمتر از scout.max_open باشد و ریسک کل (تا حد ضرر فعلی روی بروکر)
  از scout.max_total_risk_usd بیشتر نشود. خاموش کردنش رفتار قبلی را برمی‌گرداند (بی‌صدا).
- داشبورد وب (scout.dashboard، پیش‌فرض پورت 8082): وضعیت، پوزیشن‌ها، هشدارهای منتظر، سود و زیان
  روزانه، کارنامهٔ ستاپ‌ها و رویدادها؛ فقط خواندنی و فقط با توکن (env DASHBOARD_TOKEN_SCOUT یا
  DASHBOARD_TOKEN). نتیجهٔ فرضی ستاپ‌ها هر scout.dashboard.stats_minutes دوباره حساب می‌شود.
- بستن ناموفق در حد ضرر: هر scout.close_retry_seconds بی‌صدا دوباره تلاش می‌شود؛ پیام تلگرام بار اول و
  بعد حداکثر هر scout.close_fail_notify_minutes (قبلاً هر ۲ ثانیه یک پیام).
- سفارش با نتیجهٔ نامشخص («ثبت نشد» گفته نمی‌شود) و هر پوزیشن اسکات که در فهرست نیست: هر
  scout.reconcile_seconds پیدا و تحت نظر گرفته می‌شود.
- تحلیل 🤖 با کندل‌ها و قیمت لحظهٔ زدن دکمه ساخته می‌شود.
- زیر هشدار فقط برای اطلاع: کارنامهٔ ستاپ (میانگین فرضی آخرین scout.record_n نمونه؛ زیر
  scout.record_warn_r با ⚠️) و خبر پراهمیت دلار (scout.news.mode: warn | off). هیچ سیگنالی حذف نمی‌شود.
- 📊 نمودار هر هشدار (scout.chart): کندل‌ها با محدودهٔ سود (سبز)، ضرر (قرمز) و خنثی (کهربایی)، به‌صورت
  جواب زیر همان هشدار، در نخ پس‌زمینه (هشدار و دکمه‌ها منتظر نمودار نمی‌مانند).
- 🔮 «اگر گرفته بودید…» (scout.whatif): هشدار ردشده/منقضی/بازنشده بی‌صدا دنبال می‌شود و وقتی به هدف یا
  حد ضرر رسید، نتیجه با دلیلش (روند، ابر، خبر، مسیر قیمت) گفته می‌شود.
- 🏁 جدول امتیاز شبانه (scout.scoreboard): شما + اسکات در برابر ORB و ایچیموکو، با R.
- 🔔 هشدار قیمت: /alert 4150 [یادداشت] · /alerts فهرست و حذف.
- هیچ سیگنالی گم نمی‌شود: هر کندل فقط بعد از پردازش موفق «دیده‌شده» حساب می‌شود (خطا = تلاش دوباره، حداکثر
  ۳ بار و بعد پیام)، کندل‌های جاافتاده بعد از گیر کردن حلقه یا ری‌استارت تا scout.backfill_bars کندل
  (پیش‌فرض ۴ = یک ساعت) با برچسب «⏰ دیرهنگام» بررسی می‌شوند، و هشدار تکراری (همان کندل، جهت و ستاپ)
  هرگز دوباره فرستاده نمی‌شود — حتی بعد از ری‌استارت (از روی ژورنال).
- سقف تعداد/ریسک (scout.max_open / scout.max_total_risk_usd): اگر با تأیید جدید پر شود، اسکات رد نمی‌کند؛
  ضرر احتمالی کل را می‌گوید و با «✅ می‌دانم، باز کن» تصمیم را به مالک می‌سپارد.
- پوزیشن خلاف جهت (scout.block_hedge): اسکات رد نمی‌کند، می‌پرسد: «🔄 ببند و جهت را عوض کن» (اول چک می‌کند
  سیگنال هنوز معتبر است، بعد می‌بندد و باز می‌کند)، «✅ هر دو باز بمانند»، یا «❌ رد». هشدار زنده می‌ماند.
- همه‌چیز در data/scout_journal_v2.csv با ستون‌های ثابت ثبت می‌شود؛ آمار: tools/scout_stats.py

اجرا:
    py -3.11 tools/scout_bot.py --config config/settings_scout.yaml
    py -3.11 tools/scout_bot.py --config config/settings_scout.yaml --dry   # بدون ثبت سفارش
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import requests  # noqa: E402
from loguru import logger  # noqa: E402

from core import Settings  # noqa: E402
from core.mt5_client import MT5Client, MT5Config, safe_comment  # noqa: E402

try:                                   # ماژول تشخیص، کنار همین فایل یا در ریشه
    from tools.scout_setups import indicators, detect
except Exception:                      # pragma: no cover
    from scout_setups import indicators, detect
try:
    from tools import scout_tracker as trk
    from tools import scout_ai
    from tools import scout_control as ctl
    from tools import scout_dashboard as dash
    from tools import scout_stats
    from tools import scout_followup as fu
    from tools import scout_signal_audit as sig_audit
    from tools import scout_settings as sset
except Exception:                      # pragma: no cover
    import scout_tracker as trk
    import scout_ai
    import scout_control as ctl
    import scout_dashboard as dash
    import scout_stats
    import scout_followup as fu
    import scout_signal_audit as sig_audit
    import scout_settings as sset

API = "https://api.telegram.org/bot{}/{}"
# ستون‌های ثابت ژورنال — همهٔ رویدادها زیر یک سرستون (نسخهٔ قبل ستون‌ها را جابه‌جا می‌نوشت)
JOURNAL_FIELDS = ["ts", "event", "alert", "setup", "n_setups", "side", "bar", "price", "sl", "tp",
                  "atr", "spread", "risk_usd", "ticket", "entry", "profit", "ok", "note"]


# --------------------------------------------------------------------- telegram

SETUP_ABBR = {
    "kijun_pullback": "kp", "tenkan_momentum": "tm", "range_break": "rb",
    "pullback_resume": "pr", "cloud_break": "cb", "tk_cross": "tk", "rejection": "rj",
    "engulfing": "en", "ignition": "ig", "exhaustion": "ex", "manual": "man",
}


def scout_comment(setup: str) -> str:
    """Short MT5-safe order comment, e.g. 'cloud_break + kijun_pullback' -> 'scout_cb_kp'.

    The Telegram text and the journal keep the full setup names; only the
    broker-side comment is abbreviated (MT5 rejects spaces/'+'/long comments).
    """
    parts = []
    for name in str(setup or "").split("+"):
        name = name.strip()
        if not name or name.isdigit():          # the '+N' overflow marker
            continue
        parts.append(SETUP_ABBR.get(name, name[:4]))
    return safe_comment("scout_" + "_".join(parts), "scout")


class TG:
    def __init__(self, token: str, chat_id: int) -> None:
        self.token, self.chat = token, int(chat_id)
        self.q: "queue.Queue[dict]" = queue.Queue()
        self._offset = 0
        self._stop = threading.Event()
        self.last_mid: Optional[int] = None

    def _call(self, method: str, **kw: Any) -> Optional[dict]:
        try:
            r = requests.post(API.format(self.token, method), json=kw, timeout=20)
            d = r.json()
            self.last_error = "" if d.get("ok") else str(d.get("description") or "")
            return d.get("result") if d.get("ok") else None
        except Exception as exc:
            self.last_error = self._clean(exc)         # خطای قبلی («not modified») نماند
            logger.warning("telegram {} failed: {}", method, self.last_error)
            return None

    def _clean(self, x: Any) -> str:
        """متن خطا بدون توکن ربات (آدرس درخواست‌ها توکن دارد و نباید در لاگ بماند)."""
        t = str(x)
        return t.replace(self.token, "***") if getattr(self, "token", "") else t

    def send(self, text: str, buttons: Optional[list] = None,
             keyboard: Optional[dict] = None, reply_to: Optional[int] = None) -> Optional[int]:
        kw: Dict[str, Any] = {"chat_id": self.chat, "text": text, "parse_mode": "HTML"}
        if reply_to:
            kw["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        if buttons:
            kw["reply_markup"] = {"inline_keyboard": buttons}
        elif keyboard:
            kw["reply_markup"] = keyboard
        r = self._call("sendMessage", **kw)
        mid = r.get("message_id") if r else None
        if mid:
            self.last_mid = mid
        return mid

    def send_photo(self, png: bytes, caption: str = "", reply_to: Optional[int] = None) -> Optional[int]:
        data: Dict[str, Any] = {"chat_id": self.chat, "caption": caption[:1000], "parse_mode": "HTML"}
        if reply_to:
            data["reply_parameters"] = json.dumps({"message_id": int(reply_to), "allow_sending_without_reply": True})
        try:
            r = requests.post(API.format(self.token, "sendPhoto"), data=data,
                              files={"photo": ("scout.png", png, "image/png")}, timeout=30)
            d = r.json()
            if not d.get("ok"):
                logger.warning("telegram sendPhoto failed: {}", d.get("description"))
                return None
            return (d.get("result") or {}).get("message_id")
        except Exception as exc:
            logger.warning("telegram sendPhoto failed: {}", self._clean(exc))
            return None

    def delete(self, message_id: int) -> None:
        self._call("deleteMessage", chat_id=self.chat, message_id=message_id)

    def edit(self, message_id: int, text: str, buttons: Optional[list] = None) -> bool:
        kw: Dict[str, Any] = {"chat_id": self.chat, "message_id": message_id,
                              "text": text, "parse_mode": "HTML"}
        if buttons:
            kw["reply_markup"] = {"inline_keyboard": buttons}
        if self._call("editMessageText", **kw) is not None:
            return True
        # متن تکراری (مثلاً بازار بسته و قیمت ثابت) خطا نیست؛ پیام تازه نفرست
        return "not modified" in (getattr(self, "last_error", "") or "")

    def ack(self, cb_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=cb_id, text=text)

    def start(self) -> None:
        threading.Thread(target=self._poll, name="tg-poll", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                r = requests.get(API.format(self.token, "getUpdates"),
                                 params={"offset": self._offset, "timeout": 25}, timeout=40)
                for u in (r.json().get("result") or []):
                    self._offset = u["update_id"] + 1
                    self.q.put(u)
            except Exception:
                time.sleep(3)


# ------------------------------------------------------------------------ scout
class Scout:
    def __init__(self, s: Settings, dry: bool, selftest: bool = False) -> None:
        self.s, self.dry = s, dry
        self.selftest = selftest
        self.client = MT5Client(MT5Config.from_settings(s))
        self.lot = float(s.get("scout.lot", 0.01))
        self.expiry = int(s.get("scout.expiry_seconds", 1800))
        self.journal = Path(s.get("scout.journal", "data/scout_journal_v2.csv"))
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        token = os.environ.get(str(s.get("telegram.token_env", "TELEGRAM_TOKEN_SCOUT")), "")
        if not token:
            raise SystemExit("توکن تلگرام تنظیم نشده (TELEGRAM_TOKEN_SCOUT)")
        self.admins = {int(x) for x in (s.get("telegram.admin_ids") or [])}
        self.tg = TG(token, (s.get("telegram.admin_ids") or [0])[0])
        self.pending: Dict[str, dict] = {}
        self.open: Dict[int, dict] = {}
        self.last_bar: Optional[pd.Timestamp] = None      # آخرین کندلی که با موفقیت پردازش شد
        self.backfill_bars = int(s.get("scout.backfill_bars", 4))
        self._bar_fail: Dict[str, int] = {}
        self._seen_keys: Optional[set] = None             # (کندل، جهت، ستاپ) که قبلاً هشدار/ثبت شده
        self.n = 0
        self.block_hedge = bool(s.get("scout.block_hedge", True))
        self.alert_while_open = bool(s.get("scout.alert_while_open", True))
        self.max_open = int(s.get("scout.max_open", 2))
        self.max_total_risk = float(s.get("scout.max_total_risk_usd", 20.0) or 0.0)
        self.merge_setups = bool(s.get("scout.merge_setups", True))
        self.beat_path = Path(str(s.get("scout.heartbeat", "data/heartbeat_scout.txt")))
        self._last_beat = 0.0
        # حد ضرر نرم + گزارش زنده
        self.soft_stop = bool(s.get("scout.soft_stop", True))
        self.emerg_mult = float(s.get("scout.emergency_sl_mult", 3.0))
        self.status_every = float(s.get("scout.status_every_seconds", 300))
        self.state_path = Path(str(s.get("scout.state_file", "data/scout_state.json")))
        # سقف‌های دلاری (با لات ثابت به فاصلهٔ قیمت تبدیل می‌شوند)
        usd_per_point = 100.0 * self.lot                # XAUUSD: ۱ لات = ۱۰۰ اونس
        cap = float(s.get("scout.sl_cap_usd", 10.0) or 0.0)
        ecap = float(s.get("scout.emergency_cap_usd", 20.0) or 0.0)
        self.sl_cap = cap / usd_per_point if cap > 0 else None
        self.emerg_cap = ecap / usd_per_point if ecap > 0 else None
        self.sl_cap_usd, self.emerg_cap_usd = cap, ecap
        self.approach_frac = float(s.get("scout.approach_frac", 0.7))
        self.mode_path = Path(str(s.get("scout.mode_file", "data/scout_mode.json")))
        self.mode = trk.load_mode(self.mode_path, str(s.get("scout.away_default", "close")))
        self.ai_recheck = float(s.get("scout.ai_recheck_minutes", 15)) * 60.0
        self.ai_min_conf = int(s.get("scout.ai_min_confidence", 60))
        self.ai_timeout = float(s.get("scout.ai_timeout_seconds", 60))
        self._ai = None                                   # ساخته می‌شود در اولین نیاز
        self._ai_pool = None
        self._ai_futs: Dict[int, Any] = {}
        self._sl_fail_t: Dict[int, float] = {}
        # پنل کنترل و نگهبان سلامت
        self.flags_path = Path(str(s.get("scout.flags_file", "data/scout_flags.json")))
        self.paused = bool(ctl.load_flags(self.flags_path).get("paused", False))
        self.started = time.time()
        self.health_hour = int(s.get("scout.health_daily_utc_hour", 5))
        self.stale_after = float(s.get("scout.feed_stale_seconds", 600))
        self._last_data_ok = time.time()
        self._feed_alert = False
        self._health_day: Optional[str] = None
        self._last_alert_t: Optional[float] = None
        self.lock: Optional[ctl.InstanceLock] = None
        # تحلیل هوش مصنوعی برای هشدارها
        self._an_pool = None
        self._an_futs: Dict[str, Any] = {}
        self._an_cache: Dict[str, str] = {}
        self._frame: Optional[pd.DataFrame] = None
        self._closing_t: Dict[int, float] = {}
        self._an_short: Dict[str, str] = {}
        # داشبورد وب
        self.dash_enabled = bool(s.get("scout.dashboard.enabled", True))
        self.dash_port = int(s.get("scout.dashboard.port", 8082))
        self.dash_host = str(s.get("scout.dashboard.host", "0.0.0.0"))
        self.dash_token = (os.environ.get("DASHBOARD_TOKEN_SCOUT") or os.environ.get("DASHBOARD_TOKEN")
                           or str(s.get("scout.dashboard.token", "") or ""))
        self.stats_every = float(s.get("scout.dashboard.stats_minutes", 60)) * 60.0
        self.compare_bots = list(s.get("scout.dashboard.compare") or [
            {"name": "ORB", "db": "subscriptions.db"}, {"name": "ایچیموکو", "db": "subscriptions_ichimoku.db"}])
        self.dashboard: Optional[Any] = None
        self._dash_snap: Dict[str, Any] = {}
        self._dash_t = 0.0
        self._live: Dict[int, dict] = {}
        self._virtual_rows: Optional[list] = None
        self._actual_map: Dict[str, float] = {}
        self._virt_t = 0.0
        self.version = ""
        # بستن ناموفق، یافتن پوزیشن، خطوط اطلاعاتی هشدار
        self.recon_every = float(s.get("scout.reconcile_seconds", 60) or 60.0)
        self.close_retry = float(s.get("scout.close_retry_seconds", 15) or 15.0)
        self.close_fail_every = float(s.get("scout.close_fail_notify_minutes", 10) or 10.0) * 60.0
        self.news_mode = "warn" if str(s.get("scout.news.mode", "warn") or "off").lower() == "warn" else "off"
        self.news_before = int(s.get("scout.news.minutes_before", 15))
        self.news_after = int(s.get("scout.news.minutes_after", 15))
        self.record_n = int(s.get("scout.record_n", 30))
        self.record_warn = float(s.get("scout.record_warn_r", -0.5))
        self._news: Optional[Any] = None
        self._recon_t = 0.0
        self._close_try_t: Dict[int, float] = {}
        self._close_note_t: Dict[int, float] = {}
        # نمودار، «اگر گرفته بودید»، جدول امتیاز، هشدار قیمت
        self.chart_enabled = bool(s.get("scout.chart.enabled", True))
        self.chart_bars = int(s.get("scout.chart.bars", 64))
        self._chart_pool: Optional[ThreadPoolExecutor] = None
        self.whatif_enabled = bool(s.get("scout.whatif.enabled", True))
        self.whatif_path = Path(str(s.get("scout.whatif.file", "data/scout_whatif.json")))
        self.whatif_every = float(s.get("scout.whatif.check_seconds", 30))
        self._whatif: list = fu.load_json(self.whatif_path, []) if self.whatif_enabled else []
        self._whatif_t = 0.0
        self.score_enabled = bool(s.get("scout.scoreboard.enabled", True))
        self.score_time = str(s.get("scout.scoreboard.tehran_time", "23:55"))
        self.palerts_path = Path(str(s.get("scout.price_alerts_file", "data/scout_price_alerts.json")))
        self._palerts: list = fu.load_json(self.palerts_path, [])
        # قیمت خروج پوزیشن نگه‌داشته (سؤال بالای سقف در انتظار جواب) و کارت پایان معامله
        self._exit_ask: Dict[int, tuple] = {}
        self._who = ""                                    # نام کسی که همین الان دکمه زد (برای «تأیید: …»)
        self.card_enabled = bool(s.get("scout.trade_card.enabled", True))
        # کارنامهٔ سیگنال‌ها: بعد از هر هشدار بازار به کدام طرف رفت (زیر هشدار و در داشبورد)
        self.audit_enabled = bool(s.get("scout.audit.enabled", True))
        self.audit_days = int(s.get("scout.audit.days", 14))
        self.audit_every = float(s.get("scout.audit.refresh_minutes", 60)) * 60.0
        self.audit_min_n = int(s.get("scout.audit.min_samples", 5))
        self._audit: Optional[Dict[str, Any]] = None
        self._audit_setups: Dict[str, Dict[str, Any]] = {}
        self._audit_t = 0.0
        # برچسب بالای هشدار (همراه شکست / بدون شکست / حد ضرر نزدیک) با درصدِ همان کارنامه — فقط اطلاع
        self.label_enabled = bool(s.get("scout.label.enabled", True))
        self.label_tight = float(s.get("scout.label.tight_stop", sig_audit.TIGHT_STOP))
        self._audit_labels: Dict[str, Dict[str, Any]] = {}
        # تنظیمات از تلگرام (فقط ادمین): پیش‌فرض = YAML، تغییرها در فایل جدا
        self.settings_path = Path(str(s.get("scout.settings_file", "data/scout_overrides.json")))
        self._settings_defaults = self.current_settings()
        self._settings_mid: Optional[int] = None
        self._settings_ask: Optional[tuple] = None
        ov, self._settings_warn = sset.load(self.settings_path, self._settings_defaults)
        if ov:
            self._set_values(ov)

    # ---------------------------------------------------------------- journal
    def log(self, **row: Any) -> None:
        row.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        new = (not self.journal.exists()) or self.journal.stat().st_size == 0
        with self.journal.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def _who_plain(self) -> str:
        """نام کسی که الان دکمه زد، برای ژورنال (بدون HTML و بدون ; و = که یادداشت را می‌شکنند)."""
        name = html.unescape(str(getattr(self, "_who", "") or ""))
        for ch in (";", "=", "\n", "\r", ","):
            name = name.replace(ch, " ")
        return " ".join(name.split())[:40]

    def _by_note(self) -> str:
        w = self._who_plain()
        return f";by={w}" if w else ""

    # ----------------------------------------------------------------- alerts
    @staticmethod
    def merge(grp: pd.DataFrame) -> pd.Series:
        """چند ستاپ روی یک کندل و یک جهت = یک هشدار، نه چند تا.

        حد ضررِ بازترین آن‌ها انتخاب می‌شود: اگر ساختارِ یکی از ستاپ‌ها می‌گوید
        حد ضرر باید آنجا باشد، حد ضررِ نزدیک‌تر داخل همان ساختار است و زود می‌خورد.
        """
        r = grp.sort_values("sl_dist").iloc[-1].copy()
        names = list(dict.fromkeys(grp["setup"].tolist()))
        r["setup"] = " + ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        r["setups_all"] = ",".join(names)
        r["n_setups"] = len(names)
        r["tp_dist"] = 2.0 * float(r["sl_dist"])
        return r

    def levels(self, r: pd.Series):
        entry = float(r.ref_close)
        sl = entry - r.sl_dist if r.side == "BUY" else entry + r.sl_dist
        tp = entry + r.tp_dist if r.side == "BUY" else entry - r.tp_dist
        risk = round(r.sl_dist * 100 * self.lot, 2)
        n_set = int(r.get("n_setups", 1) or 1)
        return entry, sl, tp, risk, n_set

    def cap_row(self, r: pd.Series) -> pd.Series:
        """حد ضرر سیگنال حداکثر scout.sl_cap_usd دلار؛ هدف ۲ برابر همان."""
        if self.sl_cap is None or float(r.sl_dist) <= self.sl_cap:
            return r
        r = r.copy()
        r["sl_dist_orig"] = float(r.sl_dist)
        r["sl_dist"] = self.sl_cap
        r["tp_dist"] = 2.0 * self.sl_cap
        return r

    def _usd(self, price_dist: float, lot: Optional[float] = None) -> float:
        """دلار برای فاصلهٔ قیمت؛ lot = لات خود پوزیشن (اگر بعداً لات از تلگرام عوض شده باشد)."""
        return round(abs(float(price_dist)) * 100.0 * float(lot or self.lot), 2)

    def open_risk_usd(self) -> float:
        """ضرر هر پوزیشن باز اگر حد ضرر فعلی‌اش (طبق سیاست) روی بروکر بخورد."""
        total = 0.0
        for info in self.open.values():
            if info.get("closing"):                        # در حال بسته شدن (عوض کردن جهت)
                continue
            try:
                stop = trk.desired_broker_sl(info, self.mode) if self.soft_stop else float(info["sl"])
                total += max(self._usd(float(info["entry"]) - stop, info.get("lot")), 0.0)
            except (KeyError, TypeError, ValueError):
                continue
        return round(total, 2)

    @staticmethod
    def _filled_lot(res: Any, asked: float) -> float:
        """حجمی که بروکر واقعاً پر کرد (اگر گزارش داد)، وگرنه همان که خواستیم."""
        vol = getattr(res, "volume", None)
        return float(vol) if isinstance(vol, (int, float)) and not isinstance(vol, bool) and vol > 0 else float(asked)

    def _emerg_dist(self, lot: Optional[float]) -> Optional[float]:
        """سقف اضطراری (فاصلهٔ قیمت) برای این لات."""
        if lot is None or abs(float(lot) - float(self.lot)) < 1e-9:
            return self.emerg_cap
        return self._cap_dist(self._cap_usd("emerg"), lot)

    def new_risk_usd(self, side: str, entry: float, sl: float, lot: Optional[float] = None) -> float:
        if not self.soft_stop:
            return self._usd(entry - sl, lot)
        emerg = trk.emergency_sl(side, entry, sl, self.emerg_mult, self._emerg_dist(lot))
        stop = trk.desired_broker_sl({"sl": sl, "emerg": emerg, "hold": False}, self.mode)
        return self._usd(entry - stop, lot)

    def active_open(self) -> Dict[int, dict]:
        return {tk: i for tk, i in self.open.items() if not i.get("closing")}

    def opposite_of(self, side: str) -> list:
        return [tk for tk, i in self.active_open().items() if i["side"] != side]

    def entry_block(self, side: str, new_risk: float, allow_hedge: bool = False) -> Optional[str]:
        """دلیل باز نشدن تأیید جدید، یا None."""
        opposite = self.opposite_of(side)
        if self.block_hedge and opposite and not allow_hedge:
            return ("پوزیشن خلاف جهت باز است (" + "، ".join(f"#{t}" for t in opposite) + ") — "
                    "خرید و فروش هم‌زمان همدیگر را خنثی می‌کنند و فقط اسپرد می‌دهید")
        if self.max_open > 0 and len(self.active_open()) >= self.max_open:
            return f"سقف {self.max_open} پوزیشن هم‌زمان پر است"
        used = self.open_risk_usd()
        if self.max_total_risk > 0 and used + new_risk > self.max_total_risk + 0.01:
            return (f"سقف ریسک کل {self.max_total_risk:.0f}$ پر می‌شود (باز: {used:.2f}$ + "
                    f"این سیگنال: {new_risk:.2f}$)")
        return None

    def open_summary(self) -> str:
        """یک خط دربارهٔ پوزیشن‌های باز برای پیام هشدار."""
        live = {}
        try:
            live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        except Exception:
            pass
        parts = []
        for tk, i in self.open.items():
            p = live.get(int(tk))
            pl = f" {float(p.profit):+.2f}$" if p is not None else ""
            parts.append(f"#{tk} {i['side']}{pl}")
        return "، ".join(parts)

    # ------------------------------------------------------- chart
    def send_chart(self, aid: str, mid: Optional[int], r: pd.Series, entry: float, sl: float, tp: float,
                   risk: float) -> None:
        """نمودار در نخ پس‌زمینه ساخته و به‌صورت جواب زیر هشدار فرستاده می‌شود."""
        if not self.chart_enabled or self._frame is None or not mid:
            return
        try:
            tick = self.client.get_tick()
            spread_price = float(tick.ask - tick.bid) if tick is not None else 0.0
        except Exception:                              # pragma: no cover
            spread_price = 0.0
        frame = self._frame.tail(self.chart_bars).copy()
        setup = str(r.get("setups_all", r.setup))
        if self._chart_pool is None:
            self._chart_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-chart")
        self._chart_pool.submit(self._chart_job, aid, mid, frame, str(r.side), float(entry), float(sl),
                                float(tp), setup, float(risk), spread_price)

    def _chart_job(self, aid, mid, frame, side, entry, sl, tp, setup, risk, spread_price) -> None:
        try:
            from tools.scout_chart import render_alert_chart
        except Exception:                              # pragma: no cover
            from scout_chart import render_alert_chart
        try:
            png = render_alert_chart(frame, side, entry, sl, tp, setup, risk, spread_price,
                                     bars=self.chart_bars, alert_id=aid)
        except Exception as exc:
            logger.warning("scout chart #{} failed: {}", aid, exc)
            return
        self.tg.send_photo(png, f"📊 هشدار #{aid} · 🟩 محدودهٔ سود تا هدف 2R · 🟥 محدودهٔ ضرر تا حد ضرر · "
                                f"🟨 محدودهٔ خنثی (بستن آن‌جا تقریباً سر به سر است)", reply_to=mid)

    # ------------------------------------------------------- trade card
    def send_trade_card(self, tk: int, info: dict, pnl: float, exit_px: float, kind: Optional[str]) -> None:
        """بعد از بسته شدن: داده در همین نخ (MT5)، ساختن عکس و فرستادن در نخ پس‌زمینه.
        هر خطا فقط لاگ می‌شود؛ پیام متنی «بسته شد» قبلاً رفته است."""
        try:
            deals = self.client.deals_for_position(int(tk)) or []
            ins = [int(getattr(d, "time", 0) or 0) for d in deals if int(getattr(d, "entry", 0)) == 0]
            outs = [int(getattr(d, "time", 0) or 0) for d in deals if int(getattr(d, "entry", 0)) in (1, 3)]
            t_in, t_out = (min(ins) if ins else 0), (max(outs) if outs else 0)
            if not t_in or not t_out or t_out < t_in:
                logger.info("scout card #{}: deal times unavailable", tk)
                return
            secs = t_out - t_in
            tf, tf_s = ("M1", 60) if secs <= 3 * 3600 else (("M5", 300) if secs <= 12 * 3600 else ("M15", 900))
            tick = self.client.get_tick()
            now_srv = int(getattr(tick, "time", 0) or 0) or t_out
            count = int(min(max((now_srv - t_in) // tf_s + 40, 60), 3000))
            bars = self.client.get_rates(tf, count)
            if bars is None or bars.empty:
                return
            a = pd.Timestamp(t_in - 15 * tf_s, unit="s")
            b = pd.Timestamp(t_out + 8 * tf_s, unit="s")
            bars = bars[(bars.index >= a) & (bars.index <= b)].copy()
            if bars.empty:
                return
        except Exception as exc:
            logger.warning("scout card #{} data failed: {}", tk, exc)
            return
        risk_usd = float(info.get("risk") or 0.0) * 100.0 * float(info.get("lot") or self.lot)
        r_mult = round(pnl / risk_usd, 2) if risk_usd > 0.01 else None
        how = kind if kind in ("exit_tp", "exit_sl") else (info.get("why") or kind)
        if info.get("locked") and how in ("exit_tp", "exit_sl"):
            how = "lock_tp" if how == "exit_tp" else "lock_sl"
        setup = html.escape(str(info.get("setup") or ""))
        day = datetime.now(dash.TEHRAN).date()
        caption = fu.trade_card_caption(tk, info["side"], setup, float(info["entry"]), exit_px, pnl, r_mult,
                                        secs, how, info.get("by"), day)
        if info.get("contra"):
            caption = caption.replace("</code>", "</code> · 🔄 خلاف سیگنال", 1)
        levels = {"sl": info.get("sl")}
        if info.get("hold"):
            levels.update(xtp=info.get("xtp"), xsl=info.get("xsl"))
        if self._chart_pool is None:
            self._chart_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-chart")
        self._chart_pool.submit(self._card_job, tk, bars, info["side"], float(info["entry"]), exit_px,
                                pd.Timestamp(t_in, unit="s"), pd.Timestamp(t_out, unit="s"), pnl, r_mult,
                                str(info.get("setup") or ""), tf, levels, caption)

    def _card_job(self, tk, bars, side, entry, exit_px, t_in, t_out, pnl, r_mult, setup, tf, levels,
                  caption) -> None:
        try:
            from tools.scout_chart import render_trade_card
        except Exception:                              # pragma: no cover
            from scout_chart import render_trade_card
        try:
            png = render_trade_card(bars, side, entry, exit_px, t_in, t_out, pnl, r_mult, setup, tk,
                                    timeframe=tf, levels=levels)
        except Exception as exc:
            logger.warning("scout card #{} render failed: {}", tk, exc)
            return
        self.tg.send_photo(png, caption)

    # ------------------------------------------------------- what-if
    def track_whatif(self, aid: str, p: dict, decision: str) -> None:
        if not self.whatif_enabled or p.get("manual"):      # سفارش دستی سیگنال نبود؛ «اگر گرفته بودید» ندارد
            return
        try:
            r = p["row"]
            entry, sl, tp, risk, _n = self.levels(r)
            item = fu.new_whatif(aid, str(r.get("setups_all", r.setup)), r.side, entry, sl, tp, r.bar,
                                 decision, risk, p.get("wctx") or {})
            item["mid"] = p.get("mid")
            self._whatif = (self._whatif + [item])[-200:]      # سقف حافظه
            fu.save_json(self.whatif_path, self._whatif)
        except Exception as exc:
            logger.warning("scout what-if track failed: {}", exc)

    def whatif_tick(self, now: Optional[float] = None) -> None:
        if not self._whatif:
            return
        now = time.time() if now is None else now
        if now - self._whatif_t < self.whatif_every:
            return
        self._whatif_t = now
        m1 = self.client.get_rates("M1", 1600)            # بیش از ۲۴ ساعت
        keep = []
        for item in self._whatif:
            try:
                res = fu.evaluate(item, m1)
            except Exception as exc:                   # pragma: no cover
                logger.warning("scout what-if eval failed: {}", exc)
                res = None
            if res is None:
                keep.append(item)
                continue
            self.tg.send(fu.whatif_message(item, res, self.setup_record(item["setup"])),
                         reply_to=item.get("mid"))
            self.log(event="whatif", alert=item["aid"], setup=item["setup"], side=item["side"],
                     price=item["entry"], sl=round(item["sl"], 2), tp=round(item["tp"], 2),
                     profit=round(item["risk_usd"] * res["R"], 2),
                     note=f"{fu.outcome_kind(item, res)};{res['exit']};R={res['R']};{item['decision']}")
        if len(keep) != len(self._whatif):
            self._whatif = keep
            fu.save_json(self.whatif_path, self._whatif)

    # ------------------------------------------------------- price alerts
    def price_alert_cmd(self, msg: str) -> None:
        parsed = fu.parse_alert(msg)
        if not parsed:
            self.tg.send("🔔 <b>هشدار قیمت</b>\nبنویسید: <code>/alert 4150</code> یا با یادداشت: "
                         "<code>/alert 4150 مقاومت روزانه</code>\nفهرست و حذف: /alerts")
            return
        level, note = parsed
        tick = self.client.get_tick()
        if tick is None:
            self.tg.send("⚠️ قیمت از MT5 نرسید؛ چند ثانیه بعد دوباره امتحان کنید.")
            return
        a = fu.add_price_alert(self._palerts, level, float(tick.bid), note, time.time())
        if a is None:
            self.tg.send("⚠️ حداکثر ۲۰ هشدار قیمت؛ اول چندتا را از /alerts حذف کنید.")
            return
        fu.save_json(self.palerts_path, self._palerts)
        way = "بالا برود" if a["dir"] == "up" else "پایین بیاید"
        self.tg.send(f"🔔 ثبت شد: وقتی طلا تا <b>{a['level']:.2f}</b> {way} خبرتان می‌کنم "
                     f"(الان {float(tick.bid):.2f})." + (f"\n📝 {html.escape(note)}" if note else ""),
                     [[{"text": "❌ حذف این هشدار", "callback_data": f"p|{a['id']}"}]])

    def price_alerts_list(self) -> None:
        if not self._palerts:
            self.tg.send("🔔 هشدار قیمتی ندارید.\nبرای ساختن: <code>/alert 4150</code> یا "
                         "<code>/alert 4150 یادداشت</code>")
            return
        rows = [[{"text": f"❌ {a['level']:.2f} {'⬆' if a['dir'] == 'up' else '⬇'}",
                  "callback_data": f"p|{a['id']}"}] for a in self._palerts]
        lines = [f"{'⬆' if a['dir'] == 'up' else '⬇'} <b>{a['level']:.2f}</b>"
                 + (f" · {html.escape(a['note'])}" if a.get("note") else "") for a in self._palerts]
        self.tg.send("🔔 <b>هشدارهای قیمت</b>\n" + "\n".join(lines) + "\nبرای حذف، دکمه‌اش را بزنید.", rows)

    def price_alert_delete(self, aid: str, cb: Optional[str] = None) -> None:
        before = len(self._palerts)
        self._palerts = [a for a in self._palerts if str(a.get("id")) != str(aid)]
        fu.save_json(self.palerts_path, self._palerts)
        if cb:
            self.tg.ack(cb, "حذف شد" if len(self._palerts) < before else "قبلاً حذف شده")

    def price_alert_tick(self) -> None:
        if not self._palerts:
            return
        tick = self.client.get_tick()
        if tick is None:
            return
        bid = float(tick.bid)
        due = fu.due_price_alerts(self._palerts, bid)
        if not due:
            return
        for a in due:
            way = "بالا رفت و" if a["dir"] == "up" else "پایین آمد و"
            self.tg.send(f"🔔 <b>هشدار قیمت</b>: طلا {way} به <b>{a['level']:.2f}</b> رسید (الان {bid:.2f})"
                         + (f"\n📝 {html.escape(a['note'])}" if a.get("note") else ""))
            self.log(event="price_alert", price=bid, note=f"level={a['level']};{a.get('note', '')}"[:200])
        ids = {a["id"] for a in due}
        self._palerts = [a for a in self._palerts if a["id"] not in ids]
        fu.save_json(self.palerts_path, self._palerts)

    # ------------------------------------------------------- scoreboard
    def scoreboard_tick(self, now: Optional[float] = None) -> None:
        if not self.score_enabled:
            return
        now = time.time() if now is None else now
        t = datetime.fromtimestamp(now, timezone.utc).astimezone(dash.TEHRAN)
        try:
            hh, mm = (int(x) for x in self.score_time.split(":"))
        except ValueError:
            hh, mm = 23, 55
        if (t.hour, t.minute) < (hh, mm):
            return
        day = t.date().isoformat()
        flags = ctl.load_flags(self.flags_path)
        if flags.get("scoreboard_day") == day:
            return
        flags["scoreboard_day"] = day
        ctl.save_flags(self.flags_path, flags)
        self.send_scoreboard(now=now)

    def send_scoreboard(self, now: Optional[float] = None, force: bool = False) -> None:
        """جدول امتیاز امروز (روز تهران). بدون فعالیت، فقط وقتی خودتان بخواهید فرستاده می‌شود."""
        now = time.time() if now is None else now
        t = datetime.fromtimestamp(now, timezone.utc).astimezone(dash.TEHRAN)
        midnight = t.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        events = dash.read_journal(self.journal)
        bots: Dict[str, list] = {}
        for b in self.compare_bots:
            try:
                bots[str(b.get("name"))] = dash.bot_trades(b.get("db", ""))
            except Exception as exc:
                logger.warning("scout scoreboard: {} results unavailable ({})", b.get("name"), exc)
        scout = dash.scout_trades(events, float((getattr(self, "_settings_defaults", None) or {}).get("lot") or self.lot))
        today = dash.compare_since(scout, bots, midnight)
        week = dash.compare_since(scout, bots, midnight - timedelta(days=6))
        tday = t.date()
        alerts_today = taken_today = 0
        passes: Dict[str, int] = {}
        for e in events:
            et = dash._tehran(e.get("ts", ""))
            if et is None or et.date() != tday:
                continue
            ev = e.get("event")
            if ev == "alert":
                alerts_today += 1
            elif ev == "opened":
                taken_today += 1
            elif ev == "whatif":
                kind = str(e.get("note") or "").split(";")[0]
                passes[kind] = passes.get(kind, 0) + 1
        if not force and alerts_today == 0 and not any(r["n"] for r in today):
            return                                     # روز بی‌فعالیت (آخر هفته): پیامی نمی‌آید
        self.tg.send(fu.scoreboard_text(tday, today, week, alerts_today, taken_today, passes))

    # ------------------------------------------------------- bars (no signal lost)
    @staticmethod
    def signal_key(r: pd.Series) -> tuple:
        return (str(pd.Timestamp(r.bar)), str(r.side), str(r.get("setups_all", r.setup)))

    def seen_keys(self) -> set:
        """کلید هشدارها/ثبت‌های قبلی، یک بار از ژورنال (تا ری‌استارت هشدار تکراری نفرستد)."""
        if self._seen_keys is None:
            keys = set()
            for e in dash.read_journal(self.journal):
                if e.get("event") in ("alert", "suppressed") and e.get("bar"):
                    try:
                        keys.add((str(pd.Timestamp(e["bar"])), str(e.get("side")), str(e.get("setup"))))
                    except (TypeError, ValueError):
                        continue
            self._seen_keys = keys
        return self._seen_keys

    def bars_to_process(self, f: pd.DataFrame) -> list:
        idx = list(f.index)
        if not idx:
            return []
        if self.last_bar is None:                      # تازه روشن شده
            if not self.market_open():                 # آخر هفته/وقفه: کندل‌های قدیمی را هشدار نده
                self.last_bar = idx[-1]
                return []
            return idx[-self.backfill_bars:] if self.backfill_bars > 0 else idx[-1:]
        todo = [b for b in idx if b > self.last_bar]
        if len(todo) > max(self.backfill_bars, 1):
            dropped = len(todo) - max(self.backfill_bars, 1)
            logger.warning("scout: loop stalled; {} bar(s) older than the backfill window skipped", dropped)
            todo = todo[-max(self.backfill_bars, 1):]
        return todo

    def process_new_bars(self, f: pd.DataFrame) -> None:
        """هر کندل جدید (و جاافتاده) فقط بعد از پردازش موفق «دیده‌شده» حساب می‌شود."""
        todo = self.bars_to_process(f)
        if not todo:
            return
        newest = f.index[-1]
        sig_all = detect(f)
        sp = self.client.spread_points() or 0.0
        for bar in todo:
            key = str(bar)
            try:
                self.process_bar(sig_all[sig_all.bar == bar], sp, late_bars=int(f.index.get_loc(newest)
                                                                           - f.index.get_loc(bar)))
            except Exception as exc:
                n = self._bar_fail.get(key, 0) + 1
                self._bar_fail[key] = n
                if n < 3:
                    raise                                  # حلقه ۵ ثانیه بعد دوباره همین کندل را امتحان می‌کند
                logger.error("scout: bar {} failed 3 times, giving up: {}", key, exc)
                self.tg.send(f"⚠️ بررسی کندل {pd.Timestamp(bar):%H:%M} سه بار خطا داد ({html.escape(str(exc))[:80]}). "
                             "ممکن است سیگنال این کندل نیامده باشد؛ لاگ اسکات را ببینید.")
            self._bar_fail.pop(key, None)
            self.last_bar = bar

    def process_bar(self, sig: pd.DataFrame, sp: float, late_bars: int = 0) -> None:
        rows = ([self.merge(g) for _s, g in sig.groupby("side", sort=False)]
                if self.merge_setups and not sig.empty else [r for _, r in sig.iterrows()])
        rows = [self.cap_row(r) for r in rows]
        quiet = (not self.alert_while_open) and self.busy() and not self.dry
        seen = self.seen_keys()
        for r in rows:
            k = self.signal_key(r)
            if k in seen:                                  # قبلاً هشدار/ثبت شده (مثلاً قبل از ری‌استارت)
                continue
            if self.paused:
                self.record_silent(r, sp, note="paused")
            elif quiet:
                self.record_silent(r, sp)
            else:
                self.alert(r, sp, news=self.news_note(), late_bars=late_bars, key=k)
            seen.add(k)

    # ------------------------------------------------------- info lines
    @staticmethod
    def account_kind(acc: Any) -> str:
        """فقط برای نمایش در پیام روشن شدن؛ جلوی هیچ کاری را نمی‌گیرد."""
        return {0: "دمو", 1: "مسابقه", 2: "واقعی"}.get(getattr(acc, "trade_mode", None), "نوع نامعلوم")

    def news_note(self) -> str:
        """خبر پراهمیت دلار نزدیک است؟ فقط متن برای زیر هشدار؛ هیچ سیگنالی حذف نمی‌شود."""
        if self.news_mode == "off":
            return ""
        if self._news is None:
            try:
                from core.news_filter import NewsFilter, NewsFilterConfig
                self._news = NewsFilter(NewsFilterConfig(
                    enabled=True, currencies=["USD"], min_impact="High",
                    pause_minutes_before=self.news_before, pause_minutes_after=self.news_after))
                self._news.start()
            except Exception as exc:
                logger.warning("scout news calendar unavailable: {}", exc)
                self._news = False
        if not self._news:
            return ""
        try:
            active, why = self._news.is_news_active()
        except Exception:                              # pragma: no cover
            return ""
        return str(why) if active else ""

    def setup_record(self, setup: str) -> Optional[tuple]:
        """(تعداد، میانگین R) آخرین record_n نتیجهٔ فرضی همین ستاپ، یا None (کمتر از ۱۰ نمونه)."""
        key = dash._first_setup(setup)
        vals = []
        for r in self._virtual_rows or []:
            try:
                v = float(r.get("R"))
            except (TypeError, ValueError):
                continue
            if v == v and dash._first_setup(r.get("setup")) == key:
                vals.append(v)
        vals = vals[-self.record_n:]
        if len(vals) < 10:
            return None
        return len(vals), round(sum(vals) / len(vals), 2)

    def record_silent(self, r: pd.Series, spread_pts: float, note: Optional[str] = None) -> None:
        """معامله باز است یا هشدارها متوقف‌اند: فقط برای آمار ثبت می‌شود."""
        entry, sl, tp, risk, n_set = self.levels(r)
        self.log(event="suppressed", setup=str(r.get("setups_all", r.setup)), n_setups=n_set,
                 side=r.side, bar=str(r.bar), price=entry, sl=round(sl, 2), tp=round(tp, 2),
                 atr=round(r.atr, 3), spread=spread_pts, risk_usd=risk, note=note)

    def alert(self, r: pd.Series, spread_pts: float, news: str = "", late_bars: int = 0,
              key: Optional[tuple] = None) -> None:
        self.n += 1
        self._last_alert_t = time.time()
        aid = f"{self.n}"
        entry, sl, tp, risk, n_set = self.levels(r)
        head = f"  ({n_set} ستاپ هم‌زمان)" if n_set > 1 else ""
        label = self.label_line(str(r.get("setups_all", r.setup)), abs(entry - sl))
        txt = (label + "\n" if label else "") + (f"<b>{'🟢 BUY' if r.side=='BUY' else '🔴 SELL'}</b> · <code>{r.setup}</code>{head}\n"
               f"کندل: {r.bar:%H:%M}  |  قیمت: <b>{entry:.2f}</b>\n"
               f"حد ضرر: {sl:.2f}  ({r.sl_dist:.2f} = {r.sl_dist/r.atr:.1f}×ATR)"
               + (f" · سقف {risk:.0f}$ (ساختار {float(r['sl_dist_orig']):.2f})"
                  if "sl_dist_orig" in r.index else "") + "\n"
               f"هدف پیشنهادی: {tp:.2f}  (۲ برابر ریسک)\n"
               f"ریسک: ${risk} · ATR {r.atr:.2f} · اسپرد {spread_pts:.0f}\n"
               f"<i>{self.expiry // 60} دقیقه فرصت پاسخ</i>")
        if late_bars > 0:
            txt += (f"\n⏰ <b>دیرهنگام</b>: این سیگنال مال کندل {r.bar:%H:%M} است ({late_bars} کندل قبل)؛ "
                    "قیمت از آن موقع حرکت کرده. اگر تا لحظهٔ تأیید از حد ضرر رد شده باشد، باز نمی‌شود.")
        rec = self.setup_record(str(r.setup))
        if rec:
            n_rec, avg = rec
            warn = avg < self.record_warn
            txt += (f"\n{'⚠️' if warn else '📈'} کارنامهٔ <code>{dash._first_setup(r.setup)}</code>: "
                    f"میانگین فرضی {avg:+.2f}R در {n_rec} نمونهٔ آخر"
                    + (" — این ستاپ اخیراً ضعیف بوده" if warn else ""))
        aud = self.audit_line(str(r.get("setups_all", r.setup)))
        if aud:
            txt += "\n" + aud
        if news:
            txt += f"\n📰 <b>خبر مهم نزدیک است</b>: {html.escape(news)}"
        if self.open:
            block = self.entry_block(r.side, self.new_risk_usd(r.side, entry, sl))
            hedge = bool(block) and block.startswith("پوزیشن خلاف")
            txt += (f"\n⚠️ پوزیشن باز: {self.open_summary()}"
                    + (f"\n⚠️ {block}؛ با تأیید، اسکات می‌پرسد: پوزیشن قبلی را ببندد و جهت را عوض کند، "
                       "هر دو باز بمانند، یا رد." if hedge else
                       f"\n⚠️ {block}؛ با تأیید، اسکات ضرر احتمالی کل را می‌گوید و از شما می‌پرسد." if block else
                       f"\nبا تأیید، پوزیشن دوم باز می‌شود (ریسک کل حداکثر {self.max_total_risk:.0f}$)."))
        mid = self.tg.send(txt, self.alert_buttons(aid, reverse_side=self.opposite_side(r.side)))
        if key is not None:                            # فرستاده شد: تلاش دوباره (بعد از خطا) تکرارش نمی‌کند
            self.seen_keys().add(key)
        try:
            ctx = scout_ai.build_entry_message(
                r.side, str(r.get("setups_all", r.setup)), entry, sl, tp, risk, float(r.atr),
                float(spread_pts or 0.0), self._frame, self.open_summary() if self.open else "")
        except Exception as exc:                       # pragma: no cover
            logger.warning("scout: analysis context failed: {}", exc)
            ctx = ""
        wctx = fu.context_flags(self._frame, r.side)
        if news:
            wctx["news"] = True
        self.pending[aid] = {"row": r, "sl": sl, "tp": tp, "mid": mid, "t": time.time(), "txt": txt,
                             "lot": self.lot, "exp": self.expiry,     # هشدار با تنظیمات همان لحظه می‌ماند
                             "ctx": ctx, "wctx": wctx}
        self.send_chart(aid, mid, r, entry, sl, tp, risk)
        self.log(event="alert", alert=aid, setup=str(r.get("setups_all", r.setup)),
                 n_setups=n_set, side=r.side, bar=str(r.bar),
                 price=entry, sl=round(sl, 2), tp=round(tp, 2), atr=round(r.atr, 3),
                 spread=spread_pts, risk_usd=risk)

    # ------------------------------------------------------------- سفارش دستی (/buy /sell)
    MANUAL_EXPIRY = 300                                   # قیمت دستی زود کهنه می‌شود: ۵ دقیقه

    def manual_start(self, side: str, extra: str = "") -> None:
        """/buy یا /sell: کارت تأیید با حد ضرر = سقف تنظیمات؛ باز کردن فقط با «✅ تأیید» (همان مسیر decide)."""
        side = "BUY" if str(side).upper() == "BUY" else "SELL"
        if not self.market_open():
            self.tg.send("⛔ بازار بسته است؛ سفارش دستی ساخته نشد.")
            return
        if not self.sl_cap:
            self.tg.send("⛔ سقف حد ضرر در تنظیمات صفر/خالی است؛ بدون آن سفارش دستی نمی‌سازم. "
                         "از 🛠 تنظیمات «حد ضرر» را تعیین کنید.")
            return
        try:
            tick = self.client.get_tick()
        except Exception as exc:                          # pragma: no cover
            logger.warning("scout manual: tick failed: {}", exc)
            tick = None
        entry = float(getattr(tick, "ask" if side == "BUY" else "bid", 0.0) or 0.0) if tick is not None else 0.0
        if entry <= 0:
            self.tg.send("⚠️ قیمت از MT5 نرسید؛ سفارش دستی ساخته نشد. چند ثانیه بعد دوباره امتحان کنید.")
            return
        try:
            spread_pts = float(self.client.spread_points() or 0.0)
        except Exception:                                 # pragma: no cover
            spread_pts = 0.0
        try:
            atr = float(self._frame["atr"].iloc[-1])
        except Exception:
            atr = 0.0
        for old in [k for k, v in self.pending.items() if v.get("manual")]:     # فقط یک کارت دستی زنده
            q = self.pending.pop(old)
            if q["mid"]:
                self.tg.edit(q["mid"], q["txt"] + "\n\n↩️ <b>کارت جدیدتر جایگزین شد</b>")
            self.log(event="cancelled", alert=old, setup="manual", side=q["row"].side, note="replaced")
        self.n += 1
        aid = f"m{self.n}"                                # پیشوند m: با شمارهٔ هشدارهای سیگنال (که بعد از ری‌استارت از ۱ شروع می‌شود) قاطی نشود
        dist = float(self.sl_cap)
        r = pd.Series({"setup": "manual", "side": side, "ref_close": entry, "sl_dist": dist,
                       "tp_dist": 2.0 * dist, "atr": atr, "bar": pd.Timestamp(datetime.now())})
        _e, sl, tp, risk, _n = self.levels(r)
        exp = min(float(self.expiry), float(self.MANUAL_EXPIRY))
        txt = (f"✋ <b>سفارش دستی — {'🟢 BUY' if side == 'BUY' else '🔴 SELL'}</b>\n"
               f"قیمت: <b>{entry:.2f}</b> ({'ask' if side == 'BUY' else 'bid'}) · اسپرد {spread_pts:.0f}\n"
               f"حد ضرر: {sl:.2f} ({dist:.2f} = سقف تنظیمات) · لات {self.lot}\n"
               f"ریسک: ${risk} · هدف ندارد؛ خروج با خود شما (دکمهٔ بستن)\n"
               f"<i>{int(exp // 60)} دقیقه فرصت پاسخ؛ قیمت ورود لحظهٔ تأیید دوباره خوانده می‌شود</i>")
        if extra:
            txt += "\nℹ️ عدد/متن بعد از دستور نادیده گرفته شد: حد ضرر همیشه سقف تنظیمات است."
        if self.open:
            block = self.entry_block(side, self.new_risk_usd(side, entry, sl))
            hedge = bool(block) and block.startswith("پوزیشن خلاف")
            txt += (f"\n⚠️ پوزیشن باز: {self.open_summary()}"
                    + (f"\n⚠️ {block}؛ با تأیید، اسکات می‌پرسد: پوزیشن قبلی را ببندد و جهت را عوض کند، "
                       "هر دو باز بمانند، یا رد." if hedge else
                       f"\n⚠️ {block}؛ با تأیید، اسکات ضرر احتمالی کل را می‌گوید و از شما می‌پرسد." if block else
                       f"\nبا تأیید، پوزیشن دوم باز می‌شود (ریسک کل حداکثر {self.max_total_risk:.0f}$)."))
        mid = self.tg.send(txt, self.alert_buttons(aid, analyze=False))
        self.pending[aid] = {"row": r, "sl": sl, "tp": tp, "mid": mid, "t": time.time(), "txt": txt,
                             "lot": self.lot, "exp": exp, "ctx": "", "wctx": {}, "manual": True}
        self.log(event="manual_card", alert=aid, setup="manual", side=side, price=entry, sl=round(sl, 2),
                 atr=round(atr, 3), spread=spread_pts, risk_usd=risk)

    @staticmethod
    def alert_buttons(aid: str, analyze: bool = True, reverse_side: Optional[str] = None) -> list:
        row = [{"text": "✅ تأیید", "callback_data": f"a|{aid}|y"},
               {"text": "❌ رد", "callback_data": f"a|{aid}|n"}]
        if analyze:
            row.append({"text": "🤖 تحلیل", "callback_data": f"x|{aid}"})
        if reverse_side:                                  # «خلاف جهت»: تصمیم با مالک، اول می‌پرسد
            return [row, [{"text": f"🔄 خلاف جهت ({reverse_side})", "callback_data": f"v|{aid}"}]]
        return [row]

    # ------------------------------------------------------------- contra (خلاف جهت)
    @staticmethod
    def opposite_side(side: str) -> str:
        return "SELL" if str(side).upper() == "BUY" else "BUY"

    def _contra_levels(self, p: dict) -> tuple:
        """جهت مخالف با همان فاصلهٔ حد ضرر هشدار: (جهت، ورود، حد ضرر، هدف 2R، ریسک دلاری)."""
        r = p["row"]
        entry = float(r.ref_close)
        dist = abs(entry - float(p["sl"]))
        side = self.opposite_side(r.side)
        sl = entry + dist if side == "SELL" else entry - dist
        tp = entry - 2 * dist if side == "SELL" else entry + 2 * dist
        return side, entry, round(sl, 2), round(tp, 2), self._usd(dist, p.get("lot"))

    def _contra_restore(self, p: dict) -> dict:
        o = p.get("orig") or {}
        q = dict(p, row=o.get("row", p["row"]), sl=o.get("sl", p["sl"]), tp=o.get("tp", p.get("tp")), contra=False)
        q.pop("orig", None)
        return q

    def contra_ask(self, aid: str, cb: Optional[str] = None) -> None:
        p = self.pending.get(aid)
        if p is None:
            if cb:
                self.tg.ack(cb, "این هشدار دیگر معتبر نیست")
            return
        if p.get("contra"):
            p = self.pending[aid] = self._contra_restore(p)
        side, entry, sl, tp, risk = self._contra_levels(p)
        if cb:
            self.tg.ack(cb, "خلاف جهت؟ تأیید بخواهید")
        self.tg.edit(p["mid"], p["txt"] + (
            f"\n\n🔄 <b>خلاف جهت سیگنال؟</b> {side} به‌جای {p['row'].side}\n"
            f"ورود حدود {entry:.2f} · حد ضرر {sl:.2f} · هدف 2R {tp:.2f} · ریسک {risk:.2f}$\n"
            "قیمت ورود و حد ضرر مثل همیشه از لحظهٔ تأیید حساب می‌شود؛ سقف‌ها و پوزیشن خلاف جهت هم مثل همیشه پرسیده می‌شود."),
            [[{"text": f"✅ بله، {side} باز کن", "callback_data": f"v|{aid}|y"},
              {"text": "↩️ برگرد به سیگنال", "callback_data": f"v|{aid}|n"}]])
        self.log(event="contra_confirm", alert=aid, setup=p["row"].setup, side=side, price=entry, sl=sl)

    def contra_answer(self, aid: str, yes: bool, cb: str) -> None:
        p = self.pending.get(aid)
        if p is None:
            self.tg.ack(cb, "این هشدار دیگر معتبر نیست")
            return
        if not yes:
            if p.get("contra"):
                p = self.pending[aid] = self._contra_restore(p)
            self.tg.ack(cb, "برگشت به سیگنال")
            self.tg.edit(p["mid"], p["txt"], self.alert_buttons(aid, reverse_side=self.opposite_side(p["row"].side)))
            return
        if not p.get("contra"):
            side, _entry, sl, tp, _risk = self._contra_levels(p)
            row = p["row"].copy()
            row["side"] = side
            self.pending[aid] = dict(p, row=row, sl=sl, tp=tp, contra=True,
                                     orig={"row": p["row"], "sl": p["sl"], "tp": p.get("tp")})
        self.decide(aid, True, cb)

    # ------------------------------------------------------------- AI analysis
    def analyze(self, aid: str, cb: Optional[str] = None) -> None:
        """دکمهٔ «🤖 تحلیل»: نظر هوش مصنوعی دربارهٔ همین هشدار (فقط راهنما)."""
        p = self.pending.get(aid)
        if aid in self._an_cache:
            if cb:
                self.tg.ack(cb)
            self.tg.send(self._an_cache[aid], self.alert_buttons(aid, analyze=False) if p else None,
                         reply_to=p["mid"] if p else None)
            return
        if p is None:
            if cb:
                self.tg.ack(cb, "این هشدار دیگر معتبر نیست")
            return
        if aid in self._an_futs:
            if cb:
                self.tg.ack(cb, "در حال تحلیل…")
            return
        if self._ai is None:
            self._ai = scout_ai.ScoutAI.from_gate_config(self.ai_timeout) or False
        if not self._ai or not p.get("ctx"):
            if cb:
                self.tg.ack(cb, "تحلیل در دسترس نیست")
            self.tg.send("🤖 تحلیل در دسترس نیست (هوش مصنوعی تنظیم نیست). تصمیم با خودتان است.",
                         reply_to=p["mid"])
            return
        if self._an_pool is None:
            self._an_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scout-an")
        self._an_futs[aid] = self._an_pool.submit(self._ai.analyze, self.fresh_context(aid, p))
        if cb:
            self.tg.ack(cb, "در حال تحلیل… چند ثانیه")
        logger.info("scout: AI analysis requested for alert {}", aid)

    def fresh_context(self, aid: str, p: dict) -> str:
        """متن تحلیل با کندل‌ها و قیمت همین لحظه (هشدار ممکن است ۲۵ دقیقه پیش آمده باشد)."""
        try:
            r = p["row"]
            entry, sl, tp, risk, _n = self.levels(r)
            tick = self.client.get_tick()
            now_px = float(getattr(tick, "ask" if r.side == "BUY" else "bid", 0.0) or 0.0) if tick else 0.0
            sp = self.client.spread_points() or 0.0
            return scout_ai.build_entry_message(
                r.side, str(r.get("setups_all", r.setup)), entry, sl, tp, risk, float(r.atr), float(sp),
                self._frame, self.open_summary() if self.open else "",
                price_now=now_px or None, age_min=(time.time() - p["t"]) / 60.0)
        except Exception as exc:
            logger.warning("scout: fresh analysis context failed ({}); using alert-time context", exc)
            return p.get("ctx", "")

    def poll_analyses(self) -> None:
        for aid in [k for k, f in self._an_futs.items() if f.done()]:
            fut = self._an_futs.pop(aid)
            try:
                a = fut.result()
            except Exception as exc:                   # pragma: no cover
                a = scout_ai.EntryAnalysis("", 0, (), "", "", str(exc)[:120])
            p = self.pending.get(aid)
            r = p["row"] if p else None
            title = (f"🤖 <b>تحلیل هوش مصنوعی</b> · هشدار #{aid}"
                     + (f" ({'🟢 BUY' if r.side == 'BUY' else '🔴 SELL'})" if r is not None else ""))
            if a.ok:
                verdict = "✅ ورود" if a.decision == "TAKE" else "⏳ صبر / ورود نکن"
                body = "\n".join(f"• {html.escape(x)}" for x in a.reasons) or "—"
                tip = f"\n💡 {html.escape(a.tip)}" if a.tip else ""
                text = (f"{title}\nنظر: <b>{verdict}</b> · اطمینان {a.confidence}٪\n{body}{tip}\n"
                        f"<i>فقط راهنماست؛ تصمیم با شماست · {html.escape(a.model or '')}</i>")
                self._an_cache[aid] = text
                self._an_short[aid] = f"{'ورود' if a.decision == 'TAKE' else 'صبر'} · اطمینان {a.confidence}٪"
            else:
                text = (f"{title}\n⚠️ تحلیل الان در دسترس نیست ({html.escape(a.error[:80])}). "
                        "می‌توانید دوباره «🤖 تحلیل» را بزنید.")
            still = p is not None
            self.tg.send(text, self.alert_buttons(aid, analyze=not a.ok) if still else None,
                         reply_to=p["mid"] if still else None)
            self.log(event="analysis", alert=aid, setup=(r.setup if r is not None else None),
                     side=(r.side if r is not None else None),
                     note=(f"{a.decision} {a.confidence}% {a.model}" if a.ok else f"error {a.error}")[:300])

    def expire(self) -> None:
        now = time.time()
        for aid in [k for k, v in self.pending.items() if now - v["t"] > float(v.get("exp") or self.expiry)]:
            p = self.pending.pop(aid)
            if p["mid"]:
                self.tg.edit(p["mid"], p["txt"] + "\n\n⏳ <b>منقضی شد</b>")
            self.log(event="expired", alert=aid, setup=p["row"].setup, side=p["row"].side)
            self.track_whatif(aid, p, "expired")

    # ---------------------------------------------------------------- decision
    def decide(self, aid: str, yes: bool, cb: str, override: bool = False,
               hedge: Optional[str] = None) -> None:
        """hedge: None | "reverse" (پوزیشن خلاف جهت را ببند و این را باز کن) | "both" (هر دو بمانند)."""
        p = self.pending.pop(aid, None)
        if p is None:
            self.tg.ack(cb, "این هشدار دیگر معتبر نیست")
            return
        r = p["row"]
        if not yes:
            if p.get("contra"):                           # پیگیری «اگر گرفته بودید» با خود سیگنال، نه برعکسش
                p = self._contra_restore(p)
                r = p["row"]
            self.tg.ack(cb, "رد شد")
            self.tg.edit(p["mid"], p["txt"] + "\n\n❌ <b>رد شد</b>")
            self.log(event="rejected", alert=aid, setup=r.setup, side=r.side)
            self.track_whatif(aid, p, "rejected")
            return
        if self.open and not self.dry:
            if hedge == "reverse" and self.opposite_of(r.side):
                if not override and self._reverse_cap_ask(aid, p, r, cb):
                    return                                # اول سقف را بپرس، بعد چیزی ببند
                if not self._reverse(aid, p, r, cb):
                    return
            new_risk = self.new_risk_usd(r.side, float(r.ref_close), float(p["sl"]), p.get("lot"))
            block = self.entry_block(r.side, new_risk, allow_hedge=(hedge == "both"))
            is_hedge = bool(block) and block.startswith("پوزیشن خلاف")
            if is_hedge:
                # خلاف جهت: رد نکن، بپرس — ببند و جهت را عوض کن / هر دو بمانند / رد
                self._ask_hedge(aid, p, r, cb)
                return
            if block and override:
                block = None                               # مالک با دیدن هشدار سقف گفت «باز کن»
            if block:
                # سقف تعداد/ریسک: رد نکن، از مالک بپرس (تصمیم با اوست)
                total = round(self.open_risk_usd() + new_risk, 2)
                self.pending[aid] = p                      # هشدار زنده می‌ماند (همان مهلت قبلی)
                self.tg.ack(cb, "سقف پر است — تصمیم با شما")
                self.tg.edit(p["mid"], p["txt"] + (
                    f"\n\n⚠️ <b>{block}</b>.\nاگر باز کنم، با {len(self.open) + 1} پوزیشن، ضرر احتمالی کل تا "
                    f"حد ضرر فعلی‌شان می‌شود <b>{total:.2f}$</b> (سقف {self.max_total_risk:.0f}$). باز هم باز کنم؟"),
                    [[{"text": "✅ می‌دانم، باز کن", "callback_data": f"a|{aid}|{'q' if hedge == 'both' else 'o'}"},
                      {"text": "❌ رد", "callback_data": f"a|{aid}|n"}]])
                self.log(event="limit_confirm", alert=aid, setup=r.setup, side=r.side,
                         note=f"{block[:150]};total={total}")
                return
        # قیمت لحظهٔ تأیید؛ حد ضرر با همان فاصلهٔ هشدار نسبت به این قیمت
        ref = float(r.ref_close); dist = abs(ref - p["sl"])
        tick = None
        try:
            tick = self.client.get_tick()
        except Exception as exc:                       # pragma: no cover
            logger.warning("tick read failed: {}", exc)
        px = float(getattr(tick, "ask" if r.side == "BUY" else "bid", 0.0) or 0.0) if tick else 0.0
        if px <= 0:
            px = ref
        crossed = (px <= p["sl"]) if r.side == "BUY" else (px >= p["sl"])
        if crossed:
            self.tg.ack(cb, "ستاپ باطل شده")
            self.tg.edit(p["mid"], p["txt"] + f"\n\n⛔ <b>ثبت نشد — قیمت ({px:.2f}) از حد ضرر هشدار رد شده</b>")
            self.log(event="invalid", alert=aid, setup=r.setup, side=r.side, price=px, sl=round(p["sl"], 2))
            return
        sl_now = px - dist if r.side == "BUY" else px + dist
        moved = (px - ref) if r.side == "BUY" else (ref - px)
        info_line = f"ورود {px:.2f} (نسبت به هشدار {moved:+.2f}) · حد ضرر {sl_now:.2f}"
        self.tg.ack(cb, "در حال ثبت سفارش...")
        if self.dry:
            self.tg.edit(p["mid"], p["txt"] + f"\n\n🧪 <b>حالت آزمایشی — سفارشی ثبت نشد</b>\n{info_line}")
            self.log(event="approved_dry", alert=aid, setup=r.setup, side=r.side,
                     price=px, sl=round(sl_now, 2))
            return
        lot = float(p.get("lot") or self.lot)             # همان لاتی که هشدار با آن نشان داده شد
        emerg = trk.emergency_sl(r.side, px, sl_now, self.emerg_mult, self._emerg_dist(lot))
        draft = {"sl": round(sl_now, 2), "emerg": emerg, "hold": False}
        broker_sl = (round(trk.desired_broker_sl(draft, self.mode), 2)
                     if self.soft_stop else round(sl_now, 2))
        res = self.client.send_market_order(r.side, lot, sl=broker_sl,
                                            tp=None, comment=scout_comment(r.setup))
        if not res.ok and getattr(res, "uncertain", False):
            # ممکن است سفارش روی بروکر باز شده باشد: «ثبت نشد» نگوییم؛ بررسی خودکار فوری
            self._recon_t = 0.0
            self.tg.edit(p["mid"], p["txt"] + (
                f"\n\n❓ <b>نتیجهٔ سفارش نامشخص است</b> ({html.escape(str(res.comment))[:80]}). "
                "ممکن است باز شده باشد؛ اسکات چند ثانیه دیگر خودش بررسی می‌کند و اگر پیدا شد تحت نظر "
                "می‌گیرد. لطفاً در MT5 هم نگاه کنید و دوباره تأیید نکنید."))
            self.log(event="order_uncertain", alert=aid, setup=r.setup, side=r.side, note=str(res.comment)[:200])
            return
        if not res.ok:
            self.tg.edit(p["mid"], p["txt"] + f"\n\n⚠️ <b>ثبت نشد</b>: {res.comment}")
            self.log(event="order_failed", alert=aid, setup=r.setup, side=r.side, note=res.comment)
            return
        tk = self.position_ticket(res)
        self.open[tk] = {"setup": r.setup, "side": r.side, "entry": res.price,
                         "sl": round(sl_now, 2), "risk": abs(res.price - sl_now), "r1": False,
                         "opened": time.time(), "alert": aid,
                         "emerg": emerg if self.soft_stop else None,
                         "status_mid": None, "below": False, "hold": False, "warned": False,
                         "approach": False, "ai_next": 0.0, "ai_note": "", "by": self._who or None,
                         "contra": bool(p.get("contra")),
                         "lot": self._filled_lot(res, lot)}
        self._save_state()
        self.tg.edit(p["mid"], p["txt"] + (f"\n\n🔄 <b>خلاف سیگنال: {r.side} باز شد</b>" if p.get("contra")
                                           else "\n\n✅ <b>باز شد</b>") + f" #{tk} @ {res.price:.2f}\n{info_line}")
        soft_note = (f"\nحد ضرر {sl_now:.2f} · اگر برسد: <b>{trk.POLICY_FA[self.mode]}</b>"
                     f" (حالت /mode). با «نگه دار» حد ضرر به سطح اضطراری {emerg:.2f} می‌رود."
                     if self.soft_stop else "")
        more = ("هشدارهای جدید همچنان می‌آیند." if self.alert_while_open
                else "تا بسته نشود هشدار جدیدی نمی‌آید.")
        self.tg.send(f"پوزیشن #{tk} باز است. {more}{soft_note}\n"
                     f"وضعیت هر {int(self.status_every // 60)} دقیقه در یک پیام زنده به‌روز می‌شود.",
                     self._close_buttons(tk))
        self.log(event="opened", alert=aid, setup=r.setup, side=r.side, ticket=tk,
                 price=res.price, sl=round(sl_now, 2),
                 note=((f"broker_sl={broker_sl};emergency_sl={emerg};mode={self.mode}"
                        + (";over_cap=owner" if override else "")
                        + (f";hedge={hedge}" if hedge else "")
                        + (";contra=1" if p.get("contra") else "") + f";lot={lot}") if self.soft_stop else
                       ((";contra=1" if p.get("contra") else "") + f";lot={lot}")) + self._by_note())
        if not self.alert_while_open:
            self.cancel_pending("معامله‌ای باز شد")

    def cancel_pending(self, why: str) -> None:
        for aid in list(self.pending):
            q = self.pending.pop(aid)
            if q["mid"]:
                self.tg.edit(q["mid"], q["txt"] + f"\n\n⏸ <b>لغو شد — {why}</b>")
            self.log(event="cancelled", alert=aid, setup=q["row"].setup, side=q["row"].side, note=why)

    def busy(self) -> bool:
        """پوزیشن باز اسکات (در حافظه یا روی بروکر، حتی بعد از ری‌استارت)؟"""
        if self.open:
            return True
        try:
            return len(self.client.positions(magic_only=True)) > 0
        except Exception:                              # pragma: no cover
            return False

    def _side_fa(self, side: str) -> str:
        return "خرید" if side == "BUY" else "فروش"

    def _ask_hedge(self, aid: str, p: dict, r: pd.Series, cb: Optional[str]) -> None:
        opp = self.opposite_of(r.side)
        other = self._side_fa(self.open[opp[0]]["side"])
        parts = []
        for tk in opp:
            pr = (self._live.get(tk) or {}).get("profit")
            parts.append(f"#{tk}" + (f" ({pr:+.2f}$)" if pr is not None else ""))
        self.pending[aid] = p                              # هشدار زنده می‌ماند (همان مهلت قبلی)
        self.tg.ack(cb, "پوزیشن خلاف جهت باز است — تصمیم با شما")
        self.tg.edit(p["mid"], p["txt"] + (
            f"\n\n⚠️ <b>پوزیشن {other} باز است</b>: {'، '.join(parts)}. این سیگنال {self._side_fa(r.side)} است؛ "
            "خرید و فروش هم‌زمان همدیگر را خنثی می‌کنند. چه کنم؟"),
            [[{"text": f"🔄 {other} را ببند، {self._side_fa(r.side)} را باز کن", "callback_data": f"a|{aid}|r"}],
             [{"text": "✅ هر دو باز بمانند", "callback_data": f"a|{aid}|b"}],
             [{"text": "❌ رد", "callback_data": f"a|{aid}|n"}]])
        self.log(event="hedge_confirm", alert=aid, setup=r.setup, side=r.side, note=",".join(map(str, opp)))

    def _reverse_cap_ask(self, aid: str, p: dict, r: pd.Series, cb: Optional[str]) -> bool:
        """«ببند و جهت را عوض کن»: اگر بعد از بستن پوزیشن مخالف هم سقف پر است، همین الان (قبل از بستن) بپرس.
        True = سؤال فرستاده شد و هنوز چیزی بسته نشده."""
        opp = self.opposite_of(r.side)
        new_risk = self.new_risk_usd(r.side, float(r.ref_close), float(p["sl"]), p.get("lot"))
        saved = {tk: self.open[tk].get("closing") for tk in opp}
        try:
            for tk in opp:                                # فرض: مخالف‌ها بسته شده‌اند
                self.open[tk]["closing"] = True
            block = self.entry_block(r.side, new_risk, allow_hedge=True)
            total = round(self.open_risk_usd() + new_risk, 2)
            n_after = len(self.active_open()) + 1
        finally:
            for tk, v in saved.items():
                if v is None:
                    self.open[tk].pop("closing", None)
                else:
                    self.open[tk]["closing"] = v
        if not block:
            return False
        names = "، ".join(f"#{t}" for t in opp)
        self.pending[aid] = p
        if cb:
            self.tg.ack(cb, "سقف پر است — تصمیم با شما")
        self.tg.edit(p["mid"], p["txt"] + (
            f"\n\n⚠️ <b>{block}</b> (حتی بعد از بستن {names}).\nاگر {names} را ببندم و این را باز کنم، با "
            f"{n_after} پوزیشن ضرر احتمالی کل تا حد ضرر فعلی‌شان می‌شود <b>{total:.2f}$</b> "
            f"(سقف {self.max_total_risk:.0f}$). هنوز چیزی بسته نشده. ادامه بدهم؟"),
            [[{"text": f"✅ می‌دانم، {names} را ببند و باز کن", "callback_data": f"a|{aid}|x"},
              {"text": "❌ رد", "callback_data": f"a|{aid}|n"}]])
        self.log(event="limit_confirm", alert=aid, setup=r.setup, side=r.side,
                 note=f"reverse;{block[:140]};total={total}")
        return True

    def _reverse(self, aid: str, p: dict, r: pd.Series, cb: Optional[str]) -> bool:
        """پوزیشن(های) خلاف جهت را می‌بندد تا سیگنال جدید باز شود. False = چیزی باز نمی‌شود (هشدار زنده می‌ماند
        یا اگر قیمت از حد ضرر هشدار رد شده، باطل می‌شود — قبل از بستن چک می‌شود تا بی‌دلیل نبندیم)."""
        tick = None
        try:
            tick = self.client.get_tick()
        except Exception:                                  # pragma: no cover
            pass
        px = float(getattr(tick, "ask" if r.side == "BUY" else "bid", 0.0) or 0.0) if tick else 0.0
        if px > 0 and ((px <= p["sl"]) if r.side == "BUY" else (px >= p["sl"])):
            self.tg.ack(cb, "ستاپ باطل شده")
            self.tg.edit(p["mid"], p["txt"] + f"\n\n⛔ <b>ثبت نشد — قیمت ({px:.2f}) از حد ضرر هشدار رد شده</b>؛ "
                                              "پوزیشن قبلی دست نخورد.")
            self.log(event="invalid", alert=aid, setup=r.setup, side=r.side, price=px, sl=round(p["sl"], 2))
            return False
        for tk in self.opposite_of(r.side):
            info = self.open[tk]
            res = self.client.close_position(int(tk), comment="scout reverse")
            self.log(event="closed", ticket=tk, setup=info.get("setup"), side=info.get("side"),
                     price=res.price, ok=res.ok, note="reverse" + self._by_note())
            if not res.ok:
                self.pending[aid] = p
                self.tg.ack(cb, "بستن انجام نشد")
                self.tg.edit(p["mid"], p["txt"] + (
                    f"\n\n⚠️ بستن #{tk} انجام نشد ({html.escape(str(res.comment))[:80]})؛ چیزی باز نشد. "
                    "دوباره امتحان کنید:"),
                    [[{"text": f"🔄 دوباره: ببند و {self._side_fa(r.side)} را باز کن", "callback_data": f"a|{aid}|r"}],
                     [{"text": "❌ رد", "callback_data": f"a|{aid}|n"}]])
                return False
            info["closing"] = True                         # نتیجه را monitor مثل همیشه ثبت می‌کند
            info["why"] = "reverse"
            info["cby"] = self._who_plain() or None
            self.tg.send(f"🔄 #{tk} ({self._side_fa(info['side'])}) بسته شد @ {res.price:.2f} — برای باز کردن "
                         f"{self._side_fa(r.side)} هشدار #{aid}.")
        return True

    def position_ticket(self, res: Any) -> int:
        """شمارهٔ پوزیشن (نه سفارش): از جواب، وگرنه از روی deal؛ در بدترین حالت شمارهٔ سفارش
        (در حساب hedging همان است). همان روش main_live.py."""
        tk = int(getattr(res, "position", 0) or 0)
        if tk <= 0 and getattr(res, "deal", 0):
            try:
                tk = int(self.client.position_id_for_deal(int(res.deal)) or 0)
            except Exception as exc:
                logger.warning("scout: position id for deal {} not available yet ({})", res.deal, exc)
                tk = 0
        return tk if tk > 0 else int(res.order)

    def realized(self, tk: int):
        """سود واقعی و قیمت خروج از تاریخچهٔ بروکر؛ اگر هنوز نیامده None."""
        try:
            deals = self.client.deals_for_position(int(tk))
        except Exception:
            return None, None
        if not deals:
            return None, None
        pnl = sum(float(getattr(d, a, 0.0) or 0.0) for d in deals
                  for a in ("profit", "commission", "swap", "fee"))
        out = [d for d in deals if int(getattr(d, "entry", 0)) in (1, 3)]
        px = float(getattr(out[-1], "price", 0.0)) if out else None
        return (round(pnl, 2) if out else None), px

    def close(self, tk: int, cb: Optional[str] = None, why: str = "scout manual") -> None:
        now = time.time()
        if now - self._closing_t.get(int(tk), 0.0) < 20.0:     # کلیک تکراری روی «بستن»
            if cb:
                self.tg.ack(cb, "در حال بستن… یک بار کافی است")
            return
        self._closing_t[int(tk)] = now
        if int(tk) not in self.open:
            try:
                live = {int(p.ticket) for p in self.client.positions(magic_only=True)}
            except Exception:
                live = {int(tk)}
            if int(tk) not in live:
                if cb:
                    self.tg.ack(cb, "این پوزیشن قبلاً بسته شده")
                return
        try:
            res = self.client.close_position(int(tk), comment=why)
        except Exception as exc:                       # MT5 لحظه‌ای جواب نداد
            logger.warning("scout close #{} failed: {}", tk, exc)
            res = SimpleNamespace(ok=False, comment="MT5 جواب نداد؛ دوباره بزنید", price=0.0)
        if not res.ok:
            self._closing_t.pop(int(tk), None)         # ناموفق: زدن دوبارهٔ «بستن» همین الان مجاز است
        elif int(tk) in self.open:
            self.open[int(tk)]["why"] = "manual"         # برای کارت پایان معامله
            self.open[int(tk)]["cby"] = self._who_plain() or None
        if cb:
            self.tg.ack(cb, "بسته شد" if res.ok else f"خطا: {res.comment}")
        info = self.open.get(int(tk), {})
        self.tg.send(f"{'✅' if res.ok else '⚠️'} بستن #{tk}: {res.comment or 'OK'} @ {res.price:.2f}")
        self.log(event="closed", ticket=tk, setup=info.get("setup"), side=info.get("side"),
                 price=res.price, ok=res.ok, note=self._by_note().lstrip(";"))

    # -------------------------------------------------------------- monitoring
    def _close_buttons(self, tk: int, hold: Optional[bool] = None) -> list:
        """«بستن» + «نگه دار» یا «لغو نگه داشتن» بسته به وضعیت فعلی پوزیشن."""
        row = [{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]
        held = bool(self.open.get(int(tk), {}).get("hold")) if hold is None else hold
        row.append({"text": "▶️ لغو نگه داشتن", "callback_data": f"u|{tk}"} if held
                   else {"text": "⏸ نگه دار", "callback_data": f"h|{tk}"})
        if held:                                          # فقط روی پوزیشن نگه‌داشته
            return [row, [{"text": "🎯 قیمت خروج", "callback_data": f"e|{tk}"}]]
        return [row]

    def market_open(self) -> bool:
        return ctl.market_open_utc(datetime.now(timezone.utc))

    def monitor(self, f: pd.DataFrame) -> None:
        live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        now = time.time()
        mkt = self.market_open()
        ctx: Dict[str, Any] = {"server_now": None}
        for tk in list(self.open):
            try:                                          # خطای لحظه‌ای MT5 روی یک پوزیشن، بقیه را از دور خارج نکند
                self._monitor_one(tk, live, now, mkt, f, ctx)
            except Exception as exc:
                logger.exception("scout monitor #{} failed: {}", tk, exc)
                fails = self.__dict__.setdefault("_mon_fail_t", {})
                if now - fails.get(tk, 0.0) > 600 and tk in self.open:
                    fails[tk] = now                       # حداکثر هر ۱۰ دقیقه یک پیام
                    self.tg.send(f"⚠️ مدیریت #{tk} یک لحظه انجام نشد (MT5 جواب نداد: "
                                 f"{html.escape(str(exc))[:80]}). حد ضرر روی بروکر سر جایش است؛ "
                                 "چند ثانیه بعد دوباره امتحان می‌شود.", self._close_buttons(tk))

    def _monitor_one(self, tk: int, live: Dict[int, Any], now: float, mkt: bool, f: pd.DataFrame,
                     ctx: Dict[str, Any]) -> None:
        if tk not in live:                       # دستی، در حد ضرر یا اضطراری بسته شده
            pnl, px = self.realized(tk)
            if pnl is None and now - self.open[tk].get("gone_t", now) < 30:
                self.open[tk].setdefault("gone_t", now)   # تاریخچه هنوز نیامده
                return
            info = self.open.pop(tk)
            self._ai_futs.pop(tk, None)
            self._live.pop(tk, None)
            self._save_state()
            res_txt = f" · نتیجه ${pnl:+.2f}" if pnl is not None else ""
            emerg, risk = info.get("emerg"), float(info.get("risk") or 0.0)
            kind = None
            xtp, xsl = info.get("xtp"), info.get("xsl")
            if px and info.get("why") is None and (xtp or xsl):    # قیمت خروج شما روی بروکر
                tol = max(0.5, 0.25 * risk)
                near = [(abs(float(px) - float(v)), k) for k, v in (("exit_tp", xtp), ("exit_sl", xsl)) if v]
                d, k = min(near)
                if d <= tol:
                    kind = k
            elif info.get("why") in ("exit_tp", "exit_sl"):
                kind = info["why"]
            if kind is None and px and risk > 0:
                if emerg and abs(float(px) - float(emerg)) <= 0.25 * risk:
                    kind = "emergency_sl"
                elif abs(float(px) - float(info["sl"])) <= 0.25 * risk:
                    kind = "signal_sl"
            why = {"emergency_sl": "\n🛑 با حد ضرر اضطراری بسته شد.",
                   "signal_sl": "\n⛔ در حد ضرر سیگنال بسته شد.",
                   "exit_tp": f"\n🎯 به قیمت خروج شما ({float(xtp or 0):.2f}) رسید و بسته شد.",
                   "exit_sl": f"\n🎯 به قیمت خروج پایین شما ({float(xsl or 0):.2f}) رسید و بسته شد."
                   }.get(kind or "", "")
            if info.get("locked") and kind == "exit_tp":
                why = f"\n🎯 به هدف 2R ({float(xtp or 0):.2f}) رسید و بسته شد."
            elif info.get("locked") and kind == "exit_sl":
                why = f"\n🔒 به قیمت ورود ({float(xsl or 0):.2f}) برگشت و سر به سر بسته شد (بی‌ضرر)."
            self.tg.send(f"ℹ️ پوزیشن #{tk} ({info['setup']}) بسته شد{res_txt}.{why}"
                         + ("\n🔔 هشدارها دوباره فعال شد." if not self.open else ""))
            if info.get("status_mid"):
                self.tg.edit(info["status_mid"],
                             f"📊 <b>#{tk}</b> · <code>{info['setup']}</code>\n"
                             f"✅ <b>بسته شد</b>{res_txt}" + (f" @ {px:.2f}" if px else ""))
            by, cby = html.unescape(str(info.get("by") or "")), str(info.get("cby") or "")
            for ch in (";", "=", "\n", "\r", ","):
                by, cby = by.replace(ch, " "), cby.replace(ch, " ")
            extra = [f"how={info.get('why') or ''}", f"by={by.strip()[:40]}", f"cby={cby.strip()[:40]}"]
            if info.get("hold"):
                extra.append("held=1")
            if info.get("locked"):
                extra.append("locked=1")
            self.log(event="gone", alert=info.get("alert"), ticket=tk, setup=info["setup"],
                     side=info["side"], entry=round(float(info["entry"]), 2), price=px, profit=pnl,
                     note=";".join([str(kind or "")] + extra))
            self._exit_ask.pop(tk, None)
            if self.card_enabled and pnl is not None and px:
                self.send_trade_card(tk, info, pnl, float(px), kind)
            return
        p, info = live[tk], self.open[tk]
        profit = float(getattr(p, "profit", 0.0))
        swap = float(getattr(p, "swap", 0.0) or 0.0)
        price = float(getattr(p, "price_current", 0.0))
        self._live[tk] = {"price": price, "profit": profit + swap,
                          "broker_sl": float(getattr(p, "sl", 0.0) or 0.0)}
        d = trk.signed_move(info["side"], float(info["entry"]), price)
        if not info["r1"] and info["risk"] > 0 and d >= info["risk"]:
            info["r1"] = True
            self.tg.send(f"🎯 #{tk} ({info['setup']}) به ۱ برابر ریسک رسید. سود فعلی ${profit:.2f}\n"
                         "🔒 با «بی‌ضرر کن» حد ضرر به قیمت ورود و هدف به 2R می‌رود (روی بروکر).",
                         self._close_buttons(tk) + [[{"text": "🔒 بی‌ضرر کن + هدف 2R",
                                                      "callback_data": f"z|{tk}"}]])
            self.log(event="reached_1R", ticket=tk, setup=info["setup"], profit=profit)
        last = f.iloc[-1]
        broke = (info["side"] == "BUY" and last.close < last.tenkan) or \
                (info["side"] == "SELL" and last.close > last.tenkan)
        if broke and not info.get("warned"):
            info["warned"] = True
            self.tg.send(f"⚠️ #{tk} ({info['setup']}) ساختار شکست — بسته شدن کندل خلاف جهت تنکان. "
                         f"سود فعلی ${profit:.2f}", self._close_buttons(tk))
            self.log(event="structure_break", ticket=tk, setup=info["setup"], profit=profit)
        if self.soft_stop and price > 0 and mkt:     # بازار بسته: بروکر تغییر حد ضرر را رد می‌کند
            if self._manage_stop(tk, info, p, price, profit, now, f):
                return                              # همین الان بسته شد
        elif price > 0 and mkt and info.get("xdirty"):
            # soft_stop خاموش: قیمت‌های خروجی که با بازار بسته یا «لغو نگه داشتن» عوض شد، یک بار روی بروکر برود
            if self._sync_exits_plain(tk, info, p, price, profit, now):
                return
        if not mkt:                                   # یک پیام «بازار بسته» و بعد سکوت تا باز شدن
            if price > 0 and not info.get("closed_note"):
                info["closed_note"] = True
                self._push_status(tk, info, price, profit, swap, None, None, now, closed=True)
            return
        if info.pop("closed_note", None):
            info["status_t"] = 0.0                    # با باز شدن بازار گزارش فوراً ادامه پیدا کند
        if price > 0 and now - float(info.get("status_t", 0.0)) >= self.status_every:
            if ctx["server_now"] is None:
                tick = None
                try:
                    tick = self.client.get_tick()
                except Exception:                  # pragma: no cover
                    pass
                ctx["server_now"] = float(getattr(tick, "time", 0) or 0) or None
            opened_srv = float(getattr(p, "time", 0) or 0)
            secs = (ctx["server_now"] - opened_srv) if (ctx["server_now"] and opened_srv) else None
            self._push_status(tk, info, price, profit, swap, secs, ctx["server_now"], now)

    def _manage_stop(self, tk: int, info: dict, p: Any, price: float, profit: float, now: float,
                     f: Optional[pd.DataFrame]) -> bool:
        """حد ضرر بروکر را با سیاست فعلی هماهنگ می‌کند؛ True یعنی پوزیشن الان بسته شد."""
        side, soft = info["side"], float(info["sl"])
        pol = trk.effective_policy(info, self.mode)
        beyond = trk.beyond_soft_sl(side, price, soft)
        # (۱) نزدیک شدن به حد ضرر: یک بار خبر بده تا قبل از رسیدن تصمیم بگیرید
        frac = trk.adverse_fraction(side, float(info["entry"]), price, float(info["risk"]))
        if not beyond and not info.get("hold") and frac >= self.approach_frac and not info.get("approach"):
            info["approach"] = True
            self.tg.send(f"⚠️ <b>#{tk} نزدیک حد ضرر</b> ({frac * 100:.0f}٪ ریسک) · الان {price:.2f} · "
                         f"حد ضرر {soft:.2f} · زیان ${profit:+.2f}\n"
                         f"اگر برسد: <b>{trk.POLICY_FA[pol]}</b>. برای نگه داشتن «⏸ نگه دار» را بزنید.",
                         self._close_buttons(tk))
            self._save_state()
        elif info.get("approach") and frac < 0.3:
            info["approach"] = False
        # (۲) سیاست «بستن»: پوزیشن از حد مجاز ضرر رد نمی‌شود
        if pol == "close" and beyond:
            if self._close_at_stop(tk, info, price, profit, "رسیدن به حد ضرر سیگنال"):
                return True
            return False                                  # تلاش بعدی با فاصله؛ پیام با محدودیت
        # (۳) هوش مصنوعی: در حد ضرر بپرسد (هر ai_recheck یک بار تا وقتی آن طرف است)
        if pol == "ai":
            if self._ai_step(tk, info, price, profit, now, beyond, f):
                return True
        # (۴) حد ضرر بروکر = سطح درستِ همین سیاست (+ قیمت خروج بالای شما به‌عنوان حد سود)
        want = round(trk.desired_broker_sl(info, self.mode), 2)
        have = float(getattr(p, "sl", 0.0) or 0.0)
        own = bool(info.get("hold") and info.get("xsl"))
        xtp = float(info.get("xtp") or 0.0) if info.get("hold") else 0.0
        have_tp = float(getattr(p, "tp", 0.0) or 0.0)
        tp_arg: Optional[float] = None                   # None = حد سود بروکر دست نخورد
        if xtp > 0 and abs(xtp - have_tp) > 0.095:
            tp_arg = xtp
            if trk.reached(side, price, xtp, upper=True):   # مثلاً با باز شدن بازار از آن رد شده
                return self._close_at_stop(tk, info, price, profit, "رسیدن به قیمت خروج شما", code="exit_tp")
        elif xtp <= 0 and info.get("tp_owned") and have_tp > 0:
            tp_arg = 0.0                                  # حد سودی که خود اسکات گذاشته بود برداشته شود
        if (abs(want - have) > 0.095 or tp_arg is not None) and now - self._sl_fail_t.get(tk, 0.0) > 60:
            if abs(want - have) > 0.095 and trk.beyond_soft_sl(side, price, want):   # حتی همین سطح رد شده
                if own:
                    return self._close_at_stop(tk, info, price, profit, "رسیدن به قیمت خروج پایین شما",
                                               code="exit_sl")
                return self._close_at_stop(tk, info, price, profit, "عبور از حد ضرر اضطراری")
            res = (self.client.modify_sltp(tk, sl=want) if tp_arg is None
                   else self.client.modify_sltp(tk, sl=want, tp=tp_arg))
            if res.ok:
                self._sl_fail_t.pop(tk, None)
                if tp_arg is not None:
                    info["tp_owned"] = tp_arg > 0
                if info.pop("xfail", None) or tp_arg is not None:
                    self._save_state()
                logger.info("scout #{} broker SL {} -> {} ({})" + (f" TP {have_tp} -> {tp_arg}" if tp_arg is not None
                                                                     else ""), tk, have, want, pol)
            else:
                self._sl_fail_t[tk] = now
                logger.warning("scout #{} broker SL {} not set: {}", tk, want, res.comment)
                if (own or xtp > 0) and not info.get("xfail"):
                    info["xfail"] = True                  # یک بار خبر بده، نه هر دقیقه
                    self.tg.send(f"⚠️ قیمت خروج #{tk} روی بروکر گذاشته نشد: "
                                 f"{html.escape(str(res.comment))[:80]}. هر دقیقه بی‌صدا دوباره تلاش می‌شود.",
                                 self._close_buttons(tk))
        return False

    def _sync_exits_plain(self, tk: int, info: dict, p: Any, price: float, profit: float, now: float) -> bool:
        """soft_stop خاموش: فقط یک بار قیمت‌های خروج تازهٔ مالک (یا برداشتنشان) روی بروکر برود؛
        بعدش اسکات دیگر به حد ضرر/حد سود بروکر دست نمی‌زند (تغییر دستی شما در MT5 می‌ماند).
        True = همین الان بسته شد (فقط وقتی قیمت از قیمت خروج خود شما رد شده باشد)."""
        side = info["side"]
        held = bool(info.get("hold"))
        xtp = float(info.get("xtp") or 0.0) if held else 0.0
        xsl = float(info.get("xsl") or 0.0) if held else 0.0
        if xtp > 0 and trk.reached(side, price, xtp, upper=True):     # بازار با فاصله از قیمت خروج شما باز شد
            return self._close_at_stop(tk, info, price, profit, "رسیدن به قیمت خروج شما", code="exit_tp")
        if xsl > 0 and trk.beyond_soft_sl(side, price, xsl):
            return self._close_at_stop(tk, info, price, profit, "رسیدن به قیمت خروج پایین شما", code="exit_sl")
        if now - self._sl_fail_t.get(tk, 0.0) <= 60:     # بستن بالا با close_retry خودش؛ تغییر حد هر دقیقه
            return False
        have = float(getattr(p, "sl", 0.0) or 0.0)
        have_tp = float(getattr(p, "tp", 0.0) or 0.0)
        want = round(xsl or float(info["sl"]), 2)
        note = ""
        if trk.beyond_soft_sl(side, price, want):         # حد ضرر سیگنال از قیمت رد شده؛ بروکر قبول نمی‌کند
            note = (f"\nℹ️ حد ضرر سیگنال ({want:.2f}) از قیمت فعلی رد شده؛ حد ضرر بروکر همان قبلی "
                    f"({have:.2f}) ماند. اگر می‌خواهید، خودتان ببندید.")
            want = have
        tp_arg: Optional[float] = xtp if xtp > 0 else (0.0 if info.get("tp_owned") else None)
        if abs(want - have) <= 0.095 and (tp_arg is None or abs(tp_arg - have_tp) <= 0.095):
            info.pop("xdirty", None)                      # بروکر همین حالا درست است
            self._save_state()
            return False
        res = (self.client.modify_sltp(tk, sl=want) if tp_arg is None
               else self.client.modify_sltp(tk, sl=want, tp=tp_arg))
        if res.ok:
            self._sl_fail_t.pop(tk, None)
            if tp_arg is not None:
                info["tp_owned"] = tp_arg > 0
            info.pop("xdirty", None)
            info.pop("xfail", None)
            self._save_state()
            logger.info("scout #{} exits synced (plain stop): SL {} -> {} TP {} -> {}", tk, have, want, have_tp, tp_arg)
            if note:
                self.tg.send(f"#{tk}: قیمت‌های خروج روی بروکر اعمال شد." + note, self._close_buttons(tk))
        else:
            self._sl_fail_t[tk] = now                     # یک دقیقه بعد دوباره (xdirty می‌ماند)
            logger.warning("scout #{} exits not set (plain stop): {}", tk, res.comment)
            if not info.get("xfail"):
                info["xfail"] = True
                self._save_state()
                self.tg.send(f"⚠️ قیمت خروج #{tk} روی بروکر گذاشته نشد: "
                             f"{html.escape(str(res.comment))[:80]}. هر دقیقه بی‌صدا دوباره تلاش می‌شود.",
                             self._close_buttons(tk))
        return False

    def _close_at_stop(self, tk: int, info: dict, price: float, profit: float, why: str,
                       code: Optional[str] = None) -> bool:
        """True = بسته شد. تلاش ناموفق هر close_retry ثانیه بی‌صدا تکرار می‌شود و پیامش
        فقط بار اول و بعد حداکثر هر close_fail_notify_minutes می‌آید."""
        now = time.time()
        if now - self._close_try_t.get(tk, -1e18) < self.close_retry:
            return False
        self._close_try_t[tk] = now
        res = self.client.close_position(int(tk), comment="scout_sl")
        if res.ok:
            info["why"] = code or ("ai" if why.startswith("هوش مصنوعی") else "stop")
            self._close_try_t.pop(tk, None)
            self._close_note_t.pop(tk, None)
            self.log(event="sl_close", ticket=tk, setup=info["setup"], side=info["side"],
                     price=res.price or price, sl=info["sl"], profit=profit, ok=True, note=why)
            logger.info("scout #{} closed at stop ({})", tk, why)
            return True
        self._sl_fail_t[tk] = now
        logger.warning("scout #{} close at stop failed: {}", tk, res.comment)
        if now - self._close_note_t.get(tk, -1e18) >= self.close_fail_every:
            first = tk not in self._close_note_t
            self._close_note_t[tk] = now
            self.log(event="sl_close", ticket=tk, setup=info["setup"], side=info["side"],
                     price=res.price or price, sl=info["sl"], profit=profit, ok=False, note=why)
            self.tg.send(f"⚠️ بستن #{tk} در حد ضرر انجام نشد: {html.escape(str(res.comment))[:80]}. "
                         f"هر {int(self.close_retry)} ثانیه بی‌صدا دوباره تلاش می‌شود"
                         + (f" (پیام بعدی حداکثر هر {int(self.close_fail_every // 60)} دقیقه)" if first else "")
                         + "؛ حد ضرر اضطراری روی بروکر برقرار است.", self._close_buttons(tk))
        return False

    def _ai_step(self, tk: int, info: dict, price: float, profit: float, now: float,
                 beyond: bool, f: Optional[pd.DataFrame]) -> bool:
        fut = self._ai_futs.get(tk)
        if fut is not None:
            if not fut.done():
                return False
            self._ai_futs.pop(tk, None)
            try:
                v = fut.result()
            except Exception as exc:                   # pragma: no cover
                v = scout_ai.AIVerdict("CLOSE", 0, "", "", str(exc)[:120])
            hold = v.hold and v.confidence >= self.ai_min_conf
            self.log(event="ai_hold" if hold else "ai_close", ticket=tk, setup=info["setup"],
                     side=info["side"], price=price, profit=profit,
                     note=f"{v.decision} {v.confidence}% {v.model} {v.error or v.reason}"[:300])
            if hold:
                info["ai_next"] = now + self.ai_recheck
                info["ai_note"] = f"نگه داشت ({v.confidence}٪): {v.reason}"
                self._save_state()
                self.tg.send(f"🤖 <b>#{tk} در حد ضرر — هوش مصنوعی نگه داشت</b> ({v.confidence}٪)\n"
                             f"{html.escape(v.reason)}\nدوباره {int(self.ai_recheck // 60)} دقیقه دیگر "
                             f"بررسی می‌کند · حد ضرر اضطراری {float(info['emerg']):.2f}",
                             self._close_buttons(tk))
                return False
            why = (f"هوش مصنوعی گفت ببند ({v.confidence}٪): {v.reason}" if not v.error
                   else f"هوش مصنوعی جواب نداد ({v.error[:60]}) — طبق قاعده بسته شد")
            if self._close_at_stop(tk, info, price, profit, why):
                self.tg.send(f"🤖 <b>#{tk} بسته شد</b> · {html.escape(why)}")
                return True
            return False
        if beyond and now >= float(info.get("ai_next") or 0.0):
            if self._ai is None:
                self._ai = scout_ai.ScoutAI.from_gate_config(self.ai_timeout) or False
            if not self._ai:
                if not info.get("noai_sent"):            # یک بار، نه هر حلقه
                    info["noai_sent"] = True
                    self.tg.send(f"🤖 #{tk}: هوش مصنوعی تنظیم نیست — طبق قاعده در حد ضرر بسته می‌شود.")
                return self._close_at_stop(tk, info, price, profit, "هوش مصنوعی در دسترس نیست")
            if self._ai_pool is None:
                self._ai_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-ai")
            msg = scout_ai.build_message(info, price, profit, f)
            self._ai_futs[tk] = self._ai_pool.submit(self._ai.decide, msg)
            info["ai_next"] = now + self.ai_recheck
            logger.info("scout #{} at stop: asking AI", tk)
        return False

    def _push_status(self, tk: int, info: dict, price: float, profit: float, swap: float,
                     secs: Optional[float], server_now: Optional[float], now: float,
                     closed: bool = False) -> None:
        info["status_t"] = now
        text = trk.status_text(tk, info, price, profit, swap, secs, server_now, self.mode)
        if closed:
            text += "\n🔒 <b>بازار بسته است</b> — گزارش با باز شدن بازار ادامه پیدا می‌کند."
        buttons = self._close_buttons(tk)
        mid = info.get("status_mid")
        # فقط وقتی آخرین پیام چت است ویرایش شود؛ وگرنه پیام تازه پایین چت (با دکمه‌ها) و حذف قبلی
        if mid and getattr(self.tg, "last_mid", None) == mid and self.tg.edit(mid, text, buttons):
            return
        new_mid = self.tg.send(text, buttons)
        if new_mid:
            if mid and mid != new_mid:
                try:
                    self.tg.delete(mid)
                except Exception:                      # pragma: no cover
                    pass
            info["status_mid"] = new_mid
            self._save_state()

    def hold(self, tk: int, cb: Optional[str] = None, on: bool = True) -> None:
        info = self.open.get(int(tk))
        if info is None:
            if cb:
                self.tg.ack(cb, "این پوزیشن دیگر باز نیست")
            return
        info["hold"] = bool(on)
        info["status_t"] = 0.0                            # پیام وضعیت همین الان به‌روز شود
        had_exits = bool(info.get("xtp") or info.get("xsl"))
        if not on:                                        # لغو نگه داشتن = قیمت‌های خروج هم برداشته شود
            info["xtp"] = info["xsl"] = info["xprompt"] = None
            info["locked"] = False
            self._exit_ask.pop(int(tk), None)
            if had_exits and not self.soft_stop:
                info["xdirty"] = True                     # تیک بعدی مانیتور روی بروکر هم برمی‌دارد
        self._save_state()
        if cb:
            self.tg.ack(cb, "نگه داشته شد" if on else "نگه داشتن لغو شد")
        if on:
            self.tg.send(f"⏸ #{tk} نگه داشته شد تا خودتان بگویید. حد ضرر بروکر به سطح اضطراری "
                         f"{float(info.get('emerg') or info['sl']):.2f} می‌رود؛ در حد ضرر سیگنال "
                         "دیگر هشدار تکراری نمی‌آید و گزارش ۵ دقیقه‌ای ادامه دارد.\n"
                         "🎯 اگر نمی‌توانید مدام سر بزنید، با «🎯 قیمت خروج» بگویید در چه قیمتی ببندم.",
                         self._close_buttons(tk))
        else:
            self.tg.send(f"▶️ #{tk}: نگه داشتن لغو شد · از این به بعد در حد ضرر: "
                         f"<b>{trk.POLICY_FA[self.mode]}</b>."
                         + ("\n🎯 قیمت‌های خروج شما هم حذف شد." if had_exits else ""), self._close_buttons(tk))
        self.log(event="hold" if on else "unhold", ticket=tk, setup=info["setup"], side=info["side"],
                 note=self._by_note().lstrip(";"))

    # -------------------------------------------------- قیمت خروج پوزیشن نگه‌داشته
    def _close_px(self, tk: int, side: str) -> float:
        """قیمتی که پوزیشن با آن بسته می‌شود: BUY با bid، SELL با ask."""
        tick = None
        try:
            tick = self.client.get_tick()
        except Exception:                              # pragma: no cover
            pass
        px = float(getattr(tick, "bid" if side == "BUY" else "ask", 0.0) or 0.0) if tick else 0.0
        return px or float((self._live.get(int(tk)) or {}).get("price") or 0.0)

    def _level_usd(self, info: dict, level: float) -> float:
        """سود (+) یا ضرر (−) تقریبی اگر در این قیمت بسته شود."""
        return round(trk.signed_move(info["side"], float(info["entry"]), float(level)) * 100.0
                     * float(info.get("lot") or self.lot), 2)

    def _exit_lines(self, info: dict) -> str:
        tp, sl = info.get("xtp"), info.get("xsl")
        up = (f"⬆️ اگر به <b>{float(tp):.2f}</b> رسید ← بستن (حدود {self._level_usd(info, tp):+.2f}$)" if tp
              else "⬆️ بالا: ندارد (حد سودی گذاشته نشده)")
        if sl:
            down = f"⬇️ اگر به <b>{float(sl):.2f}</b> رسید ← بستن (حدود {self._level_usd(info, sl):+.2f}$)"
        else:
            em = float(info.get("emerg") or info["sl"])
            down = f"⬇️ پایین: حد ضرر اضطراری {em:.2f} (حدود {self._level_usd(info, em):+.2f}$)"
        return up + "\n" + down

    def _exit_buttons(self, tk: int) -> list:
        return [[{"text": "🎯 تغییر", "callback_data": f"e|{tk}"},
                 {"text": "🗑 حذف قیمت‌های خروج", "callback_data": f"e|{tk}|d"}]]

    def exit_prompt(self, tk: int, cb: Optional[str] = None) -> None:
        info = self.open.get(int(tk))
        if info is None or not info.get("hold"):
            if cb:
                self.tg.ack(cb, "این پوزیشن باز نیست" if info is None else "اول «⏸ نگه دار» را بزنید")
            return
        if cb:
            self.tg.ack(cb)
        px = self._close_px(tk, info["side"])
        now_txt = f" · الان {px:.2f}" if px else ""
        mid = self.tg.send(
            f"🎯 <b>قیمت خروج #{tk}</b> · {'🟢 BUY' if info['side'] == 'BUY' else '🔴 SELL'} از "
            f"{float(info['entry']):.2f}{now_txt}\n"
            "روی <b>همین پیام ریپلای</b> کنید و عدد بالا و/یا پایین را بنویسید، مثل: <code>4120 4095</code>\n"
            "⬆️ عدد " + ("بالاتر" if info["side"] == "BUY" else "پایین‌تر") + " از قیمت فعلی = اگر برگشت، آنجا ببند\n"
            "⬇️ عدد " + ("پایین‌تر" if info["side"] == "BUY" else "بالاتر") + " = اگر بدتر شد، آنجا ببند\n"
            "یک عدد هم کافی است. هر دو روی بروکر گذاشته می‌شوند و حتی اگر VPS خاموش باشد کار می‌کنند.",
            keyboard={"force_reply": True, "input_field_placeholder": "4120 4095"})
        if mid:
            info["xprompt"] = mid
            self._save_state()

    def exit_for_reply(self, reply_mid: Any) -> Optional[int]:
        try:
            rid = int(reply_mid)
        except (TypeError, ValueError):
            return None
        for tk, info in self.open.items():
            if info.get("xprompt") and int(info["xprompt"]) == rid:
                return tk
        return None

    def exit_reply(self, tk: int, text: str) -> None:
        info = self.open.get(int(tk))
        if info is None or not info.get("hold"):
            self.tg.send(f"ℹ️ #{tk} دیگر باز یا نگه‌داشته نیست؛ قیمت خروجی ثبت نشد.")
            return
        px = self._close_px(tk, info["side"])
        if px <= 0:
            self.tg.send("⚠️ قیمت از MT5 نرسید؛ چند ثانیه بعد دوباره روی همان پیام ریپلای کنید.")
            return
        try:
            tp, sl = trk.classify_exits(info["side"], px, trk.parse_numbers(text))
        except ValueError as exc:
            self.tg.send(f"⚠️ {html.escape(str(exc))} (الان {px:.2f}). دوباره روی همان پیام سؤال ریپلای کنید.")
            return
        cap_usd = float(getattr(self, "emerg_cap_usd", None) or (self.emerg_cap or 0.0) * 100.0 * self.lot)
        loss = -self._level_usd(info, sl) if sl else 0.0
        if sl and cap_usd > 0 and loss > cap_usd + 0.01:
            self._exit_ask[int(tk)] = (tp, sl)
            self.tg.send(f"⚠️ <b>#{tk}: با {sl:.2f} تا {loss:.2f}$ ضرر ممکن است</b>، بیشتر از سقف {cap_usd:.0f}$. "
                         "مطمئنید؟",
                         [[{"text": "✅ بله، بگذار", "callback_data": f"e|{tk}|y"},
                           {"text": "❌ نه", "callback_data": f"e|{tk}|n"}]])
            self.log(event="exit_confirm", ticket=tk, setup=info["setup"], side=info["side"], price=px,
                     sl=sl, note=f"tp={tp};loss={loss};cap={cap_usd}")
            return
        self._apply_exits(int(tk), tp, sl)

    def exit_confirm(self, tk: int, yes: bool, cb: Optional[str] = None, mid: Optional[int] = None) -> None:
        ask = self._exit_ask.pop(int(tk), None)
        if ask is None or int(tk) not in self.open:
            if cb:
                self.tg.ack(cb, "این سؤال دیگر معتبر نیست")
            if mid:
                self.tg.edit(int(mid), f"⌛ سؤال قیمت خروج #{tk} — دیگر معتبر نیست.")
            return
        if cb:
            self.tg.ack(cb, "ثبت می‌شود" if yes else "لغو شد")
        if mid:                                           # سؤال بدون دکمه، با جوابی که داده شد
            self.tg.edit(int(mid), f"⚠️ #{tk}: قیمت خروج پایین {float(ask[1]):.2f} (بیشتر از سقف) — "
                                   + ("✅ <b>تأیید شد</b>" if yes else "❌ <b>نه</b>") + self._by())
        if not yes:
            self.tg.send(f"❌ #{tk}: قیمت خروج عوض نشد.\n" + self._exit_lines(self.open[int(tk)]))
            return
        self._apply_exits(int(tk), ask[0], ask[1], over_cap=True)

    def exit_delete(self, tk: int, cb: Optional[str] = None) -> None:
        info = self.open.get(int(tk))
        if info is None or not (info.get("xtp") or info.get("xsl")):
            if cb:
                self.tg.ack(cb, "قیمت خروجی ثبت نیست")
            return
        if cb:
            self.tg.ack(cb, "حذف می‌شود")
        self._apply_exits(int(tk), None, None)

    def _apply_exits(self, tk: int, tp: Optional[float], sl: Optional[float], over_cap: bool = False,
                     lock: bool = False) -> bool:
        """True = ثبت شد (یا با بسته بودن بازار ذخیره شد)؛ False = بروکر قبول نکرد و چیزی عوض نشد."""
        info = self.open[tk]
        prev = (info.get("xtp"), info.get("xsl"), info.get("locked"))
        info["xtp"], info["xsl"], info["xprompt"] = tp, sl, None
        info["locked"] = bool(lock)
        info.pop("xfail", None)
        info["status_t"] = 0.0
        deleted = tp is None and sl is None
        live = False
        if self.market_open() and not self.dry:
            want = round(trk.desired_broker_sl(info, self.mode), 2)
            tp_arg = tp if tp else (0.0 if info.get("tp_owned") else None)
            res = (self.client.modify_sltp(tk, sl=want) if tp_arg is None
                   else self.client.modify_sltp(tk, sl=want, tp=tp_arg))
            if not res.ok:
                info["xtp"], info["xsl"], info["locked"] = prev
                self._save_state()
                self.tg.send(f"⚠️ <b>#{tk}: بروکر قبول نکرد</b> ({html.escape(str(res.comment))[:80]}). "
                             "چیزی عوض نشد؛ شاید عدد خیلی به قیمت فعلی نزدیک است. دوباره امتحان کنید.",
                             [[{"text": "🎯 دوباره", "callback_data": f"e|{tk}"}]])
                self.log(event="exit_failed", ticket=tk, setup=info["setup"], side=info["side"],
                         sl=sl, note=f"tp={tp};{str(res.comment)[:120]}" + (";lock" if lock else ""))
                return False
            if tp_arg is not None:
                info["tp_owned"] = tp_arg > 0
            self._sl_fail_t.pop(tk, None)
            info.pop("xdirty", None)
            live = True
        elif not self.soft_stop:
            info["xdirty"] = True                         # با باز شدن بازار روی بروکر می‌رود
        self._save_state()
        if deleted:
            em = float(info.get("emerg") or info["sl"])
            head = (f"🗑 <b>#{tk}: قیمت‌های خروج حذف شد</b> · حد ضرر اضطراری {em:.2f}، بدون حد سود."
                    + ("" if live else "\n🔒 بازار بسته است؛ با باز شدن بازار روی بروکر هم اعمال می‌شود."))
            self.tg.send(head, self._close_buttons(tk))
        elif lock:
            head = (f"🔒 <b>#{tk} بی‌ضرر شد</b> · حد ضرر {float(sl):.2f} (قیمت ورود) · هدف 2R {float(tp):.2f} "
                    f"(حدود {self._level_usd(info, tp):+.2f}$)")
            if not live:
                head += "\n🔒 بازار بسته است؛ با باز شدن بازار روی بروکر می‌رود."
            self.tg.send(head + "\nاز این به بعد یا به هدف می‌رسد یا سر به سر بسته می‌شود؛ هر دو روی بروکر.",
                         self._exit_buttons(tk))
        else:
            head = (f"✅ <b>ثبت شد روی بروکر</b> · #{tk}" if live else
                    f"🔒 <b>بازار بسته است</b> — #{tk} ذخیره شد و با باز شدن بازار خودکار روی بروکر می‌رود.")
            self.tg.send(head + "\n" + self._exit_lines(info) + "\nهر کدام زودتر برسد، بسته می‌شود و خبرتان می‌کنم.",
                         self._exit_buttons(tk))
        self.log(event="lock_profit" if lock else ("exit_del" if deleted else "exit_set"), ticket=tk,
                 setup=info["setup"], side=info["side"], sl=sl,
                 note=f"tp={tp};live={live}" + (";over_cap=owner" if over_cap else ""))
        return True

    def lock_profit(self, tk: int, cb: Optional[str] = None) -> None:
        """«🔒 بی‌ضرر کن + هدف 2R»: حد ضرر روی قیمت ورود، حد سود روی 2R — هر دو روی بروکر (با سازوکار قیمت خروج)."""
        info = self.open.get(int(tk))
        if info is None:
            if cb:
                self.tg.ack(cb, "این پوزیشن دیگر باز نیست")
            return
        side, entry, risk = info["side"], float(info["entry"]), float(info.get("risk") or 0.0)
        if risk <= 0:
            if cb:
                self.tg.ack(cb, "ریسک این پوزیشن معلوم نیست؛ از «🎯 قیمت خروج» استفاده کنید")
            return
        tp = round(entry + 2 * risk if side == "BUY" else entry - 2 * risk, 2)
        sl = round(entry, 2)
        px = self._close_px(tk, side)
        if px <= 0:
            if cb:
                self.tg.ack(cb, "قیمت از MT5 نرسید؛ چند ثانیه بعد دوباره بزنید")
            return
        if not trk.reached(side, px, sl, upper=True) or abs(px - sl) < 0.01:
            if cb:
                self.tg.ack(cb, "قیمت به ورود برگشته")
            self.tg.send(f"⚠️ #{tk}: الان نمی‌شود بی‌ضرر کرد — قیمت ({px:.2f}) "
                         f"{'زیر' if side == 'BUY' else 'بالای'} قیمت ورود ({entry:.2f}) است. چیزی عوض نشد.",
                         self._close_buttons(tk))
            return
        if trk.reached(side, px, tp, upper=True):
            if cb:
                self.tg.ack(cb, "از هدف رد شده")
            self.tg.send(f"🎯 #{tk}: قیمت ({px:.2f}) همین الان از هدف 2R ({tp:.2f}) رد شده؛ "
                         "اگر می‌خواهید سود را بگیرید، ببندید.", [[{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]])
            return
        if cb:
            self.tg.ack(cb, "بی‌ضرر می‌شود")
        was_hold = bool(info.get("hold"))
        info["hold"] = True                               # قیمت‌های خروج فقط روی پوزیشن نگه‌داشته معنا دارند
        self._exit_ask.pop(int(tk), None)
        if not self._apply_exits(int(tk), tp, sl, lock=True):
            info["hold"] = was_hold                       # بروکر قبول نکرد: همه‌چیز مثل قبل
            self._save_state()

    # ------------------------------------------------------------------- mode
    def mode_menu(self) -> None:
        cur = trk.POLICY_FA[self.mode]
        self.tg.send(
            f"⚙️ <b>وقتی گرفتارم، در حد ضرر چه شود؟</b>\nالان: <b>{cur}</b>\n"
            "• ببند: پوزیشن از حد ضرر رد نمی‌شود (پیش‌فرض)\n"
            "• نگه دار: تا خودتان بگویید باز می‌ماند (فقط حد ضرر اضطراری)\n"
            "• هوش مصنوعی: در حد ضرر می‌پرسد؛ اگر جواب ندهد یا مطمئن نباشد، می‌بندد\n"
            "«نگه دار» روی هر پوزیشن همیشه بر این حالت مقدم است.",
            [[{"text": "⛔ ببند", "callback_data": "m|close"},
              {"text": "⏸ نگه دار", "callback_data": "m|hold"},
              {"text": "🤖 هوش مصنوعی", "callback_data": "m|ai"}]])

    def set_mode(self, mode: str, cb: Optional[str] = None, mid: Optional[int] = None) -> None:
        if mode not in trk.POLICIES:
            return
        self.mode = mode
        try:
            trk.save_mode(self.mode_path, mode)
        except Exception as exc:                       # pragma: no cover
            logger.warning("scout mode save failed: {}", exc)
        for info in self.open.values():
            info["status_t"] = 0.0
            info["ai_next"] = 0.0
        if cb:
            self.tg.ack(cb, trk.POLICY_FA[mode])
        if mid:                                           # منوی انتخاب بدون دکمه (کلیک دوباره ممکن نیست)
            self.tg.edit(mid, f"⚙️ حالت حد ضرر: <b>{trk.POLICY_FA[mode]}</b> ✅" + self._by())
        self.tg.send(f"⚙️ حالت ثبت شد: در حد ضرر <b>{trk.POLICY_FA[mode]}</b>."
                     + (f" حد ضرر {len(self.open)} پوزیشن باز هماهنگ می‌شود." if self.open else ""))
        self.log(event="mode", note=mode)

    def _by(self) -> str:
        who = getattr(self, "_who", "")
        return f" · {who}" if who else ""

    # ------------------------------------------------------------------ state
    def _save_state(self) -> None:
        try:
            trk.save_state(self.state_path, self.open)
        except Exception as exc:                       # pragma: no cover
            logger.warning("scout state save failed: {}", exc)

    def _cap_dist(self, usd: Optional[float], lot: Optional[float]) -> Optional[float]:
        """سقف دلاری → فاصلهٔ قیمت برای لات همان پوزیشن."""
        if not usd or float(usd) <= 0:
            return None
        return float(usd) / (100.0 * float(lot or self.lot))

    def _apply_caps(self, info: dict) -> None:
        """سقف‌های جدید روی پوزیشن‌های قدیمی هم اعمال شود (با لات خود پوزیشن)."""
        side, entry = info["side"], float(info["entry"])
        lot = info.get("lot")
        sl_cap = self._cap_dist(self._cap_usd("sl"), lot) if lot else self.sl_cap
        em_cap = self._cap_dist(self._cap_usd("emerg"), lot) if lot else self.emerg_cap
        info["sl"] = trk.capped_sl(side, entry, float(info["sl"]), sl_cap)
        info["risk"] = abs(entry - float(info["sl"]))
        info["emerg"] = trk.emergency_sl(side, entry, float(info["sl"]), self.emerg_mult, em_cap)

    def restore(self) -> None:
        """بعد از ری‌استارت: پوزیشن‌های باز اسکات دوباره دنبال می‌شوند (با سقف‌های فعلی)."""
        saved = trk.load_state(self.state_path)
        try:
            live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        except Exception as exc:
            logger.warning("scout restore: positions unavailable ({}); keeping saved state", exc)
            live = {}
        for tk, info in saved.items():                 # بسته‌شده‌ها هم برگردند تا نتیجه ثبت شود
            info.setdefault("r1", False)
            info.setdefault("warned", False)
            info.setdefault("status_mid", None)
            info.setdefault("approach", False)
            info.setdefault("ai_next", 0.0)
            info.setdefault("ai_note", "")
            info["hold"] = bool(info.get("hold"))
            if not info.get("lot"):                    # پوزیشن قدیمی (قبل از تنظیمات تلگرام): مثل قبل سقف‌ها
                lp = live.get(tk)
                info["lot"] = float(getattr(lp, "volume", 0) or 0) or self.lot
                self._apply_caps(info)
            self.open[tk] = info
        for tk, p in live.items():
            if tk not in self.open:
                self._adopt(tk, p, "🔁 پوزیشن #{tk} ({side} @ {entry:.2f}) دوباره تحت نظر است.")
        if self.open:
            for info in self.open.values():
                info["status_t"] = 0.0                   # پیام وضعیت تازه با دکمه‌ها همین الان
            self._save_state()
            logger.info("scout tracking {} open position(s) | mode={}", len(self.open), self.mode)

    def _adopt(self, tk: int, p: Any, head: str, note: Optional[str] = None) -> None:
        side = "BUY" if int(getattr(p, "type", 0)) == 0 else "SELL"
        entry = float(p.price_open)
        soft = float(getattr(p, "sl", 0.0) or 0.0)
        if soft <= 0:                              # بدون حد ضرر: سقف دلاری یا ۱٪ قیمت
            soft = round(entry * (0.99 if side == "BUY" else 1.01), 2)
        info = {"setup": str(getattr(p, "comment", "") or "scout"), "side": side, "entry": entry,
                "sl": soft, "risk": 0.0, "r1": False, "warned": False, "opened": time.time(),
                "alert": None, "status_mid": None, "below": False, "hold": False,
                "adopted": True, "emerg": None, "approach": False, "ai_next": 0.0, "ai_note": "",
                "lot": float(getattr(p, "volume", 0) or 0) or self.lot}
        self._apply_caps(info)
        self.open[tk] = info
        self.log(event="adopted", ticket=tk, setup=info["setup"], side=side, entry=entry,
                 sl=info["sl"], note=note or f"emergency_sl={info['emerg']}")
        self.tg.send(head.format(tk=tk, side=side, entry=entry) + "\n"
                     f"حد ضرر {info['sl']:.2f} · اضطراری {info['emerg']:.2f} · در حد ضرر: "
                     f"<b>{trk.POLICY_FA[self.mode]}</b>", self._close_buttons(tk))

    def reconcile(self, now: Optional[float] = None) -> None:
        """هر reconcile_seconds: پوزیشن‌های باز با مجیک اسکات که در فهرست نیستند (مثلاً سفارش با
        نتیجهٔ نامشخص) تحت نظر گرفته می‌شوند تا مدیریت شوند و در سقف ریسک حساب شوند."""
        if self.dry:
            return
        now = time.time() if now is None else now
        if now - self._recon_t < self.recon_every:
            return
        self._recon_t = now
        try:
            live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        except Exception as exc:
            logger.warning("scout reconcile: positions unavailable ({})", exc)
            return
        found = [tk for tk in live if tk not in self.open]
        for tk in found:
            self._adopt(tk, live[tk], "🔎 پوزیشن #{tk} ({side} @ {entry:.2f}) روی بروکر باز بود ولی اسکات "
                        "دنبالش نمی‌کرد (احتمالاً سفارش با نتیجهٔ نامشخص). از این به بعد تحت نظر است.",
                        note="reconcile")
        if found:
            self._save_state()

    # ------------------------------------------------------------- control panel
    MENU_BUTTONS = [
        [{"text": "💰 قیمت", "callback_data": "k|price"}, {"text": "📊 وضعیت", "callback_data": "k|status"}],
        [{"text": "📋 پوزیشن‌ها", "callback_data": "k|pos"}, {"text": "⚙️ حالت حد ضرر", "callback_data": "k|mode"}],
        [{"text": "⏸ توقف هشدارها", "callback_data": "k|pause"},
         {"text": "▶️ ادامهٔ هشدارها", "callback_data": "k|resume"}],
        [{"text": "🔔 هشدار قیمت", "callback_data": "k|palert"},
         {"text": "🏁 جدول امتیاز", "callback_data": "k|score"}],
        [{"text": "🟢 خرید دستی", "callback_data": "k|buy"}, {"text": "🔴 فروش دستی", "callback_data": "k|sell"}],
        [{"text": "🛠 تنظیمات", "callback_data": "k|settings"}],
        [{"text": "🔄 ری‌استارت", "callback_data": "k|restart"},
         {"text": "❓ راهنما", "callback_data": "k|help"}],
    ]

    # دکمه‌های ثابت پایین صفحهٔ تلگرام — همیشه در دسترس، بدون نیاز به دانستن دستورها
    KEYBOARD = {"keyboard": [[{"text": "📋 پوزیشن‌ها"}, {"text": "💰 قیمت"}],
                             [{"text": "📊 وضعیت"}, {"text": "🎛 منو"}],
                             [{"text": "❓ راهنما"}]],
                "is_persistent": True, "resize_keyboard": True}
    KEYBOARD_ACTIONS = {"📋 پوزیشن‌ها": "pos", "💰 قیمت": "price", "📊 وضعیت": "status", "🎛 منو": "menu",
                        "❓ راهنما": "help"}

    def is_admin(self, u: dict) -> bool:
        if not self.admins:                            # بدون admin_ids هیچ‌کس، نه همه
            return False
        cq = u.get("callback_query") or {}
        who = (cq.get("from") or {}).get("id") or ((u.get("message") or {}).get("from") or {}).get("id")
        try:
            return int(who) in self.admins
        except (TypeError, ValueError):
            return False

    def _stale_alert_tap(self, aid: str, cq_mid: Optional[int], cb: str) -> bool:
        """شمارهٔ هشدار بعد از ری‌استارت از ۱ شروع می‌شود؛ دکمهٔ پیام قدیمی نباید هشدار جدیدِ هم‌شماره را باز کند.
        فقط وقتی پیامی که دکمه‌اش زده شد همان پیام هشدار فعلی باشد، دکمه کار می‌کند."""
        p = self.pending.get(aid)
        if p is None or cq_mid is None or cq_mid == p.get("mid"):
            return False
        self.tg.ack(cb, "این دکمه مال یک هشدار قدیمی است (قبل از ری‌استارت) و دیگر معتبر نیست")
        logger.warning("scout: stale alert button ignored (alert {} msg {} != {})", aid, cq_mid, p.get("mid"))
        self.log(event="stale_tap", alert=aid, note=f"msg={cq_mid};current={p.get('mid')}")
        return True

    def handle_update(self, u: dict) -> None:
        if not self.is_admin(u):
            logger.warning("scout: ignored telegram update from non-admin")
            return
        cq = u.get("callback_query")
        who = (cq or u.get("message") or {}).get("from") or {}
        self._who = html.escape(str(who.get("first_name") or who.get("username") or ""))[:40]
        if cq:
            cq_mid = (cq.get("message") or {}).get("message_id")     # پیامی که دکمه‌اش زده شد
            parts = str(cq.get("data", "")).split("|")
            if parts[0] in ("a", "v", "x") and len(parts) >= 2 and self._stale_alert_tap(parts[1], cq_mid, cq["id"]):
                return
            if parts[0] == "a" and len(parts) == 3:
                code = parts[2]
                self.decide(parts[1], code in ("y", "o", "r", "b", "q", "x"), cq["id"],
                            override=code in ("o", "q", "x"),
                            hedge={"r": "reverse", "x": "reverse", "b": "both", "q": "both"}.get(code))
            elif parts[0] == "c" and len(parts) == 2:
                self.close(int(parts[1]), cq["id"])
            elif parts[0] == "h" and len(parts) == 2:
                self.hold(int(parts[1]), cq["id"])
            elif parts[0] == "u" and len(parts) == 2:
                self.hold(int(parts[1]), cq["id"], on=False)
            elif parts[0] == "v" and len(parts) == 2:
                self.contra_ask(parts[1], cq["id"])
            elif parts[0] == "v" and len(parts) == 3 and parts[2] in ("y", "n"):
                self.contra_answer(parts[1], parts[2] == "y", cq["id"])
            elif parts[0] == "z" and len(parts) == 2 and parts[1].isdigit():
                self.lock_profit(int(parts[1]), cq["id"])
            elif parts[0] == "e" and len(parts) == 2 and parts[1].isdigit():
                self.exit_prompt(int(parts[1]), cq["id"])
            elif parts[0] == "e" and len(parts) == 3 and parts[1].isdigit():
                if parts[2] == "d":
                    self.exit_delete(int(parts[1]), cq["id"])
                elif parts[2] in ("y", "n"):
                    self.exit_confirm(int(parts[1]), parts[2] == "y", cq["id"], mid=cq_mid)
            elif parts[0] == "m" and len(parts) == 2:
                self.set_mode(parts[1], cq["id"], mid=cq_mid)
            elif parts[0] == "x" and len(parts) == 2:
                self.analyze(parts[1], cq["id"])
            elif parts[0] == "p" and len(parts) == 2:
                self.price_alert_delete(parts[1], cq["id"])
            elif parts[0] == "g" and len(parts) >= 2:
                self.settings_tap(parts[1:], cq["id"], cq_mid)
            elif parts[0] == "k" and len(parts) == 2 and parts[1] in ("restart_yes", "restart_no"):
                self.restart_answer(parts[1] == "restart_yes", cq["id"], cq_mid)
            elif parts[0] == "k" and len(parts) == 2:
                self.tg.ack(cq["id"])
                self.panel(parts[1])
            return
        msg = str((u.get("message") or {}).get("text", "") or "").strip()
        reply_to = ((u.get("message") or {}).get("reply_to_message") or {}).get("message_id")
        if reply_to and msg and not msg.startswith("/"):
            tk = self.exit_for_reply(reply_to)
            if tk is not None:                            # جواب سؤال «🎯 قیمت خروج»
                self.exit_reply(tk, msg)
                return
        if msg in self.KEYBOARD_ACTIONS:
            self.panel(self.KEYBOARD_ACTIONS[msg])
            return
        cmd = msg.split()[0].split("@")[0].lower() if msg else ""
        if cmd in ("/start", "/menu"):
            self.panel("menu")
        elif cmd in ("/mode", "/busy"):
            self.mode_menu()
        elif cmd in ("/help", "/guide"):
            self.panel("help")
        elif cmd in ("/price", "/status", "/positions", "/pause", "/resume", "/restart"):
            self.panel({"/positions": "pos"}.get(cmd, cmd[1:]))
        elif cmd == "/settings":
            self.settings_show()
        elif cmd in ("/buy", "/sell"):
            self.manual_start(cmd[1:].upper(), extra=" ".join(msg.split()[1:]))
        elif cmd == "/alert":
            self.price_alert_cmd(msg)
        elif cmd == "/alerts":
            self.price_alerts_list()
        elif cmd == "/close":
            bits = msg.split()
            if len(bits) > 1 and bits[1].isdigit():
                self.close(int(bits[1]))

    # ------------------------------------------------------- تنظیمات از تلگرام
    def _cap_usd(self, which: str) -> float:
        """سقف دلاری فعلی (sl / emerg)."""
        usd = getattr(self, "sl_cap_usd" if which == "sl" else "emerg_cap_usd", None)
        if usd is not None:
            return float(usd or 0.0)
        dist = self.sl_cap if which == "sl" else self.emerg_cap
        return float(dist or 0.0) * 100.0 * self.lot

    def _settings_note(self) -> str:
        """یک خط برای پیام روشن شدن: تنظیمات تلگرام فعال / خطای فایل."""
        warn = getattr(self, "_settings_warn", None)
        if warn:
            return f"⚠️ {html.escape(warn)} — پیش‌فرض‌های فایل YAML به کار رفت.\n"
        base = getattr(self, "_settings_defaults", None)
        if not base:
            return ""
        d = sset.diff(self.current_settings(), base)
        if not d:
            return ""
        return "🛠 تنظیمات تلگرام فعال: " + " · ".join(f"{sset.SPECS[k]['fa']} {sset.fmt(k, v)}" for k, v in d.items()) + "\n"

    def current_settings(self) -> Dict[str, Any]:
        return {"lot": round(float(self.lot), 2), "sl_cap": self._cap_usd("sl"), "emerg_cap": self._cap_usd("emerg"),
                "max_open": int(self.max_open), "max_risk": float(self.max_total_risk),
                "expiry_min": int(self.expiry // 60)}

    def _set_values(self, values: Dict[str, Any]) -> None:
        """مقدارها را روی خود ربات بگذار؛ سقف‌های قیمتی از روی دلار و لات جدید دوباره حساب می‌شود."""
        if "lot" in values:
            self.lot = float(values["lot"])
        if "sl_cap" in values:
            self.sl_cap_usd = float(values["sl_cap"])
        if "emerg_cap" in values:
            self.emerg_cap_usd = float(values["emerg_cap"])
        if "max_open" in values:
            self.max_open = int(values["max_open"])
        if "max_risk" in values:
            self.max_total_risk = float(values["max_risk"])
        if "expiry_min" in values:
            self.expiry = int(values["expiry_min"]) * 60
        self.sl_cap = self._cap_dist(self._cap_usd("sl"), self.lot)
        self.emerg_cap = self._cap_dist(self._cap_usd("emerg"), self.lot)

    def settings_text(self, extra: str = "") -> str:
        cur, base = self.current_settings(), getattr(self, "_settings_defaults", None) or self.current_settings()
        lines = ["🛠 <b>تنظیمات اسکات</b> (فقط ادمین)"]
        for k, sp in sset.SPECS.items():
            mark = "" if abs(float(cur[k]) - float(base.get(k, cur[k]))) < 1e-9 else \
                f"  <i>(پیش‌فرض {sset.fmt(k, base[k])})</i>"
            lines.append(f"• {sp['fa']}: <b>{sset.fmt(k, cur[k])}</b>{mark}")
        lines.append("<i>تغییر از سیگنال بعدی اعمال می‌شود؛ پوزیشن‌های باز دست نمی‌خورند. ری‌استارت لازم نیست.</i>")
        return "\n".join(lines) + (("\n\n" + extra) if extra else "")

    def settings_buttons(self) -> list:
        cur = self.current_settings()
        rows = [[{"text": sset.short_fmt(k, cur[k]), "callback_data": f"g|i|{k}"},
                 {"text": "➖", "callback_data": f"g|{k}|-"}, {"text": "➕", "callback_data": f"g|{k}|+"}]
                for k, sp in sset.SPECS.items()]
        rows.append([{"text": "↩️ پیش‌فرض‌ها", "callback_data": "g|reset"},
                     {"text": "✖️ بستن", "callback_data": "g|close"}])
        return rows

    def settings_show(self) -> None:
        old = getattr(self, "_settings_mid", None)
        if old:                                           # فقط یک پیام تنظیمات زنده بماند
            self.tg.delete(old)
        self._settings_ask = None
        self._settings_mid = self.tg.send(self.settings_text(), self.settings_buttons())

    def _settings_refresh(self, extra: str = "") -> None:
        if getattr(self, "_settings_mid", None):
            self.tg.edit(self._settings_mid, self.settings_text(extra), self.settings_buttons())

    def settings_tap(self, args: list, cb: str, mid: Optional[int]) -> None:
        cur_mid = getattr(self, "_settings_mid", None)
        if mid is not None and mid != cur_mid:            # پیام تنظیمات قدیمی (مثلاً قبل از ری‌استارت)
            self.tg.ack(cb, "این پیام تنظیمات قدیمی است؛ دوباره «🛠 تنظیمات» را بزنید")
            return
        what = args[0]
        cur = self.current_settings()
        if what == "i":
            k = args[1] if len(args) > 1 else ""
            sp = sset.SPECS.get(k)
            self.tg.ack(cb, f"{sp['fa']}: بین {sset.fmt(k, sp['lo'])} و {sset.fmt(k, sp['hi'])}، قدم "
                            f"{sset.fmt(k, sp['step'])}" if sp else "")
            return
        if what == "close":
            self.tg.ack(cb, "بسته شد")
            if cur_mid:
                self.tg.delete(cur_mid)
            self._settings_mid, self._settings_ask = None, None
            return
        if what == "back":
            self.tg.ack(cb, "لغو شد")
            self._settings_ask = None
            self._settings_refresh()
            return
        if what == "reset":
            base = self._settings_defaults
            changes = {k: (cur[k], base[k]) for k in sset.SPECS if abs(float(cur[k]) - float(base[k])) > 1e-9}
            if not changes:
                self.tg.ack(cb, "همین الان همه پیش‌فرض است")
                return
            if len(args) > 1 and args[1] == "ok":
                if self._settings_ask != ("reset",):
                    self.tg.ack(cb, "این سؤال دیگر معتبر نیست")
                    self._settings_refresh()
                    return
                self.tg.ack(cb, "برگشت به پیش‌فرض")
                self._settings_apply(changes, reset=True)
                return
            self._settings_ask = ("reset",)
            self.tg.ack(cb)
            lines = "\n".join(f"• {sset.SPECS[k]['fa']}: {sset.fmt(k, a)} → <b>{sset.fmt(k, b)}</b>"
                              for k, (a, b) in changes.items())
            self.tg.edit(cur_mid, self.settings_text(f"↩️ <b>همه به پیش‌فرض برگردد؟</b>\n{lines}"),
                         [[{"text": "✅ بله، برگردان", "callback_data": "g|reset|ok"},
                           {"text": "↩️ نه", "callback_data": "g|back"}]])
            return
        k = what
        if k not in sset.SPECS or len(args) < 2:
            self.tg.ack(cb)
            return
        if args[1] == "ok":                               # تأیید تغییر بزرگ / لات
            try:
                new = sset.norm(k, args[2])
            except (IndexError, TypeError, ValueError, OverflowError):
                self.tg.ack(cb)
                return
            if self._settings_ask != (k, new) or not sset.in_range(k, new):
                self.tg.ack(cb, "این سؤال دیگر معتبر نیست")
                self._settings_refresh()
                return
            bad = sset.problems(dict(cur, **{k: new}))
            if bad:
                self.tg.ack(cb, bad[0][:190])
                self._settings_refresh()
                return
            self.tg.ack(cb, "ثبت شد")
            self._settings_apply({k: (cur[k], new)})
            return
        if args[1] not in ("+", "-"):
            self.tg.ack(cb)
            return
        new = sset.step(k, cur[k], args[1])
        if abs(float(new) - float(cur[k])) < 1e-9:
            self.tg.ack(cb, "به حداقل رسیده" if args[1] == "-" else "به حداکثر رسیده")
            return
        bad = sset.problems(dict(cur, **{k: new}))
        if bad:
            self.tg.ack(cb, bad[0][:190])
            return
        if sset.needs_confirm(k, cur[k], new):
            self._settings_ask = (k, new)
            self.tg.ack(cb)
            why = ("لات مستقیم روی اندازهٔ سود و ضرر اثر دارد" if k == "lot"
                   else "بیشتر از دو برابر مقدار فعلی است")
            extra = (f"⚠️ <b>{sset.SPECS[k]['fa']}: {sset.fmt(k, cur[k])} → {sset.fmt(k, new)}</b>؟\n{why}.")
            if k == "lot":
                extra += (f"\nبا لات {sset.fmt(k, new)}، سقف {sset.fmt('sl_cap', cur['sl_cap'])} یعنی حد ضرر "
                          f"حداکثر {cur['sl_cap'] / (100 * float(new)):.2f} دلار فاصلهٔ قیمت.")
            self.tg.edit(cur_mid, self.settings_text(extra),
                         [[{"text": "✅ بله، عوض کن", "callback_data": f"g|{k}|ok|{new}"},
                           {"text": "↩️ نه", "callback_data": "g|back"}]])
            return
        self.tg.ack(cb, f"{sset.SPECS[k]['fa']}: {sset.fmt(k, new)}")
        self._settings_apply({k: (cur[k], new)})

    def _settings_apply(self, changes: Dict[str, tuple], reset: bool = False) -> None:
        """changes: {key: (قبلی، جدید)} — اعمال، ذخیره، ژورنال، اعلام در گروه."""
        if "lot" in changes:                              # پوزیشن‌های باز با لات قبلی خودشان حساب شوند
            for info in self.open.values():
                info.setdefault("lot", float(changes["lot"][0]))
        self._set_values({k: v[1] for k, v in changes.items()})
        self._settings_ask = None
        saved = True
        try:
            sset.save(self.settings_path, sset.diff(self.current_settings(), self._settings_defaults))
            self._settings_warn = None
        except OSError as exc:
            saved = False
            logger.warning("scout settings not saved: {}", exc)
        if self.open:
            self._save_state()
        who = self._who or "?"
        note = ";".join(f"{k}:{a}->{b}" for k, (a, b) in changes.items()) + f";by={who}" + (";reset" if reset else "")
        self.log(event="setting", note=note[:250])
        lines = "\n".join(f"• {sset.SPECS[k]['fa']}: {sset.fmt(k, a)} → <b>{sset.fmt(k, b)}</b>"
                          for k, (a, b) in changes.items())
        self.tg.send(f"🛠 <b>{'تنظیمات به پیش‌فرض برگشت' if reset else 'تنظیم عوض شد'}</b> · {who}\n{lines}\n"
                     "از سیگنال بعدی اعمال می‌شود؛ پوزیشن‌های باز دست نمی‌خورند."
                     + ("" if saved else "\n⚠️ در فایل ذخیره نشد؛ با ری‌استارت به قبل برمی‌گردد."))
        self._settings_refresh()

    def panel(self, what: str) -> None:
        try:
            if what == "menu":
                self.tg.send("⌨️ دکمه‌های پایین صفحه فعال است.", keyboard=self.KEYBOARD)
                self.tg.send(f"🎛 <b>پنل کنترل اسکات</b>\n{self._one_line_state()}", self.MENU_BUTTONS)
            elif what == "price":
                self.tg.send(self.price_text())
            elif what == "status":
                self.tg.send(self.status_text(), self.MENU_BUTTONS)
            elif what == "pos":
                self.positions_report()
            elif what == "mode":
                self.mode_menu()
            elif what in ("pause", "resume"):
                self.set_paused(what == "pause")
            elif what == "restart":
                old = getattr(self, "_restart_mid", None)
                if old:                                   # سؤال قبلی بی‌جواب: پاک شود تا دو سؤال زنده نماند
                    self.tg.delete(old)
                self._restart_mid = self.tg.send(
                    "🔄 <b>ری‌استارت اسکات؟</b>\nپوزیشن‌های باز بسته نمی‌شوند و بعد از روشن شدن "
                    "دوباره زیر نظر می‌آیند. حدود ۳۰ ثانیه طول می‌کشد.",
                    [[{"text": "✅ بله، ری‌استارت", "callback_data": "k|restart_yes"},
                      {"text": "❌ نه", "callback_data": "k|restart_no"}]])
            elif what == "restart_yes":
                self.restart()
            elif what == "palert":
                self.price_alerts_list()
            elif what == "settings":
                self.settings_show()
            elif what in ("buy", "sell"):
                self.manual_start(what.upper())
            elif what == "score":
                self.send_scoreboard(force=True)
            elif what == "restart_no":
                self.tg.send("ری‌استارت لغو شد.")
            elif what == "help":
                self.tg.send(ctl.HELP_TEXT.format(max_open=self.max_open, risk=self.max_total_risk,
                                                  sl=self._cap_usd("sl"), em=self._cap_usd("emerg"),
                                                  status=int(self.status_every // 60)))
        except Exception as exc:
            logger.warning("scout panel {} failed: {}", what, exc)
            self.tg.send(f"⚠️ {what}: {html.escape(str(exc))[:150]}")

    def _one_line_state(self) -> str:
        pause = "⏸ هشدارها متوقف" if self.paused else "🔔 هشدارها فعال"
        return (f"{pause} · در حد ضرر: {trk.POLICY_FA[self.mode]} · پوزیشن باز: {len(self.open)} · "
                f"روشن از {ctl.fmt_age(time.time() - self.started)} پیش")

    def price_text(self) -> str:
        tick = self.client.get_tick()
        if tick is None:
            return "⚠️ قیمت از MT5 نرسید."
        bid, ask = float(tick.bid), float(tick.ask)
        sp = self.client.spread_points()
        srv = datetime.fromtimestamp(float(tick.time), timezone.utc).strftime("%H:%M:%S")
        line = f"💰 <b>{self.client.symbol}</b>\nخرید (Ask): <b>{ask:.2f}</b> · فروش (Bid): <b>{bid:.2f}</b>\n"
        line += f"اسپرد {sp:.0f} پوینت · ساعت سرور {srv}" if sp is not None else f"ساعت سرور {srv}"
        try:
            d1 = self.client.get_rates("D1", 2)
            if d1 is not None and not d1.empty:
                o, hi, lo = float(d1.open.iloc[-1]), float(d1.high.iloc[-1]), float(d1.low.iloc[-1])
                line += f"\nامروز: باز {o:.2f} · سقف {hi:.2f} · کف {lo:.2f} · تغییر {bid - o:+.2f}"
        except Exception:
            pass
        if self.open:
            line += "\n" + "\n".join(
                f"#{tk} {i['side']} از {float(i['entry']):.2f} → "
                f"{trk.signed_move(i['side'], float(i['entry']), bid if i['side'] == 'BUY' else ask):+.2f}"
                for tk, i in self.open.items())
        return line

    def status_text(self) -> str:
        connected = False
        try:
            probe = self.client.is_connected            # MT5Client: property (bool), not a method
            connected = bool(probe() if callable(probe) else probe)
        except Exception:
            pass
        tick = None
        try:
            tick = self.client.get_tick()
        except Exception:
            pass
        acc = None
        try:
            acc = self.client.account_info()
        except Exception:
            pass
        tick_age = None
        if tick is not None and getattr(tick, "time", 0):
            srv_now = None
            try:
                st = self.client.server_time()
                srv_now = st.replace(tzinfo=timezone.utc).timestamp() if st is not None else None
            except Exception:
                srv_now = None
            tick_age = (srv_now - float(tick.time)) if srv_now else None
        js = ctl.journal_summary(self.journal, datetime.now() - timedelta(hours=24))
        rev = "?"
        try:
            rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                 timeout=5, cwd=str(Path(__file__).resolve().parents[1])).stdout.strip() or "?"
        except Exception:
            pass
        lines = [
            "✅ <b>اسکات سالم است</b>" if connected and not self._feed_alert else "⚠️ <b>مشکل در اتصال</b>",
            f"نسخه {rev} · روشن از {ctl.fmt_age(time.time() - self.started)} پیش",
            f"MT5: {'وصل' if connected else 'قطع'} · آخرین تیک {ctl.fmt_age(tick_age)} پیش"
            + ("" if ctl.market_open_utc(datetime.now(timezone.utc)) else " (بازار بسته)"),
        ]
        if acc is not None:
            lines.append(f"حساب {getattr(acc, 'login', '?')} · موجودی ${float(getattr(acc, 'balance', 0)):.2f}"
                         f" · اکوئیتی ${float(getattr(acc, 'equity', 0)):.2f}")
        lines.append(self._one_line_state())
        if self._last_alert_t:
            lines.append(f"آخرین هشدار: {ctl.fmt_age(time.time() - self._last_alert_t)} پیش")
        lines.append("۲۴ ساعت اخیر: " + ctl.summary_line(js))
        return "\n".join(lines)

    def positions_report(self) -> None:
        if not self.open:
            self.tg.send("📋 پوزیشن بازی نیست.")
            return
        live = {}
        try:
            live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        except Exception:
            pass
        for tk in list(self.open):
            info = self.open[tk]
            p = live.get(int(tk))
            now_txt = (f" · الان {float(p.price_current):.2f} · سود/زیان <b>{float(p.profit):+.2f}$</b>"
                       if p is not None else "")
            held = " · ⏸ نگه داشته شده" if info.get("hold") else ""
            self.tg.send(f"📋 <b>#{tk}</b> {info['side']} · <code>{info['setup']}</code> · ورود "
                         f"{float(info['entry']):.2f}{now_txt}\nحد ضرر {float(info['sl']):.2f}"
                         f" · اضطراری {float(info.get('emerg') or info['sl']):.2f}{held}",
                         self._close_buttons(tk))

    def set_paused(self, on: bool) -> None:
        self.paused = bool(on)
        try:
            flags = ctl.load_flags(self.flags_path)
            flags["paused"] = self.paused
            ctl.save_flags(self.flags_path, flags)
        except Exception as exc:                       # pragma: no cover
            logger.warning("scout flags save failed: {}", exc)
        if on:
            self.cancel_pending("هشدارها متوقف شد")
        self.tg.send("⏸ <b>هشدارهای جدید متوقف شد.</b> ستاپ‌ها فقط بی‌صدا ثبت می‌شوند؛ پوزیشن‌های باز "
                     "مثل قبل مدیریت و گزارش می‌شوند. برای ادامه: /resume" if on else
                     "▶️ <b>هشدارها دوباره فعال شد.</b>")
        self.log(event="pause" if on else "resume")

    def restart_answer(self, yes: bool, cb: str, mid: Optional[int]) -> None:
        """جواب سؤال ری‌استارت. «بله» = سؤال به یک خط سابقه (بدون دکمه) تبدیل و ری‌استارت؛ «نه» = سؤال پاک.
        دکمهٔ یک سؤال قدیمی (مثلاً از قبل از ری‌استارت قبلی) دیگر ری‌استارت نمی‌کند."""
        current = getattr(self, "_restart_mid", None)
        if mid and int(mid) != int(current or 0):
            self.tg.ack(cb, "این سؤال قدیمی است؛ برای ری‌استارت دوباره از منو بزنید")
            self.tg.edit(int(mid), "⌛ سؤال ری‌استارت قدیمی — بی‌اثر شد.")
            return
        self._restart_mid = None
        if not yes:
            self.tg.ack(cb, "ری‌استارت لغو شد")
            if mid:
                self.tg.delete(int(mid))
            else:
                self.tg.send("ری‌استارت لغو شد.")
            return
        self.tg.ack(cb, "در حال ری‌استارت…")
        if mid:
            t = datetime.now(dash.TEHRAN).strftime("%H:%M")
            self.tg.edit(int(mid), f"🔄 <b>ری‌استارت شد</b>{self._by()} · ساعت {t} تهران")
        self.restart(announce=not mid)

    def restart(self, announce: bool = True) -> None:
        if announce:
            self.tg.send("🔄 اسکات در حال ری‌استارت است…")
        self.log(event="restart")
        self._save_state()
        logger.warning("scout restart requested from telegram")
        try:   # تأیید آپدیت‌های خوانده‌شده؛ وگرنه نسخهٔ جدید همین «بله، ری‌استارت» را دوباره می‌گیرد
            self.tg._call("getUpdates", offset=self.tg._offset, timeout=0)
        except Exception:                              # pragma: no cover
            pass
        if self.lock is not None:
            self.lock.release()
        try:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                             cwd=str(Path(__file__).resolve().parents[1]), creationflags=flags)
        except Exception as exc:
            logger.error("scout relaunch failed: {}", exc)
            self.tg.send(f"⚠️ ری‌استارت ناموفق: {html.escape(str(exc))[:150]}")
            if self.lock is not None:
                self.lock.acquire(wait=5)
            return
        try:
            self.client.shutdown()
        finally:
            os._exit(0)

    # ------------------------------------------------------------- dashboard
    def start_dashboard(self) -> None:
        if not self.dash_enabled:
            return
        try:
            self.version = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                          text=True, timeout=5,
                                          cwd=str(Path(__file__).resolve().parents[1])).stdout.strip()
        except Exception:
            self.version = ""
        self.dashboard = dash.ScoutDashboard(lambda: self._dash_snap, self.dash_stats, self.dash_token,
                                             self.dash_host, self.dash_port)
        if not self.dashboard.start():
            self.dashboard = None

    def dash_stats(self) -> Dict[str, Any]:
        """از نخ وب صدا زده می‌شود: فقط دیسک و حافظه، بدون MT5."""
        events = dash.read_journal(self.journal)
        lot = float((getattr(self, "_settings_defaults", None) or {}).get("lot") or self.lot)
        st = dash.build_stats(events, virtual=self._virtual_rows, actual_by_ticket=self._actual_map, lot=lot)
        st["picks"] = dash.build_picks(self._virtual_rows)
        return st

    def refresh_virtual(self) -> None:
        """نتیجهٔ فرضی همهٔ ستاپ‌ها (مثل scout_stats) — در حلقهٔ اصلی، چون M1 از MT5 می‌آید."""
        legacy = self.journal.with_name("scout_journal.csv")
        df = scout_stats.build(scout_stats.read_events(self.journal, legacy))
        if df.empty:
            self._virtual_rows = []
            return
        m1 = self.client.get_rates("M1", 60000)
        df = scout_stats.attach_actual(scout_stats.virtual(df, m1, 24.0), self.client.deals_for_position)
        self._virtual_rows = [{"setup": r.setup, "side": r.side, "status": r.status, "R": r.R}
                              for r in df.itertuples()]
        self._actual_map = {str(r.ticket): float(r.actual) for r in df.itertuples()
                            if r.status == "opened" and r.ticket and r.actual == r.actual}

    def dash_snapshot(self, now: float) -> Dict[str, Any]:
        acc = self.client.account_info()
        tick = self.client.get_tick()
        utc = datetime.fromtimestamp(now, timezone.utc)
        opened = []
        for tk, info in list(self.open.items()):
            lv = self._live.get(tk, {})
            price = lv.get("price") or 0.0
            risk = float(info.get("risk") or 0.0)
            r = trk.signed_move(info["side"], float(info["entry"]), price) / risk if price and risk else None
            opened.append({"ticket": tk, "side": info["side"], "setup": info.get("setup", ""),
                           "entry": float(info["entry"]), "sl": float(info["sl"]), "price": price or None,
                           "profit": lv.get("profit"), "broker_sl": lv.get("broker_sl") or None,
                           "r": round(r, 2) if r is not None else None, "hold": bool(info.get("hold")),
                           "warned": bool(info.get("warned"))})
        pending = []
        for aid, p in list(self.pending.items()):
            row = p["row"]
            try:
                entry, sl, _tp, risk, _n = self.levels(row)
            except Exception:
                entry, sl, risk = 0.0, float(p.get("sl") or 0.0), 0.0
            pending.append({"id": aid, "side": row.side, "setup": str(row.get("setups_all", row.setup)),
                            "price": float(entry), "sl": float(sl), "risk": float(risk or 0.0),
                            "left_min": max(0, int((float(p.get("exp") or self.expiry) - (now - p["t"])) // 60)),
                            "analysis": self._an_short.get(aid, "")})
        return {
            "status": {"alive": True, "mt5": bool(self.client.is_connected), "market": ctl.market_open_utc(utc),
                       "paused": self.paused, "mode": self.mode, "mode_fa": trk.POLICY_FA.get(self.mode, self.mode),
                       "now_tehran": utc.astimezone(dash.TEHRAN).strftime("%H:%M:%S"),
                       "up_min": int((now - self.started) // 60), "version": self.version},
            "account": {"balance": getattr(acc, "balance", None), "equity": getattr(acc, "equity", None),
                        "profit": getattr(acc, "profit", None)} if acc is not None else {},
            "price": {"bid": float(tick.bid), "ask": float(tick.ask)} if tick is not None else None,
            "open": opened, "open_risk": self.open_risk_usd(), "pending": pending,
            "audit": getattr(self, "_audit", None),
            "rules": {"lot": self.lot, "sl_cap": round(self._cap_usd("sl"), 2),
                      "emerg_cap": round(self._cap_usd("emerg"), 2),
                      "approach": int(self.approach_frac * 100), "max_open": self.max_open,
                      "max_risk": self.max_total_risk, "expiry_min": self.expiry // 60,
                      "status_min": int(self.status_every // 60)},
        }

    def stats_tick(self, now: Optional[float] = None) -> None:
        """نتیجهٔ فرضی ستاپ‌ها (برای کارنامهٔ زیر هشدار و داشبورد) هر stats_minutes."""
        now = time.time() if now is None else now
        if now - self._virt_t >= self.stats_every:
            self._virt_t = now
            try:
                self.refresh_virtual()
            except Exception as exc:
                logger.warning("scout stats refresh failed: {}", exc)

    def audit_tick(self, now: Optional[float] = None) -> None:
        """هر audit_every: برای هشدارهای audit_days روز اخیر حساب می‌کند بازار بعدش به کدام طرف رفت.
        فقط خواندن (ژورنال + کندل‌های M1)؛ خطا فقط لاگ می‌شود."""
        if not getattr(self, "audit_enabled", False):
            return
        now = time.time() if now is None else now
        if now - self._audit_t < self.audit_every:
            return
        self._audit_t = now
        try:
            since = (datetime.now() - timedelta(days=self.audit_days)).isoformat(timespec="seconds")
            alerts = sig_audit.read_alerts(self.journal, since=since)
            if alerts.empty:
                return
            m1 = self.client.get_history_bars("M1", self.audit_days * 1440 + 600)
            res = sig_audit.audit(alerts, m1)
            rep = sig_audit.audit_report(res, tight=getattr(self, "label_tight", sig_audit.TIGHT_STOP))
            rep["days"] = self.audit_days
            rep["updated"] = datetime.now(dash.TEHRAN).strftime("%Y-%m-%d %H:%M")
            self._audit = rep
            self._audit_setups = {x["key"]: x for x in rep.get("setup", [])}
            self._audit_labels = {x["key"]: x for x in rep.get("label", [])}
        except Exception as exc:
            logger.warning("scout signal audit failed: {}", exc)

    def audit_line(self, setups: str) -> str:
        """یک خط زیر هشدار: هر ستاپ در audit_days روز اخیر چند درصد اول به ‎+1R رسید."""
        table = getattr(self, "_audit_setups", None) or {}
        parts, weak = [], False
        for name in dict.fromkeys(x.strip() for x in setups.replace("+", ",").split(",") if x.strip()):
            st = table.get(name)
            if not st or st.get("first") is None or st.get("n", 0) < getattr(self, "audit_min_n", 5):
                continue
            pct = int(round(st["first"] * 100))
            # همان منطق برچسب بالای هشدار (بازهٔ ۹۵٪)، تا دو خط یک هشدار با هم تناقض نداشته باشند
            decided = int(st.get("plus", 0)) + int(st.get("minus", 0)) + int(st.get("both", 0))
            lo, hi = sig_audit.wilson(int(st.get("plus", 0)), decided)
            mark = "✅" if lo > 0.5 else ("⚠️" if hi < 0.5 else "")
            weak = weak or hi < 0.5
            parts.append(f"{mark}<code>{name}</code> {pct}٪ ({st['n']})")
        if not parts:
            return ""
        days = getattr(self, "audit_days", 14)
        return (f"🧭 بعد از سیگنال ({days} روز اخیر، اول ‎+1R): " + " · ".join(parts)
                + (" — اکثراً اول خلاف جهت رفته" if weak and len(parts) == 1 else ""))

    def label_line(self, setups: str, stop_dist: float) -> str:
        """خط اول هشدار: این سیگنال در کدام دسته است و آن دسته در audit_days روز اخیر چند درصد اول ‎+1R شد.
        علامت با اطمینان آماری (بازهٔ ۹۵٪): کل بازه بالای ۵۰٪ ← ✅، کل بازه زیر ۵۰٪ ← ⚠️، بقیه ⚪ (سبز/قرمز مال جهت است)؛ فقط اطلاع است."""
        if not getattr(self, "label_enabled", True):
            return ""
        tight = getattr(self, "label_tight", sig_audit.TIGHT_STOP)
        key = sig_audit.label_group(setups, stop_dist, tight)
        st = (getattr(self, "_audit_labels", None) or {}).get(key)
        if not st or st.get("first") is None or st.get("n", 0) < getattr(self, "audit_min_n", 5):
            return ""
        f = st["first"]
        decided = int(st.get("plus", 0)) + int(st.get("minus", 0)) + int(st.get("both", 0))
        lo, hi = sig_audit.wilson(int(st.get("plus", 0)), decided)
        if lo > 0.5:                                  # حتی با احتساب شانس بالای ۵۰٪
            icon, word = "✅", "خوب"
        elif hi < 0.5:                                # حتی با احتساب شانس زیر ۵۰٪
            icon, word = "⚠️", "ضعیف"
        else:
            icon, word = "⚪", ("شانسی" if 0.45 <= f <= 0.55 else "هنوز معلوم نیست")
        what = (f"حد ضرر نزدیک ({float(stop_dist):.2f})" if key == "tight" else sig_audit.LABEL_FA[key])
        days = getattr(self, "audit_days", 14)
        return f"{icon} <b>{word}</b> — {what} ({int(round(f * 100))}٪ اول ‎+1R در {days} روز، {st['n']} سیگنال)"

    def dash_tick(self, now: Optional[float] = None) -> None:
        if self.dashboard is None:
            return
        now = time.time() if now is None else now
        if now - self._dash_t >= 5.0:
            self._dash_t = now
            try:
                self._dash_snap = self.dash_snapshot(now)
            except Exception as exc:
                logger.warning("scout dashboard snapshot failed: {}", exc)

    # ------------------------------------------------------------ health watch
    def health_tick(self, data_ok: bool, now: Optional[float] = None) -> None:
        """هشدار وقتی داده/تیک از MT5 نمی‌رسد، پیام بازگشت، و «سالمم» روزانه."""
        now = time.time() if now is None else now
        utc = datetime.fromtimestamp(now, timezone.utc)
        stale_tick = getattr(self, "_stale_tick", False)
        if not data_ok or not ctl.market_open_utc(utc):
            stale_tick = False
        elif now - getattr(self, "_tick_check_t", 0.0) >= 30.0:     # هر ۳۰ ثانیه، نه هر حلقه
            self._tick_check_t = now
            stale_tick = False
            try:
                tick = self.client.get_tick()
                st = self.client.server_time()
                if tick is not None and st is not None and getattr(tick, "time", 0):
                    age = st.replace(tzinfo=timezone.utc).timestamp() - float(tick.time)
                    stale_tick = age > self.stale_after
            except Exception:
                pass
        self._stale_tick = stale_tick
        if data_ok and not stale_tick:
            self._last_data_ok = now
            if self._feed_alert:
                self._feed_alert = False
                self.tg.send("✅ <b>اتصال اسکات به MT5 برگشت</b> — داده و قیمت دوباره می‌رسد.")
                self.log(event="feed_ok")
        elif not self._feed_alert and now - self._last_data_ok > self.stale_after:
            self._feed_alert = True
            why = "قیمت تازه نمی‌آید" if data_ok else "کندل‌ها از MT5 نمی‌رسد"
            self.tg.send(f"⚠️ <b>اسکات: {why}</b> (بیش از {int(self.stale_after // 60)} دقیقه).\n"
                         "ربات روشن است و خودش دوباره تلاش می‌کند؛ اگر ادامه داشت ترمینال MT5 را "
                         "روی VPS بررسی کنید یا 🔄 ری‌استارت بزنید.", self.MENU_BUTTONS)
            self.log(event="feed_stale", note=why)
        day = utc.strftime("%Y-%m-%d")
        if utc.hour >= self.health_hour and self._health_day != day:
            first = self._health_day is None
            self._health_day = day
            if not first or utc.hour == self.health_hour:
                js = ctl.journal_summary(self.journal, datetime.now() - timedelta(hours=24))
                self.tg.send(("✅ <b>اسکات سالم است</b>" if not self._feed_alert else
                              "⚠️ <b>اسکات روشن است ولی اتصال MT5 مشکل دارد</b>")
                             + f"\n{self._one_line_state()}\n۲۴ ساعت اخیر: {ctl.summary_line(js)}")

    # -------------------------------------------------------------- heartbeat
    def beat(self) -> None:
        """هر دقیقه یک بار زمان را در فایل ضربان می‌نویسد تا نگهبان بفهمد زنده است."""
        now = time.time()
        if now - self._last_beat < 60.0:
            return
        self._last_beat = now
        try:
            self.beat_path.parent.mkdir(parents=True, exist_ok=True)
            self.beat_path.write_text(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except Exception as exc:                       # pragma: no cover
            logger.warning("heartbeat write failed: {}", exc)

    # --------------------------------------------------------------- selftest
    def fire_selftest(self) -> None:
        """یک هشدار از آخرین ستاپِ موجود در تاریخچه می‌فرستد تا دکمه‌ها را تست کنید.

        فقط برای وقتی که بازار بسته است و کندل جدیدی ساخته نمی‌شود. چون همیشه با
        --dry اجرا می‌شود، تأیید هم هیچ سفارشی ثبت نمی‌کند.
        """
        m15 = self.client.get_rates("M15", 700)
        if m15 is None or m15.empty:
            self.tg.send("⚠️ تست: کندلی از ترمینال نیامد.")
            return
        f = indicators(m15.iloc[:-1], self.client.get_rates("H1", 300),
                       int(self.s.get("scout.h1_ema_period", 20)))
        sig = detect(f)
        if sig.empty:
            self.tg.send("⚠️ تست: در تاریخچهٔ موجود هیچ ستاپی پیدا نشد.")
            return
        r = sig.iloc[-1]
        self.tg.send("🧪 <b>پیام آزمایشی</b> — از آخرین ستاپ تاریخچه ساخته شده، "
                     "نه از بازار زنده. دکمه‌ها را بزنید؛ هیچ سفارشی ثبت نمی‌شود.")
        self.alert(r, self.client.spread_points() or 0.0)

    # -------------------------------------------------------------------- loop
    def _startup_failed(self, why: str, now: Optional[float] = None) -> None:
        """اسکات نتوانست روشن شود: در تلگرام بگو (حداکثر هر startup_notify_minutes یک بار، چون
        تسک ویندوز هر چند دقیقه دوباره امتحان می‌کند)."""
        now = time.time() if now is None else now
        path = Path(str(self.s.get("scout.startup_fail_file", "data/scout_startup_fail.json")))
        every = float(self.s.get("scout.startup_notify_minutes", 30)) * 60.0
        last = float((fu.load_json(path, {}) or {}).get("t", 0.0) or 0.0)
        logger.error("scout startup failed: {}", why)
        if now - last < every:
            return
        self.tg.send(f"⚠️ <b>اسکات روشن نشد</b>: {html.escape(why)[:200]}\n"
                     "پوزیشن‌های باز فقط با حد ضرر روی بروکر محافظت می‌شوند. تسک ویندوز چند دقیقه بعد "
                     "دوباره امتحان می‌کند؛ اگر نشد، MT5 را روی VPS باز و وصل کنید.")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"t": now, "why": why[:200]}), encoding="utf-8")
        except OSError:                                # pragma: no cover
            pass

    def _safe_update(self, u: dict) -> None:
        """خطای یک دکمه/پیام بقیهٔ صف و حلقه را نگه ندارد؛ به مالک هم بگو."""
        try:
            self.handle_update(u)
        except Exception as exc:
            logger.exception("scout: telegram update failed: {}", exc)
            cq = u.get("callback_query") or {}
            if cq.get("id"):
                try:
                    self.tg.ack(cq["id"], "خطا در ارتباط با MT5 — چند ثانیه بعد دوباره بزنید")
                except Exception:                      # pragma: no cover
                    pass

    def run(self) -> None:
        if not self.client.connect():
            self._startup_failed("اتصال به MT5 برقرار نشد")
            raise SystemExit("MT5 connect failed")
        acc = self.client.account_info()
        self.restore()                                  # اول پوزیشن‌های باز، تا پیام روشن شدن تعدادشان را درست بگوید
        self.tg.start()
        self.tg._call("setMyCommands", commands=ctl.MENU_COMMANDS)
        self.tg.send(f"👀 <b>اسکات روشن شد</b>{' (آزمایشی)' if self.dry else ''}\n"
                     f"حساب {getattr(acc,'login','?')} ({self.account_kind(acc)}) · {getattr(acc,'server','?')} · "
                     f"موجودی ${getattr(acc,'balance',0):.2f}\nلات {self.lot} · انقضای هشدار "
                     f"{self.expiry//60} دقیقه\n{self._one_line_state()}\n"
                     + self._settings_note() +
                     "دکمه‌های پایین صفحه: 📋 پوزیشن‌ها (بستن/نگه داشتن) · 💰 قیمت · 📊 وضعیت · 🎛 منو",
                     keyboard=self.KEYBOARD)
        logger.info("scout online")
        self.start_dashboard()
        self.news_note()                                # تقویم خبر همین‌جا بار شود، نه وسط اولین هشدار
        if self.selftest:
            self.fire_selftest()
        while True:
            try:
                self.beat()
                while not self.tg.q.empty():
                    self._safe_update(self.tg.q.get_nowait())

                self.expire()
                m15 = self.client.get_rates("M15", 700)
                self.health_tick(m15 is not None and not m15.empty)
                if m15 is None or m15.empty:
                    time.sleep(2); continue
                m15 = m15.iloc[:-1]                      # فقط کندل‌های بسته
                h1 = self.client.get_rates("H1", 300)
                f = indicators(m15, h1, int(self.s.get("scout.h1_ema_period", 20)))
                self._frame = f
                self.monitor(f)
                self.reconcile()
                self.poll_analyses()
                self.dash_tick()
                self.stats_tick()
                self.audit_tick()
                self.whatif_tick()
                self.price_alert_tick()
                self.scoreboard_tick()
                self.process_new_bars(f)
                time.sleep(float(self.s.get("scout.loop_seconds", 2)))
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.exception("loop error: {}", exc)
                time.sleep(5)
        self.tg.send("🛑 اسکات خاموش شد")
        self.client.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings_scout.yaml")
    ap.add_argument("--dry", action="store_true", help="هشدار بفرست ولی سفارش ثبت نکن")
    ap.add_argument("--selftest", action="store_true",
                    help="در شروع یک هشدار آزمایشی بفرست (خودش --dry را روشن می‌کند)")
    a = ap.parse_args()
    st = Settings.load(a.config)
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
    lp = str(st.get("logging.path", "") or "")
    if lp:
        try:
            Path(lp).parent.mkdir(parents=True, exist_ok=True)
            logger.add(lp, level=str(st.get("logging.level", "INFO")),
                       rotation=str(st.get("logging.rotation", "10 MB")),
                       retention=str(st.get("logging.retention", "21 days")),
                       encoding="utf-8")
        except Exception as exc:                       # pragma: no cover
            logger.warning("file log disabled: {}", exc)
    lock = ctl.InstanceLock(Path(str(st.get("scout.lock_file", "data/scout.lock"))))
    if not lock.acquire(wait=40):                      # ری‌استارت: صبر تا نسخهٔ قبلی قفل را آزاد کند
        logger.error("another Scout instance is already running (lock {}); exiting", lock.path)
        raise SystemExit(3)
    try:
        sc = Scout(st, a.dry or a.selftest, a.selftest)
        sc.lock = lock
        sc.run()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
