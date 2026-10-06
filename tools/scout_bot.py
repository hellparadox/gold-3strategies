"""SCOUT BOT — هر ستاپ را به تلگرام می‌فرستد و منتظر تأیید یا رد شما می‌ماند.

- خودش وارد نمی‌شود. فقط بعد از «تأیید» شما سفارش می‌گذارد.
- حد ضرر (قواعد مالک، ۲۰۲۶-۱۰-۰۲): فاصلهٔ حد ضرر سیگنال حداکثر scout.sl_cap_usd دلار است و
  به‌طور پیش‌فرض روی بروکر واقعی است — پوزیشن از حد مجاز ضرر رد نمی‌شود، مگر شما «نگه دار» زده
  باشید. «نگه دار» حد ضرر بروکر را به سطح اضطراری می‌برد (scout.emergency_sl_mult × فاصله،
  حداکثر scout.emergency_cap_usd دلار) و فقط هشدار/گزارش می‌آید. حد سود گذاشته نمی‌شود.
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
except Exception:                      # pragma: no cover
    import scout_tracker as trk
    import scout_ai
    import scout_control as ctl
    import scout_dashboard as dash
    import scout_stats
    import scout_followup as fu

API = "https://api.telegram.org/bot{}/{}"
# ستون‌های ثابت ژورنال — همهٔ رویدادها زیر یک سرستون (نسخهٔ قبل ستون‌ها را جابه‌جا می‌نوشت)
JOURNAL_FIELDS = ["ts", "event", "alert", "setup", "n_setups", "side", "bar", "price", "sl", "tp",
                  "atr", "spread", "risk_usd", "ticket", "entry", "profit", "ok", "note"]


# --------------------------------------------------------------------- telegram

SETUP_ABBR = {
    "kijun_pullback": "kp", "tenkan_momentum": "tm", "range_break": "rb",
    "pullback_resume": "pr", "cloud_break": "cb", "tk_cross": "tk", "rejection": "rj",
    "engulfing": "en", "ignition": "ig", "exhaustion": "ex",
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
            self.last_error = str(exc)                 # خطای قبلی («not modified») نماند
            logger.warning("telegram {} failed: {}", method, exc)
            return None

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
            logger.warning("telegram sendPhoto failed: {}", exc)
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

    # ---------------------------------------------------------------- journal
    def log(self, **row: Any) -> None:
        row.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        new = (not self.journal.exists()) or self.journal.stat().st_size == 0
        with self.journal.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

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

    def _usd(self, price_dist: float) -> float:
        return round(abs(float(price_dist)) * 100.0 * self.lot, 2)

    def open_risk_usd(self) -> float:
        """ضرر هر پوزیشن باز اگر حد ضرر فعلی‌اش (طبق سیاست) روی بروکر بخورد."""
        total = 0.0
        for info in self.open.values():
            if info.get("closing"):                        # در حال بسته شدن (عوض کردن جهت)
                continue
            try:
                stop = trk.desired_broker_sl(info, self.mode) if self.soft_stop else float(info["sl"])
                total += max(self._usd(float(info["entry"]) - stop), 0.0)
            except (KeyError, TypeError, ValueError):
                continue
        return round(total, 2)

    def new_risk_usd(self, side: str, entry: float, sl: float) -> float:
        if not self.soft_stop:
            return self._usd(entry - sl)
        emerg = trk.emergency_sl(side, entry, sl, self.emerg_mult, self.emerg_cap)
        stop = trk.desired_broker_sl({"sl": sl, "emerg": emerg, "hold": False}, self.mode)
        return self._usd(entry - stop)

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

    # ------------------------------------------------------- what-if
    def track_whatif(self, aid: str, p: dict, decision: str) -> None:
        if not self.whatif_enabled:
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
        scout = dash.scout_trades(events, self.lot)
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
        txt = (f"<b>{'🟢 BUY' if r.side=='BUY' else '🔴 SELL'}</b> · <code>{r.setup}</code>{head}\n"
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
        mid = self.tg.send(txt, self.alert_buttons(aid))
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
                             "ctx": ctx, "wctx": wctx}
        self.send_chart(aid, mid, r, entry, sl, tp, risk)
        self.log(event="alert", alert=aid, setup=str(r.get("setups_all", r.setup)),
                 n_setups=n_set, side=r.side, bar=str(r.bar),
                 price=entry, sl=round(sl, 2), tp=round(tp, 2), atr=round(r.atr, 3),
                 spread=spread_pts, risk_usd=risk)

    @staticmethod
    def alert_buttons(aid: str, analyze: bool = True) -> list:
        row = [{"text": "✅ تأیید", "callback_data": f"a|{aid}|y"},
               {"text": "❌ رد", "callback_data": f"a|{aid}|n"}]
        if analyze:
            row.append({"text": "🤖 تحلیل", "callback_data": f"x|{aid}"})
        return [row]

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
        for aid in [k for k, v in self.pending.items() if now - v["t"] > self.expiry]:
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
            self.tg.ack(cb, "رد شد")
            self.tg.edit(p["mid"], p["txt"] + "\n\n❌ <b>رد شد</b>")
            self.log(event="rejected", alert=aid, setup=r.setup, side=r.side)
            self.track_whatif(aid, p, "rejected")
            return
        if self.open and not self.dry:
            if hedge == "reverse" and self.opposite_of(r.side):
                if not self._reverse(aid, p, r, cb):
                    return
            new_risk = self.new_risk_usd(r.side, float(r.ref_close), float(p["sl"]))
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
        emerg = trk.emergency_sl(r.side, px, sl_now, self.emerg_mult, self.emerg_cap)
        draft = {"sl": round(sl_now, 2), "emerg": emerg, "hold": False}
        broker_sl = (round(trk.desired_broker_sl(draft, self.mode), 2)
                     if self.soft_stop else round(sl_now, 2))
        res = self.client.send_market_order(r.side, self.lot, sl=broker_sl,
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
                         "approach": False, "ai_next": 0.0, "ai_note": ""}
        self._save_state()
        self.tg.edit(p["mid"], p["txt"] + f"\n\n✅ <b>باز شد</b> #{tk} @ {res.price:.2f}\n{info_line}")
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
                 note=(f"broker_sl={broker_sl};emergency_sl={emerg};mode={self.mode}"
                       + (";over_cap=owner" if override else "")
                       + (f";hedge={hedge}" if hedge else "")) if self.soft_stop else None)
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
                     price=res.price, ok=res.ok, note="reverse")
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
        res = self.client.close_position(int(tk), comment=why)
        if not res.ok:
            self._closing_t.pop(int(tk), None)         # ناموفق: زدن دوبارهٔ «بستن» همین الان مجاز است
        if cb:
            self.tg.ack(cb, "بسته شد" if res.ok else f"خطا: {res.comment}")
        info = self.open.get(int(tk), {})
        self.tg.send(f"{'✅' if res.ok else '⚠️'} بستن #{tk}: {res.comment or 'OK'} @ {res.price:.2f}")
        self.log(event="closed", ticket=tk, setup=info.get("setup"), side=info.get("side"),
                 price=res.price, ok=res.ok)

    # -------------------------------------------------------------- monitoring
    def _close_buttons(self, tk: int, hold: Optional[bool] = None) -> list:
        """«بستن» + «نگه دار» یا «لغو نگه داشتن» بسته به وضعیت فعلی پوزیشن."""
        row = [{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]
        held = bool(self.open.get(int(tk), {}).get("hold")) if hold is None else hold
        row.append({"text": "▶️ لغو نگه داشتن", "callback_data": f"u|{tk}"} if held
                   else {"text": "⏸ نگه دار", "callback_data": f"h|{tk}"})
        return [row]

    def market_open(self) -> bool:
        return ctl.market_open_utc(datetime.now(timezone.utc))

    def monitor(self, f: pd.DataFrame) -> None:
        live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        now = time.time()
        mkt = self.market_open()
        server_now: Optional[float] = None
        for tk in list(self.open):
            if tk not in live:                       # دستی، در حد ضرر یا اضطراری بسته شده
                pnl, px = self.realized(tk)
                if pnl is None and now - self.open[tk].get("gone_t", now) < 30:
                    self.open[tk].setdefault("gone_t", now)   # تاریخچه هنوز نیامده
                    continue
                info = self.open.pop(tk)
                self._ai_futs.pop(tk, None)
                self._live.pop(tk, None)
                self._save_state()
                res_txt = f" · نتیجه ${pnl:+.2f}" if pnl is not None else ""
                emerg, risk = info.get("emerg"), float(info.get("risk") or 0.0)
                kind = None
                if px and risk > 0:
                    if emerg and abs(float(px) - float(emerg)) <= 0.25 * risk:
                        kind = "emergency_sl"
                    elif abs(float(px) - float(info["sl"])) <= 0.25 * risk:
                        kind = "signal_sl"
                why = {"emergency_sl": "\n🛑 با حد ضرر اضطراری بسته شد.",
                       "signal_sl": "\n⛔ در حد ضرر سیگنال بسته شد."}.get(kind or "", "")
                self.tg.send(f"ℹ️ پوزیشن #{tk} ({info['setup']}) بسته شد{res_txt}.{why}"
                             + ("\n🔔 هشدارها دوباره فعال شد." if not self.open else ""))
                if info.get("status_mid"):
                    self.tg.edit(info["status_mid"],
                                 f"📊 <b>#{tk}</b> · <code>{info['setup']}</code>\n"
                                 f"✅ <b>بسته شد</b>{res_txt}" + (f" @ {px:.2f}" if px else ""))
                self.log(event="gone", alert=info.get("alert"), ticket=tk, setup=info["setup"],
                         side=info["side"], entry=round(float(info["entry"]), 2), price=px, profit=pnl,
                         note=kind)
                continue
            p, info = live[tk], self.open[tk]
            profit = float(getattr(p, "profit", 0.0))
            swap = float(getattr(p, "swap", 0.0) or 0.0)
            price = float(getattr(p, "price_current", 0.0))
            self._live[tk] = {"price": price, "profit": profit + swap,
                              "broker_sl": float(getattr(p, "sl", 0.0) or 0.0)}
            d = trk.signed_move(info["side"], float(info["entry"]), price)
            if not info["r1"] and info["risk"] > 0 and d >= info["risk"]:
                info["r1"] = True
                self.tg.send(f"🎯 #{tk} ({info['setup']}) به ۱ برابر ریسک رسید. سود فعلی ${profit:.2f}",
                             self._close_buttons(tk))
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
                    continue                              # همین الان بسته شد
            if not mkt:                                   # یک پیام «بازار بسته» و بعد سکوت تا باز شدن
                if price > 0 and not info.get("closed_note"):
                    info["closed_note"] = True
                    self._push_status(tk, info, price, profit, swap, None, None, now, closed=True)
                continue
            if info.pop("closed_note", None):
                info["status_t"] = 0.0                    # با باز شدن بازار گزارش فوراً ادامه پیدا کند
            if price > 0 and now - float(info.get("status_t", 0.0)) >= self.status_every:
                if server_now is None:
                    tick = None
                    try:
                        tick = self.client.get_tick()
                    except Exception:                  # pragma: no cover
                        pass
                    server_now = float(getattr(tick, "time", 0) or 0) or None
                opened_srv = float(getattr(p, "time", 0) or 0)
                secs = (server_now - opened_srv) if (server_now and opened_srv) else None
                self._push_status(tk, info, price, profit, swap, secs, server_now, now)

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
        # (۴) حد ضرر بروکر = سطح درستِ همین سیاست
        want = round(trk.desired_broker_sl(info, self.mode), 2)
        have = float(getattr(p, "sl", 0.0) or 0.0)
        if abs(want - have) > 0.095 and now - self._sl_fail_t.get(tk, 0.0) > 60:   # زیر ۱۰ سنت = همان
            wrong_side = trk.beyond_soft_sl(side, price, want)
            if wrong_side:                              # حتی سطح اضطراری رد شده
                return self._close_at_stop(tk, info, price, profit, "عبور از حد ضرر اضطراری")
            res = self.client.modify_sltp(tk, sl=want)
            if res.ok:
                self._sl_fail_t.pop(tk, None)
                logger.info("scout #{} broker SL {} -> {} ({})", tk, have, want, pol)
            else:
                self._sl_fail_t[tk] = now
                logger.warning("scout #{} broker SL {} not set: {}", tk, want, res.comment)
        return False

    def _close_at_stop(self, tk: int, info: dict, price: float, profit: float, why: str) -> bool:
        """True = بسته شد. تلاش ناموفق هر close_retry ثانیه بی‌صدا تکرار می‌شود و پیامش
        فقط بار اول و بعد حداکثر هر close_fail_notify_minutes می‌آید."""
        now = time.time()
        if now - self._close_try_t.get(tk, -1e18) < self.close_retry:
            return False
        self._close_try_t[tk] = now
        res = self.client.close_position(int(tk), comment="scout_sl")
        if res.ok:
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
        self._save_state()
        if cb:
            self.tg.ack(cb, "نگه داشته شد" if on else "نگه داشتن لغو شد")
        if on:
            self.tg.send(f"⏸ #{tk} نگه داشته شد تا خودتان بگویید. حد ضرر بروکر به سطح اضطراری "
                         f"{float(info.get('emerg') or info['sl']):.2f} می‌رود؛ در حد ضرر سیگنال "
                         "دیگر هشدار تکراری نمی‌آید و گزارش ۵ دقیقه‌ای ادامه دارد.", self._close_buttons(tk))
        else:
            self.tg.send(f"▶️ #{tk}: نگه داشتن لغو شد · از این به بعد در حد ضرر: "
                         f"<b>{trk.POLICY_FA[self.mode]}</b>.", self._close_buttons(tk))
        self.log(event="hold" if on else "unhold", ticket=tk, setup=info["setup"], side=info["side"])

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

    def set_mode(self, mode: str, cb: Optional[str] = None) -> None:
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
        self.tg.send(f"⚙️ حالت ثبت شد: در حد ضرر <b>{trk.POLICY_FA[mode]}</b>."
                     + (f" حد ضرر {len(self.open)} پوزیشن باز هماهنگ می‌شود." if self.open else ""))
        self.log(event="mode", note=mode)

    # ------------------------------------------------------------------ state
    def _save_state(self) -> None:
        try:
            trk.save_state(self.state_path, self.open)
        except Exception as exc:                       # pragma: no cover
            logger.warning("scout state save failed: {}", exc)

    def _apply_caps(self, info: dict) -> None:
        """سقف‌های جدید روی پوزیشن‌های قدیمی هم اعمال شود."""
        side, entry = info["side"], float(info["entry"])
        info["sl"] = trk.capped_sl(side, entry, float(info["sl"]), self.sl_cap)
        info["risk"] = abs(entry - float(info["sl"]))
        info["emerg"] = trk.emergency_sl(side, entry, float(info["sl"]), self.emerg_mult, self.emerg_cap)

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
                "adopted": True, "emerg": None, "approach": False, "ai_next": 0.0, "ai_note": ""}
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

    def handle_update(self, u: dict) -> None:
        if not self.is_admin(u):
            logger.warning("scout: ignored telegram update from non-admin")
            return
        cq = u.get("callback_query")
        if cq:
            parts = str(cq.get("data", "")).split("|")
            if parts[0] == "a" and len(parts) == 3:
                code = parts[2]
                self.decide(parts[1], code in ("y", "o", "r", "b", "q"), cq["id"], override=code in ("o", "q"),
                            hedge={"r": "reverse", "b": "both", "q": "both"}.get(code))
            elif parts[0] == "c" and len(parts) == 2:
                self.close(int(parts[1]), cq["id"])
            elif parts[0] == "h" and len(parts) == 2:
                self.hold(int(parts[1]), cq["id"])
            elif parts[0] == "u" and len(parts) == 2:
                self.hold(int(parts[1]), cq["id"], on=False)
            elif parts[0] == "m" and len(parts) == 2:
                self.set_mode(parts[1], cq["id"])
            elif parts[0] == "x" and len(parts) == 2:
                self.analyze(parts[1], cq["id"])
            elif parts[0] == "p" and len(parts) == 2:
                self.price_alert_delete(parts[1], cq["id"])
            elif parts[0] == "k" and len(parts) == 2:
                self.tg.ack(cq["id"])
                self.panel(parts[1])
            return
        msg = str((u.get("message") or {}).get("text", "") or "").strip()
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
        elif cmd == "/alert":
            self.price_alert_cmd(msg)
        elif cmd == "/alerts":
            self.price_alerts_list()
        elif cmd == "/close":
            bits = msg.split()
            if len(bits) > 1 and bits[1].isdigit():
                self.close(int(bits[1]))

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
                self.tg.send("🔄 <b>ری‌استارت اسکات؟</b>\nپوزیشن‌های باز بسته نمی‌شوند و بعد از روشن شدن "
                             "دوباره زیر نظر می‌آیند. حدود ۳۰ ثانیه طول می‌کشد.",
                             [[{"text": "✅ بله، ری‌استارت", "callback_data": "k|restart_yes"},
                               {"text": "❌ نه", "callback_data": "k|restart_no"}]])
            elif what == "restart_yes":
                self.restart()
            elif what == "palert":
                self.price_alerts_list()
            elif what == "score":
                self.send_scoreboard(force=True)
            elif what == "restart_no":
                self.tg.send("ری‌استارت لغو شد.")
            elif what == "help":
                self.tg.send(ctl.HELP_TEXT.format(max_open=self.max_open, risk=self.max_total_risk,
                                                  sl=(self.sl_cap or 0) * 100 * self.lot,
                                                  em=(self.emerg_cap or 0) * 100 * self.lot,
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

    def restart(self) -> None:
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
        st = dash.build_stats(events, virtual=self._virtual_rows, actual_by_ticket=self._actual_map)
        bots: Dict[str, list] = {}
        for b in self.compare_bots:                    # فقط خواندن دیتابیس دو ربات دیگر
            try:
                bots[str(b.get("name"))] = dash.bot_trades(b.get("db", ""))
            except Exception as exc:
                logger.warning("scout dashboard: {} results unavailable ({})", b.get("name"), exc)
        st["compare"] = dash.build_compare(dash.scout_trades(events, self.lot), bots)
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
                            "left_min": max(0, int((self.expiry - (now - p["t"])) // 60)),
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
            "rules": {"lot": self.lot, "sl_cap": round((self.sl_cap or 0) * 100 * self.lot, 2),
                      "emerg_cap": round((self.emerg_cap or 0) * 100 * self.lot, 2),
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
    def run(self) -> None:
        if not self.client.connect():
            raise SystemExit("MT5 connect failed")
        acc = self.client.account_info()
        self.tg.start()
        self.tg._call("setMyCommands", commands=ctl.MENU_COMMANDS)
        self.tg.send(f"👀 <b>اسکات روشن شد</b>{' (آزمایشی)' if self.dry else ''}\n"
                     f"حساب {getattr(acc,'login','?')} ({self.account_kind(acc)}) · {getattr(acc,'server','?')} · "
                     f"موجودی ${getattr(acc,'balance',0):.2f}\nلات {self.lot} · انقضای هشدار "
                     f"{self.expiry//60} دقیقه\n{self._one_line_state()}\n"
                     "دکمه‌های پایین صفحه: 📋 پوزیشن‌ها (بستن/نگه داشتن) · 💰 قیمت · 📊 وضعیت · 🎛 منو",
                     keyboard=self.KEYBOARD)
        logger.info("scout online")
        self.restore()
        self.start_dashboard()
        self.news_note()                                # تقویم خبر همین‌جا بار شود، نه وسط اولین هشدار
        if self.selftest:
            self.fire_selftest()
        while True:
            try:
                self.beat()
                while not self.tg.q.empty():
                    self.handle_update(self.tg.q.get_nowait())

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
