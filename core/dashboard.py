"""Live web dashboard served from inside the bot process.

Zero new dependencies: a ``ThreadingHTTPServer`` on a daemon thread serving
one embedded dark RTL page plus a JSON API.  The page has two tabs: the live
dashboard (``/api/stats``), a plain-language Persian report (``/api/report``)
built from the signals table, and the bot's log file translated into plain
Persian (``/api/log``).  Telemetry is pulled through the
same hooks the Telegram panel uses, throttled by a snapshot cache so page
polls can never hammer MT5.

Settings (settings.yaml):
    dashboard:
      enabled: true
      host: "0.0.0.0"
      port: 8080
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from loguru import logger

from core.ai_gate.gate import ACTIONS_FA

__all__ = ["DashboardServer", "build_report", "translate_log_event"]

_SNAPSHOT_TTL = 3.0  # seconds a telemetry snapshot stays fresh


class DashboardServer:
    """Serves ``/`` (HTML) and ``/api/stats`` (JSON) from the live bot.

    When ``token`` is set, every request must carry ``?token=<value>``
    (page and API alike) — otherwise the dashboard is fully public.
    """

    def __init__(
        self,
        telemetry_provider: Callable[[], Dict[str, Any]],
        db: Any,
        host: str = "0.0.0.0",
        port: int = 8080,
        brand: str = "GoldBot",
        token: str = "",
        log_path: Optional[str] = None,
        ai_gate_status: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        ai_gate_report: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self._telemetry = telemetry_provider
        self._ai_gate_status = ai_gate_status
        self._ai_gate_report = ai_gate_report
        self._ai_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        self._db = db
        self._host = host
        self._port = int(port)
        self._brand = brand
        self._token = str(token or "").strip()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._cache_lock = threading.Lock()
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0.0
        self._started_at = time.time()
        self._events: "deque[Dict[str, Any]]" = deque(maxlen=400)
        self._events_lock = threading.Lock()
        self._sink_id: Optional[int] = None
        self._report_cache: Dict[str, Any] = {}
        self._report_ts: float = 0.0
        self._log_glob = _log_glob(log_path)
        self._log_cache: Dict[str, Any] = {}

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self._httpd is not None:
            return True
        outer = self

        class Handler(_BaseHandler):
            dashboard = outer

        class QuietServer(ThreadingHTTPServer):
            """Swallows connection-reset noise from internet port scanners."""

            daemon_threads = True

            def handle_error(self, request, client_address):  # noqa: N802
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                    return  # remote hung up mid-request — normal on the open internet
                super().handle_error(request, client_address)

        try:
            self._httpd = QuietServer((self._host, self._port), Handler)
            self._httpd.daemon_threads = True
        except OSError as exc:
            logger.error("dashboard could not bind {}:{} ({})", self._host, self._port, exc)
            self._httpd = None
            return False

        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="dashboard-http", daemon=True
        )
        self._thread.start()
        try:
            self._sink_id = logger.add(self._on_log, level="INFO", filter=_log_filter,
                                       format="{message}", enqueue=False)
        except Exception as exc:  # the report tab still works without the event feed
            logger.warning("dashboard event feed disabled: {}", exc)
            self._sink_id = None
        url = f"http://{self._host}:{self._port}/"
        if self._token:
            url += f"?token={self._token}"
        logger.success("📊 dashboard online → {}", url)
        return True

    def stop(self) -> None:
        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except ValueError:
                pass
            self._sink_id = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("dashboard stopped")

    # ------------------------------------------------------------------ data
    def snapshot(self, force: bool = False) -> Dict[str, Any]:
        """Cached telemetry + db stats; safe to call from any thread."""
        now = time.time()
        with self._cache_lock:
            fresh = (now - self._cache_ts) < _SNAPSHOT_TTL
            if fresh and self._cache and not force:
                return self._cache

        payload: Dict[str, Any] = {}
        try:
            payload["telemetry"] = self._telemetry() or {}
        except Exception as exc:
            logger.warning("dashboard telemetry failed: {}", exc)
            payload["telemetry"] = {"connected": False}
        try:
            payload["performance"] = self._db.signal_performance()
        except Exception:
            payload["performance"] = {}
        try:
            rows = self._db.recent_signals(limit=60)
        except Exception:
            rows = []
        payload["recent"] = rows
        try:
            payload["users"] = self._db.stats()
        except Exception:
            payload["users"] = {}
        payload["dashboard_age"] = int(now - self._started_at)

        with self._cache_lock:
            self._cache = payload
            self._cache_ts = now
        return payload


    # ------------------------------------------------------------ report tab
    def _on_log(self, message: Any) -> None:
        """loguru sink: keep a short, translated feed of what the bot did."""
        try:
            record = message.record
            ev = translate_log_event(record["message"], record["level"].name)
            if ev is None:
                return
            ts = record["time"].timestamp()
            with self._events_lock:
                # news blocks repeat on every bar inside the window: keep one per 15 min
                for old in reversed(self._events):
                    if ts - old["ts"] > 900:
                        break
                    if old["kind"] == ev["kind"] and old["text"] == ev["text"]:
                        return
                self._events.append({"ts": ts, "kind": ev["kind"], "text": ev["text"]})
        except Exception:
            pass  # a logging sink must never raise

    def report(self) -> Dict[str, Any]:
        now = time.time()
        with self._cache_lock:
            if self._report_cache and (now - self._report_ts) < _REPORT_TTL:
                return self._report_cache
        snap = self.snapshot()
        try:
            rows = self._db.recent_signals(limit=5000)
        except Exception as exc:
            logger.warning("dashboard report: signals unavailable: {}", exc)
            rows = []
        with self._events_lock:
            events = list(self._events)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        started = datetime.fromtimestamp(self._started_at, tz=timezone.utc).replace(tzinfo=None)
        ai_state = None
        if self._ai_gate_status is not None:
            try:
                ai_state = self._ai_gate_status()
            except Exception as exc:
                logger.warning("dashboard report: ai gate status unavailable: {}", exc)
        payload = build_report(rows, events, snap.get("telemetry") or {}, now_utc, started,
                               ai_gate=ai_state)
        payload["brand"] = self._brand
        payload["strategy"] = (snap.get("telemetry") or {}).get("strategy", "")
        with self._cache_lock:
            self._report_cache = payload
            self._report_ts = now
        return payload


    def ai_report(self) -> Dict[str, Any]:
        """AI tab payload (gate journal + stored simulations), cached briefly."""
        now = time.time()
        with self._cache_lock:
            if self._ai_cache and (now - self._ai_cache[0]) < _REPORT_TTL:
                return self._ai_cache[1]
        payload: Dict[str, Any] = {"available": self._ai_gate_report is not None}
        if self._ai_gate_report is not None:
            try:
                payload.update(self._ai_gate_report() or {})
            except Exception as exc:
                logger.warning("dashboard: ai gate report unavailable: {}", exc)
                payload["error"] = "گزارش هوش مصنوعی در دسترس نیست"
        with self._cache_lock:
            self._ai_cache = (now, payload)
        return payload

    def read_log(self, view: str = "important") -> Dict[str, Any]:
        """Translate the tail of the bot's own log file(s) for the log tab."""
        view = "all" if view == "all" else "important"
        now = time.time()
        with self._cache_lock:
            hit = self._log_cache.get(view)
            if hit and (now - hit[0]) < _REPORT_TTL:
                return hit[1]
        payload = read_log_files(self._log_glob, view)
        with self._cache_lock:
            self._log_cache[view] = (now, payload)
        return payload


def _log_filter(record: Dict[str, Any]) -> bool:
    return record["name"] in _LOG_SOURCES


# ============================================================================
# Plain-language report tab (/api/report)
# ============================================================================
_REPORT_TZ_MINUTES = 210        # Tehran, UTC+03:30 (no DST since 2022)
_REPORT_TZ_LABEL = "تهران"
_REPORT_TTL = 10.0              # seconds a report stays cached
_BE_BAND = 0.50                 # |net profit| below this ($) counts as break-even
_WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
_SIDE_FA = {"BUY": "خرید", "SELL": "فروش"}

# Log line -> plain Persian.  Each rule: (substring, kind, text).  ``text`` is a fixed string,
# a callable(msg) -> str, or None (= drop the line as noise).  First match wins.
def _rx(pattern: str, fmt: Callable[..., str]) -> Callable[[str], Optional[str]]:
    compiled = re.compile(pattern)

    def build(msg: str) -> Optional[str]:
        m = compiled.search(msg)
        return fmt(*m.groups()) if m else msg
    return build


def _news_text(msg: str) -> str:
    detail = _news_fa(msg.split("|", 1)[1].strip()) if "|" in msg else ""
    return "ورود به‌خاطر خبر مهم ممنوع بود" + (f": {detail}" if detail else "")


def _manage_text(what: str, ticket: str, old: str, new: str) -> str:
    label = "به نقطهٔ ورود (سربه‌سر)" if what == "breakeven" else "با حد ضرر متحرک (تریل)"
    return f"حد ضرر پوزیشن #{ticket} {label} رفت: {old} ← {new}"


_NEWS_OFFLINE = "تقویم اینترنتی خبرها در دسترس نبود؛ فایل خبرهای ذخیره‌شده استفاده می‌شود"

def _ai_line(mode: str, label: str, conf: str, action: str, reason: str) -> str:
    mode_fa = "سایه" if mode == "shadow" else ("فعال" if mode == "live" else mode)
    if label == "TAKE":
        verdict = f"ورود با اطمینان {conf}٪"
    elif label == "SKIP":
        verdict = f"رد با اطمینان {conf}٪"
    else:
        verdict = f"بدون نظر ({label})"
    return f"هوش مصنوعی ({mode_fa}): {verdict} → {ACTIONS_FA.get(action, action)} — {reason}"


_EVENT_RULES: List[tuple] = [
    # ---- AI pre-trade gate (core/ai_gate)
    ("🤖 AI gate [", "ai", _rx(r"🤖 AI gate \[(\w+)\] (\w+) (\d+)% → (\w+) \| (.*)$", _ai_line)),
    ("AI gate [live]: waiting", "ai", "هوش مصنوعی (فعال): منتظر نظر برای ورود…"),
    ("AI gate disabled", "ai", _rx(r"AI gate disabled: (.*)$",
        lambda why: "هوش مصنوعی غیرفعال است: " + ("کلید API یا مدل تنظیم نشده"
                                                  if "API key" in why else why))),
    ("AI gate ready", "ai", _rx(r"mode=(\w+) \((\w+)\) \| model=(\S*)",
        lambda m, src, model: f"هوش مصنوعی آماده است — حالت {m}، مدل {model}")),
    ("AI gate mode", "ai", _rx(r"AI gate mode (\w+) -> (\w+)",
        lambda a, b: f"حالت هوش مصنوعی از {a} به {b} تغییر کرد")),
    ("AI gate: delayed entry cancelled", "ai", _rx(r"cancelled[ ,]*\(?(.*?)\)?$",
        lambda why: f"ورود بعد از انتظار برای هوش مصنوعی لغو شد ({why})")),
    ("AI gate: a live decision is still pending", "ai",
     "سیگنال جدید نادیده گرفته شد: هنوز منتظر نظر هوش مصنوعی برای سیگنال قبلی"),
    ("AI gate config invalid", "error", _rx(r"off: (.*)$",
        lambda why: f"تنظیمات هوش مصنوعی نامعتبر است؛ خاموش ماند: {why}")),
    ("AI gate", "ai", lambda msg: "هوش مصنوعی: " + msg.split("AI gate", 1)[1].lstrip(" :")[:200]),
    # ---- trading decisions (main_live / risk_manager)
    ("سیگنال شناسایی شد", "signal", _rx(r"شد:\s*(.+?)\s*→\s*(BUY|SELL)\s*\((.*)\)\s*$",
        lambda bar, side, why: f"سیگنال {_SIDE_FA.get(side, side)} ({why}) روی کندل {bar} (ساعت سرور)")),
    ("بلاک خبری", "news", _news_text),
    ("DAILY GUARD ACTIVE", "guard", "گارد روزانه فعال شد؛ تا شروع روز بعد سرور ورود جدید ندارد"),
    ("risk guard", "guard", "گارد ریسک ورود را رد کرد: با کمترین حجم، ریسک از سقف مجاز بیشتر می‌شد"),
    ("forced-risk tighten", "risk", "حد ضرر کوچک‌تر شد تا ریسک از سقف مجاز بیشتر نشود"),
    ("trading min lot risks", "risk", _rx(r"\(([\d.]+)% of balance\)",
        lambda pct: f"با کمترین حجم (۰.۰۱ لات) وارد شد؛ ریسک این معامله {pct}٪ حساب است")),
    ("order not confirmed", "error", "بروکر سفارش را تأیید نکرد"),
    ("entry skipped", "error", "ورود انجام نشد: سیگنال تکراری یا سفارش نامعلوم"),
    ("SL move rejected", "error", "بروکر جابه‌جایی حد ضرر را رد کرد"),
    ("breakeven #", "manage", _rx(r"^(breakeven) #(\d+) SL ([\d.]+) -> ([\d.]+)", _manage_text)),
    ("trailing #", "manage", _rx(r"^(trailing) #(\d+) SL ([\d.]+) -> ([\d.]+)", _manage_text)),
    (" closed | ", "close", _rx(r"position #(\d+) closed \| \w+ \| \$(-?[\d.]+)",
        lambda t, p: f"پوزیشن #{t} بسته شد: {_money(float(p))}")),
    ("manual close #", "close", _rx(r"manual close #(\d+)", lambda t: f"پوزیشن #{t} به دستور ادمین بسته شد")),
    ("adopted existing position", "open", "پوزیشن باز قبلی پیدا شد و مدیریت می‌شود"),
    ("broker state unknown", "error", "وضعیت بروکر معلوم نبود؛ این دور بررسی نشد"),
    ("GOLD M5 BOT starting", "start", "ربات روشن شد"),
    ("shutdown requested", "stop", "ربات در حال خاموش شدن است"),
    ("RESTART requested", "start", "ری‌استارت ربات درخواست شد"),
    ("engine kill-switch -> PAUSED", "stop", "موتور ربات متوقف شد (ورود جدید ندارد)"),
    ("engine kill-switch -> RUNNING", "start", "موتور ربات دوباره فعال شد"),
    ("risk switched", "system", _rx(r"-> ([\d.]+)%", lambda v: f"درصد ریسک هر معامله به {v}٪ تغییر کرد")),
    ("daily digest skipped", "system", "گزارش روزانه فرستاده نشد: روز قبل معامله‌ای نبود"),
    ("daily digest for", "system", "گزارش روزانه به تلگرام فرستاده شد"),
    ("به همراه چارت به تلگرام", "system", "سیگنال با چارت به تلگرام فرستاده شد"),
    ("shadow verdict", "system", "داوری معاملهٔ دستی ثبت شد"),
    ("signal score", "system", None),
    ("entering main loop", "system", None),
    ("side gates effective", "system", None),
    # ---- broker link and orders (mt5_client)
    ("MT5 connected", "conn", _rx(r"balance=([\d.]+)", lambda b: f"به بروکر وصل شد؛ موجودی ${b}")),
    ("link LOST", "error", "ارتباط با بروکر قطع شد"),
    ("link RESTORED", "conn", "ارتباط با بروکر برگشت"),
    ("reconnected after", "conn", "دوباره به بروکر وصل شد"),
    ("reconnect exhausted", "error", "وصل شدن دوباره به بروکر ممکن نشد؛ ربات آفلاین مانده"),
    ("reconnect attempt", "error", "تلاش برای وصل شدن دوباره به بروکر"),
    ("Algo Trading is DISABLED", "error", "دکمهٔ Algo Trading در MT5 خاموش است؛ سفارش‌ها رد می‌شوند"),
    (" lots @ ", "open", _rx(r"^(BUY|SELL) ([\d.]+) lots @ ([\d.]+) \| sl=([\d.]+) tp=([\d.]+) ticket=(\d+)",
        lambda s, v, p, sl, tp, t: f"سفارش {_SIDE_FA.get(s, s)} {v} لات در {p} باز شد "
                                   f"(حد ضرر {sl}، حد سود {tp}، تیکت #{t})")),
    ("order rejected", "error", _rx(r"order rejected: \((\d+)\) (.*)$",
        lambda code, why: f"بروکر سفارش را رد کرد: {why} (کد {code})")),
    ("SLTP rejected", "error", "بروکر تغییر حد ضرر/حد سود را رد کرد"),
    ("SLTP updated", "system", None),
    ("closed position", "close", _rx(r"closed position (\d+)", lambda t: f"پوزیشن #{t} بسته شد")),
    ("fuzzy-matched gold symbol", "system", None),
    ("heartbeat thread", "system", None),
    # ---- news calendar and telegram
    ("no news data available", "error", "هیچ دادهٔ خبری در دسترس نیست؛ فیلتر خبر درست کار نمی‌کند"),
    ("calendar fetch failed", "system", _NEWS_OFFLINE),
    ("network calendar parsed but empty", "system", _NEWS_OFFLINE),
    ("news network refresh failed", "system", _NEWS_OFFLINE),
    ("calendar parse error", "system", _NEWS_OFFLINE),
    ("matching events from historical", "system", None),
    ("using local historical calendar", "system", None),
    ("cold-start", "system", None),
    ("background refresh started", "system", None),
    ("telegram controller online", "conn", "تلگرام ربات وصل شد"),
    ("telegram menu commands", "system", None),
    ("send_message to", "error", "ارسال پیام تلگرام ناموفق بود"),
    ("dashboard online", "system", None),       # contains the access token
    ("subscription database ready", "system", None),
    ("signal scores loaded", "system", None),
]
_LOG_SOURCES = ("__main__", "main_live", "core.risk_manager", "core.mt5_client",
                "core.news_filter", "core.telegram_bot", "core.ai_gate.gate", "core.ai_gate.config")
IMPORTANT_KINDS = ("signal", "news", "guard", "risk", "error", "close", "manage", "open",
                   "start", "stop", "conn", "ai")
_RE_TOKEN = re.compile(r"token=[^\s&]+")


def translate_log_event(message: str, level: str, keep_raw: bool = False) -> Optional[Dict[str, str]]:
    """Map one bot log line to ``{kind, text}`` in plain Persian, or None to ignore.

    ``keep_raw`` (log tab, "all" view): unknown lines are returned as kind ``raw``/``warn``
    instead of being dropped, and known noise is returned as ``system``.
    """
    msg = str(message or "").strip()
    for needle, kind, text in _EVENT_RULES:
        if needle not in msg:
            continue
        if text is None:
            if not keep_raw:
                return None
            return {"kind": "system", "text": _RE_TOKEN.sub("token=***", msg)[:300]}
        if callable(text):
            text = text(msg)
        return {"kind": kind, "text": text}
    if level in ("ERROR", "CRITICAL"):
        return {"kind": "error", "text": "خطا: " + _RE_TOKEN.sub("token=***", msg)[:300]}
    if not keep_raw:
        return None
    kind = "warn" if level == "WARNING" else "raw"
    return {"kind": kind, "text": _RE_TOKEN.sub("token=***", msg)[:300]}


def _money(v: float) -> str:
    # wrapped in Unicode LTR isolates so the sign stays on the left inside Persian (RTL) text
    return "\u2066" + ("+" if v >= 0 else "−") + f"${abs(v):,.2f}" + "\u2069"


_RE_NEWS_BEFORE = re.compile(r"(\d+) mins before [^:]*:\s*(.+)$")
_RE_NEWS_AFTER = re.compile(r"(\d+) mins after (.+?)(?: \(|$)")


def _news_fa(detail: str) -> str:
    m = _RE_NEWS_BEFORE.search(detail)
    if m:
        return f"{m.group(1)} دقیقه مانده به خبر {m.group(2).strip()}"
    m = _RE_NEWS_AFTER.search(detail)
    if m:
        return f"{m.group(1)} دقیقه بعد از خبر {m.group(2).strip()}"
    return detail


def _parse_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return None


def _ago(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} دقیقه پیش"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ساعت پیش"
    return f"{hours // 24} روز پیش"


def _duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} دقیقه"
    h, m = divmod(minutes, 60)
    return f"{h} ساعت" + (f" و {m} دقیقه" if m else "")


def _result(profit: float) -> str:
    if profit >= _BE_BAND:
        return "win"
    if profit <= -_BE_BAND:
        return "loss"
    return "be"


def _exit_kind(row: Dict[str, Any]) -> str:
    """Best guess of how a closed trade ended (the database does not store it)."""
    try:
        close = float(row.get("close_price") or 0)
        entry = float(row.get("entry") or 0)
        sl = float(row.get("sl") or 0)
        tp = float(row.get("tp") or 0)
        profit = float(row.get("profit") or 0)
    except (TypeError, ValueError):
        return "نامعلوم"
    tol = 1.0
    if tp and abs(close - tp) <= tol:
        return "حد سود"
    if sl and abs(close - sl) <= tol:
        return "حد ضرر"
    if abs(close - entry) <= tol and abs(profit) < _BE_BAND:
        return "سربه‌سر"
    if profit > 0:
        return "تریل با سود"
    return "حد ضرر جابه‌جاشده یا دستی"


def _stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t["result"] == "win")
    losses = sum(1 for t in trades if t["result"] == "loss")
    net = round(sum(t["profit"] for t in trades), 2)
    decided = wins + losses
    return {
        "trades": n, "wins": wins, "losses": losses, "be": n - wins - losses,
        "net": net,
        "win_rate": round(wins / decided * 100.0, 1) if decided else None,
        "best": round(max((t["profit"] for t in trades), default=0.0), 2),
        "worst": round(min((t["profit"] for t in trades), default=0.0), 2),
    }


_AI_MIN_SAMPLE = 20


def _ai_gate_section(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Plain-Persian summary of the AI gate for the report tab (None = hide section)."""
    if not state:
        return None
    source = "دستور تلگرام" if state.get("source") == "runtime" else "فایل تنظیمات"
    lines = [f"حالت: {state.get('mode_fa') or state.get('mode')} (از {source})."]
    if state.get("mode") != "off" and state.get("active_mode") == "off":
        why = state.get("disabled_reason")
        lines.append("⚠️ غیرفعال است: " + ("کلید API یا مدل تنظیم نشده."
                                           if why == "no_key" else f"{why}."))
    if state.get("model"):
        lines.append(f"مدل: {state.get('model')}")
    if state.get("mode") == "live":
        policy = "بدون نظر وارد می‌شود" if state.get("fail_policy") == "open" else "وارد نمی‌شود"
        lines.append(f"جلوی ورود را فقط وقتی می‌گیرد که با اطمینان ≥ {state.get('block_min_confidence')}٪ "
                     f"بگوید «وارد نشو». اگر جواب ندهد، ربات {policy}.")
    journal = state.get("journal") or {}
    today = journal.get("today") or {}
    if today.get("total"):
        lines.append(f"امروز: {today['total']} درخواست — «وارد شو» {today['take']['n']}، "
                     f"«وارد نشو» {today['skip']['n']}، خطا {today['errors']}، "
                     f"هزینه {_money(float(today.get('cost') or 0)).replace('+', '')}.")
    period = journal.get("period") or {}
    rows = []
    for key, name in (("take", "گفته «وارد شو»"), ("skip", "گفته «وارد نشو»")):
        g = period.get(key) or {}
        if not g:
            continue
        rows.append({
            "name": name, "n": g.get("n", 0), "closed": g.get("closed", 0),
            "net": g.get("net", 0.0), "avg": g.get("avg"),
            "note": "نمونه کم است" if (g.get("closed") or 0) < _AI_MIN_SAMPLE else "",
        })
    if period.get("blocked"):
        lines.append(f"در {journal.get('period_days', 30)} روز اخیر {period['blocked']} ورود را متوقف کرده است.")
    return {"lines": lines, "rows": rows, "days": journal.get("period_days", 30)}


def build_report(
    rows: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    telemetry: Dict[str, Any],
    now_utc: datetime,
    started_utc: datetime,
    tz_minutes: int = _REPORT_TZ_MINUTES,
    tz_label: str = _REPORT_TZ_LABEL,
    ai_gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Pure function: signals-table rows + log events -> plain-language report."""
    shift = timedelta(minutes=tz_minutes)
    local_now = now_utc + shift
    today = local_now.date()

    closed: List[Dict[str, Any]] = []
    open_rows: List[Dict[str, Any]] = []
    for r in rows:
        opened = _parse_utc(r.get("created_at"))
        if opened is None:
            continue
        side = str(r.get("side") or "").upper()
        layer = str(r.get("reason") or r.get("strategy") or "").strip()[:40]
        item = {
            "ticket": r.get("ticket"),
            "side": side,
            "side_fa": _SIDE_FA.get(side, side),
            "layer": layer,
            "entry": float(r.get("entry") or 0),
            "opened": (opened + shift).strftime("%Y-%m-%d %H:%M"),
            "_opened_utc": opened,
        }
        closed_at = _parse_utc(r.get("closed_at"))
        if closed_at is None:
            open_rows.append(item)
            continue
        profit = float(r.get("profit") or 0.0)
        item.update({
            "closed": (closed_at + shift).strftime("%Y-%m-%d %H:%M"),
            "close_price": float(r.get("close_price") or 0),
            "profit": round(profit, 2),
            "result": _result(profit),
            "exit": _exit_kind(r),
            "duration": _duration(closed_at - opened),
            "_closed_utc": closed_at,
            "_day": (closed_at + shift).date(),
        })
        closed.append(item)
    closed.sort(key=lambda t: t["_closed_utc"])

    def window(days: Optional[int]) -> List[Dict[str, Any]]:
        if days is None:
            return closed
        first = today - timedelta(days=days - 1)
        return [t for t in closed if t["_day"] >= first]

    periods = []
    for key, title, days in (("today", "امروز", 1), ("7d", "۷ روز اخیر", 7),
                             ("30d", "۳۰ روز اخیر", 30), ("all", "از ابتدا", None)):
        s = _stats(window(days))
        s.update({"key": key, "title": title})
        periods.append(s)

    # daily table: last 14 calendar days (weekends only when something closed)
    by_day: Dict[Any, List[Dict[str, Any]]] = {}
    for t in closed:
        by_day.setdefault(t["_day"], []).append(t)
    cum = round(sum(t["profit"] for t in closed if t["_day"] < today - timedelta(days=13)), 2)
    days_out = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        items = by_day.get(d, [])
        if not items and d.weekday() >= 5:
            continue
        s = _stats(items)
        cum = round(cum + s["net"], 2)
        days_out.append({"date": d.isoformat(), "weekday": _WEEKDAYS_FA[d.weekday()],
                         "trades": s["trades"], "wins": s["wins"], "losses": s["losses"],
                         "be": s["be"], "net": s["net"], "cum": cum})
    days_out.reverse()

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in closed:
        groups.setdefault(f"{t['side_fa']} — {t['layer'] or 'نامشخص'}", []).append(t)
    groups_out = []
    for name, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        s = _stats(items)
        s["name"] = name
        groups_out.append(s)

    # ------------------------------------------------------------- headline
    lines: List[str] = []
    connected = bool(telemetry.get("connected"))
    running = bool(telemetry.get("engine_running", True))
    if not connected:
        lines.append("❌ اتصال ربات به بروکر قطع است؛ الان معامله نمی‌کند.")
    elif not running:
        lines.append("⏸ ربات به بروکر وصل است ولی موتورش متوقف شده (دستور توقف)؛ ورود جدید ندارد.")
    else:
        lines.append("✅ ربات روشن است و به بروکر وصل است.")

    positions = telemetry.get("positions") or []
    if positions:
        for p in positions:
            side = _SIDE_FA.get(str(p.get("side", "")).upper(), p.get("side", ""))
            lines.append(f"📌 پوزیشن باز: {side} از {float(p.get('price_open') or 0):,.2f} — "
                         f"سود/زیان شناور الان {_money(float(p.get('profit') or 0))}.")
    else:
        lines.append("📌 الان پوزیشن بازی ندارد.")

    tday = periods[0]
    if tday["trades"]:
        lines.append(f"📅 امروز {tday['trades']} معامله بسته شد: {tday['wins']} سود، "
                     f"{tday['losses']} ضرر، {tday['be']} سربه‌سر — جمع {_money(tday['net'])}.")
    else:
        lines.append("📅 امروز هنوز معامله‌ای بسته نشده.")

    if closed:
        last = closed[-1]
        lines.append(f"🕘 آخرین معامله: {last['side_fa']}، {_ago(now_utc - last['_closed_utc'])} "
                     f"بسته شد با {_money(last['profit'])} ({last['exit']}).")

    since = now_utc - started_utc
    n_sig = sum(1 for e in events if e["kind"] == "signal")
    n_news = sum(1 for e in events if e["kind"] == "news")
    n_guard = sum(1 for e in events if e["kind"] == "guard")
    n_err = sum(1 for e in events if e["kind"] == "error")
    txt = f"🔎 از روشن شدن ربات ({_duration(since)} پیش): {n_sig} سیگنال"
    if n_news:
        txt += f"، {n_news} بار بلاک خبری"
    if n_guard:
        txt += f"، {n_guard} بار رد توسط گارد"
    if n_err:
        txt += f"، {n_err} خطا"
    txt += "."
    lines.append(txt)
    if n_sig == 0 and not positions:
        lines.append("ℹ️ در این مدت شرایط ورود استراتژی پیش نیامده؛ اگر گارد یا خطایی جلوی ورود را گرفته بود، "
                     "در فهرست رویدادها دیده می‌شد.")

    w7 = periods[1]
    if w7["trades"]:
        lines.append(f"📈 ۷ روز اخیر: {w7['trades']} معامله، جمع {_money(w7['net'])}.")

    def public(t: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in t.items() if not k.startswith("_")}

    return {
        "ai_gate": _ai_gate_section(ai_gate),
        "tz": tz_label,
        "generated": local_now.strftime("%Y-%m-%d %H:%M"),
        "headline": lines,
        "periods": periods,
        "days": days_out,
        "groups": groups_out,
        "open": [public(t) for t in open_rows],
        "trades": [public(t) for t in reversed(closed[-40:])],
        "events": [{"time": (datetime.fromtimestamp(e["ts"], tz=timezone.utc).replace(tzinfo=None)
                             + shift).strftime("%m-%d %H:%M"),
                    "kind": e["kind"], "text": e["text"]} for e in reversed(events[-60:])],
    }


# ============================================================================
# Log tab (/api/log): the bot's own log file, translated
# ============================================================================
_LOG_TAIL_BYTES = 3_000_000     # read at most this much from the end of the log files
_LOG_MAX_ITEMS = 400
_RE_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)? \| (\w+)\s*\| [^|]*\| ([\w.<>]+):[^ ]* - (.*)$"
)


def _log_glob(log_path: Optional[str]) -> Optional[str]:
    """``logs/ichimoku_{time:YYYY-MM-DD}.log`` -> absolute ``.../logs/ichimoku_*.log``."""
    if not log_path:
        return None
    pattern = re.sub(r"\{time[^}]*\}", "*", str(log_path))
    if not os.path.isabs(pattern):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = os.path.join(root, pattern)
    return os.path.normpath(pattern)


def _tail_lines(path: str, max_bytes: int) -> List[str]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 else lines          # first line may be cut


def parse_log_lines(lines: List[str], view: str = "important",
                    tz_minutes: int = _REPORT_TZ_MINUTES) -> List[Dict[str, Any]]:
    """Log lines (local-time stamps of the machine running the bot) -> translated events."""
    keep_raw = view == "all"
    shift = timedelta(minutes=tz_minutes)
    out: List[Dict[str, Any]] = []
    last_seen: Dict[tuple, float] = {}
    for line in lines:
        m = _RE_LOG_LINE.match(line)
        if not m:
            continue                                      # traceback / continuation line
        stamp, level, _name, message = m.groups()
        ev = translate_log_event(message, level.upper(), keep_raw=keep_raw)
        if ev is None:
            continue
        if not keep_raw and ev["kind"] not in IMPORTANT_KINDS:
            continue
        try:
            local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            ts = local.astimezone(timezone.utc).timestamp()   # naive = this machine's local time
        except (ValueError, OSError, OverflowError):
            continue
        key = (ev["kind"], ev["text"])
        if key in last_seen and ts - last_seen[key] < 900:   # repeated lines within 15 min
            continue
        last_seen[key] = ts
        tehran = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None) + shift
        out.append({"ts": ts, "day": tehran.strftime("%Y-%m-%d"),
                    "weekday": _WEEKDAYS_FA[tehran.weekday()], "time": tehran.strftime("%H:%M"),
                    "kind": ev["kind"], "text": ev["text"]})
    out.sort(key=lambda e: e["ts"])
    return out


def read_log_files(pattern: Optional[str], view: str = "important") -> Dict[str, Any]:
    if not pattern:
        return {"ok": False, "message": "مسیر فایل لاگ برای داشبورد مشخص نشده است.", "items": []}
    files = sorted(glob.glob(pattern), key=lambda f: os.path.getmtime(f))
    if not files:
        return {"ok": False, "message": "فایل لاگی پیدا نشد: " + os.path.basename(pattern), "items": []}
    lines: List[str] = []
    budget = _LOG_TAIL_BYTES
    for path in reversed(files[-3:]):                     # newest first until the budget is used
        if budget <= 0:
            break
        try:
            chunk = _tail_lines(path, budget)
            budget -= os.path.getsize(path)
        except OSError:
            continue
        lines = chunk + lines
    items = parse_log_lines(lines, view)[-_LOG_MAX_ITEMS:]
    items.reverse()
    for it in items:
        it.pop("ts", None)
    return {"ok": True, "view": view, "files": [os.path.basename(f) for f in files[-3:]],
            "items": items}


class _BaseHandler(BaseHTTPRequestHandler):
    dashboard: DashboardServer = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stdlib noise
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path_only = self.path.split("?")[0]
        # token gate: when configured, every request must carry ?token=<value>
        if self.dashboard._token:
            query = parse_qs(urlparse(self.path).query)
            supplied = (query.get("token") or [""])[0]
            if supplied != self.dashboard._token:
                self._send(403, "text/plain; charset=utf-8", b"forbidden")
                return
        if path_only.startswith("/api/ai"):
            try:
                body = json.dumps(self.dashboard.ai_report(), ensure_ascii=False, default=str).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            except Exception as exc:
                self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        elif path_only.startswith("/api/log"):
            try:
                view = (parse_qs(urlparse(self.path).query).get("view") or ["important"])[0]
                body = json.dumps(self.dashboard.read_log(view), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            except Exception as exc:
                self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        elif path_only.startswith("/api/report"):
            try:
                body = json.dumps(self.dashboard.report(), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            except Exception as exc:
                self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        elif path_only.startswith("/api/stats"):
            try:
                body = json.dumps(self.dashboard.snapshot()).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            except Exception as exc:
                self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        elif path_only in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


# ============================================================================
# Embedded page (dark, RTL, vanilla JS — no external resources)
# ============================================================================
_PAGE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GoldBot Dashboard</title>
<style>
:root{
  --bg:#131722; --panel:#1c2230; --panel2:#232a3b; --grid:#2a2e39;
  --text:#d1d4dc; --muted:#8a8f9e; --up:#26a69a; --down:#ef5350;
  --gold:#f5c542; --blue:#42a5f5;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
  font-family:Vazirmatn,Tahoma,'Segoe UI',sans-serif;padding:20px;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:center;
  padding:14px 18px;background:var(--panel);border-radius:14px;margin-bottom:16px}
.brand{font-size:20px;font-weight:700;color:var(--gold)}
.status{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--down)}
.dot.ok{background:var(--up);box-shadow:0 0 8px var(--up)}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
.card{background:var(--panel);border-radius:14px;padding:16px}
.card h3{font-size:12px;color:var(--muted);font-weight:400;margin-bottom:8px}
.big{font-size:26px;font-weight:700}
.big.up{color:var(--up)} .big.down{color:var(--down)} .big.gold{color:var(--gold)}
.price-row{display:flex;gap:14px;flex-wrap:wrap}
.pcard{flex:1;min-width:140px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);font-weight:400;text-align:right;padding:8px 6px;
  border-bottom:1px solid var(--grid)}
td{padding:8px 6px;border-bottom:1px solid var(--grid)}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:2px 10px;border-radius:8px;font-size:12px;font-weight:700}
.tag.buy{background:rgba(38,166,154,.16);color:var(--up)}
.tag.sell{background:rgba(239,83,80,.16);color:var(--down)}
.pos{color:var(--up);font-weight:700} .neg{color:var(--down);font-weight:700}
.muted{color:var(--muted);font-size:12px}
canvas{width:100%;height:120px;display:block}
section{margin-bottom:14px}
section h2{font-size:14px;color:var(--gold);margin-bottom:10px}
.empty{padding:18px;text-align:center;color:var(--muted);font-size:13px}
footer{text-align:center;color:var(--muted);font-size:11px;padding:16px 0}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{background:var(--panel);color:var(--muted);border:1px solid var(--grid);border-radius:10px;
  padding:8px 20px;font:inherit;font-size:14px;cursor:pointer}
.tab.on{background:var(--gold);border-color:var(--gold);color:var(--bg);font-weight:700}
.lines{list-style:none;display:grid;gap:8px;font-size:15px;line-height:1.9}
.pcards{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.small{font-size:12px;color:var(--muted);line-height:1.9}
.scroll{overflow-x:auto}
.ev{padding:6px 0;border-bottom:1px solid var(--grid);font-size:13px;line-height:1.8}
.ev:last-child{border-bottom:none}
.k-signal{color:var(--gold)} .k-news{color:var(--blue)} .k-guard,.k-error{color:var(--down)}
.k-close,.k-manage,.k-open,.k-conn{color:var(--up)} .k-start,.k-stop,.k-risk{color:var(--text)}
.k-system,.k-raw{color:var(--muted)} .k-warn{color:#ffb74d} .k-ai{color:#b39ddb}
#a-items table{min-width:880px} #a-groups table,#r-trades table{min-width:560px}
.tab{white-space:nowrap} .tabs{flex-wrap:wrap}
@media (max-width:600px){.tab{padding:7px 12px;font-size:13px}}
.day{color:var(--gold);font-size:13px;font-weight:700;padding:12px 0 4px}
@media (max-width:600px){body{padding:10px} td,th{padding:6px 4px;font-size:12px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">🥇 GoldBot <span style="color:var(--muted);font-size:12px" id="strategy"></span></div>
    <div class="status"><span class="dot" id="dot"></span><span id="status-text">…</span></div>
  </header>

  <nav class="tabs">
    <button class="tab on" data-tab="dash" onclick="showTab('dash')">داشبورد</button>
    <button class="tab" data-tab="report" onclick="showTab('report')">گزارش</button>
    <button class="tab" data-tab="log" onclick="showTab('log')">لاگ</button>
    <button class="tab" data-tab="ai" onclick="showTab('ai')">هوش مصنوعی</button>
  </nav>

  <div id="tab-dash">
  <div class="grid kpis">
    <div class="card"><h3>قیمت لحظه‌ای</h3><div class="big gold" id="price">—</div>
      <div class="muted" id="spread"></div></div>
    <div class="card"><h3>بالانس</h3><div class="big" id="balance">—</div>
      <div class="muted" id="server"></div></div>
    <div class="card"><h3>اکوییتی</h3><div class="big" id="equity">—</div>
      <div class="muted" id="floating"></div></div>
    <div class="card"><h3>سود شناور</h3><div class="big" id="profit">—</div>
      <div class="muted" id="atr"></div></div>
    <div class="card"><h3>وین‌ریت کل</h3><div class="big up" id="winrate">—</div>
      <div class="muted" id="trades"></div></div>
    <div class="card"><h3>سود خالص تاریخچه</h3><div class="big" id="net">—</div>
      <div class="muted" id="signals"></div></div>
  </div>

  <section>
    <h2>منحنی رشد (معاملات بسته‌شده)</h2>
    <div class="card"><canvas id="eq" width="1000" height="120"></canvas>
      <div class="muted" id="eq-note"></div></div>
  </section>

  <section>
    <h2>پوزیشن‌های باز</h2>
    <div class="card" id="positions"><div class="empty">پوزیشن بازی نیست</div></div>
  </section>

  <section>
    <h2>سیگنال‌های اخیر</h2>
    <div class="card" id="recent"><div class="empty">در حال بارگذاری…</div></div>
  </section>
  </div>

  <div id="tab-report" hidden>
    <section><h2>خلاصه به زبان ساده</h2>
      <div class="card"><ul class="lines" id="r-lines"><li class="muted">در حال بارگذاری…</li></ul>
      <div class="small" id="r-note"></div></div></section>
    <section><h2>نتیجه‌ها (خالص، بعد از کمیسیون و سواپ)</h2>
      <div class="grid pcards" id="r-periods"></div></section>
    <section><h2>روز به روز — ۱۴ روز اخیر</h2>
      <div class="card scroll" id="r-days"></div></section>
    <section><h2>به تفکیک جهت و نوع سیگنال (از ابتدا)</h2>
      <div class="card scroll" id="r-groups"></div></section>
    <section id="r-ai-sec" hidden><h2>فیلتر هوش مصنوعی</h2>
      <div class="card"><ul class="lines" id="r-ai-lines"></ul>
      <div class="scroll" id="r-ai-table" style="margin-top:10px"></div></div></section>
    <section><h2>معاملات اخیر</h2>
      <div class="card scroll" id="r-trades"></div></section>
    <section><h2>کارهای ربات از آخرین روشن شدن</h2>
      <div class="card" id="r-events"></div></section>
    <div class="card small">
      راهنما: همهٔ ساعت‌ها به وقت تهران است، به‌جز «ساعت سرور» که ساعت MT5 بروکر است.
      «سربه‌سر» یعنی سود یا زیان کمتر از ۵۰ سنت. «درصد برد» بدون معاملات سربه‌سر حساب می‌شود.
      «نوع خروج» تخمینی است (قیمت خروج با حد سود و حد ضرر اولیه مقایسه می‌شود).
      سودها از تاریخچهٔ خود بروکر و خالص است. فهرست «کارهای ربات» در حافظه است و با ری‌استارت از نو شروع می‌شود.
    </div>
  </div>

  <div id="tab-log" hidden>
    <section><h2>لاگ ربات به زبان ساده</h2>
      <div class="tabs">
        <button class="tab on" data-view="important" onclick="setView('important')">فقط موارد مهم</button>
        <button class="tab" data-view="all" onclick="setView('all')">همه (با پیام‌های فنی)</button>
      </div>
      <div class="card" id="l-items"><div class="empty">در حال بارگذاری…</div></div>
      <div class="small" id="l-note" style="margin-top:8px"></div>
    </section>
    <div class="card small">
      رنگ‌ها: <span class="k-signal">سیگنال</span> · <span class="k-news">خبر</span> ·
      <span class="k-guard">گارد و خطا</span> · <span class="k-close">باز و بسته شدن و مدیریت پوزیشن</span> ·
      <span class="k-ai">هوش مصنوعی</span> ·
      <span class="k-system">پیام فنی</span>. ساعت‌ها به وقت تهران است.
      این صفحه از فایل لاگ خود ربات خوانده می‌شود و با ری‌استارت پاک نمی‌شود (حدود چند روز اخیر).
      پیام‌های تکراری در فاصلهٔ ۱۵ دقیقه یک بار نشان داده می‌شوند.
    </div>
  </div>

  <div id="tab-ai" hidden>
    <section><h2>هوش مصنوعی — وضعیت</h2>
      <div class="card"><ul class="lines" id="a-status"><li class="muted">در حال بارگذاری…</li></ul></div></section>
    <section id="a-body-sec">
      <h2>دقت و اثر نظرها</h2>
      <div class="grid kpis" id="a-kpis"></div>
    </section>
    <section id="a-verdict-sec"><h2>آیا حالت live را روشن کنیم؟</h2>
      <div class="card" id="a-verdict"></div></section>
    <section id="a-groups-sec"><h2>«وارد شو» در برابر «وارد نشو»</h2>
      <div class="card scroll" id="a-groups"></div></section>
    <section id="a-buckets-sec"><h2>بر حسب میزان اطمینان</h2>
      <div class="card scroll" id="a-buckets"></div></section>
    <section><h2>تصمیم‌های اخیر</h2>
      <div class="card scroll" id="a-items"></div></section>
    <div class="card small">
      راهنما: برای هر نظر، ربات همان معامله را با قوانین خودش (حد ضرر، حد سود، سربه‌سر و تریل، با اسپرد) روی
      کندل‌های یک‌دقیقه‌ای بروکر شبیه‌سازی می‌کند — چه وارد شده باشد چه نه — تا «وارد شو» و «وارد نشو» منصفانه مقایسه شوند.
      نظر «درست» است اگر «وارد شو» با سود بیش از ۵۰ سنت یا «وارد نشو» با ضرر بیش از ۵۰ سنت تمام شده باشد؛ نزدیک صفر = خنثی.
      «اثر» یعنی اگر فقط «وارد شو»ها گرفته می‌شد، چقدر بیشتر (یا کمتر) سود می‌کردیم؛ بازهٔ ۹۰٪ نشان می‌دهد این عدد چقدر مطمئن است.
      شبیه‌سازی هر ۵ دقیقه به‌روز می‌شود و تا بسته شدن معامله (حداکثر ۴۸ ساعت) «در جریان» است. ساعت‌ها به وقت تهران.
    </div>
  </div>

  <footer>GoldBot Dashboard · آپدیت خودکار هر ۵ ثانیه · <span id="uptime"></span></footer>
</div>

<script>
const $=id=>document.getElementById(id);
const fmt=n=>(n??0).toLocaleString('en-US',{maximumFractionDigits:2});
const money=n=>(n>=0?'+':'')+'$'+fmt(n);

function drawEquity(rows){
  const cv=$('eq'),ctx=cv.getContext('2d');
  ctx.clearRect(0,0,cv.width,cv.height);
  const closed=rows.filter(r=>r.closed_at).slice().reverse();
  if(closed.length<2){$('eq-note').textContent='برای رسم منحنی حداقل ۲ معامله‌ی بسته لازم است';return;}
  let run=0;const eq=closed.map(r=>run+=(+r.profit||0));
  const W=cv.width,H=cv.height,min=Math.min(...eq,0),max=Math.max(...eq,0),pad=8;
  const y=v=>H-pad-((v-min)/((max-min)||1))*(H-2*pad);
  const x=i=>pad+(i/(eq.length-1))*(W-2*pad);
  ctx.strokeStyle='#2a2e39';ctx.beginPath();ctx.moveTo(0,y(0));ctx.lineTo(W,y(0));ctx.stroke();
  ctx.beginPath();eq.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));
  ctx.strokeStyle=eq[eq.length-1]>=0?'#26a69a':'#ef5350';ctx.lineWidth=2;ctx.stroke();
  ctx.lineTo(x(eq.length-1),H);ctx.lineTo(x(0),H);ctx.closePath();
  ctx.fillStyle=eq[eq.length-1]>=0?'rgba(38,166,154,.12)':'rgba(239,83,80,.12)';ctx.fill();
  $('eq-note').textContent=eq.length+' معامله · جمع: '+money(eq[eq.length-1]);
}

function render(d){
  const t=d.telemetry||{},p=d.performance||{};
  const ok=t.connected&&t.engine_running;
  $('dot').className='dot'+(ok?' ok':'');
  $('status-text').textContent=ok?'ربات فعال':(t.connected?'موتور متوقف':'قطع از سرور');
  $('strategy').textContent=t.strategy?('· '+t.strategy):'';
  if(t.bid){$('price').textContent=fmt(t.bid);
    $('spread').textContent='اسپرد: '+fmt(t.spread_points)+' پوینت';}
  if(t.balance){$('balance').textContent='$'+fmt(t.balance);
    $('server').textContent=t.server||'';}
  if(t.equity){$('equity').textContent='$'+fmt(t.equity);
    $('floating').textContent='لوریج 1:'+(t.leverage||'—');}
  const pr=+t.profit||0;
  $('profit').textContent=money(pr);
  $('profit').className='big '+(pr>=0?'up':'down');
  $('atr').textContent=t.atr?('ATR: '+fmt(t.atr)):'';
  if(p.trades){$('winrate').textContent=fmt(p.win_rate)+'%';
    $('trades').textContent=p.trades+' معامله ('+p.wins+'W/'+p.losses+'L)';}
  if(p.net!==undefined){const n=+p.net||0;
    $('net').textContent=money(n);$('net').className='big '+(n>=0?'up':'down');
    $('signals').textContent=(d.telemetry.signals_sent||0)+' سیگنال ارسال‌شده';}
  $('uptime').textContent='آپ‌تایم ربات: '+(t.uptime||'—');

  const pos=t.positions||[];
  $('positions').innerHTML=pos.length?('<table><tr><th>تیکت</th><th>جهت</th><th>حجم</th>'+
    '<th>ورود</th><th>SL</th><th>TP</th><th>سود</th></tr>'+
    pos.map(x=>'<tr><td>#'+x.ticket+'</td><td><span class="tag '+
      (x.side==='BUY'?'buy">BUY':'sell">SELL')+'</span></td><td>'+x.volume+
      '</td><td>'+fmt(x.price_open)+'</td><td>'+fmt(x.sl)+'</td><td>'+fmt(x.tp)+
      '</td><td class="'+(x.profit>=0?'pos':'neg')+'">'+money(x.profit)+
      '</td></tr>').join('')+'</table>'):'<div class="empty">پوزیشن بازی نیست</div>';

  const rows=d.recent||[];
  $('recent').innerHTML=rows.length?('<table><tr><th>زمان</th><th>جهت</th>'+
    '<th>ورود</th><th>خروج</th><th>نتیجه</th><th>وضعیت</th></tr>'+
    rows.slice(0,15).map(r=>'<tr><td class="muted">'+(r.created_at||'')+'</td>'+
    '<td><span class="tag '+(r.side==='BUY'?'buy">BUY':'sell">SELL')+'</span></td>'+
    '<td>'+fmt(r.entry)+'</td><td>'+(r.closed_at?fmt(r.close_price):'—')+'</td>'+
    '<td class="'+((+r.profit||0)>=0?'pos':'neg')+'">'+
    (r.closed_at?money(+r.profit||0):'—')+'</td>'+
    '<td class="muted">'+(r.closed_at?(r.outcome||''):'در جریان')+'</td></tr>').join('')+
    '</table>'):'<div class="empty">هنوز سیگنالی ثبت نشده</div>';
  drawEquity(rows);
}

async function tick(){
  try{const r=await fetch('/api/stats'+location.search);render(await r.json());}
  catch(e){$('status-text').textContent='خطای ارتباط';$('dot').className='dot';}
}
tick();setInterval(tick,5000);

// ------------------------------------------------------------- report tab
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const iso=s=>'\u2066'+s+'\u2069';
const usd=n=>{n=+n||0;return iso((n>0?'+':(n<0?'−':''))+'$'+fmt(Math.abs(n)));};
const pn=n=>(+n>0?'pos':(+n<0?'neg':''));
const RES={win:'سود',loss:'ضرر',be:'سربه‌سر'};
function table(head,rows){return '<table><tr>'+head.map(h=>'<th>'+h+'</th>').join('')+'</tr>'+
  rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</table>';}
function renderReport(d){
  $('r-lines').innerHTML=(d.headline||[]).map(x=>'<li>'+esc(x)+'</li>').join('');
  $('r-note').textContent='به‌روزرسانی: '+d.generated+' (وقت '+d.tz+')'+(d.strategy?' · '+d.strategy:'');
  $('r-periods').innerHTML=(d.periods||[]).map(p=>'<div class="card"><h3>'+esc(p.title)+'</h3>'+
    '<div class="big '+(p.net>0?'up':(p.net<0?'down':''))+'">'+usd(p.net)+'</div><div class="small">'+
    (p.trades?(p.trades+' معامله: '+p.wins+' سود · '+p.losses+' ضرر · '+p.be+' سربه‌سر'+
      (p.win_rate!==null?'<br>درصد برد: '+iso(p.win_rate+'%'):'')+
      '<br>بهترین '+usd(p.best)+' · بدترین '+usd(p.worst)):'معامله‌ای نبوده')+'</div></div>').join('');
  const days=d.days||[];
  $('r-days').innerHTML=days.length?table(['تاریخ','روز','معامله','سود / ضرر / سربه‌سر','جمع روز','جمع از ابتدا'],
    days.map(x=>[x.date,x.weekday,x.trades||'—',x.trades?(x.wins+' / '+x.losses+' / '+x.be):'—',
      '<span class="'+pn(x.net)+'">'+(x.trades?usd(x.net):'—')+'</span>','<span class="'+pn(x.cum)+'">'+usd(x.cum)+'</span>'])):
    '<div class="empty">معامله‌ای نیست</div>';
  const g=d.groups||[];
  $('r-groups').innerHTML=g.length?table(['نوع','معامله','سود','ضرر','سربه‌سر','درصد برد','جمع'],
    g.map(x=>[esc(x.name),x.trades,x.wins,x.losses,x.be,x.win_rate===null?'—':iso(x.win_rate+'%'),
      '<span class="'+pn(x.net)+'">'+usd(x.net)+'</span>'])):'<div class="empty">معامله‌ای نیست</div>';
  const t=d.trades||[];
  $('r-trades').innerHTML=t.length?table(['باز شد','بسته شد','جهت','نوع سیگنال','ورود ← خروج','نتیجه','نوع خروج','مدت'],
    t.map(x=>['<span class="muted">'+x.opened+'</span>','<span class="muted">'+x.closed+'</span>',
      '<span class="tag '+(x.side==='BUY'?'buy':'sell')+'">'+x.side_fa+'</span>',esc(x.layer),
      fmt(x.entry)+' ← '+fmt(x.close_price),
      '<span class="'+pn(x.profit)+'">'+usd(x.profit)+'</span> <span class="muted">('+RES[x.result]+')</span>',
      esc(x.exit),x.duration])):'<div class="empty">هنوز معامله‌ی بسته‌ای نیست</div>';
  const ai=d.ai_gate;
  $('r-ai-sec').hidden=!ai;
  if(ai){
    $('r-ai-lines').innerHTML=(ai.lines||[]).map(x=>'<li>'+esc(x)+'</li>').join('');
    const ar=ai.rows||[];
    $('r-ai-table').innerHTML=ar.length?table(['نظر هوش مصنوعی ('+ai.days+' روز)','تعداد','بسته‌شده','جمع سود','میانگین هر معامله',''],
      ar.map(x=>[esc(x.name),x.n,x.closed,'<span class="'+pn(x.net)+'">'+usd(x.net)+'</span>',
        x.avg===null?'—':'<span class="'+pn(x.avg)+'">'+usd(x.avg)+'</span>','<span class="muted">'+esc(x.note)+'</span>'])):'';
  }
  const ev=d.events||[];
  $('r-events').innerHTML=ev.length?ev.map(e=>'<div class="ev"><span class="muted">'+e.time+'</span> &nbsp; '+
    '<span class="k-'+e.kind+'">'+esc(e.text)+'</span></div>').join(''):
    '<div class="empty">از آخرین روشن شدن، رویدادی ثبت نشده</div>';
}
async function loadReport(){
  try{const r=await fetch('/api/report'+location.search);const d=await r.json();
    if(d.error)throw new Error(d.error);renderReport(d);}
  catch(e){$('r-lines').innerHTML='<li class="neg">خطا در گرفتن گزارش</li>';}
}
let logView='important';
function renderLog(d){
  const it=d.items||[];
  if(!d.ok){$('l-items').innerHTML='<div class="empty">'+esc(d.message||'لاگ در دسترس نیست')+'</div>';$('l-note').textContent='';return;}
  let html='',day='';
  it.forEach(e=>{if(e.day!==day){day=e.day;html+='<div class="day">'+e.weekday+' '+e.day+'</div>';}
    html+='<div class="ev"><span class="muted">'+e.time+'</span> &nbsp; <span class="k-'+e.kind+'">'+esc(e.text)+'</span></div>';});
  $('l-items').innerHTML=html||'<div class="empty">موردی نیست</div>';
  $('l-note').textContent=it.length+' مورد · فایل: '+(d.files||[]).join('، ');
}
async function loadLog(){
  try{const q=location.search+(location.search?'&':'?')+'view='+logView;
    const r=await fetch('/api/log'+q);const d=await r.json();if(d.error)throw new Error(d.error);renderLog(d);}
  catch(e){$('l-items').innerHTML='<div class="empty neg">خطا در گرفتن لاگ</div>';}
}
function setView(v){logView=v;
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('on',b.dataset.view===v));loadLog();}
const CORR={right:'<span class="pos">✔ درست</span>',wrong:'<span class="neg">✘ غلط</span>',neutral:'<span class="muted">خنثی</span>'};
const EXIT_FA={tp:'حد سود',sl:'حد ضرر',be:'سربه‌سر',trail:'تریل',horizon:'۴۸ ساعت',no_data:'بدون داده'};
function renderAI(d){
  const st=d.status||null, rp=d.report||null;
  const lines=[];
  if(!d.available||!st){lines.push('هوش مصنوعی در این ربات در دسترس نیست.');}
  else{
    lines.push('حالت: '+esc(st.mode_fa||st.mode)+' (از '+(st.source==='runtime'?'دستور تلگرام':'فایل تنظیمات')+')');
    if(st.mode==='off')lines.push('برای روشن کردن در تلگرام بزن: /aigate shadow');
    if(st.mode!=='off'&&st.active_mode==='off')lines.push('⚠️ غیرفعال: '+(st.disabled_reason==='no_key'?'کلید API یا مدل تنظیم نشده':esc(st.disabled_reason)));
    if(st.model)lines.push('مدل: '+esc(st.model));
    if(st.mode==='live')lines.push('جلوی ورود را می‌گیرد اگر با اطمینان ≥ '+st.block_min_confidence+'٪ بگوید «وارد نشو». اگر جواب ندهد: '+(st.fail_policy==='open'?'بدون نظر وارد می‌شود':'وارد نمی‌شود')+'.');
  }
  if(d.error)lines.push('⚠️ '+esc(d.error));
  $('a-status').innerHTML=lines.map(x=>'<li>'+esc(x)+'</li>').join('');
  const has=!!rp;
  ['a-body-sec','a-verdict-sec','a-groups-sec','a-buckets-sec'].forEach(id=>$(id).hidden=!has);
  if(!has){$('a-items').innerHTML='<div class="empty">هنوز هیچ نظری ثبت نشده</div>';return;}
  const acc=rp.accuracy||{}, up=rp.uplift||{};
  const card=(t,v,sub,c)=>'<div class="card"><h3>'+t+'</h3><div class="big '+(c||'')+'">'+v+'</div><div class="muted">'+sub+'</div></div>';
  $('a-kpis').innerHTML=
    card('تصمیم‌ها',rp.ok,(rp.total-rp.ok)+' خطا/تأخیر · '+(rp.pending||0)+' در انتظار نتیجه')+
    card('دقت',acc.pct===null||acc.pct===undefined?'—':iso(acc.pct+'%'),(acc.right||0)+' درست · '+(acc.wrong||0)+' غلط · '+(acc.neutral||0)+' خنثی',acc.pct>=50?'up':(acc.pct===null?'':'down'))+
    card('اثر اگر به حرفش گوش می‌دادیم',usd(up.uplift||0),'بازهٔ ۹۰٪: '+usd(up.lo||0)+' تا '+usd(up.hi||0),(up.uplift||0)>0?'up':((up.uplift||0)<0?'down':''))+
    card('هزینه و سرعت','$'+fmt(rp.cost||0),'تأخیر معمول: '+(rp.latency_p50===null?'—':(rp.latency_p50/1000).toFixed(1)+' ثانیه'));
  const v=rp.verdict||{checks:[]};
  $('a-verdict').innerHTML='<div class="big '+(v.go_live?'up':'')+'" style="font-size:18px;margin-bottom:8px">'+
    (v.go_live?'✅ بله — شرایط از پیش تعیین‌شده برقرار است':'⏳ هنوز نه')+'</div>'+
    v.checks.map(c=>'<div class="ev">'+(c.ok?'<span class="pos">✔</span> ':'<span class="neg">✘</span> ')+esc(c.text)+'</div>').join('');
  const g=[['گفته «وارد شو»',rp.take,rp.actual.TAKE],['گفته «وارد نشو»',rp.skip,rp.actual.SKIP]];
  $('a-groups').innerHTML=table(['نظر','تعداد ارزیابی‌شده','جمع (شبیه‌سازی)','میانگین هر معامله','نتیجهٔ واقعی (معاملات باز شده)'],
    g.map(x=>[x[0],x[1].n,'<span class="'+pn(x[1].sum)+'">'+usd(x[1].sum)+'</span>',
      x[1].mean===null?'—':'<span class="'+pn(x[1].mean)+'">'+usd(x[1].mean)+'</span>',
      x[2].n?('<span class="'+pn(x[2].sum)+'">'+usd(x[2].sum)+'</span> <span class="muted">('+x[2].n+')</span>'):'—']));
  const bk=rp.buckets||{}; const keys=Object.keys(bk);
  $('a-buckets').innerHTML=keys.length?table(['اطمینان','«وارد شو» (تعداد · میانگین)','«وارد نشو» (تعداد · میانگین)'],
    keys.map(k=>{const b=bk[k];const f=o=>o&&o.n?(o.n+' · <span class="'+pn(o.mean)+'">'+usd(o.mean)+'</span>'):'—';
      return [iso(k+'٪'),f(b.TAKE),f(b.SKIP)];})):'<div class="empty">هنوز ارزیابی کاملی نیست</div>';
  const it=d.items||[];
  $('a-items').innerHTML=it.length?table(['زمان','جهت','نوع','نظر','دلیل اصلی','اقدام','نتیجهٔ واقعی','شبیه‌سازی','ارزیابی'],
    it.map(x=>{
      const verdict=x.status==='ok'?((x.decision==='TAKE'?'<span class="pos">ورود</span>':'<span class="neg">رد</span>')+' '+iso((x.confidence||0)+'٪'))
        :'<span class="muted">بدون نظر ('+esc(x.status)+')</span>';
      const sim=x.sim_profit===null||x.sim_profit===undefined?'<span class="muted">در انتظار</span>'
        :'<span class="'+pn(x.sim_profit)+'">'+usd(x.sim_profit)+'</span> <span class="muted">('+(EXIT_FA[x.sim_exit]||esc(x.sim_exit))+(x.sim_done?'':' · در جریان')+')</span>';
      return ['<span class="muted">'+x.time+'</span>','<span class="tag '+(x.side==='BUY'?'buy':'sell')+'">'+(x.side==='BUY'?'خرید':'فروش')+'</span>',
        esc(x.layer||''),verdict,esc((x.reasons||[])[0]||x.error||''),esc(x.action_fa||'—'),
        x.actual_profit===null||x.actual_profit===undefined?'—':'<span class="'+pn(x.actual_profit)+'">'+usd(x.actual_profit)+'</span>',
        sim,x.correct?CORR[x.correct]:'<span class="muted">—</span>'];})):'<div class="empty">هنوز هیچ نظری ثبت نشده</div>';
}
async function loadAI(){
  try{const r=await fetch('/api/ai'+location.search);const d=await r.json();if(d.error&&!d.status)throw new Error(d.error);renderAI(d);}
  catch(e){$('a-status').innerHTML='<li class="neg">خطا در گرفتن گزارش هوش مصنوعی</li>';}
}
function showTab(name){
  ['dash','report','log','ai'].forEach(t=>$('tab-'+t).hidden=name!==t);
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  history.replaceState(null,'',location.pathname+location.search+(name==='dash'?'':'#'+name));
  if(name==='report')loadReport();
  if(name==='log')loadLog();
  if(name==='ai')loadAI();
}
setInterval(()=>{if(!$('tab-report').hidden)loadReport();if(!$('tab-log').hidden)loadLog();if(!$('tab-ai').hidden)loadAI();},15000);
showTab(['#report','#log','#ai'].includes(location.hash)?location.hash.slice(1):'dash');
</script>
</body>
</html>
"""
