"""داشبورد وب اسکات — مثل داشبورد دو ربات دیگر، از داخل خود پروسهٔ اسکات.

http://SERVER_IP:8082/?token=...   (پورت در scout.dashboard.port)

- بدون وابستگی تازه: ThreadingHTTPServer روی یک نخ daemon.
- توکن از env (DASHBOARD_TOKEN_SCOUT یا DASHBOARD_TOKEN، همان توکن دو داشبورد دیگر) یا از
  scout.dashboard.token. بدون توکن داشبورد اصلاً روشن نمی‌شود (هیچ‌وقت عمومی نیست).
- نخ وب هیچ‌وقت MT5 را صدا نمی‌زند: فقط عکس‌لحظه‌ای را می‌خواند که حلقهٔ اصلی می‌سازد، به‌علاوهٔ
  ژورنال روی دیسک. فقط خواندنی است؛ هیچ دکمه‌ای سفارش نمی‌دهد یا چیزی را تغییر نمی‌دهد.
"""
from __future__ import annotations

import csv
import hmac
import sqlite3
import json
import math
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from loguru import logger

TEHRAN = timezone(timedelta(hours=3, minutes=30))     # ایران از ۱۴۰۱ ساعت تابستانی ندارد

EVENT_FA = {
    "alert": "🔔 هشدار", "suppressed": "🔕 بی‌صدا", "opened": "✅ باز شد", "rejected": "❌ رد شد",
    "expired": "⌛ منقضی شد", "cancelled": "↩️ لغو شد", "invalid": "🚫 باطل شد",
    "blocked_hedge": "⛔ بلاک (پوزیشن مخالف)", "blocked_limit": "⛔ بلاک (سقف ریسک/تعداد)",
    "order_failed": "⚠️ خطای سفارش", "closed": "🔻 بستن دستی", "gone": "ℹ️ بسته شد",
    "reached_1R": "🎯 رسید به 1R", "structure_break": "⚠️ شکست ساختار", "hold": "⏸ نگه دار",
    "unhold": "▶️ لغو نگه داشتن", "sl_close": "⛔ بسته در حد ضرر", "ai_hold": "🤖 هوش مصنوعی نگه داشت",
    "ai_close": "🤖 هوش مصنوعی بست", "analysis": "🤖 تحلیل", "feed_stale": "📡 قطع داده",
    "feed_ok": "📡 داده برگشت", "approved_dry": "🧪 تأیید آزمایشی",
}
HIDDEN_EVENTS = {"suppressed"}


def _f(x: Any) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _tehran(ts: str) -> Optional[datetime]:
    """ts ژورنال = ساعت محلی VPS (بدون منطقهٔ زمانی) → ساعت تهران."""
    try:
        d = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.astimezone()                     # محلی همین ماشین
    return d.astimezone(TEHRAN)


def read_journal(path: Path) -> List[Dict[str, str]]:
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _first_setup(s: Any) -> str:
    s = str(s or "").strip()
    for sep in (",", "+", " "):
        s = s.split(sep)[0]
    return s or "—"


# --------------------------------------------------------------------------- رقابت
# مقایسهٔ منصفانه با R: سود هر معامله تقسیم بر ریسک اولیه‌اش (فاصلهٔ ورود تا حد ضرر × حجم).
# حجم ربات‌ها فرق دارد، پس دلار به‌تنهایی مقایسهٔ درستی نیست؛ R هست.
CONTRACT = 100.0                                       # XAUUSD: ۱ لات = ۱۰۰ اونس
TAKEN = {"opened", "approved_dry"}
PASSED = {"rejected", "expired", "cancelled", "pending", "invalid", "blocked_hedge",
          "blocked_limit", "order_failed"}


def _r(profit: float, entry: float, sl: float, lot: float) -> Optional[float]:
    risk = abs(float(entry) - float(sl)) * CONTRACT * float(lot)
    return round(float(profit) / risk, 3) if risk > 0 else None


def bot_trades(db_path: Any) -> List[Dict[str, Any]]:
    """معاملات بسته‌شدهٔ ORB/ایچیموکو از جدول signals (فقط خواندنی، بدون قفل کردن ربات)."""
    path = Path(db_path)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)
    try:
        rows = con.execute("SELECT entry, sl, lot, profit, closed_at FROM signals "
                           "WHERE closed_at IS NOT NULL AND profit IS NOT NULL").fetchall()
    finally:
        con.close()
    for entry, sl, lot, profit, closed in rows:
        try:
            t = datetime.strptime(str(closed)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            out.append({"t": t, "profit": float(profit), "r": _r(profit, entry, sl, lot or 0.0)})
        except (TypeError, ValueError):
            continue
    return out


def scout_trades(events: List[Dict[str, str]], lot: float = 0.01) -> List[Dict[str, Any]]:
    """معاملات بسته‌شدهٔ اسکات از ژورنال: ورود و حد ضرر از «opened»، سود از «gone»."""
    opened: Dict[str, Dict[str, str]] = {}
    out, seen = [], set()
    for e in events:
        ev, tk = e.get("event"), str(e.get("ticket") or "")
        if ev == "opened" and tk:
            opened[tk] = e
        elif ev == "gone" and tk and tk not in seen:
            p, t = _f(e.get("profit")), _tehran(e.get("ts", ""))
            if math.isnan(p) or t is None:
                continue
            seen.add(tk)
            o = opened.get(tk) or {}
            entry, sl = _f(o.get("price")), _f(o.get("sl"))
            r = None if (math.isnan(entry) or math.isnan(sl)) else _r(p, entry, sl, lot)
            out.append({"t": t.astimezone(timezone.utc), "profit": p, "r": r})
    return out


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    rs = [r["r"] for r in rows if r.get("r") is not None]
    return {"n": n, "win": round(100.0 * sum(1 for r in rows if r["profit"] > 0) / n) if n else None,
            "pnl": round(sum(r["profit"] for r in rows), 2),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "sum_r": round(sum(rs), 1) if rs else None}


def compare_since(scout: List[Dict[str, Any]], bots: Dict[str, List[Dict[str, Any]]],
                  since: Optional[datetime]) -> List[Dict[str, Any]]:
    """یک ردیف برای «شما + اسکات» و یکی برای هر ربات، از since به بعد (UTC)."""
    rows = [{"name": "شما + اسکات", "me": True, **_summary([r for r in scout if since is None or r["t"] >= since])}]
    for name, trades in bots.items():
        rows.append({"name": name, "me": False, **_summary([r for r in trades if since is None or r["t"] >= since])})
    return rows


def build_compare(scout: List[Dict[str, Any]], bots: Dict[str, List[Dict[str, Any]]],
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """اسکات (شما) در برابر ربات‌های خودکار در سه بازه؛ «از شروع اسکات» از اولین معاملهٔ اسکات."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = min((r["t"] for r in scout), default=None)
    windows = {"7d": now - timedelta(days=7), "30d": now - timedelta(days=30), "start": start}
    out: Dict[str, Any] = {"since": start.astimezone(TEHRAN).strftime("%Y-%m-%d") if start else None}
    for key, since in windows.items():
        rows = [{"name": "شما + اسکات", "me": True, **_summary([r for r in scout if since is None or r["t"] >= since])}]
        for name, trades in bots.items():
            rows.append({"name": name, "me": False,
                         **_summary([r for r in trades if since is None or r["t"] >= since])})
        out[key] = rows
    return out


def build_picks(virtual: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """انتخاب‌های شما در برابر بقیهٔ سیگنال‌ها، با نتیجهٔ فرضی (هدف 2R) برای همه به یک شکل."""
    if not virtual:
        return None

    def grp(pred) -> Dict[str, Any]:
        rs = []
        for v in virtual:
            R = _f(v.get("R"))
            if not math.isnan(R) and pred(str(v.get("status") or "")):
                rs.append(R)
        n = len(rs)
        return {"n": n, "win": round(100.0 * sum(1 for x in rs if x > 0) / n) if n else None,
                "avg_r": round(sum(rs) / n, 2) if n else None}
    return {"taken": grp(lambda s: s in TAKEN), "passed": grp(lambda s: s in PASSED),
            "silent": grp(lambda s: s == "suppressed"), "all": grp(lambda s: True)}


def build_stats(events: List[Dict[str, str]], now: Optional[datetime] = None,
                virtual: Optional[List[Dict[str, Any]]] = None,
                actual_by_ticket: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """آمار صفحه از ژورنال. virtual = ردیف‌های scout_stats (setup, side, status, R) اگر حساب شده باشد."""
    now = (now or datetime.now(TEHRAN)).astimezone(TEHRAN)
    actual_by_ticket = actual_by_ticket or {}
    today = now.date()
    trades: List[Dict[str, Any]] = []
    seen: set = set()
    for e in events:
        if e.get("event") != "gone":
            continue
        tk = str(e.get("ticket") or "")
        if tk and tk in seen:
            continue
        p = _f(e.get("profit"))
        if math.isnan(p) and tk in actual_by_ticket:
            p = float(actual_by_ticket[tk])
        t = _tehran(e.get("ts", ""))
        if math.isnan(p) or t is None:
            continue
        seen.add(tk)
        trades.append({"t": t, "profit": p, "setup": _first_setup(e.get("setup")), "side": e.get("side") or ""})

    def agg(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(rows)
        wins = sum(1 for r in rows if r["profit"] > 0)
        return {"n": n, "pnl": round(sum(r["profit"] for r in rows), 2),
                "win": round(100.0 * wins / n) if n else None}

    week0 = today - timedelta(days=6)
    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        rows = [r for r in trades if r["t"].date() == d]
        days.append({"d": d.isoformat(), "pnl": round(sum(r["profit"] for r in rows), 2), "n": len(rows)})

    alerts_today = sum(1 for e in events if e.get("event") == "alert"
                       and (_tehran(e.get("ts", "")) or now).date() == today)

    # به تفکیک ستاپ: شمارش از ژورنال، نتیجهٔ فرضی از scout_stats (اگر هست)
    per: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("event") not in ("alert", "suppressed"):
            continue
        k = _first_setup(e.get("setup"))
        per.setdefault(k, {"setup": k, "alerts": 0, "taken": 0, "pnl": 0.0, "trades": 0, "R": [], })
        per[k]["alerts"] += 1
    for e in events:
        if e.get("event") == "opened":
            k = _first_setup(e.get("setup"))
            if k in per:
                per[k]["taken"] += 1
    for r in trades:
        if r["setup"] in per:
            per[r["setup"]]["pnl"] += r["profit"]
            per[r["setup"]]["trades"] += 1
    sides: Dict[str, List[float]] = {"BUY": [], "SELL": []}
    for v in virtual or []:
        R = _f(v.get("R"))
        if math.isnan(R):
            continue
        k = _first_setup(v.get("setup"))
        per.setdefault(k, {"setup": k, "alerts": 0, "taken": 0, "pnl": 0.0, "trades": 0, "R": []})
        per[k]["R"].append(R)
        if v.get("side") in sides:
            sides[str(v.get("side"))].append(R)
    setups = []
    for k, d in per.items():
        R = d.pop("R")
        d["pnl"] = round(d["pnl"], 2)
        d["n_virtual"] = len(R)
        d["avg_R"] = round(sum(R) / len(R), 2) if len(R) >= 5 else None
        d["win_R"] = round(100.0 * sum(1 for x in R if x > 0) / len(R)) if len(R) >= 5 else None
        setups.append(d)
    setups.sort(key=lambda d: (d["avg_R"] is None, -(d["avg_R"] or 0), -d["alerts"]))

    recent = []
    for e in reversed(events):
        ev = e.get("event", "")
        if ev in HIDDEN_EVENTS:
            continue
        t = _tehran(e.get("ts", ""))
        bits = [EVENT_FA.get(ev, ev)]
        if e.get("setup"):
            bits.append(str(e.get("setup")).replace(",", "+"))
        if e.get("side"):
            bits.append(str(e.get("side")))
        if e.get("ticket"):
            bits.append(f"#{e.get('ticket')}")
        p = _f(e.get("profit"))
        price = _f(e.get("price"))
        recent.append({"t": t.strftime("%m/%d %H:%M") if t else "", "text": " · ".join(bits),
                       "price": None if math.isnan(price) else round(price, 2),
                       "profit": None if math.isnan(p) else round(p, 2), "kind": ev})
        if len(recent) >= 30:
            break

    return {
        "today": agg([r for r in trades if r["t"].date() == today]),
        "week": agg([r for r in trades if r["t"].date() >= week0]),
        "all": agg(trades),
        "alerts_today": alerts_today,
        "days": days,
        "setups": setups,
        "sides": {s: {"n": len(v), "sum_R": round(sum(v), 1) if v else None} for s, v in sides.items()},
        "has_virtual": bool(virtual),
        "recent": recent,
    }


class ScoutDashboard:
    def __init__(self, snapshot: Callable[[], Dict[str, Any]], stats: Callable[[], Dict[str, Any]],
                 token: str, host: str = "0.0.0.0", port: int = 8082) -> None:
        self._snapshot, self._stats = snapshot, stats
        self._token = str(token or "").strip()
        self._host, self._port = host, int(port)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_t = 0.0
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self._port

    def start(self) -> bool:
        if not self._token:
            logger.warning("scout dashboard off: no token (DASHBOARD_TOKEN_SCOUT / DASHBOARD_TOKEN)")
            return False
        outer = self

        class Handler(_Handler):
            dash = outer

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):  # noqa: N802
                if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        try:
            self._httpd = Quiet((self._host, self._port), Handler)
        except OSError as exc:
            logger.error("scout dashboard could not bind {}:{} ({})", self._host, self._port, exc)
            return False
        threading.Thread(target=self._httpd.serve_forever, name="scout-dash", daemon=True).start()
        logger.info("scout dashboard on port {}", self.port)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def data(self) -> Dict[str, Any]:
        with self._lock:
            if self._stats_cache is None or time.time() - self._stats_t > 15:
                try:
                    self._stats_cache = self._stats()
                except Exception as exc:              # صفحه بدون آمار هم باید بیاید
                    logger.warning("scout dashboard stats failed: {}", exc)
                    self._stats_cache = {"error": str(exc)[:200]}
                self._stats_t = time.time()
            st = self._stats_cache
        try:
            snap = dict(self._snapshot() or {})
        except Exception as exc:
            snap = {"error": str(exc)[:200]}
        snap["stats"] = st
        return snap

    def authorized(self, query: str) -> bool:
        supplied = (parse_qs(query).get("token") or [""])[0]
        return bool(self._token) and hmac.compare_digest(supplied.encode(), self._token.encode())


class _Handler(BaseHTTPRequestHandler):
    dash: ScoutDashboard

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if not self.dash.authorized(u.query):
            self._send(403, "text/plain; charset=utf-8", "دسترسی ندارید (token)".encode("utf-8"))
            return
        if u.path.startswith("/api/data"):
            body = json.dumps(self.dash.data(), ensure_ascii=False, default=str).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif u.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.replace("<!--GUIDE-->", GUIDE).encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


GUIDE = r'''<div class="pane guide" id="p-guide" role="tabpanel" aria-labelledby="t-guide" hidden>

  <section><h2>اسکات چیست؟</h2>
    <p>اسکات سومین ربات طلاست و با دو ربات دیگر یک فرق اصلی دارد: <b>خودش وارد معامله نمی‌شود.</b> ORB و ایچیموکو خودکار معامله می‌کنند، ولی اسکات فقط ستاپ‌ها را پیدا می‌کند، در تلگرام خبر می‌دهد و تصمیم را به شما می‌سپارد.</p>
    <p>همهٔ معاملات با حجم ثابت <b data-r="lot">0.01</b> لات روی حساب دمو انجام می‌شوند تا نتیجهٔ ستاپ‌ها منصفانه مقایسه شود. همه‌چیز در دفتر اسکات ثبت می‌شود، حتی هشدارهایی که رد کرده‌اید. همین‌طور می‌شود فهمید کدام ستاپ واقعاً جواب می‌دهد.</p>
  </section>

  <section><h2>یک معامله از اول تا آخر</h2>
    <ol class="steps">
      <li><div><b>پیدا کردن ستاپ.</b> هر بار که یک کندل ۱۵ دقیقه‌ای بسته می‌شود، اسکات ۱۰ نوع ستاپ را روی آن چک می‌کند. اگر چند ستاپ هم‌زمان و هم‌جهت بودند، در یک پیام می‌آیند.</div></li>
      <li><div><b>هشدار در تلگرام.</b> پیام شامل جهت (خرید یا فروش)، قیمت، حد ضرر، هدف پیشنهادی (۲ برابر ریسک) و ریسک به دلار است.</div></li>
      <li><div><b>تصمیم شما.</b> <b data-r="expiry_min">30</b> دقیقه فرصت دارید: ✅ تأیید، ❌ رد، یا اول 🤖 تحلیل بگیرید. اگر جواب ندهید، هشدار منقضی می‌شود.</div></li>
      <li><div><b>باز شدن معامله.</b> با تأیید، معامله با قیمت همان لحظه باز می‌شود و حد ضرر با همان فاصلهٔ هشدار روی بروکر ثبت می‌شود. اگر قیمت در این فاصله از حد ضرر رد شده باشد، معامله باز نمی‌شود.</div></li>
      <li><div><b>مراقبت.</b> هر <b data-r="status_min">5</b> دقیقه یک پیام وضعیت با سود و زیان می‌آید. در <b data-r="approach" data-suf="٪">70٪</b> فاصله تا حد ضرر یک هشدار «نزدیک حد ضرر» می‌آید و رسیدن به ۱ برابر ریسک (1R) هم خبر داده می‌شود.</div></li>
      <li><div><b>بسته شدن.</b> یا خودتان با دکمهٔ 🔻 بستن می‌بندید، یا قیمت به حد ضرر می‌رسد و طبق «حالت» (پایین‌تر توضیح داده شده) رفتار می‌شود. اسکات حد سود خودکار نمی‌گذارد؛ زمان گرفتن سود با شماست.</div></li>
    </ol>
  </section>

  <section><h2>۱۰ ستاپ اسکات</h2>
    <p>چهار ستاپ اول فقط در جهت روند ظاهر می‌شوند. روند یعنی قیمت بالای ابر ایچیموکو، بالای میانگین ۲۰۰ و هم‌جهت با روند یک‌ساعته (برای فروش برعکس). بقیه به روند وابسته نیستند.</p>
    <div class="tablewrap"><table class="setgrid"><thead><tr><th>ستاپ</th><th>یعنی چه</th></tr></thead><tbody>
      <tr><td class="mono">kijun_pullback</td><td>در روند، قیمت تا نزدیک خط کیجون برمی‌گردد و دوباره در جهت روند بسته می‌شود.</td></tr>
      <tr><td class="mono">tenkan_momentum</td><td>در روند و با نوسان رو به افزایش، قیمت خط تنکان را لمس می‌کند و با یک کندل قوی در جهت روند بسته می‌شود.</td></tr>
      <tr><td class="mono">range_break</td><td>در روند، یک کندل قوی بالاتر از سقف ۲۰ کندل قبل (یا پایین‌تر از کف آن برای فروش) بسته می‌شود.</td></tr>
      <tr><td class="mono">pullback_resume</td><td>در روند، بعد از برگشت به تنکان در ۳ کندل اخیر، قیمت از سقف کندل قبل رد می‌شود و روند ادامه پیدا می‌کند.</td></tr>
      <tr><td class="mono">cloud_break</td><td>قیمت از ابر ایچیموکو عبور می‌کند و بیرون آن بسته می‌شود، همراه با افزایش نوسان.</td></tr>
      <tr><td class="mono">tk_cross</td><td>خط تنکان خط کیجون را قطع می‌کند.</td></tr>
      <tr><td class="mono">rejection</td><td>قیمت کف (یا سقف) ۲۰ کندل را می‌شکند ولی با سایهٔ بلند برمی‌گردد و داخل محدوده بسته می‌شود.</td></tr>
      <tr><td class="mono">engulfing</td><td>کندل پوشا: بدنهٔ کندل فعلی بدنهٔ کندل مخالف قبلی را کامل می‌پوشاند.</td></tr>
      <tr><td class="mono">ignition</td><td>یک کندل خیلی بزرگ (بیش از ۲ برابر ATR) با بدنهٔ پر و حجم بالا.</td></tr>
      <tr><td class="mono">exhaustion</td><td>قیمت بیش از ۳ برابر ATR از تنکان دور شده است. این ستاپ خلاف حرکت است و روی برگشت حساب می‌کند.</td></tr>
    </tbody></table></div>
    <p class="hint">حد ضرر هر ستاپ پشت کف یا سقف کندل (یا تنکان) گذاشته می‌شود، بین ۱ تا ۲.۵ برابر ATR، ولی هیچ‌وقت بیشتر از <b data-r="sl_cap" data-pre="$">$10</b> ضرر نیست.</p>
  </section>

  <section><h2>دکمه‌های تلگرام</h2>
    <div class="gl">
      <div><b>✅ تأیید</b><span>با قیمت همین لحظه وارد می‌شود.</span></div>
      <div><b>❌ رد</b><span>هشدار کنار گذاشته می‌شود ولی در آمار می‌ماند.</span></div>
      <div><b>🤖 تحلیل</b><span>نظر کوتاه هوش مصنوعی: ورود یا صبر، درصد اطمینان، دلیل‌ها و یک نکته. فقط راهنماست؛ تصمیم با شماست.</span></div>
      <div><b>🔻 بستن</b><span>پوزیشن را همان لحظه می‌بندد. زدن چندباره فقط یک بار اجرا می‌شود.</span></div>
      <div><b>⏸ نگه دار</b><span>در حد ضرر سیگنال بسته نشود. حد ضرر روی بروکر به سطح اضطراری می‌رود.</span></div>
      <div><b>▶️ لغو نگه داشتن</b><span>دوباره طبق حالت انتخاب‌شده رفتار می‌شود.</span></div>
      <div><b>📋 پوزیشن‌ها · 💰 قیمت · 📊 وضعیت</b><span>کیبورد پایین چت، برای دیدن سریع.</span></div>
      <div><b>🎛 منو</b><span>توقف و ادامهٔ هشدارها، سلامت ربات، حالت حد ضرر، ری‌استارت.</span></div>
      <div><b>❓ راهنما</b><span>همین توضیحات، کوتاه، داخل تلگرام (<span class="mono">/help</span>).</span></div>
    </div>
  </section>

  <section><h2>حد ضرر و «حالت»</h2>
    <p>اسکات از دو سطح حد ضرر استفاده می‌کند:</p>
    <div class="gl">
      <div><b>حد ضرر سیگنال</b><span>همان حدی که در هشدار آمده، حداکثر <b data-r="sl_cap" data-pre="$">$10</b> ضرر. در حالت عادی همین روی بروکر ثبت است.</span></div>
      <div><b>حد ضرر اضطراری</b><span>وقتی «نگه دار» زده‌اید یا حالت «نگه داشتن» است: ۳ برابر فاصلهٔ سیگنال، ولی حداکثر <b data-r="emerg_cap" data-pre="$">$20</b> ضرر. این سقف آخر است تا ضرر هیچ‌وقت از کنترل خارج نشود.</span></div>
    </div>
    <p>«حالت» تعیین می‌کند وقتی قیمت به حد ضرر سیگنال رسید و شما در دسترس نبودید چه شود. با دستور <span class="mono">/mode</span> یا از منو عوض می‌شود:</p>
    <div class="gl">
      <div><b>بستن</b><span>در حد ضرر سیگنال بسته می‌شود. پیش‌فرض و محتاطانه‌ترین حالت.</span></div>
      <div><b>نگه داشتن</b><span>بسته نمی‌شود تا خودتان بگویید. فقط هشدار می‌آید و حد ضرر اضطراری از ضرر بزرگ جلوگیری می‌کند.</span></div>
      <div><b>هوش مصنوعی</b><span>در حد ضرر از هوش مصنوعی می‌پرسد. اگر با اطمینان کافی گفت «نگه دار»، نگه می‌دارد و ۱۵ دقیقه بعد دوباره می‌پرسد. اگر جواب نداد یا مطمئن نبود، می‌بندد.</span></div>
    </div>
  </section>

  <section><h2>سقف‌های هم‌زمانی</h2>
    <p>هشدارها همیشه می‌آیند، حتی وقتی پوزیشن باز دارید. ولی تأیید جدید فقط وقتی معامله باز می‌کند که:</p>
    <div class="gl">
      <div><b>حداکثر <span data-r="max_open">2</span> پوزیشن</b><span>پوزیشن سوم باز نمی‌شود.</span></div>
      <div><b>سقف ریسک کل <span data-r="max_risk" data-pre="$">$20</span></b><span>جمع ضرر ممکن همهٔ پوزیشن‌های باز تا حد ضرر فعلی‌شان از این بیشتر نشود.</span></div>
      <div><b>بدون خرید و فروش هم‌زمان</b><span>اگر پوزیشن خرید باز است، تأیید فروش باز نمی‌شود (و برعکس).</span></div>
    </div>
    <p class="hint">اگر یکی از این‌ها مانع شود، زیر خود هشدار نوشته می‌شود «فعلاً قابل باز شدن نیست» و دلیلش.</p>
  </section>

  <section><h2>بخش رقابت چطور حساب می‌شود</h2>
    <p>هر معامله با <b>R</b> سنجیده می‌شود: سود تقسیم بر ریسک اولیه (فاصلهٔ ورود تا حد ضرر ضرب در حجم). مثلاً اگر ریسک یک معامله ۱۰ دلار بوده و ۲۰ دلار سود داده، نتیجه‌اش ‎+2R است. این‌طوری ربات‌هایی که حجم متفاوت دارند منصفانه مقایسه می‌شوند.</p>
    <p>نتایج ORB و ایچیموکو از دیتابیس خودشان خوانده می‌شود (فقط خواندن). نتایج اسکات معاملاتی است که شما تأیید کرده‌اید. بخش «انتخاب‌های شما» همهٔ سیگنال‌های اسکات را با یک معیار فرضی مقایسه می‌کند تا معلوم شود انتخاب شما بهتر از گرفتن همهٔ سیگنال‌هاست یا نه.</p>
  </section>

  <section><h2>نمودار، «اگر گرفته بودید» و ابزارها</h2>
    <div class="gl">
      <div><b>📊 نمودار هشدار</b><span>زیر هر هشدار یک نمودار ۱۵ دقیقه‌ای می‌آید. سبز: از ورود تا هدف 2R (محدودهٔ سود). قرمز: از ورود تا حد ضرر. کهربایی: محدودهٔ خنثی دور ورود، جایی که بستن به‌خاطر اسپرد تقریباً سر به سر است. خط نقطه‌چین سبز: 1R.</span></div>
      <div><b>🔮 اگر گرفته بودید…</b><span>هشداری که رد کنید، منقضی شود یا به‌خاطر سقف باز نشود، بی‌صدا دنبال می‌شود. وقتی به هدف یا حد ضرر رسید (حداکثر ۲۴ ساعت)، نتیجه و دلیلش زیر همان هشدار می‌آید: هم‌جهت یا خلاف روند یک‌ساعته، جای قیمت نسبت به ابر، خبر، و مسیر قیمت.</span></div>
      <div><b>🏁 جدول امتیاز شبانه</b><span>هر شب ساعت ۲۳:۵۵ تهران: امروز و ۷ روز اخیر، شما + اسکات در برابر ORB و ایچیموکو با R، به‌علاوهٔ تعداد ردهای درست. روزهای بی‌فعالیت پیامی نمی‌آید؛ از منو هر وقت خواستید.</span></div>
      <div><b>🔔 هشدار قیمت</b><span><span class="mono">/alert 4150</span> وقتی طلا به ۴۱۵۰ برسد خبر می‌دهد (با یادداشت هم می‌شود). <span class="mono">/alerts</span> فهرست و حذف.</span></div>
    </div>
  </section>

  <section><h2>اطلاعات اضافه زیر هشدار</h2>
    <div class="gl">
      <div><b>📈 کارنامهٔ ستاپ</b><span>میانگین نتیجهٔ فرضی ۳۰ نمونهٔ آخر همان ستاپ. اگر از ‎−0.5R بدتر باشد با ⚠️ می‌آید. فقط اطلاع است و هیچ سیگنالی حذف نمی‌شود.</span></div>
      <div><b>📰 خبر مهم</b><span>اگر خبر پراهمیت دلار در ۱۵ دقیقهٔ قبل یا بعد باشد، یک خط زیر هشدار می‌آید. هشدار مثل همیشه می‌آید.</span></div>
      <div><b>❓ سفارش نامشخص</b><span>اگر جواب بروکر معلوم نباشد، اسکات «ثبت نشد» نمی‌گوید. چند ثانیه بعد خودش بررسی می‌کند و اگر پوزیشن باز شده بود، تحت نظر می‌گیرد.</span></div>
      <div><b>🤖 تحلیل به‌روز</b><span>تحلیل با قیمت و نمودار لحظهٔ زدن دکمه ساخته می‌شود، نه لحظهٔ آمدن هشدار.</span></div>
    </div>
  </section>

  <section><h2>پیام‌های خودکار</h2>
    <div class="gl">
      <div><b>📊 وضعیت زنده</b><span>هر <b data-r="status_min">5</b> دقیقه برای هر پوزیشن. وقتی بازار بسته است فقط یک پیام «بازار بسته» می‌آید.</span></div>
      <div><b>⚠️ نزدیک حد ضرر</b><span>یک بار، با دکمهٔ «نگه دار»، تا قبل از رسیدن تصمیم بگیرید.</span></div>
      <div><b>🎯 رسیدن به 1R</b><span>سود به اندازهٔ ریسک اولیه رسیده است.</span></div>
      <div><b>⚠️ شکست ساختار</b><span>کندل خلاف جهت تنکان بسته شده؛ نشانهٔ ضعیف شدن حرکت.</span></div>
      <div><b>💚 سالمم</b><span>هر روز حدود ساعت ۸:۳۰ تهران. اگر نیامد، یعنی اسکات مشکل دارد.</span></div>
      <div><b>📡 قطع داده</b><span>اگر ۱۰ دقیقه قیمت از MT5 نرسد. وقتی برگشت هم خبر می‌دهد.</span></div>
    </div>
  </section>

  <section><h2>واژه‌نامه</h2>
    <div class="gl">
      <div><b>R</b><span>واحد ریسک. 1R یعنی به اندازهٔ فاصلهٔ ورود تا حد ضرر. ‎+2R یعنی دو برابر ریسک سود، ‎−1R یعنی خوردن حد ضرر.</span></div>
      <div><b>نتیجهٔ فرضی</b><span>اگر هشدار با همان قیمت، همان حد ضرر و هدف 2R گرفته می‌شد چه می‌شد. روی کندل‌های ۱ دقیقه‌ای واقعی و با کسر اسپرد حساب می‌شود.</span></div>
      <div><b>ATR</b><span>میانگین اندازهٔ حرکت هر کندل؛ معیار نوسان بازار.</span></div>
      <div><b>اسپرد</b><span>فاصلهٔ قیمت خرید و فروش؛ هزینهٔ ورود به معامله.</span></div>
      <div><b>تنکان و کیجون</b><span>دو خط ایچیموکو: میانهٔ ۹ و ۲۶ کندل اخیر. تنکان سریع‌تر است.</span></div>
      <div><b>ابر ایچیموکو</b><span>محدوده‌ای که بالای آن معمولاً روند صعودی و پایینش نزولی حساب می‌شود.</span></div>
      <div><b>اکوییتی</b><span>موجودی به‌علاوهٔ سود و زیان پوزیشن‌های باز.</span></div>
      <div><b>منقضی</b><span>هشداری که در مهلت جواب نگرفت.</span></div>
    </div>
  </section>

  <section><h2>سؤال‌های رایج</h2>
    <div>
      <details><summary>تأیید کردم ولی معامله باز نشد. چرا؟</summary>
        <p>یکی از این دلیل‌ها: سقف تعداد یا ریسک کل پر بود، پوزیشن خلاف جهت باز بود، یا قیمت تا لحظهٔ تأیید از حد ضرر هشدار رد شده بود. دلیل دقیق در جواب تلگرام می‌آید.</p></details>
      <details><summary>آخر هفته چرا پیامی نمی‌آید؟</summary>
        <p>بازار طلا از جمعه‌شب تا حدود ساعت ۱:۳۰ بامداد دوشنبه به وقت تهران بسته است. اسکات روشن می‌ماند ولی ستاپ و پیام وضعیت تکراری ندارد.</p></details>
      <details><summary>«تحلیل در دسترس نیست» یعنی چه؟</summary>
        <p>سرویس هوش مصنوعی جواب نداده (معمولاً شلوغی موقت). دکمه می‌ماند و می‌توانید دوباره بزنید. تصمیم بدون تحلیل هم کاملاً ممکن است.</p></details>
      <details><summary>اگر اسکات خاموش یا گیر کند چه می‌شود؟</summary>
        <p>نگهبان VPS خودش ری‌استارتش می‌کند و بعد از ری‌استارت ویندوز هم خودش بالا می‌آید. پوزیشن‌های باز ذخیره شده‌اند و دوباره دنبال می‌شوند. حد ضرر هم روی بروکر است، پس حتی وقتی اسکات خاموش است پوزیشن محافظت می‌شود. از منوی تلگرام هم می‌شود ری‌استارت زد.</p></details>
      <details><summary>این صفحه را چه کسی می‌تواند ببیند؟</summary>
        <p>فقط کسی که آدرس کامل همراه با توکن را دارد. صفحه فقط خواندنی است و هیچ دکمه‌ای در آن معامله نمی‌کند.</p></details>
    </div>
  </section>

  <p class="callout">اسکات روی حساب دمو کار می‌کند و هدفش پیدا کردن ستاپ‌هایی است که واقعاً جواب می‌دهند. عددهای «فرضی» گذشته را نشان می‌دهند و تضمینی برای آینده نیستند.</p>
</div>'''


PAGE = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>داشبورد اسکات</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;700;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* layout: header with live status → KPI row → open positions & pending alerts → P/L by day → setups → events → rules */
:root{
  --bg:#f3f4f1;--surface:#fff;--ink:#1b2320;--muted:#5f6a64;--line:#d9ddd6;
  --gold:#9a6b07;--gold-soft:#f1e4c3;--good:#1f7a4d;--good-soft:#dcefe4;--bad:#b4372b;--bad-soft:#f6dcd8;
  --warn:#9a5a00;--warn-soft:#f6e6cc;
  --f-body:"Vazirmatn",Tahoma,system-ui,sans-serif;--f-mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#101614;--surface:#18201d;--ink:#e7ebe6;--muted:#9aa59f;--line:#2b3531;
  --gold:#e2b64e;--gold-soft:#3a2f14;--good:#58c48b;--good-soft:#173326;--bad:#ef7d70;--bad-soft:#3a1d1a;
  --warn:#f0b35a;--warn-soft:#3a2a12;color-scheme:dark}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--f-body);font-size:15px;line-height:1.8}
.wrap{max-width:1040px;margin:0 auto;padding-inline:16px;padding-block:22px 56px;display:flex;flex-direction:column;gap:28px}
h1,h2,h3{margin:0}
h2{font-size:1.15rem;font-weight:800;display:flex;gap:10px;align-items:baseline}
h2 small{font-size:.78rem;font-weight:500;color:var(--muted)}
section{display:flex;flex-direction:column;gap:12px}
.num{font-variant-numeric:tabular-nums;direction:ltr;unicode-bidi:isolate}
.mono{font-family:var(--f-mono);font-size:.86em;direction:ltr;unicode-bidi:isolate}
.muted{color:var(--muted)} .pos{color:var(--good)} .neg{color:var(--bad)}
header{border-bottom:2px solid var(--gold);padding-bottom:16px;display:flex;flex-wrap:wrap;gap:12px 20px;align-items:flex-end;justify-content:space-between}
header h1{font-size:1.9rem;font-weight:800}
header .sub{color:var(--muted);font-size:.86rem}
.pills{display:flex;flex-wrap:wrap;gap:6px}
.pill{display:inline-flex;gap:6px;align-items:center;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:1px 11px;font-size:.82rem}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--good)} .dot.bad{background:var(--bad)} .dot.warn{background:var(--warn)}
.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
@media (max-width:900px){.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:520px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.tile .k{font-size:.76rem;color:var(--muted)} .tile .v{font-size:1.35rem;font-weight:800;font-variant-numeric:tabular-nums}
.tile .s{font-size:.76rem;color:var(--muted)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:760px){.cols{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:6px;min-width:0}
.card.buy{border-inline-start:4px solid var(--good)} .card.sell{border-inline-start:4px solid var(--bad)}
.card .top{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:baseline}
.card .pnl{font-size:1.3rem;font-weight:800}
.kv{display:flex;flex-wrap:wrap;gap:2px 16px;font-size:.86rem;color:var(--muted)}
.kv b{color:var(--ink);font-weight:600}
.tag{font-size:.72rem;border-radius:6px;padding:0 7px;font-weight:700;background:var(--warn-soft);color:var(--warn)}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:10px;padding:14px 16px;color:var(--muted);font-size:.9rem}
.chart{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.bars{display:grid;grid-template-columns:repeat(14,minmax(0,1fr));gap:4px;height:150px;direction:ltr;position:relative}
.bars::after{content:"";position:absolute;left:0;right:0;top:50%;border-top:1px solid var(--line)}
.b{position:relative;height:100%}
.b i{position:absolute;left:12%;right:12%;border-radius:3px}
.b i.p{bottom:50%;background:var(--good)} .b i.n{top:50%;background:var(--bad)}
.blab{display:grid;grid-template-columns:repeat(14,minmax(0,1fr));gap:4px;direction:ltr;font-size:.68rem;color:var(--muted);text-align:center;margin-top:4px;font-variant-numeric:tabular-nums}
@media (max-width:560px){.blab span:nth-child(even){visibility:hidden}.blab{font-size:.62rem}}
.scale{display:flex;justify-content:space-between;font-size:.72rem;color:var(--muted);margin-bottom:4px}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:560px}
th,td{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line);font-size:.88rem}
th{font-size:.76rem;color:var(--muted);background:color-mix(in srgb,var(--gold-soft) 45%,var(--surface))}
tr:last-child td{border-bottom:0}
td.n{font-variant-numeric:tabular-nums;direction:ltr;text-align:left}
.ev{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;max-height:420px;overflow:auto}
.ev li{display:grid;grid-template-columns:86px 1fr auto;gap:10px;padding:7px 14px;border-bottom:1px solid var(--line);font-size:.86rem}
.ev li:last-child{border-bottom:0}
.ev .t{color:var(--muted);font-variant-numeric:tabular-nums;direction:ltr;text-align:right}
.rules{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.rule{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:.86rem}
.rule b{display:block;font-size:1.05rem;font-variant-numeric:tabular-nums}
footer{font-size:.78rem;color:var(--muted);border-top:1px solid var(--line);padding-top:12px}
.err{background:var(--bad-soft);color:var(--bad);border-radius:10px;padding:10px 14px}
.picks{grid-template-columns:repeat(4,minmax(0,1fr))}
@media (max-width:720px){.picks{grid-template-columns:repeat(2,minmax(0,1fr))}}
tr.me td{background:color-mix(in srgb,var(--gold-soft) 55%,transparent);font-weight:700}
#cmpwin{margin-top:0}
.hint{margin:-6px 0 0;font-size:.84rem;color:var(--muted);max-width:75ch}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-top:-14px}
.tab{font:inherit;font-size:.92rem;font-weight:700;border:1px solid var(--line);background:var(--surface);color:var(--muted);border-radius:8px;padding:5px 14px;cursor:pointer}
.tab[aria-selected="true"]{background:var(--gold-soft);color:var(--gold);border-color:color-mix(in srgb,var(--gold) 45%,transparent)}
.tab:focus-visible,a:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.intro{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 16px;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:.9rem}
.intro p{margin:0;flex:1 1 360px;min-width:0}
.linkbtn{font:inherit;font-weight:700;color:var(--gold);background:none;border:0;padding:0;cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.pane{display:flex;flex-direction:column;gap:28px}
.guide{display:flex;flex-direction:column;gap:26px;max-width:860px}
.guide h2{font-size:1.2rem}
.guide p{margin:0;max-width:72ch}
.guide h3{font-size:1rem;font-weight:700}
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;counter-reset:st}
.steps li{counter-increment:st;display:grid;grid-template-columns:32px 1fr;gap:12px;align-items:start;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.steps li::before{content:counter(st);font-weight:800;color:var(--gold);background:var(--gold-soft);border-radius:50%;width:28px;height:28px;display:grid;place-items:center;font-variant-numeric:tabular-nums}
.gl{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.gl > div{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 14px;display:flex;flex-direction:column;gap:2px;min-width:0}
.gl b{font-size:.95rem}
.gl span{font-size:.86rem;color:var(--muted)}
.setgrid td:first-child{white-space:nowrap}
details{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 16px}
details+details{margin-top:8px}
summary{cursor:pointer;font-weight:700}
details p{margin-top:6px;color:var(--muted);font-size:.9rem}
.callout{background:var(--warn-soft);border-radius:10px;padding:10px 16px;font-size:.9rem}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1>اسکات</h1><div class="sub">دیدبان ستاپ‌های طلا · تأیید دستی در تلگرام · فقط خواندنی</div></div>
    <div class="pills" id="pills"><span class="pill"><span class="dot"></span>در حال دریافت…</span></div>
  </header>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" id="t-dash" aria-selected="true" aria-controls="p-dash">📊 داشبورد</button>
    <button class="tab" role="tab" id="t-guide" aria-selected="false" aria-controls="p-guide">📖 راهنمای اسکات</button>
  </div>
  <div id="err" class="err" hidden></div>

  <div class="pane" id="p-dash" role="tabpanel" aria-labelledby="t-dash">
  <div class="intro"><p><b>اسکات</b> خودش معامله نمی‌کند. هر ۱۵ دقیقه ستاپ‌های طلا را پیدا می‌کند، در تلگرام هشدار می‌دهد و فقط اگر شما تأیید کنید معامله باز می‌کند. این صفحه فقط نمایش است؛ همهٔ تصمیم‌ها از تلگرام گرفته می‌شود.</p>
    <button class="linkbtn" data-go="guide">راهنمای کامل ←</button></div>

  <section><h2>امروز و کل <small id="asof"></small></h2>
    <p class="hint">موجودی و اکوییتی از خود حساب می‌آید. «اکوییتی» یعنی موجودی به‌علاوهٔ سود و زیان پوزیشن‌های باز. سود امروز و ۷ روز فقط معاملات بسته‌شدهٔ اسکات را می‌شمارد، به روز تهران.</p>
    <div class="tiles" id="tiles"></div></section>

  <section><h2>🏁 رقابت <small id="cmpnote">شما + اسکات در برابر دو ربات خودکار</small></h2>
    <p class="hint">مقایسهٔ اصلی با <b>R</b> است: سود هر معامله تقسیم بر ریسک اولیه‌اش. چون حجم معاملهٔ ربات‌ها با هم فرق دارد، دلار به‌تنهایی مقایسهٔ منصفانه‌ای نیست و فقط برای اطلاع آمده است. «جمع R» یعنی در کل چند برابر ریسک یک معامله سود یا ضرر شده.</p>
    <div class="tabs" role="tablist" id="cmpwin">
      <button class="tab" data-w="7d" aria-selected="true">۷ روز اخیر</button>
      <button class="tab" data-w="30d" aria-selected="false">۳۰ روز</button>
      <button class="tab" data-w="start" aria-selected="false">از شروع اسکات</button>
    </div>
    <div class="tablewrap"><table><thead><tr><th>رقیب</th><th>معامله</th><th>برد</th><th>میانگین R</th><th>جمع R</th><th>سود دلاری</th></tr></thead><tbody id="cmp"></tbody></table></div>
  </section>

  <section><h2>انتخاب‌های شما در برابر همهٔ سیگنال‌ها</h2>
    <p class="hint">همهٔ سیگنال‌ها با یک معیار سنجیده شده‌اند: اگر هر کدام با حد ضرر خودش و هدف 2R گرفته می‌شد. اگر ستون «تأییدشده‌های شما» از «همهٔ سیگنال‌ها» بهتر باشد، یعنی انتخاب شما به نتیجه اضافه می‌کند.</p>
    <div class="tiles picks" id="picks"></div>
    <p class="hint" id="picksay"></p>
  </section>

  <div class="cols">
    <section><h2>پوزیشن‌های باز</h2>
      <p class="hint">«حد ضرر سیگنال» حدی است که اسکات پیشنهاد داده. «روی بروکر» حدی است که الان واقعاً در MT5 ثبت است. اگر «نگه دار» زده باشید، این دو فرق دارند.</p>
      <div id="open"></div></section>
    <section><h2>هشدارهای منتظر جواب</h2>
      <p class="hint">هشدارهایی که هنوز تأیید یا رد نکرده‌اید. جواب فقط از تلگرام داده می‌شود و بعد از مهلت، هشدار منقضی می‌شود.</p>
      <div id="pending"></div></section>
  </div>

  <section><h2>سود و زیان روزانه <small>۱۴ روز اخیر، به وقت تهران</small></h2>
    <p class="hint">هر ستون جمع سود و زیان معاملاتی است که آن روز بسته شده‌اند. سبز سود، قرمز ضرر. نشانگر را روی ستون نگه دارید تا عدد دقیق را ببینید.</p>
    <div class="chart"><div class="scale"><span id="smax"></span><span>دلار</span></div>
      <div class="bars" id="bars"></div><div class="blab" id="blab"></div></div></section>

  <section><h2>ستاپ‌ها <small id="vnote"></small></h2>
    <p class="hint">«سود واقعی» از معاملاتی است که تأیید کرده‌اید. «میانگین فرضی» نشان می‌دهد اگر همهٔ هشدارهای آن ستاپ با حد ضرر خودش و هدف ۲ برابر ریسک گرفته می‌شد، به‌طور میانگین چند R می‌داد. عدد مثبت یعنی آن ستاپ در تاریخچه جواب داده است. ستاپ‌های با کمتر از ۵ نمونه «—» دارند.</p>
    <div class="tablewrap"><table><thead><tr><th>ستاپ</th><th>هشدار</th><th>تأیید</th><th>معامله</th><th>سود واقعی</th><th>میانگین فرضی</th><th>برد فرضی</th></tr></thead><tbody id="setups"></tbody></table></div>
    <div class="cols" id="sides"></div></section>

  <section><h2>رویدادهای اخیر</h2>
    <p class="hint">۳۰ اتفاق آخر از دفتر اسکات: هشدارها، تأیید و ردها، باز و بسته شدن‌ها و تحلیل‌ها. جدیدترین بالاست.</p>
    <ul class="ev" id="events"></ul></section>

  <section><h2>قواعد فعلی</h2>
    <p class="hint">حدهایی که اسکات الان با آن‌ها کار می‌کند. توضیح هر کدام در راهنما آمده است.</p>
    <div class="rules" id="rules"></div></section>
  </div>

<!--GUIDE-->

  <footer>هر ۱۰ ثانیه به‌روز می‌شود. «فرضی» یعنی اگر هر ستاپ با حد ضرر خودش و هدف 2R گرفته می‌شد (هر ساعت دوباره حساب می‌شود). <span id="upd"></span></footer>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=v=>v==null||isNaN(v)?'—':(v>0?'+':v<0?'−':'')+'$'+Math.abs(v).toFixed(2);
const cls=v=>v>0?'pos':v<0?'neg':'';
const fa=n=>n==null?'—':String(n);
function pill(ok,text){return `<span class="pill"><span class="dot ${ok===true?'ok':ok===false?'bad':'warn'}"></span>${esc(text)}</span>`}
function render(d){
  $('err').hidden=!d.error; if(d.error)$('err').textContent='خطا: '+d.error;
  const s=d.status||{}, a=d.account||{}, st=d.stats||{};
  $('pills').innerHTML=[pill(s.alive,s.alive?'روشن':'بی‌پاسخ'),pill(s.mt5,s.mt5?'MT5 وصل':'MT5 قطع'),
    pill(s.market?true:null,s.market?'بازار باز':'بازار بسته'),pill(s.paused?false:true,s.paused?'هشدارها متوقف':'هشدارها فعال'),
    `<span class="pill">حالت: ${esc(s.mode_fa||s.mode||'—')}</span>`,
    d.price?`<span class="pill">طلا <b class="num">${d.price.bid.toFixed(2)}</b></span>`:''].join('');
  $('asof').textContent=s.now_tehran?('به‌روز '+s.now_tehran+' تهران'):'';
  const t=st.today||{},w=st.week||{},al=st.all||{};
  $('tiles').innerHTML=[
    ['موجودی',a.balance!=null?'$'+a.balance.toFixed(2):'—','',''],
    ['اکوییتی',a.equity!=null?'$'+a.equity.toFixed(2):'—',a.profit!=null?('شناور '+money(a.profit)):'',''],
    ['امروز',money(t.pnl),`${fa(t.n)} معامله · ${fa(st.alerts_today)} هشدار`,cls(t.pnl)],
    ['۷ روز',money(w.pnl),`${fa(w.n)} معامله · برد ${w.win!=null?w.win+'٪':'—'}`,cls(w.pnl)],
    ['کل',money(al.pnl),`${fa(al.n)} معامله · برد ${al.win!=null?al.win+'٪':'—'}`,cls(al.pnl)],
    ['پوزیشن باز',fa((d.open||[]).length),d.open_risk!=null?('ریسک '+'$'+d.open_risk.toFixed(2)+' از $'+(d.rules?.max_risk??'—')):'',''],
  ].map(([k,v,sub,c])=>`<div class="tile"><div class="k">${k}</div><div class="v num ${c}">${v}</div><div class="s">${sub}</div></div>`).join('');
  const op=d.open||[];
  $('open').innerHTML=op.length?op.map(p=>`<div class="card ${p.side==='BUY'?'buy':'sell'}">
     <div class="top"><b>${p.side==='BUY'?'🟢 خرید':'🔴 فروش'} <span class="mono">#${p.ticket}</span> · <span class="mono">${esc(p.setup)}</span></b>
     <span class="pnl num ${cls(p.profit)}">${money(p.profit)}</span></div>
     <div class="kv"><span>ورود <b class="num">${p.entry.toFixed(2)}</b></span><span>الان <b class="num">${p.price?p.price.toFixed(2):'—'}</b></span>
     <span>حد ضرر سیگنال <b class="num">${p.sl.toFixed(2)}</b></span><span>روی بروکر <b class="num">${p.broker_sl?p.broker_sl.toFixed(2):'—'}</b></span>
     ${p.r!=null?`<span>R <b class="num">${p.r>0?'+':''}${p.r.toFixed(2)}</b></span>`:''}</div>
     <div>${p.hold?'<span class="tag">⏸ نگه داشته</span>':''} ${p.warned?'<span class="tag">⚠️ ساختار شکسته</span>':''}</div></div>`).join('')
    :'<div class="empty">پوزیشن بازی نیست.</div>';
  const pe=d.pending||[];
  $('pending').innerHTML=pe.length?pe.map(p=>`<div class="card ${p.side==='BUY'?'buy':'sell'}">
     <div class="top"><b>${p.side==='BUY'?'🟢 خرید':'🔴 فروش'} · <span class="mono">${esc(p.setup)}</span></b><span class="muted">${p.left_min} دقیقه مانده</span></div>
     <div class="kv"><span>قیمت <b class="num">${p.price.toFixed(2)}</b></span><span>حد ضرر <b class="num">${p.sl.toFixed(2)}</b></span><span>ریسک <b class="num">$${p.risk.toFixed(2)}</b></span></div>
     ${p.analysis?`<div class="muted">🤖 ${esc(p.analysis)}</div>`:''}</div>`).join('')
    :'<div class="empty">هشدار بی‌جوابی نیست. جواب دادن فقط از تلگرام است.</div>';
  const days=st.days||[]; const mx=Math.max(1,...days.map(x=>Math.abs(x.pnl)));
  $('smax').textContent='حداکثر ±$'+mx.toFixed(0);
  $('bars').innerHTML=days.map(x=>{const h=(Math.abs(x.pnl)/mx*50).toFixed(1);
     return `<div class="b" title="${x.d}: ${money(x.pnl)} · ${x.n} معامله"><i class="${x.pnl>=0?'p':'n'}" style="height:${x.pnl?h:0}%"></i></div>`}).join('');
  $('blab').innerHTML=days.map(x=>`<span>${x.d.slice(5).replace('-','/')}</span>`).join('');
  $('vnote').textContent=st.has_virtual?'':'(نتیجهٔ فرضی هنوز حساب نشده)';
  $('setups').innerHTML=(st.setups||[]).map(r=>`<tr><td class="mono">${esc(r.setup)}</td><td class="n">${r.alerts}</td><td class="n">${r.taken}</td><td class="n">${r.trades}</td>
     <td class="n ${cls(r.pnl)}">${r.trades?money(r.pnl):'—'}</td><td class="n ${cls(r.avg_R)}">${r.avg_R!=null?(r.avg_R>0?'+':'')+r.avg_R.toFixed(2)+'R':'—'}</td>
     <td class="n">${r.win_R!=null?r.win_R+'٪':'—'}</td></tr>`).join('')||'<tr><td colspan="7" class="muted">هنوز داده‌ای نیست</td></tr>';
  const sd=st.sides||{};
  $('sides').innerHTML=['SELL','BUY'].map(k=>{const v=sd[k]||{};return `<div class="card ${k==='BUY'?'buy':'sell'}"><div class="top"><b>${k==='BUY'?'خرید':'فروش'} (همهٔ ستاپ‌ها، فرضی)</b>
     <span class="pnl num ${cls(v.sum_R)}">${v.sum_R!=null?(v.sum_R>0?'+':'')+v.sum_R.toFixed(1)+'R':'—'}</span></div><div class="muted">${fa(v.n)} نمونه</div></div>`}).join('');
  $('events').innerHTML=(st.recent||[]).map(e=>`<li><span class="t">${esc(e.t)}</span><span>${esc(e.text)}${e.price!=null?` <span class="muted num">@ ${e.price.toFixed(2)}</span>`:''}</span>
     <span class="num ${cls(e.profit)}">${e.profit!=null?money(e.profit):''}</span></li>`).join('')||'<li><span></span><span class="muted">رویدادی نیست</span><span></span></li>';
  const r=d.rules||{};
  $('rules').innerHTML=[['حجم',r.lot+' لات'],['حد ضرر سیگنال','حداکثر $'+r.sl_cap],['حد ضرر اضطراری','حداکثر $'+r.emerg_cap],
    ['هشدار نزدیک حد ضرر',r.approach+'٪ ریسک'],['پوزیشن هم‌زمان','حداکثر '+r.max_open],['سقف ریسک کل','$'+r.max_risk],
    ['مهلت جواب',r.expiry_min+' دقیقه'],['گزارش زنده','هر '+r.status_min+' دقیقه']]
    .map(([k,v])=>`<div class="rule">${k}<b>${esc(v)}</b></div>`).join('');
  $('upd').textContent=s.version?('نسخه '+s.version):'';
  LAST=st; renderCompare(); renderPicks(st.picks);
  document.querySelectorAll('[data-r]').forEach(el=>{const v=r[el.dataset.r];if(v!=null)el.textContent=(el.dataset.pre||'')+v+(el.dataset.suf||'');});
}
let LAST={}, WIN='7d';
const rfmt=v=>v==null?'—':(v>0?'+':'')+v.toFixed(2)+'R';
function renderCompare(){
  const c=LAST.compare||{}; const rows=c[WIN]||[];
  const best=Math.max(...rows.filter(r=>r.sum_r!=null).map(r=>r.sum_r));
  $('cmp').innerHTML=rows.map(r=>`<tr class="${r.me?'me':''}"><td>${r.sum_r!=null&&r.sum_r===best&&r.n?'🏆 ':''}${esc(r.name)}</td>
    <td class="n">${r.n}</td><td class="n">${r.win!=null?r.win+'٪':'—'}</td><td class="n ${cls(r.avg_r)}">${rfmt(r.avg_r)}</td>
    <td class="n ${cls(r.sum_r)}">${r.sum_r!=null?(r.sum_r>0?'+':'')+r.sum_r.toFixed(1)+'R':'—'}</td><td class="n ${cls(r.pnl)}">${r.n?money(r.pnl):'—'}</td></tr>`).join('')
    ||'<tr><td colspan="6" class="muted">هنوز معامله‌ای برای مقایسه نیست</td></tr>';
  $('cmpnote').textContent='شما + اسکات در برابر دو ربات خودکار'+(c.since?(' · اسکات از '+c.since):'');
}
function renderPicks(p){
  if(!p){$('picks').innerHTML='<div class="empty">نتیجهٔ فرضی هنوز حساب نشده؛ چند دقیقه بعد از روشن شدن اسکات می‌آید.</div>';$('picksay').textContent='';return;}
  const card=(k,t)=>{const v=p[k]||{};return `<div class="tile"><div class="k">${t}</div><div class="v num ${cls(v.avg_r)}">${rfmt(v.avg_r)}</div><div class="s">${fa(v.n)} نمونه · برد ${v.win!=null?v.win+'٪':'—'}</div></div>`};
  $('picks').innerHTML=card('taken','✅ تأییدشده‌های شما')+card('passed','❌ ردشده یا منقضی')+card('silent','🔕 بی‌صدا (پوزیشن باز یا توقف)')+card('all','همهٔ سیگنال‌ها');
  const a=p.taken||{}, b=p.all||{};
  $('picksay').textContent=(a.avg_r==null||b.avg_r==null||a.n<5)?'برای مقایسه هنوز تأییدهای کافی نیست.'
    :(a.avg_r>b.avg_r?`انتخاب‌های شما تا الان به‌طور میانگین ${(a.avg_r-b.avg_r).toFixed(2)}R در هر معامله بهتر از کل سیگنال‌ها بوده است.`
    :`انتخاب‌های شما فعلاً ${(b.avg_r-a.avg_r).toFixed(2)}R در هر معامله پایین‌تر از کل سیگنال‌هاست؛ با معاملات بیشتر دقیق‌تر می‌شود.`);
}
document.querySelectorAll('#cmpwin .tab').forEach(b=>b.onclick=()=>{WIN=b.dataset.w;
  document.querySelectorAll('#cmpwin .tab').forEach(x=>x.setAttribute('aria-selected',String(x===b)));renderCompare();});
function show(tab){
  const g=tab==='guide';
  $('p-dash').hidden=g;$('p-guide').hidden=!g;
  $('t-dash').setAttribute('aria-selected',String(!g));$('t-guide').setAttribute('aria-selected',String(g));
  try{history.replaceState(null,'',location.pathname+location.search+(g?'#guide':''));}catch(e){}
  if(g)window.scrollTo(0,0);
}
$('t-dash').onclick=()=>show('dash');$('t-guide').onclick=()=>show('guide');
document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>show(b.dataset.go));
show(location.hash==='#guide'?'guide':'dash');
async function tick(){
  try{const res=await fetch('/api/data'+location.search,{cache:'no-store'});
      if(!res.ok)throw new Error('HTTP '+res.status);render(await res.json());}
  catch(e){$('err').hidden=false;$('err').textContent='اتصال به اسکات برقرار نشد: '+e.message;}
}
tick();setInterval(tick,10000);
</script>
</body>
</html>
"""
