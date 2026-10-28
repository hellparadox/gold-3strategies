"""داشبورد وب اسکات — از داخل خود پروسهٔ اسکات.

http://SERVER_IP:8082/?token=...   (پورت در scout.dashboard.port)

- بدون وابستگی تازه: ThreadingHTTPServer روی یک نخ daemon.
- توکن از env (DASHBOARD_TOKEN_SCOUT یا DASHBOARD_TOKEN) یا از scout.dashboard.token. بدون توکن داشبورد اصلاً روشن نمی‌شود (هیچ‌وقت عمومی نیست).
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

try:
    from tools import scout_followup as _fu
except Exception:                      # pragma: no cover
    import scout_followup as _fu

TEHRAN = timezone(timedelta(hours=3, minutes=30))     # ایران از ۱۴۰۱ ساعت تابستانی ندارد

EVENT_FA = {
    "alert": "🔔 هشدار", "suppressed": "🔕 بی‌صدا", "opened": "✅ باز شد", "rejected": "❌ رد شد",
    "expired": "⌛ منقضی شد", "cancelled": "↩️ لغو شد", "invalid": "🚫 باطل شد",
    "blocked_hedge": "⛔ بلاک (پوزیشن مخالف)", "blocked_limit": "⛔ بلاک (سقف ریسک/تعداد)",
    "order_failed": "⚠️ خطای سفارش", "closed": "🔻 بستن دستی", "gone": "ℹ️ بسته شد",
    "reached_1R": "🎯 رسید به 1R", "structure_break": "⚠️ شکست ساختار", "hold": "⏸ نگه دار",
    "unhold": "▶️ لغو نگه داشتن", "sl_close": "⛔ بسته در حد ضرر", "ai_hold": "🤖 هوش مصنوعی نگه داشت",
    "ai_close": "🤖 هوش مصنوعی بست", "analysis": "🤖 تحلیل", "feed_stale": "📡 قطع داده",
    "feed_ok": "📡 داده برگشت", "approved_dry": "🧪 تأیید آزمایشی", "manual_card": "✋ کارت سفارش دستی",
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


def _note_lot(note: Any, default: float) -> float:
    """لات همان معامله از یادداشت «opened» (…;lot=0.02)؛ معاملات قدیمی‌تر: پیش‌فرض."""
    for part in str(note or "").split(";"):
        if part.startswith("lot="):
            v = _f(part[4:])
            if not math.isnan(v) and v > 0:
                return v
    return default


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
            r = None if (math.isnan(entry) or math.isnan(sl)) else _r(p, entry, sl, _note_lot(o.get("note"), lot))
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


# --------------------------------------------------------------------------- جدول معاملات
# «چه کسی/چه چیزی بست»: برای معاملات جدید از یادداشت gone (how=…;by=…;cby=…)، برای قدیمی‌ها از
# رویدادهای closed / sl_close و نوع خروج.
CLOSER = {
    "manual":       ("شما", "دکمهٔ بستن"),
    "reverse":      ("شما", "عوض کردن جهت"),
    "ai":           ("هوش مصنوعی", "تصمیم در حد ضرر"),
    "stop":         ("ربات", "حد ضرر سیگنال"),
    "signal_sl":    ("ربات", "حد ضرر سیگنال"),
    "emergency_sl": ("بروکر", "حد ضرر اضطراری"),
    "emergency_bot": ("ربات", "حد ضرر اضطراری"),
    "exit_tp":      ("ربات", "قیمت خروج شما (بالا)"),
    "exit_sl":      ("ربات", "قیمت خروج شما (پایین)"),
    "external":     ("بیرون از اسکات", "متاتریدر یا بروکر"),
}


def _kv(note: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in str(note or "").split(";"):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def _ok(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1")


def _old_close_code(evs: List[Dict[str, str]], kind: str) -> str:
    """معاملات قدیمی (بدون how در gone): از رویدادهای همان تیکت حدس می‌زنیم."""
    for e in evs:
        if e.get("event") == "closed" and _ok(e.get("ok")):
            return "reverse" if str(e.get("note") or "").startswith("reverse") else "manual"
    for e in evs:
        if e.get("event") == "ai_close":
            return "ai"
    for e in evs:
        if e.get("event") == "sl_close" and _ok(e.get("ok")):
            n = str(e.get("note") or "")
            if "قیمت خروج پایین" in n:
                return "exit_sl"
            if "قیمت خروج" in n:
                return "exit_tp"
            if "اضطراری" in n:
                return "emergency_bot"
            if "هوش مصنوعی" in n:
                return "ai"
            return "stop"
    return kind if kind in ("emergency_sl", "signal_sl", "exit_tp", "exit_sl") else "external"


def build_trades(events: List[Dict[str, str]], lot: float = 0.01,
                 actual_by_ticket: Optional[Dict[str, float]] = None, limit: int = 1000) -> List[Dict[str, Any]]:
    """معاملات تأییدشده و بسته‌شدهٔ اسکات، جدیدترین بالا. هر ردیف همه‌چیز را دارد: باز/بسته چه کسی،
    کی، جهت، ستاپ، ورود/خروج، سود و R، و چطور بسته شد."""
    actual_by_ticket = actual_by_ticket or {}
    by_tk: Dict[str, List[Dict[str, str]]] = {}
    for e in events:
        tk = str(e.get("ticket") or "").strip()
        if tk:
            by_tk.setdefault(tk, []).append(e)
    rows: List[Dict[str, Any]] = []
    for tk, evs in by_tk.items():
        gone = next((e for e in evs if e.get("event") == "gone"), None)
        if gone is None:
            continue
        opened = next((e for e in evs if e.get("event") == "opened"), None)
        adopted = next((e for e in evs if e.get("event") == "adopted"), None)
        profit = _f(gone.get("profit"))
        if math.isnan(profit) and tk in actual_by_ticket:
            profit = float(actual_by_ticket[tk])
        t_close = _tehran(gone.get("ts", ""))
        if math.isnan(profit) or t_close is None:
            continue
        o = opened or {}
        gk = _kv(gone.get("note"))
        ok_ = _kv(o.get("note"))
        kind = str(gone.get("note") or "").split(";")[0].strip()
        t_open = _tehran((opened or adopted or {}).get("ts", ""))
        entry = _f(o.get("price"))
        if math.isnan(entry):
            entry = _f(gone.get("entry"))
        exit_px = _f(gone.get("price"))
        sl = _f(o.get("sl"))
        if math.isnan(sl) and adopted is not None:
            sl = _f(adopted.get("sl"))
        trade_lot = _note_lot(o.get("note"), lot)
        r = None if (math.isnan(entry) or math.isnan(sl)) else _r(profit, entry, sl, trade_lot)

        code = gk.get("how") or ""
        if code == "stop":                       # ربات بست؛ حد سیگنال یا اضطراری؟ از نوع خروج
            code = "emergency_bot" if kind == "emergency_sl" else "stop"
        elif not code:
            code = _old_close_code(evs, kind)
        who, how = CLOSER.get(code, CLOSER["external"])
        cname = gk.get("cby") or next((_kv(e.get("note")).get("by", "") for e in evs
                                        if e.get("event") == "closed" and _ok(e.get("ok"))), "")
        closed_by = who + (f" ({cname})" if who == "شما" and cname else "")

        oname = ok_.get("by") or gk.get("by") or ""
        if adopted is not None and opened is None:
            opened_by = "بیرون از اسکات (پیدا و تحت نظر گرفته شد)"
        elif oname:
            opened_by = f"شما ({oname})"
        else:
            opened_by = "شما (نام ثبت نشده)"
        opener = (oname or "").strip() or ("بیرون از اسکات" if (adopted is not None and opened is None)
                                           else "نام ثبت نشده")
        secs = (t_close - t_open).total_seconds() if t_open else None
        held = any(e.get("event") == "hold" for e in evs) or gk.get("held") == "1"
        rows.append({
            "ticket": tk, "alert": str(gone.get("alert") or o.get("alert") or ""),
            "t_close": t_close.astimezone(timezone.utc).isoformat(),
            "day": t_close.date().isoformat(), "day_fa": _fu.jalali_label(t_close.date()),
            "open_hm": t_open.strftime("%H:%M") if t_open else "—",
            "open_day": t_open.date().isoformat() if t_open else "",
            "close_hm": t_close.strftime("%H:%M"),
            "secs": None if secs is None or secs < 0 else int(secs),
            "side": gone.get("side") or o.get("side") or "",
            "setup": str(gone.get("setup") or o.get("setup") or "").replace(",", "+") or "—",
            "lot": trade_lot,
            "entry": None if math.isnan(entry) else round(entry, 2),
            "exit": None if math.isnan(exit_px) else round(exit_px, 2),
            "sl": None if math.isnan(sl) else round(sl, 2),
            "profit": round(profit, 2), "r": r,
            "opened_by": opened_by, "opener": opener, "closed_by": closed_by, "closed_how": how, "close_code": code,
            "held": bool(held), "contra": "contra=1" in str(o.get("note") or ""),
            "locked": gk.get("locked") == "1",
        })
    rows.sort(key=lambda x: x["t_close"], reverse=True)
    return rows[:limit]



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
                actual_by_ticket: Optional[Dict[str, float]] = None, lot: float = 0.01) -> Dict[str, Any]:
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
        "trades": build_trades(events, lot, actual_by_ticket),
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
    <section>
      <div class="sh"><span class="idx">00</span><h2>راهنمای اسکات</h2></div>
      <p class="guide-lead" style="color:var(--ink)">اسکات یک <em>دیدبان</em> است، نه ربات خودکار.</p>
      <p><b>خودش وارد معامله نمی‌شود.</b> فقط ستاپ‌ها را پیدا می‌کند، در تلگرام خبر می‌دهد و تصمیم را به شما می‌سپارد.</p>
      <p>همهٔ معاملات با حجم ثابت <b data-r="lot">0.01</b> لات روی حساب دمو انجام می‌شوند تا نتیجهٔ ستاپ‌ها منصفانه مقایسه شود. همه‌چیز در دفتر اسکات ثبت می‌شود، حتی هشدارهایی که رد کرده‌اید. همین‌طور می‌شود فهمید کدام ستاپ واقعاً جواب می‌دهد.</p>
    </section>

    <section><div class="sh"><span class="idx">01</span><h2>یک معامله از اول تا آخر</h2></div>
      <ol class="steps">
        <li><b>پیدا کردن ستاپ.</b> هر بار که یک کندل ۱۵ دقیقه‌ای بسته می‌شود، اسکات ۱۰ نوع ستاپ را روی آن چک می‌کند. اگر چند ستاپ هم‌زمان و هم‌جهت بودند، در یک پیام می‌آیند.</li>
        <li><b>هشدار در تلگرام.</b> پیام شامل جهت (خرید یا فروش)، قیمت، حد ضرر، هدف پیشنهادی (۲ برابر ریسک) و ریسک به دلار است.</li>
        <li><b>تصمیم شما.</b> <b data-r="expiry_min">30</b> دقیقه فرصت دارید: ✅ تأیید، ❌ رد، یا اول 🤖 تحلیل بگیرید. اگر جواب ندهید، هشدار منقضی می‌شود.</li>
        <li><b>باز شدن معامله.</b> با تأیید، معامله با قیمت همان لحظه باز می‌شود و حد ضرر با همان فاصلهٔ هشدار روی بروکر ثبت می‌شود. اگر قیمت در این فاصله از حد ضرر رد شده باشد، معامله باز نمی‌شود.</li>
        <li><b>مراقبت.</b> هر <b data-r="status_min">5</b> دقیقه یک پیام وضعیت با سود و زیان می‌آید. در <b data-r="approach" data-suf="٪">70٪</b> فاصله تا حد ضرر یک هشدار «نزدیک حد ضرر» می‌آید و رسیدن به ۱ برابر ریسک (1R) هم خبر داده می‌شود.</li>
        <li><b>بسته شدن.</b> یا خودتان با دکمهٔ 🔻 بستن می‌بندید، یا قیمت به حد ضرر می‌رسد و طبق «حالت» (پایین‌تر توضیح داده شده) رفتار می‌شود. اسکات حد سود خودکار نمی‌گذارد؛ زمان گرفتن سود با شماست.</li>
      </ol>
    </section>

    <section><div class="sh"><span class="idx">02</span><h2>۱۰ ستاپ اسکات</h2></div>
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

    <section><div class="sh"><span class="idx">03</span><h2>دکمه‌های تلگرام</h2></div>
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

    <section><div class="sh"><span class="idx">04</span><h2>حد ضرر و «حالت»</h2></div>
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

    <section><div class="sh"><span class="idx">05</span><h2>سقف‌های هم‌زمانی</h2></div>
      <p>هشدارها همیشه می‌آیند، حتی وقتی پوزیشن باز دارید. با تأیید جدید:</p>
      <div class="gl">
        <div><b>حداکثر <span data-r="max_open">2</span> پوزیشن</b><span>اگر پر باشد، اسکات رد نمی‌کند؛ می‌پرسد «باز هم باز کنم؟» و با «✅ می‌دانم، باز کن» باز می‌کند.</span></div>
        <div><b>سقف ریسک کل <span data-r="max_risk" data-pre="$">$20</span></b><span>جمع ضرر احتمالی همهٔ پوزیشن‌ها تا حد ضرر فعلی‌شان. اگر با این تأیید بیشتر شود، اسکات عددش را می‌گوید و تصمیم را به شما می‌سپارد.</span></div>
        <div><b>پوزیشن خلاف جهت</b><span>اگر خرید باز است و سیگنال فروش را تأیید کنید (یا برعکس)، اسکات می‌پرسد: «🔄 خرید را ببند و فروش را باز کن»، «✅ هر دو باز بمانند» یا «❌ رد». قبل از بستن چک می‌کند سیگنال هنوز معتبر باشد، و هشدار تا جواب شما زنده می‌ماند.</span></div>
      </div>
      <p class="hint">در حالت «نگه داشتن» هر پوزیشن تا حد ضرر اضطراری (حداکثر <b data-r="emerg_cap" data-pre="$">$20</b>) حساب می‌شود؛ برای همین با یک پوزیشن باز، سقف ۲۰ دلاری معمولاً پر است و اسکات برای دومی از شما می‌پرسد.</p>
    </section>

    <section><div class="sh"><span class="idx">06</span><h2>R و انتخاب‌های شما</h2></div>
      <p>هر معامله با <b>R</b> سنجیده می‌شود: سود تقسیم بر ریسک اولیه (فاصلهٔ ورود تا حد ضرر ضرب در حجم). مثلاً اگر ریسک یک معامله ۱۰ دلار بوده و ۲۰ دلار سود داده، نتیجه‌اش ‎+2R است. بخش «انتخاب‌های شما» همهٔ سیگنال‌های اسکات را با یک معیار فرضی مقایسه می‌کند تا معلوم شود انتخاب شما بهتر از گرفتن همهٔ سیگنال‌هاست یا نه.</p>
    </section>

    <section><div class="sh"><span class="idx">07</span><h2>تب «معاملات»</h2></div>
      <p>فهرست همهٔ معاملاتی که تأیید و بسته شده‌اند، جدیدترین بالا و به تفکیک روز. برای هر معامله: زمان باز و بسته شدن، مدت، جهت، ستاپ، لات، قیمت ورود و خروج، سود و ضرر، و اینکه چه کسی باز کرد و چه کسی یا چه چیزی بست (شما با دکمهٔ بستن، حد ضرر سیگنال، حد ضرر اضطراری، هوش مصنوعی، قیمت خروجی که شما گذاشتید، یا بیرون از اسکات). نام کسی که تأیید یا بسته است از این نسخه به بعد ثبت می‌شود؛ معاملات قدیمی‌تر «نام ثبت نشده» دارند.</p>
    </section>

    <section><div class="sh"><span class="idx">08</span><h2>نمودار، «اگر گرفته بودید» و ابزارها</h2></div>
      <div class="gl">
        <div><b>📊 نمودار هشدار</b><span>زیر هر هشدار یک نمودار ۱۵ دقیقه‌ای می‌آید. سبز: از ورود تا هدف 2R (محدودهٔ سود). قرمز: از ورود تا حد ضرر. کهربایی: محدودهٔ خنثی دور ورود، جایی که بستن به‌خاطر اسپرد تقریباً سر به سر است. خط نقطه‌چین سبز: 1R.</span></div>
        <div><b>🔮 اگر گرفته بودید…</b><span>هشداری که رد کنید، منقضی شود یا به‌خاطر سقف باز نشود، بی‌صدا دنبال می‌شود. وقتی به هدف یا حد ضرر رسید (حداکثر ۲۴ ساعت)، نتیجه و دلیلش زیر همان هشدار می‌آید: هم‌جهت یا خلاف روند یک‌ساعته، جای قیمت نسبت به ابر، خبر، و مسیر قیمت.</span></div>
        <div><b>🏁 جدول امتیاز شبانه</b><span>هر شب ساعت ۲۳:۵۵ تهران: امروز و ۷ روز اخیر با R، به‌علاوهٔ تعداد ردهای درست. روزهای بی‌فعالیت پیامی نمی‌آید؛ از منو هر وقت خواستید.</span></div>
        <div><b>🔔 هشدار قیمت</b><span><span class="mono">/alert 4150</span> وقتی طلا به ۴۱۵۰ برسد خبر می‌دهد (با یادداشت هم می‌شود). <span class="mono">/alerts</span> فهرست و حذف.</span></div>
      </div>
    </section>

    <section><div class="sh"><span class="idx">09</span><h2>اطلاعات اضافه زیر هشدار</h2></div>
      <div class="gl">
        <div><b>📈 کارنامهٔ ستاپ</b><span>میانگین نتیجهٔ فرضی ۳۰ نمونهٔ آخر همان ستاپ. اگر از ‎−0.5R بدتر باشد با ⚠️ می‌آید. فقط اطلاع است و هیچ سیگنالی حذف نمی‌شود.</span></div>
        <div><b>📰 خبر مهم</b><span>اگر خبر پراهمیت دلار در ۱۵ دقیقهٔ قبل یا بعد باشد، یک خط زیر هشدار می‌آید. هشدار مثل همیشه می‌آید.</span></div>
        <div><b>❓ سفارش نامشخص</b><span>اگر جواب بروکر معلوم نباشد، اسکات «ثبت نشد» نمی‌گوید. چند ثانیه بعد خودش بررسی می‌کند و اگر پوزیشن باز شده بود، تحت نظر می‌گیرد.</span></div>
        <div><b>🤖 تحلیل به‌روز</b><span>تحلیل با قیمت و نمودار لحظهٔ زدن دکمه ساخته می‌شود، نه لحظهٔ آمدن هشدار.</span></div>
      </div>
    </section>

    <section><div class="sh"><span class="idx">10</span><h2>پیام‌های خودکار</h2></div>
      <div class="gl">
        <div><b>📊 وضعیت زنده</b><span>هر <b data-r="status_min">5</b> دقیقه برای هر پوزیشن. وقتی بازار بسته است فقط یک پیام «بازار بسته» می‌آید.</span></div>
        <div><b>⚠️ نزدیک حد ضرر</b><span>یک بار، با دکمهٔ «نگه دار»، تا قبل از رسیدن تصمیم بگیرید.</span></div>
        <div><b>🎯 رسیدن به 1R</b><span>سود به اندازهٔ ریسک اولیه رسیده است.</span></div>
        <div><b>⚠️ شکست ساختار</b><span>کندل خلاف جهت تنکان بسته شده؛ نشانهٔ ضعیف شدن حرکت.</span></div>
        <div><b>💚 سالمم</b><span>هر روز حدود ساعت ۸:۳۰ تهران. اگر نیامد، یعنی اسکات مشکل دارد.</span></div>
        <div><b>📡 قطع داده</b><span>اگر ۱۰ دقیقه قیمت از MT5 نرسد. وقتی برگشت هم خبر می‌دهد.</span></div>
      </div>
    </section>

    <section><div class="sh"><span class="idx">11</span><h2>واژه‌نامه</h2></div>
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

    <section><div class="sh"><span class="idx">12</span><h2>سؤال‌های رایج</h2></div>
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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#0b0b0a">
<title>داشبورد اسکات</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%230b0b0a'/%3E%3Cpath d='M16 6l3.2 6.8L26 16l-6.8 3.2L16 26l-3.2-6.8L6 16l6.8-3.2z' fill='%23D8B25E'/%3E%3C/svg%3E">
<!-- فونت منتظر نمی‌ماند: اگر Google Fonts باز نشد (مثلاً بدون فیلترشکن)، صفحه فوراً با Tahoma باز می‌شود -->
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;700;800&family=JetBrains+Mono:wght@400;500&display=swap" media="print" onload="this.media='all'">
<style>
html,body{background:#0b0b0a}
:root{
  --bg:#0b0b0a;--card:rgba(24,22,19,.62);--card-solid:#141310;
  --line:rgba(236,226,200,.08);--line2:rgba(236,226,200,.15);
  --ink:#EDE8DC;--mute:rgba(237,232,220,.62);--faint:rgba(237,232,220,.40);
  --gold:#D8B25E;--gold-hi:#F3D896;--gold-soft:rgba(216,178,94,.12);
  --good:#5BCB9C;--good-soft:rgba(91,203,156,.12);--bad:#F07D6E;--bad-soft:rgba(240,125,110,.12);
  --warn:#E9AB52;--warn-soft:rgba(233,171,82,.13);
  --f-body:"Vazirmatn",Tahoma,system-ui,sans-serif;--f-mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
  --ease:cubic-bezier(.22,1,.36,1);--r:22px;
  color-scheme:dark;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{overflow-x:clip}
body{margin:0;color:var(--ink);font-family:var(--f-body);font-size:15px;line-height:1.75;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;min-height:100vh;
  background:
    radial-gradient(1100px 520px at 78% -8%,rgba(216,178,94,.10),transparent 62%),
    radial-gradient(900px 600px at 5% 10%,rgba(216,178,94,.035),transparent 60%),
    var(--bg)}
::selection{background:rgba(216,178,94,.32);color:#fff}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:100;opacity:.06;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
h1,h2,h3{margin:0;font-weight:700}
a{color:var(--gold)}
.num{font-variant-numeric:tabular-nums;direction:ltr;unicode-bidi:isolate}
.mono{font-family:var(--f-mono);font-size:.86em;direction:ltr;unicode-bidi:isolate;letter-spacing:-.01em}
.muted{color:var(--mute)} .pos{color:var(--good)} .neg{color:var(--bad)}
.wrap{max-width:1240px;margin:0 auto;padding:0 clamp(16px,3.4vw,44px) 72px}

/* ───────── top bar ───────── */
.top{position:sticky;top:0;z-index:50;padding:14px 0;margin:0 calc(-1*clamp(16px,3.4vw,44px));padding-inline:clamp(16px,3.4vw,44px);
  display:flex;align-items:center;gap:14px;transition:background .3s,border-color .3s;border-bottom:1px solid transparent}
.top.scrolled{background:rgba(11,11,10,.78);-webkit-backdrop-filter:blur(18px) saturate(1.3);backdrop-filter:blur(18px) saturate(1.3);border-color:var(--line)}
.brand{display:flex;align-items:center;gap:10px;flex:none}
.brand svg{width:34px;height:34px;flex:none}
.brand b{font-size:1.32rem;font-weight:800;letter-spacing:-.01em;line-height:1}
.brand small{display:block;font-family:var(--f-mono);font-size:9.5px;letter-spacing:.22em;color:var(--faint);direction:ltr;text-align:right;margin-top:3px}
.tabs{display:flex;gap:2px;padding:4px;margin-inline:auto;border-radius:999px;background:rgba(237,232,220,.045);border:1px solid var(--line);
  overflow-x:auto;scrollbar-width:none;max-width:100%}
.tabs::-webkit-scrollbar{display:none}
.tab{font:inherit;font-size:13.5px;font-weight:500;border:0;background:none;color:var(--mute);border-radius:999px;padding:7px 16px;cursor:pointer;
  display:flex;align-items:center;gap:7px;white-space:nowrap;transition:color .2s,background .25s var(--ease)}
.tab svg{width:15px;height:15px;opacity:.75}
.tab:hover{color:var(--ink)}
.tab[aria-selected="true"]{background:var(--ink);color:#12110e}
.tab[aria-selected="true"] svg{opacity:1}
.tab:focus-visible,button:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.tick{flex:none;display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mute);border:1px solid var(--line);border-radius:999px;padding:6px 14px}
.tick b{color:var(--ink);font-weight:500;font-size:14px}

/* ───────── status line ───────── */
.pills{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12.5px;color:var(--mute);padding:4px 0 0}
.pill{display:inline-flex;gap:7px;align-items:center}
.pill b{color:var(--ink);font-weight:500}
.dot{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:none}
.dot.ok{background:var(--good);box-shadow:0 0 0 3px var(--good-soft)} .dot.bad{background:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)} .dot.warn{background:var(--warn);box-shadow:0 0 0 3px var(--warn-soft)}
.live .dot.ok{animation:beat 2.4s ease-in-out infinite}
@keyframes beat{50%{box-shadow:0 0 0 6px rgba(91,203,156,0)}}
.err{background:var(--bad-soft);color:var(--bad);border:1px solid rgba(240,125,110,.25);border-radius:14px;padding:10px 16px;margin-top:14px}

/* ───────── hero ───────── */
.hero{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:clamp(20px,4vw,56px);align-items:end;padding:clamp(28px,6vw,72px) 0 clamp(26px,4vw,40px);position:relative}
.eyebrow{font-family:var(--f-mono);font-size:11px;letter-spacing:.22em;color:var(--faint);text-transform:uppercase;display:flex;align-items:center;gap:12px;direction:ltr;justify-content:flex-end}
.eyebrow::before{content:"";width:36px;height:1px;background:var(--gold);opacity:.7}
.hero-copy{position:relative;min-width:0;isolation:isolate}
.ghost{position:absolute;inset-inline-start:-.04em;top:-.32em;font-family:var(--f-body);font-weight:800;font-size:clamp(120px,22vw,300px);line-height:.8;letter-spacing:-.06em;
  color:transparent;-webkit-text-stroke:1px rgba(216,178,94,.10);pointer-events:none;user-select:none;direction:ltr;z-index:-1}
.price{display:flex;align-items:baseline;gap:14px;margin:14px 0 6px;direction:ltr;justify-content:flex-end}
.price .cur{font-size:clamp(20px,2.2vw,28px);font-weight:300;color:var(--gold)}
.mega{font-size:clamp(64px,10.4vw,148px);font-weight:300;line-height:.9;letter-spacing:-.045em;font-variant-numeric:tabular-nums;
  background:linear-gradient(180deg,#fff 0%,var(--ink) 45%,#b9b2a2 100%);-webkit-background-clip:text;background-clip:text;color:transparent;
  display:inline-flex;overflow:hidden;padding-bottom:.04em}
.mega span{display:inline-block}
.mega .up{animation:rollUp .6s var(--ease)} .mega .dn{animation:rollDn .6s var(--ease)}
@keyframes rollUp{from{transform:translateY(55%);opacity:0;filter:blur(4px)}to{transform:none;opacity:1}}
@keyframes rollDn{from{transform:translateY(-55%);opacity:0;filter:blur(4px)}to{transform:none;opacity:1}}
.flash-up{color:var(--good)!important} .flash-dn{color:var(--bad)!important}
.quote{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mute);justify-content:flex-start}
.quote b{color:var(--ink);font-weight:500}
.lede{max-width:34rem;color:var(--mute);font-size:15.5px;line-height:1.85;margin:22px 0 22px}
.lede b{color:var(--ink);font-weight:500}
.ctas{display:flex;gap:10px;flex-wrap:wrap}
.btn{font:inherit;font-size:14px;font-weight:500;border-radius:999px;padding:11px 14px 11px 22px;cursor:pointer;display:inline-flex;align-items:center;gap:12px;border:1px solid var(--line2);background:rgba(237,232,220,.04);color:var(--ink);transition:transform .2s var(--ease),background .2s,border-color .2s}
.btn:hover{background:rgba(237,232,220,.08);border-color:rgba(236,226,200,.28)}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--ink);color:#12110e;border-color:var(--ink)}
.btn.primary:hover{background:#fff}
.btn i{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;background:#12110e;color:var(--ink);font-style:normal;transition:transform .3s var(--ease)}
.btn:not(.primary) i{background:rgba(237,232,220,.08)}
.btn:hover i{transform:translateX(-3px)}
.btn i svg{width:13px;height:13px}

/* chart card */
.glass{background:var(--card);border:1px solid var(--line);border-radius:var(--r);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 24px 60px rgba(0,0,0,.28)}
.chart{padding:20px 22px 16px;min-width:0;overflow:clip}
.chart-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:14px}
.chart-head h3{font-size:14px;font-weight:500;color:var(--mute)}
.chart-head .big{font-size:clamp(26px,3vw,34px);font-weight:300;letter-spacing:-.03em;line-height:1}
.scale{display:flex;justify-content:space-between;font-family:var(--f-mono);font-size:10px;letter-spacing:.08em;color:var(--faint);margin-bottom:6px}
.bars{display:grid;grid-template-columns:repeat(14,minmax(0,1fr));gap:5px;height:150px;direction:ltr;position:relative}
.bars::after{content:"";position:absolute;left:0;right:0;top:50%;border-top:1px dashed var(--line2)}
.b{position:relative;height:100%;cursor:default}
.b i{position:absolute;left:14%;right:14%;border-radius:4px;transform-origin:bottom;transition:filter .2s}
.b i.p{bottom:50%;background:linear-gradient(180deg,var(--good),rgba(91,203,156,.45));border-radius:4px 4px 1px 1px}
.b i.n{top:50%;background:linear-gradient(0deg,var(--bad),rgba(240,125,110,.45));border-radius:1px 1px 4px 4px;transform-origin:top}
.b i.z{top:calc(50% - 1px);height:2px!important;background:var(--line2)}
.b:hover i{filter:brightness(1.25)}
.b .tip{position:absolute;bottom:calc(100% + 6px);left:50%;transform:translate(-50%,4px);opacity:0;pointer-events:none;white-space:nowrap;background:#1d1b17;border:1px solid var(--line2);border-radius:10px;padding:4px 9px;font-size:11.5px;line-height:1.5;color:var(--ink);transition:.18s;z-index:5}
.b:hover .tip{opacity:1;transform:translate(-50%,0)}
.b:nth-child(-n+3) .tip{left:0;transform:translate(0,4px)}.b:nth-child(-n+3):hover .tip{transform:none}
.b:nth-last-child(-n+3) .tip{left:auto;right:0;transform:translate(0,4px)}.b:nth-last-child(-n+3):hover .tip{transform:none}
.bars-in .b i{animation:grow .9s var(--ease) both;animation-delay:calc(var(--i)*35ms + .35s)}
@keyframes grow{from{transform:scaleY(0)}}
.blab{display:grid;grid-template-columns:repeat(14,minmax(0,1fr));gap:5px;direction:ltr;font-family:var(--f-mono);font-size:9.5px;color:var(--faint);text-align:center;margin-top:8px}
@media (max-width:560px){.blab span:nth-child(even){visibility:hidden}}

/* stat strip */
.strip{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.strip .tile{padding:20px 20px 18px;border-inline-start:1px solid var(--line);min-width:0}
.strip .tile:first-child{border-inline-start:0;padding-inline-start:0}
.k{font-size:12px;color:var(--mute)}
.v{font-size:clamp(22px,2.3vw,30px);font-weight:300;letter-spacing:-.03em;line-height:1.25;font-variant-numeric:tabular-nums;margin-top:4px}
.s{font-size:12px;color:var(--faint);margin-top:2px}
@media (max-width:980px){.strip{grid-template-columns:repeat(3,minmax(0,1fr))}.strip .tile:nth-child(4){border-inline-start:0;padding-inline-start:0}.strip .tile:nth-child(n+4){border-top:1px solid var(--line)}}
@media (max-width:560px){.strip{grid-template-columns:repeat(2,minmax(0,1fr))}.strip .tile{padding:16px 14px}.strip .tile:nth-child(odd){border-inline-start:0;padding-inline-start:0}.strip .tile:nth-child(4){border-inline-start:1px solid var(--line);padding-inline-start:14px}.strip .tile:nth-child(n+3){border-top:1px solid var(--line)}}

/* ───────── sections ───────── */
.pane{display:flex;flex-direction:column;gap:clamp(48px,7vw,84px)}
.pane:not(#p-dash){padding-top:clamp(28px,5vw,56px)}
section{display:flex;flex-direction:column;gap:14px;min-width:0}
.sh{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap}
.idx{font-family:var(--f-mono);font-size:11px;color:var(--gold);letter-spacing:.12em;direction:ltr;padding-bottom:6px}
h2{font-size:clamp(22px,2.4vw,30px);font-weight:500;letter-spacing:-.02em;line-height:1.3;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
h2 small{font-size:13px;font-weight:400;color:var(--faint)}
.hint{margin:0;font-size:13.5px;color:var(--mute);max-width:78ch;line-height:1.85}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:clamp(24px,3vw,40px)}
@media (max-width:820px){.cols{grid-template-columns:1fr}}

.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.tiles .tile{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px 18px;min-width:0}
.tiles .v{font-size:clamp(20px,2vw,27px);white-space:nowrap}
.picks{grid-template-columns:repeat(4,minmax(0,1fr))}
#trtiles{grid-template-columns:repeat(4,minmax(0,1fr))}
@media (max-width:900px){.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:720px){.picks,#trtiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:520px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.picks .tile:first-child{border-color:rgba(216,178,94,.35);background:linear-gradient(180deg,var(--gold-soft),var(--card) 70%)}
.meter{height:3px;border-radius:3px;background:var(--line);margin-top:12px;overflow:hidden;direction:ltr}
.meter i{display:block;height:100%;border-radius:3px;background:var(--gold)}
.say{font-size:14px;color:var(--ink);border-inline-start:2px solid var(--gold);padding:2px 14px;margin:0;max-width:78ch}
.say:empty{display:none}

/* cards (positions, pending) */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;display:flex;flex-direction:column;gap:10px;min-width:0;position:relative;overflow:hidden}
.card::before{content:"";position:absolute;inset-block:18px;inset-inline-start:0;width:3px;border-radius:0 3px 3px 0;background:var(--faint)}
.card.buy::before{background:var(--good)} .card.sell::before{background:var(--bad)}
.card+.card{margin-top:10px}
.card .top{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center;position:static;padding:0;margin:0;border:0;background:none;backdrop-filter:none}
.card .ttl{font-weight:500;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.side{font-size:12px;font-weight:700;border-radius:999px;padding:1px 10px}
.side.buy{background:var(--good-soft);color:var(--good)} .side.sell{background:var(--bad-soft);color:var(--bad)}
.card .pnl{font-size:26px;font-weight:300;letter-spacing:-.03em;line-height:1}
.kv{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px;color:var(--mute)}
.kv b{color:var(--ink);font-weight:500}
.track{position:relative;height:38px;direction:ltr;margin:4px 0 0}
.track .rail{position:absolute;left:0;right:0;top:15px;height:2px;border-radius:2px;background:var(--line2)}
.track .seg{position:absolute;top:15px;height:2px;border-radius:2px}
.track .m{position:absolute;top:9px;width:2px;height:14px;border-radius:2px;background:var(--mute);transform:translateX(-1px)}
.track .m.sl{background:var(--bad)} .track .m.em{background:transparent;border-left:2px dashed rgba(240,125,110,.6)} .track .m.en{background:var(--ink)}
.track .now{position:absolute;top:10px;width:12px;height:12px;border-radius:50%;background:var(--gold);transform:translateX(-6px);box-shadow:0 0 0 4px var(--gold-soft)}
.track .lb{position:absolute;top:24px;font-size:9.5px;font-family:var(--f-mono);color:var(--faint);transform:translateX(-50%);white-space:nowrap}
.tag{font-size:11.5px;border-radius:999px;padding:1px 9px;font-weight:500;background:var(--warn-soft);color:var(--warn);white-space:nowrap;display:inline-block}
.empty{border:1px dashed var(--line2);border-radius:var(--r);padding:22px 20px;color:var(--mute);font-size:14px;display:flex;align-items:center;gap:12px}
.empty::before{content:"";width:8px;height:8px;border-radius:50%;border:1.5px solid var(--faint);flex:none}

/* tables */
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:var(--r);scrollbar-width:thin;scrollbar-color:var(--line2) transparent}
/* موبایل: در جدول نفرات فقط ستون‌های مهم (رتبه، نفر، معاملات، سود و ضرر، میانگین R) — بقیه در دسکتاپ */
@media (max-width:760px){#pptab{min-width:0}#pptab th:nth-child(4),#pptab td:nth-child(4),#pptab th:nth-child(6),#pptab td:nth-child(6),#pptab th:nth-child(8),#pptab td:nth-child(8),#pptab th:nth-child(9),#pptab td:nth-child(9){display:none}}
table{border-collapse:collapse;width:100%;min-width:560px}
th,td{padding:11px 14px;text-align:right;border-bottom:1px solid var(--line);font-size:13.5px}
th{font-size:11.5px;font-weight:500;color:var(--faint);background:rgba(237,232,220,.025);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr{transition:background .15s}
tbody tr:hover td{background:rgba(237,232,220,.025)}
td.n{font-variant-numeric:tabular-nums;direction:ltr;text-align:right}
tr.muted td{color:var(--faint)}
table.wide{min-width:980px}
table.wide td{vertical-align:top}
table.wide th,table.wide td{padding:11px 9px}
.nw{white-space:nowrap}
.setup{min-width:140px}
.sub{display:block;color:var(--faint);font-size:11.5px}
tr.dayrow td{background:linear-gradient(90deg,transparent,var(--gold-soft));font-weight:500;font-size:13px;color:var(--ink);border-bottom-color:var(--line2);padding-top:14px}
tr.dayrow:hover td{background:linear-gradient(90deg,transparent,var(--gold-soft))}
th.srt{cursor:pointer;user-select:none;transition:color .15s} th.srt:hover{color:var(--ink)} th.srt.on{color:var(--gold)}
tr.pick{cursor:pointer}
tr.pick:hover td{background:var(--gold-soft)}
.who{font-size:13px;line-height:1.55} .who small{display:block;color:var(--faint);font-size:11.5px}
.rank{font-size:16px}
.chip{font-size:11.5px;border-radius:999px;padding:1px 9px;font-weight:500}
.chip.buy{background:var(--good-soft);color:var(--good)} .chip.sell{background:var(--bad-soft);color:var(--bad)}

/* filters */
.filters{display:flex;gap:8px;flex-wrap:wrap}
.filters select{font:inherit;font-size:13px;background:rgba(237,232,220,.04);color:var(--ink);border:1px solid var(--line2);border-radius:999px;padding:7px 16px 7px 38px;
  appearance:none;-webkit-appearance:none;cursor:pointer;transition:border-color .2s,background .2s;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%23D8B25E' fill='none' stroke-width='1.5'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:left 14px center}
.filters select:hover{border-color:rgba(236,226,200,.3)}
.filters select option{background:#141310;color:var(--ink)}

/* events, rules */
.ev{list-style:none;margin:0;padding:6px 0;background:var(--card);border:1px solid var(--line);border-radius:var(--r);max-height:440px;overflow:auto;scrollbar-width:thin;scrollbar-color:var(--line2) transparent}
.ev li{display:grid;grid-template-columns:90px 1fr auto;gap:12px;padding:9px 20px;font-size:13.5px;position:relative}
.ev li+li{border-top:1px solid var(--line)}
.ev .t{color:var(--faint);font-family:var(--f-mono);font-size:11.5px;direction:ltr;text-align:right;padding-top:2px}
.rules{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0;background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.rule{padding:14px 20px;font-size:12.5px;color:var(--mute);border-bottom:1px solid var(--line)}
.rule:nth-child(odd){border-inline-end:1px solid var(--line)}
.rule:nth-last-child(-n+2){border-bottom:0}
.rule b{display:block;font-size:19px;font-weight:300;letter-spacing:-.02em;color:var(--ink);font-variant-numeric:tabular-nums;margin-top:2px}

/* guide */
.guide{max-width:900px}
.guide p{margin:0;max-width:72ch;color:var(--mute);line-height:1.95}
.guide p b{color:var(--ink);font-weight:500}
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;counter-reset:st;border-inline-start:1px solid var(--line2);margin-inline-start:15px}
.steps li{counter-increment:st;position:relative;padding:4px 34px 22px 0;color:var(--mute);line-height:1.9}
.steps li b{color:var(--ink);font-weight:500}
.steps li::before{content:counter(st,persian);position:absolute;inset-inline-start:-16px;top:2px;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;
  background:var(--bg);border:1px solid var(--gold);color:var(--gold);font-size:13px;font-weight:500}
.gl{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.gl > div{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px 18px;display:flex;flex-direction:column;gap:4px;min-width:0;transition:border-color .2s}
.gl > div:hover{border-color:var(--line2)}
.gl b{font-size:14.5px;font-weight:500}
.gl span{font-size:13px;color:var(--mute);line-height:1.8}
.setgrid td:first-child{white-space:nowrap;color:var(--gold)}
details{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px 20px;transition:border-color .2s}
details[open]{border-color:var(--line2)}
details+details{margin-top:8px}
summary{cursor:pointer;font-weight:500;list-style:none;display:flex;justify-content:space-between;gap:12px}
summary::-webkit-details-marker{display:none}
summary::after{content:"+";color:var(--gold);font-size:20px;line-height:1;transition:transform .3s var(--ease)}
details[open] summary::after{transform:rotate(45deg)}
details p{margin-top:8px;color:var(--mute);font-size:14px}
.callout{background:var(--warn-soft);border:1px solid rgba(233,171,82,.22);color:var(--ink);border-radius:18px;padding:14px 20px;font-size:14px}
.guide-lead{font-size:clamp(28px,4vw,46px);font-weight:300;letter-spacing:-.03em;line-height:1.25;max-width:20ch}
.guide-lead em{font-style:normal;color:var(--gold)}

footer{margin-top:clamp(56px,8vw,96px);padding-top:18px;border-top:1px solid var(--line);font-size:12px;color:var(--faint);display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}

/* entrance (fail-safe: final state is the default; animation only runs once on load) */
.rise{animation:rise .9s var(--ease) both;animation-delay:calc(var(--d,0)*110ms + 60ms)}
@keyframes rise{from{opacity:0;transform:translateY(22px);filter:blur(8px)}}
.reveal{opacity:1}
.js .reveal{opacity:0;transform:translateY(26px);transition:opacity .8s var(--ease),transform .8s var(--ease)}
.js .reveal.in{opacity:1;transform:none}

@media (max-width:980px){
  .hero{grid-template-columns:1fr;align-items:stretch}
  .tick{display:none}
}
@media (max-width:760px){
  .top{flex-wrap:wrap;gap:10px;padding-block:10px}
  .tabs{order:3;width:100%;margin:0;justify-content:space-between}
  .tab{padding:7px 12px;font-size:13px;flex:1;justify-content:center}
  .tab svg{display:none}
  .brand{margin-inline-end:auto}
  .tick{display:flex;padding:4px 12px}
  .price{justify-content:flex-start;direction:rtl}
  .price .mega{direction:ltr}
  .eyebrow{justify-content:flex-start;direction:rtl}
  .ctas .btn{flex:1;justify-content:space-between}
  .rules{grid-template-columns:1fr}.rule{border-inline-end:0!important}.rule:nth-last-child(2){border-bottom:1px solid var(--line)}
  .ev li{grid-template-columns:76px 1fr;padding:9px 16px}.ev li > :last-child{grid-column:2}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
  .js .reveal{opacity:1;transform:none}
}

/* ───────── منحنی سود، سکوی نفرات، افکت‌ها ───────── */
@property --ang{syntax:'<angle>';inherits:false;initial-value:0deg}
.card,.glass,.pod{position:relative}
.card::after,.glass::after,.pod::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;opacity:0;transition:opacity .35s;
  background:radial-gradient(380px circle at var(--mx,50%) var(--my,50%),rgba(216,178,94,.12),transparent 62%)}
.card:hover::after,.glass:hover::after,.pod:hover::after{opacity:1}
.card.ring{border:1px solid transparent;
  background:linear-gradient(var(--card-solid),var(--card-solid)) padding-box,conic-gradient(from var(--ang),transparent 0 58%,var(--rc,var(--gold)) 84%,transparent 100%) border-box;
  animation:eqspin 6s linear infinite}
.card.ring.rp{--rc:var(--good)} .card.ring.rn{--rc:var(--bad)}
@keyframes eqspin{to{--ang:360deg}}
.eq{margin-top:34px}
.rng{display:flex;gap:4px;margin-inline-start:auto;padding:3px;border-radius:999px;background:rgba(237,232,220,.045);border:1px solid var(--line)}
.rng button{font:inherit;font-size:12.5px;border:0;background:none;color:var(--mute);padding:5px 14px;border-radius:999px;cursor:pointer;transition:background .2s,color .2s}
.rng button:hover{color:var(--ink)} .rng button.on{background:var(--ink);color:#12110e}
.eqbox{padding:18px 18px 12px;margin-top:14px}
.eqstats{display:flex;flex-wrap:wrap;gap:10px 30px;padding:2px 4px 16px}
.eqstats small{display:block;font-size:11.5px;color:var(--faint)}
.eqstats b{font-size:21px;font-weight:300;letter-spacing:-.02em}
.eqplot{position:relative;direction:ltr;cursor:crosshair;touch-action:pan-y;min-height:120px}
.eqplot svg{display:block;width:100%;height:auto;overflow:visible}
.eqplot .ax{font:10px var(--f-mono);fill:var(--faint)}
.eqline{fill:none;stroke:var(--gold-hi);stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round;filter:drop-shadow(0 0 6px rgba(216,178,94,.55))}
.eqarea{animation:eqfade 1.4s .4s both}
@keyframes eqfade{from{opacity:0}to{opacity:1}}
.eqzero{stroke:var(--line2);stroke-dasharray:3 5}
.eqgrid{stroke:var(--line)}
.eqend{fill:var(--gold-hi)} .eqping{fill:none;stroke:var(--gold-hi);stroke-width:1.5;transform-box:fill-box;transform-origin:center;animation:eqping 2.2s ease-out infinite}
@keyframes eqping{from{transform:scale(.5);opacity:.9}to{transform:scale(2.8);opacity:0}}
.eqx{stroke:var(--line2);stroke-dasharray:3 4}
.eqhov{fill:var(--bg);stroke:var(--gold-hi);stroke-width:2}
.eqtip{position:absolute;top:6px;transform:translateX(-50%);pointer-events:none;background:rgba(20,19,16,.96);border:1px solid var(--line2);border-radius:14px;
  padding:9px 12px;font-size:12.5px;white-space:nowrap;z-index:5;direction:rtl;text-align:right;line-height:1.55;box-shadow:0 12px 30px rgba(0,0,0,.45)}
.eqtip b{font-size:16px;font-weight:500}
.eqtip small{display:block;color:var(--faint);font-size:11.5px}
.eqempty{padding:34px 10px;text-align:center;color:var(--faint);font-size:13.5px}
.podium{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:14px 0}
.podium:empty{display:none}
.pod{border:1px solid var(--line);background:var(--card);border-radius:var(--r);padding:18px 20px;display:flex;align-items:center;gap:16px;cursor:pointer;overflow:hidden;
  transition:transform .3s var(--ease),border-color .3s}
.pod:hover{transform:translateY(-3px);border-color:var(--line2)}
.pod.r1{border-color:rgba(216,178,94,.45);background:linear-gradient(160deg,rgba(216,178,94,.15),var(--card) 62%)}
.pod .bd{min-width:0;flex:1}
.pod .rk{font-size:11.5px;letter-spacing:.06em;font-weight:500}
.pod.r1 .rk{color:var(--gold-hi)} .pod.r2 .rk{color:#cfd3da} .pod.r3 .rk{color:#d49a6a}
.pod .nm{font-size:1.12rem;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pod .pv{font-size:30px;font-weight:300;letter-spacing:-.03em;line-height:1.15}
.pod .ms{font-size:12px;color:var(--faint)}
.wr{width:58px;height:58px;flex:none}
.wr .trk{fill:none;stroke:var(--line2);stroke-width:5}
.wr .arc{fill:none;stroke:var(--gold);stroke-width:5;stroke-linecap:round;transform:rotate(-90deg);transform-origin:center}
.wr text{fill:var(--ink);font:500 11px var(--f-mono)}
@media (max-width:760px){.podium{grid-template-columns:1fr}.eqstats{gap:10px 22px}.rng button{padding:5px 11px}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top" id="top">
    <div class="brand rise" style="--d:0">
      <svg viewBox="0 0 34 34" aria-hidden="true"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#F3D896"/><stop offset=".55" stop-color="#D8B25E"/><stop offset="1" stop-color="#8E6A24"/></linearGradient></defs>
        <rect x=".5" y=".5" width="33" height="33" rx="10" fill="none" stroke="rgba(236,226,200,.15)"/><path d="M17 6.5l3.1 7.4 7.4 3.1-7.4 3.1-3.1 7.4-3.1-7.4-7.4-3.1 7.4-3.1z" fill="url(#g)"/></svg>
      <div><b>اسکات</b><small>SCOUT · XAU/USD</small></div>
    </div>
    <nav class="tabs rise" role="tablist" aria-label="بخش‌ها" style="--d:1">
      <button class="tab" role="tab" id="t-dash" aria-selected="true" aria-controls="p-dash"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 13.5h12M4 11V7M8 11V3M12 11V8.5"/></svg>داشبورد</button>
      <button class="tab" role="tab" id="t-trades" aria-selected="false" aria-controls="p-trades"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 2.5h10v11l-2-1.2-1.6 1.2L8 12.3l-1.4 1.2L5 12.3l-2 1.2z"/><path d="M5.5 6h5M5.5 8.6h3.5"/></svg>معاملات</button>
      <button class="tab" role="tab" id="t-audit" aria-selected="false" aria-controls="p-audit"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="5.8"/><path d="M10.4 5.6L9 9 5.6 10.4 7 7z"/></svg>کارنامهٔ سیگنال‌ها</button>
      <button class="tab" role="tab" id="t-guide" aria-selected="false" aria-controls="p-guide"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2.5 3.2c2-.8 3.8-.6 5.5.6v9.4c-1.7-1.2-3.5-1.4-5.5-.6zM13.5 3.2c-2-.8-3.8-.6-5.5.6v9.4c1.7-1.2 3.5-1.4 5.5-.6z"/></svg>راهنما</button>
    </nav>
    <div class="tick rise" style="--d:2"><span class="dot ok" id="tickdot"></span>طلا <b class="num" id="tickp">—</b></div>
  </header>

  <div class="pills live rise" id="pills" style="--d:2"><span class="pill"><span class="dot"></span>در حال دریافت…</span></div>
  <div id="err" class="err" hidden></div>

  <!-- ═════════ داشبورد ═════════ -->
  <div class="pane" id="p-dash" role="tabpanel" aria-labelledby="t-dash">
    <div>
      <div class="hero">
        <div class="hero-copy">
          <div class="ghost" aria-hidden="true">XAU</div>
          <div class="eyebrow rise" style="--d:2">Gold · live bid</div>
          <div class="price rise" style="--d:3"><span class="cur">$</span><span class="mega" id="hp" aria-live="off">—</span></div>
          <div class="quote rise" id="hq" style="--d:4"></div>
          <p class="lede rise" style="--d:5"><b>اسکات خودش معامله نمی‌کند.</b> هر ۱۵ دقیقه ستاپ‌های طلا را پیدا می‌کند، در تلگرام هشدار می‌دهد و فقط اگر شما تأیید کنید معامله باز می‌کند. این صفحه فقط نمایش است؛ همهٔ تصمیم‌ها از تلگرام گرفته می‌شود.</p>
          <div class="ctas rise" style="--d:6">
            <button class="btn primary" data-go="trades">معاملات و رتبه‌ها<i><svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M11 7H3M6.5 3.5L3 7l3.5 3.5"/></svg></i></button>
            <button class="btn" data-go="guide">راهنمای کامل<i><svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M11 7H3M6.5 3.5L3 7l3.5 3.5"/></svg></i></button>
          </div>
        </div>
        <div class="glass chart rise" style="--d:4">
          <div class="chart-head"><div><h3>سود و زیان ۱۴ روز اخیر</h3><div class="s">به وقت تهران · روی ستون نگه دارید</div></div><div class="big num" id="sum14">—</div></div>
          <div class="scale"><span id="smax"></span><span>USD</span></div>
          <div class="bars" id="bars"></div><div class="blab" id="blab"></div>
        </div>
      </div>
      <div class="strip rise" id="tiles" style="--d:7"></div>
      <p class="hint" style="margin-top:12px">موجودی و اکوییتی از خود حساب می‌آید. «اکوییتی» یعنی موجودی به‌علاوهٔ سود و زیان پوزیشن‌های باز. سود امروز و ۷ روز فقط معاملات بسته‌شدهٔ اسکات را می‌شمارد، به روز تهران. <span id="asof"></span></p>
    </div>

    <section class="reveal eq" id="eqsec">
      <div class="sh"><span class="idx">00</span><h2>منحنی سود <small id="eqnote"></small></h2>
        <div class="rng" id="eqchips" role="group" aria-label="بازهٔ زمانی"><button data-d="7">۷ روز</button><button data-d="30" class="on">۳۰ روز</button><button data-d="0">همه</button></div></div>
      <div class="glass eqbox"><div class="eqstats" id="eqstats"></div>
        <div class="eqplot" id="eqplot"><svg id="eqsvg" viewBox="0 0 600 280" role="img" aria-label="منحنی جمع سود و زیان معاملات تأییدشده"></svg><div class="eqtip" id="eqtip" hidden></div></div></div>
      <p class="hint" style="margin-top:10px">جمع سود و زیان خالص معاملات تأییدشده و بسته‌شده، به ترتیب زمان بسته شدن. روی نمودار بروید تا هر معامله را ببینید.</p>
    </section>

    <div class="cols reveal">
      <section><div class="sh"><span class="idx">01</span><h2>پوزیشن‌های باز</h2></div>
        <p class="hint">«حد ضرر سیگنال» حدی است که اسکات پیشنهاد داده. «روی بروکر» حدی است که الان واقعاً در MT5 ثبت است. اگر «نگه دار» زده باشید، این دو فرق دارند.</p>
        <div id="open"></div></section>
      <section><div class="sh"><span class="idx">02</span><h2>هشدارهای منتظر جواب</h2></div>
        <p class="hint">هشدارهایی که هنوز تأیید یا رد نکرده‌اید. جواب فقط از تلگرام داده می‌شود و بعد از مهلت، هشدار منقضی می‌شود.</p>
        <div id="pending"></div></section>
    </div>

    <section class="reveal"><div class="sh"><span class="idx">03</span><h2>انتخاب‌های شما در برابر همهٔ سیگنال‌ها</h2></div>
      <p class="hint">همهٔ سیگنال‌ها با یک معیار سنجیده شده‌اند: اگر هر کدام با حد ضرر خودش و هدف 2R گرفته می‌شد. اگر «تأییدشده‌های شما» از «همهٔ سیگنال‌ها» بهتر باشد، یعنی انتخاب شما به نتیجه اضافه می‌کند.</p>
      <div class="tiles picks" id="picks"></div>
      <p class="say" id="picksay"></p>
    </section>

    <section class="reveal"><div class="sh"><span class="idx">04</span><h2>ستاپ‌ها <small id="vnote"></small></h2></div>
      <p class="hint">«سود واقعی» از معاملاتی است که تأیید کرده‌اید. «میانگین فرضی» نشان می‌دهد اگر همهٔ هشدارهای آن ستاپ با حد ضرر خودش و هدف ۲ برابر ریسک گرفته می‌شد، به‌طور میانگین چند R می‌داد. عدد مثبت یعنی آن ستاپ در تاریخچه جواب داده است. ستاپ‌های با کمتر از ۵ نمونه «—» دارند.</p>
      <div class="tablewrap"><table><thead><tr><th>ستاپ</th><th>هشدار</th><th>تأیید</th><th>معامله</th><th>سود واقعی</th><th>میانگین فرضی</th><th>برد فرضی</th></tr></thead><tbody id="setups"></tbody></table></div>
      <div class="cols" id="sides" style="gap:10px"></div></section>

    <div class="cols reveal">
      <section><div class="sh"><span class="idx">05</span><h2>رویدادهای اخیر</h2></div>
        <p class="hint">۳۰ اتفاق آخر از دفتر اسکات: هشدارها، تأیید و ردها، باز و بسته شدن‌ها و تحلیل‌ها. جدیدترین بالاست.</p>
        <ul class="ev" id="events"></ul></section>
      <section><div class="sh"><span class="idx">06</span><h2>قواعد فعلی</h2></div>
        <p class="hint">حدهایی که اسکات الان با آن‌ها کار می‌کند. توضیح هر کدام در راهنما آمده است.</p>
        <div class="rules" id="rules"></div></section>
    </div>
  </div>

  <!-- ═════════ معاملات ═════════ -->
  <div class="pane" id="p-trades" role="tabpanel" aria-labelledby="t-trades" hidden>
    <section><div class="sh"><span class="idx">01</span><h2>کدام نفر بهتر بوده؟ <small id="ppnote"></small></h2></div>
      <p class="hint">همهٔ معاملات دفتر به تفکیک کسی که تأیید یا باز کرده است. برای مرتب‌کردن روی عنوان ستون بزنید؛ برای دیدن معاملات یک نفر، روی ردیفش. «میانگین R» منصفانه‌ترین معیار است (به تعداد معاملات بستگی ندارد). نفرات با کمتر از ۱۰ معامله کم‌نمونه‌اند و رتبه نمی‌گیرند؛ چند معاملهٔ خوش‌شانس هنوز مهارت را ثابت نمی‌کند.</p>
      <div class="podium" id="podium"></div>
      <div class="tablewrap"><table class="wide" id="pptab"><thead id="pphead"></thead><tbody id="ppbody"></tbody></table></div>
    </section>
    <section><div class="sh"><span class="idx">02</span><h2>معاملات تأییدشده <small id="trnote"></small></h2></div>
      <p class="hint">همهٔ معاملاتی که با تأیید شما باز شدند و بسته شده‌اند، جدیدترین بالا و به تفکیک روز (به وقت تهران). «چه کسی بست» نشان می‌دهد خودتان با دکمهٔ بستن بستید یا حد ضرر، هوش مصنوعی، قیمت خروج شما یا بروکر. نام تأییدکننده از نسخهٔ جدید به بعد ثبت می‌شود. پوزیشن‌های باز در تب داشبورد هستند.</p>
      <div class="tiles" id="trtiles"></div>
      <div class="filters">
        <select id="trday" aria-label="روز"></select>
        <select id="trres" aria-label="نتیجه"><option value="">سود و ضرر</option><option value="win">فقط سودده</option><option value="loss">فقط ضررده</option></select>
        <select id="trwho" aria-label="نفر"></select>
        <select id="trsort" aria-label="مرتب‌سازی"><option value="">جدیدترین (به تفکیک روز)</option><option value="best">بیشترین سود</option><option value="worst">بیشترین ضرر</option><option value="r">بهترین R</option><option value="person">بر اساس نام نفر</option></select>
        <select id="trside" aria-label="جهت"><option value="">خرید و فروش</option><option value="BUY">فقط خرید</option><option value="SELL">فقط فروش</option></select>
      </div>
      <div class="tablewrap"><table class="wide"><thead><tr><th>تیکت</th><th>باز شد ← بسته شد</th><th>جهت</th><th>ستاپ</th><th>لات</th><th>ورود ← خروج</th><th>سود / ضرر</th><th>چه کسی باز کرد</th><th>چه کسی بست</th><th>توضیح</th></tr></thead><tbody id="trbody"></tbody></table></div>
    </section>
  </div>

  <!-- ═════════ کارنامه ═════════ -->
  <div class="pane" id="p-audit" role="tabpanel" aria-labelledby="t-audit" hidden>
    <section><div class="sh"><span class="idx">01</span><h2>بعد از هر سیگنال، بازار به کدام طرف رفت؟ <small id="aunote"></small></h2></div>
      <p class="hint">برای هر هشدار، از بسته شدن کندل هشدار روی کندل‌های ۱ دقیقه‌ای بروکر سنجیده شده است. «اول ‎+1R» یعنی قیمت قبل از اینکه به اندازهٔ حد ضرر هشدار خلاف جهت برود، به همان اندازه در جهت سیگنال رفت (تا ۴ ساعت). «در جهت سیگنال» یعنی چند درصد سیگنال‌ها بعد از آن مدت در جهت درست بودند. این صفحه فقط آمار است و هیچ سیگنالی را حذف نمی‌کند. هر ساعت خودکار به‌روز می‌شود.</p>
      <div class="tiles" id="autiles"></div></section>
    <section><div class="sh"><span class="idx">02</span><h2>برچسب هشدارها <small>همان خط اول هر هشدار</small></h2></div>
      <p class="hint">«حد ضرر نزدیک» یعنی فاصلهٔ حد ضرر هشدار <span id="autight">۶</span> یا کمتر بوده. «همراه شکست» یعنی سیگنال range_break یا cloud_break هم داشته. بقیه «بدون شکست»‌اند. علامت هشدار با احتساب شانس گذاشته می‌شود: ✅ خوب فقط وقتی که با تعداد نمونه‌ها، حتی در بدترین حالت هم بالای ۵۰٪ باشد؛ ⚠️ ضعیف فقط وقتی که حتی در بهترین حالت هم زیر ۵۰٪ باشد؛ بقیه ⚪ (شانسی یا هنوز معلوم نیست).</p>
      <div class="tablewrap"><table><thead class="auh"></thead><tbody id="aulabel"></tbody></table></div></section>
    <section><div class="sh"><span class="idx">03</span><h2>ستاپ‌ها <small>سیگنالی که چند ستاپ دارد در هر کدام شمرده می‌شود · زیر ۵ نمونه کم‌رنگ است</small></h2></div>
      <div class="tablewrap"><table><thead id="auhead"></thead><tbody id="ausetup"></tbody></table></div></section>
    <section><div class="sh"><span class="idx">04</span><h2>ساعت سیگنال (تهران)</h2></div>
      <div class="tablewrap"><table><thead class="auh"></thead><tbody id="auhour"></tbody></table></div></section>
    <section><div class="sh"><span class="idx">05</span><h2>تصمیم شما</h2></div>
      <p class="hint">برای «رد کردید»، درصد پایین یعنی رد درست بوده.</p>
      <div class="tablewrap"><table><thead class="auh"></thead><tbody id="audec"></tbody></table></div></section>
    <section><div class="sh"><span class="idx">06</span><h2>خرید و فروش</h2></div>
      <div class="tablewrap"><table><thead class="auh"></thead><tbody id="auside"></tbody></table></div></section>
    <section><div class="sh"><span class="idx">07</span><h2>روز هفته (تهران)</h2></div>
      <div class="tablewrap"><table><thead class="auh"></thead><tbody id="auweek"></tbody></table></div></section>
    <section><div class="sh"><span class="idx">08</span><h2>شرایط لحظهٔ هشدار <small>فقط هشدارهای بعد از این به‌روزرسانی این اطلاعات را دارند</small></h2></div>
      <p class="hint">«دیرهنگام» یعنی هشدار مال کندل قبل بوده. «اسپرد» یعنی اسپرد لحظهٔ هشدار چند درصدِ فاصلهٔ حد ضرر بوده. «خبر» یعنی خبر مهم نزدیک بوده. فقط آمار است.</p>
      <div class="tablewrap"><table><thead class="auh"></thead><tbody id="aucond"></tbody></table></div></section>
  </div>

  <!-- ═════════ راهنما ═════════ -->
  <!--GUIDE-->

  <footer><span>هر ۱۰ ثانیه به‌روز می‌شود. «فرضی» یعنی اگر هر ستاپ با حد ضرر خودش و هدف 2R گرفته می‌شد (هر ساعت دوباره حساب می‌شود).</span><span id="upd" class="mono"></span></footer>
</div>
<script>
document.documentElement.classList.add('js');
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=v=>v==null||isNaN(v)?'—':(v>0?'+':v<0?'−':'')+'$'+Math.abs(v).toFixed(2);
const usd=v=>v==null||isNaN(v)?'—':'$'+v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const cls=v=>v>0?'pos':v<0?'neg':'';
const fa=n=>n==null?'—':String(n);
const SIDE_FA={BUY:'<span class="chip buy">خرید</span>',SELL:'<span class="chip sell">فروش</span>'};
const sideTag=s=>`<span class="side ${s==='BUY'?'buy':'sell'}">${s==='BUY'?'خرید':'فروش'}</span>`;
function pill(ok,text){return `<span class="pill"><span class="dot ${ok===true?'ok':ok===false?'bad':'warn'}"></span>${esc(text)}</span>`}

/* signature: rolling digits on the live price */
let lastPrice=null;
function setPrice(v){
  const el=$('hp'), s=v.toFixed(2);
  $('tickp').textContent=s;
  if(lastPrice===null){el.textContent=s;lastPrice=v;return;}
  if(s===lastPrice.toFixed(2))return;
  const old=lastPrice.toFixed(2).padStart(s.length,' '), dir=v>lastPrice?'up':'dn';
  el.innerHTML=[...s].map((c,i)=>c!==old[i]?`<span class="${dir}">${c}</span>`:`<span>${c}</span>`).join('');
  const t=$('tickp');t.classList.remove('flash-up','flash-dn');void t.offsetWidth;t.classList.add(dir==='up'?'flash-up':'flash-dn');
  setTimeout(()=>t.classList.remove('flash-up','flash-dn'),900);
  lastPrice=v;
}

/* position track: SL, emergency SL, entry and live price on one line */
function track(p){
  const pts=[p.sl,p.entry,p.price,p.broker_sl].filter(x=>x!=null);
  let lo=Math.min(...pts),hi=Math.max(...pts);const pad=(hi-lo)*.12||1;lo-=pad;hi+=pad;
  const x=v=>((v-lo)/(hi-lo)*100).toFixed(2)+'%';
  const segA=Math.min(p.entry,p.price??p.entry),segB=Math.max(p.entry,p.price??p.entry);
  const good=p.profit>=0;
  const em=p.broker_sl&&Math.abs(p.broker_sl-p.sl)>.01;
  return `<div class="track" aria-hidden="true"><div class="rail"></div>
    <div class="seg" style="left:${x(segA)};width:calc(${x(segB)} - ${x(segA)});background:${good?'var(--good)':'var(--bad)'}"></div>
    <div class="m sl" style="left:${x(p.sl)}"></div><span class="lb" style="left:${x(p.sl)}">SL</span>
    ${em?`<div class="m em" style="left:${x(p.broker_sl)}"></div><span class="lb" style="left:${x(p.broker_sl)}">SL·MT5</span>`:''}
    <div class="m en" style="left:${x(p.entry)}"></div><span class="lb" style="left:${x(p.entry)}">ENTRY</span>
    ${p.price!=null?`<div class="now" style="left:${x(p.price)}"></div>`:''}</div>`;
}

let firstRender=true;
function render(d){
  $('err').hidden=!d.error; if(d.error)$('err').textContent='خطا: '+d.error;
  const s=d.status||{}, a=d.account||{}, st=d.stats||{};
  $('pills').innerHTML=[pill(s.alive,s.alive?'روشن':'بی‌پاسخ'),pill(s.mt5,s.mt5?'MT5 وصل':'MT5 قطع'),
    pill(s.market?true:null,s.market?'بازار باز':'بازار بسته'),pill(s.paused?false:true,s.paused?'هشدارها متوقف':'هشدارها فعال'),
    `<span class="pill">حالت: <b>${esc(s.mode_fa||s.mode||'—')}</b></span>`,
    s.now_tehran?`<span class="pill">به‌روز <b class="num">${esc(s.now_tehran)}</b> تهران</span>`:''].join('');
  $('tickdot').className='dot '+(s.alive&&s.mt5?'ok':'bad');
  if(d.price){setPrice(d.price.bid);
    $('hq').innerHTML=`<span>خرید <b class="num">${d.price.ask.toFixed(2)}</b></span><span>فروش <b class="num">${d.price.bid.toFixed(2)}</b></span><span>اسپرد <b class="num">${(d.price.ask-d.price.bid).toFixed(2)}</b></span>`;}
  $('asof').textContent='';
  const t=st.today||{},w=st.week||{},al=st.all||{};
  $('tiles').innerHTML=[
    ['موجودی',usd(a.balance),'حساب دمو',''],
    ['اکوییتی',usd(a.equity),a.profit!=null?('شناور '+money(a.profit)):'',''],
    ['امروز',money(t.pnl),`${fa(t.n)} معامله · ${fa(st.alerts_today)} هشدار`,cls(t.pnl)],
    ['۷ روز',money(w.pnl),`${fa(w.n)} معامله · برد ${w.win!=null?w.win+'٪':'—'}`,cls(w.pnl)],
    ['کل',money(al.pnl),`${fa(al.n)} معامله · برد ${al.win!=null?al.win+'٪':'—'}`,cls(al.pnl)],
    ['پوزیشن باز',fa((d.open||[]).length),d.open_risk!=null?('ریسک $'+d.open_risk.toFixed(2)+' از $'+(d.rules?.max_risk??'—')):'',''],
  ].map(([k,v,sub,c])=>`<div class="tile"><div class="k">${k}</div><div class="v num ${c}">${v}</div><div class="s">${sub}</div></div>`).join('');
  const op=d.open||[];
  $('open').innerHTML=op.length?op.map(p=>`<div class="card ${p.side==='BUY'?'buy':'sell'} ring ${p.profit>=0?'rp':'rn'}">
     <div class="top"><span class="ttl">${sideTag(p.side)}<span class="mono">#${p.ticket}</span><span class="mono muted">${esc(setupFa(p.setup))}</span></span>
     <span class="pnl num ${cls(p.profit)}">${money(p.profit)}</span></div>
     ${track(p)}
     <div class="kv"><span>ورود <b class="num">${p.entry.toFixed(2)}</b></span><span>الان <b class="num">${p.price?p.price.toFixed(2):'—'}</b></span>
     <span>حد ضرر سیگنال <b class="num">${p.sl.toFixed(2)}</b></span><span>روی بروکر <b class="num">${p.broker_sl?p.broker_sl.toFixed(2):'—'}</b></span>
     ${p.r!=null?`<span>R <b class="num">${p.r>0?'+':''}${p.r.toFixed(2)}</b></span>`:''}</div>
     ${p.hold||p.warned?`<div>${p.hold?'<span class="tag">⏸ نگه داشته</span>':''} ${p.warned?'<span class="tag">⚠️ ساختار شکسته</span>':''}</div>`:''}</div>`).join('')
    :'<div class="empty">پوزیشن بازی نیست.</div>';
  const pe=d.pending||[];
  $('pending').innerHTML=pe.length?pe.map(p=>`<div class="card ${p.side==='BUY'?'buy':'sell'}">
     <div class="top"><span class="ttl">${sideTag(p.side)}<span class="mono muted">${esc(setupFa(p.setup))}</span></span><span class="tag">${p.left_min} دقیقه مانده</span></div>
     <div class="kv"><span>قیمت <b class="num">${p.price.toFixed(2)}</b></span><span>حد ضرر <b class="num">${p.sl.toFixed(2)}</b></span><span>ریسک <b class="num">$${p.risk.toFixed(2)}</b></span></div>
     ${p.analysis?`<div class="muted" style="font-size:13px">🤖 ${esc(p.analysis)}</div>`:''}</div>`).join('')
    :'<div class="empty">هشدار بی‌جوابی نیست. جواب دادن فقط از تلگرام است.</div>';
  const days=st.days||[]; const mx=Math.max(1,...days.map(x=>Math.abs(x.pnl)));
  const sum=days.reduce((a,x)=>a+x.pnl,0);
  $('sum14').textContent=money(sum); $('sum14').className='big num '+cls(sum);
  $('smax').textContent='±$'+mx.toFixed(0);
  $('bars').innerHTML=days.map((x,i)=>{const h=(Math.abs(x.pnl)/mx*50).toFixed(1);
     return `<div class="b" style="--i:${i}"><i class="${!x.pnl?'z':x.pnl>0?'p':'n'}" style="height:${x.pnl?h:0}%"></i><span class="tip"><span class="num">${x.d.slice(5).replace('-','/')}</span> · <b class="num ${cls(x.pnl)}">${money(x.pnl)}</b> · ${x.n} معامله</span></div>`}).join('');
  if(firstRender)$('bars').classList.add('bars-in'); else $('bars').classList.remove('bars-in');
  $('blab').innerHTML=days.map(x=>`<span>${x.d.slice(5).replace('-','/')}</span>`).join('');
  $('vnote').textContent=st.has_virtual?'':'(نتیجهٔ فرضی هنوز حساب نشده)';
  $('setups').innerHTML=(st.setups||[]).map(r=>`<tr><td class="mono">${esc(setupFa(r.setup))}</td><td class="n">${r.alerts}</td><td class="n">${r.taken}</td><td class="n">${r.trades}</td>
     <td class="n ${cls(r.pnl)}">${r.trades?money(r.pnl):'—'}</td><td class="n ${cls(r.avg_R)}">${r.avg_R!=null?(r.avg_R>0?'+':'')+r.avg_R.toFixed(2)+'R':'—'}</td>
     <td class="n">${r.win_R!=null?r.win_R+'٪':'—'}</td></tr>`).join('')||'<tr><td colspan="7" class="muted">هنوز داده‌ای نیست</td></tr>';
  const sd=st.sides||{};
  $('sides').innerHTML=['SELL','BUY'].map(k=>{const v=sd[k]||{};return `<div class="card ${k==='BUY'?'buy':'sell'}" style="margin:0"><div class="top"><span class="ttl">${sideTag(k)}<span class="muted" style="font-size:13px">همهٔ ستاپ‌ها، فرضی · ${fa(v.n)} نمونه</span></span>
     <span class="pnl num ${cls(v.sum_R)}">${v.sum_R!=null?(v.sum_R>0?'+':'')+v.sum_R.toFixed(1)+'R':'—'}</span></div></div>`}).join('');
  $('events').innerHTML=(st.recent||[]).map(e=>`<li><span class="t">${esc(e.t)}</span><span>${esc(e.text)}${e.price!=null?` <span class="muted num">@ ${e.price.toFixed(2)}</span>`:''}</span>
     <span class="num ${cls(e.profit)}">${e.profit!=null?money(e.profit):''}</span></li>`).join('')||'<li><span></span><span class="muted">رویدادی نیست</span><span></span></li>';
  const r=d.rules||{};
  $('rules').innerHTML=[['حجم',r.lot+' لات'],['حد ضرر سیگنال','حداکثر $'+r.sl_cap],['حد ضرر اضطراری','حداکثر $'+r.emerg_cap],
    ['هشدار نزدیک حد ضرر',r.approach+'٪ ریسک'],['پوزیشن هم‌زمان','حداکثر '+r.max_open],['سقف ریسک کل','$'+r.max_risk],
    ['مهلت جواب',r.expiry_min+' دقیقه'],['گزارش زنده','هر '+r.status_min+' دقیقه']]
    .map(([k,v])=>`<div class="rule">${k}<b>${esc(v)}</b></div>`).join('');
  $('upd').textContent=s.version?('v '+s.version):'';
  LAST=st; renderPicks(st.picks); renderAudit(d.audit); renderTrades(st.trades); renderEquity(st.trades);
  if(firstRender)document.querySelectorAll('#tiles .v').forEach(countUp);
  document.querySelectorAll('[data-r]').forEach(el=>{const v=r[el.dataset.r];if(v!=null)el.textContent=(el.dataset.pre||'')+v+(el.dataset.suf||'');});
  firstRender=false;
}
let LAST={};
const rfmt=v=>v==null?'—':(v>0?'+':'')+v.toFixed(2)+'R';
function renderPicks(p){
  if(!p){$('picks').innerHTML='<div class="empty" style="grid-column:1/-1">نتیجهٔ فرضی هنوز حساب نشده؛ چند دقیقه بعد از روشن شدن اسکات می‌آید.</div>';$('picksay').textContent='';return;}
  const card=(k,t)=>{const v=p[k]||{};return `<div class="tile"><div class="k">${t}</div><div class="v num ${cls(v.avg_r)}">${rfmt(v.avg_r)}</div><div class="s">${fa(v.n)} نمونه · برد ${v.win!=null?v.win+'٪':'—'}</div><div class="meter"><i style="width:${v.win||0}%"></i></div></div>`};
  $('picks').innerHTML=card('taken','تأییدشده‌های شما')+card('passed','ردشده یا منقضی')+card('silent','بی‌صدا (پوزیشن باز یا توقف)')+card('all','همهٔ سیگنال‌ها');
  const a=p.taken||{}, b=p.all||{};
  $('picksay').textContent=(a.avg_r==null||b.avg_r==null||a.n<5)?'برای مقایسه هنوز تأییدهای کافی نیست.'
    :(a.avg_r>b.avg_r?`انتخاب‌های شما تا الان به‌طور میانگین ${(a.avg_r-b.avg_r).toFixed(2)}R در هر معامله بهتر از کل سیگنال‌ها بوده است.`
    :`انتخاب‌های شما فعلاً ${(b.avg_r-a.avg_r).toFixed(2)}R در هر معامله پایین‌تر از کل سیگنال‌هاست؛ با معاملات بیشتر دقیق‌تر می‌شود.`);
}

const setupFa=s=>String(s)==='manual'?'✋ دستی':String(s);
function dur(sec){if(sec==null)return '—';const m=Math.round(sec/60);if(m<60)return m+' د';const h=Math.floor(m/60),r=m%60;return h+' س'+(r?' '+r+' د':'');}
const PPCOLS=[['rank','رتبه',0],['name','نفر',1],['n','معاملات',1],['win','برد',1],['pnl','جمع سود و ضرر',1],['avg','میانگین هر معامله',1],['avgR','میانگین R',1],['best','بهترین',1],['worst','بدترین',1]];
const PP_MIN=10;
function peopleStats(list){
  const m={};
  for(const x of list){const k=x.opener||'نام ثبت نشده';
    const p=m[k]||(m[k]={name:k,n:0,wins:0,pnl:0,rs:[],best:-1e9,worst:1e9});
    p.n++;if(x.profit>0)p.wins++;p.pnl+=x.profit;if(x.r!=null)p.rs.push(x.r);
    p.best=Math.max(p.best,x.profit);p.worst=Math.min(p.worst,x.profit);}
  return Object.values(m).map(p=>({name:p.name,n:p.n,win:p.n?p.wins/p.n:null,pnl:p.pnl,avg:p.n?p.pnl/p.n:null,
    avgR:p.rs.length?p.rs.reduce((a,v)=>a+v,0)/p.rs.length:null,best:p.best,worst:p.worst,few:p.n<PP_MIN}));
}
function renderPeople(list){
  const ps=peopleStats(list||[]), key=PP.key, dir=PP.dir;
  const val=p=>key==='name'?p.name:(p[key]==null?-1e9:p[key]);
  ps.sort((a,b)=>(a.few-b.few)||(key==='name'?String(a.name).localeCompare(String(b.name),'fa')*dir:(val(b)-val(a))*dir));
  let rank=0;
  const qual=peopleStats(list||[]).filter(q=>!q.few).sort((x,y)=>y.pnl-x.pnl).slice(0,3), ringSvg=q=>{const C=2*Math.PI*21;
    return `<svg class="wr" viewBox="0 0 52 52" aria-hidden="true"><circle cx="26" cy="26" r="21" class="trk"/><circle cx="26" cy="26" r="21" class="arc" style="stroke-dasharray:${(C*q.win).toFixed(1)} ${C.toFixed(1)}"/><text x="26" y="30" text-anchor="middle">${Math.round(100*q.win)}٪</text></svg>`};
  $('podium').innerHTML=qual.map((q,i)=>`<div class="pod r${i+1}" data-who="${esc(q.name)}" role="button" tabindex="0" title="فقط معاملات ${esc(q.name)} را نشان بده">
    ${ringSvg(q)}<div class="bd"><div class="rk">رتبهٔ ${['۱','۲','۳'][i]}</div><div class="nm">${esc(q.name)}</div>
    <div class="pv num ${cls(q.pnl)}">${money(q.pnl)}</div><div class="ms">${q.n} معامله · میانگین ${q.avgR!=null?(q.avgR>0?'+':'')+q.avgR.toFixed(2)+'R':'—'}</div></div></div>`).join('');
  $('pphead').innerHTML='<tr>'+PPCOLS.map(([k,t,s])=>s?`<th class="srt${k===key?' on':''}" data-k="${k}">${t}${k===key?(dir>0?' ▼':' ▲'):''}</th>`:`<th>${t}</th>`).join('')+'</tr>';
  $('ppbody').innerHTML=ps.map(p=>{if(!p.few)rank++;
    const medal=p.few?'':(['🥇','🥈','🥉'][rank-1]||rank);
    return `<tr class="pick${p.few?' muted':''}" data-who="${esc(p.name)}"><td class="n rank">${medal}</td><td><b>${esc(p.name)}</b>${p.few?'<span class="sub">کم‌نمونه</span>':''}</td>
      <td class="n">${p.n}</td><td class="n">${Math.round(100*p.win)}٪</td><td class="n ${cls(p.pnl)}"><b>${money(p.pnl)}</b></td>
      <td class="n ${cls(p.avg)}">${money(p.avg)}</td><td class="n ${cls(p.avgR)}">${p.avgR!=null?(p.avgR>0?'+':'')+p.avgR.toFixed(2)+'R':'—'}</td>
      <td class="n ${cls(p.best)}">${money(p.best)}</td><td class="n ${cls(p.worst)}">${money(p.worst)}</td></tr>`}).join('')
    ||'<tr><td colspan="9" class="muted">هنوز معامله‌ای نیست</td></tr>';
  $('ppnote').textContent=ps.length?`${ps.length} نفر`:'';
}

/* ───────── منحنی سود ───────── */
let EQ={d:30,drawn:null};
function eqSeries(list,days){
  const rows=[...(list||[])].filter(x=>x.t_close).sort((a,b)=>a.t_close<b.t_close?-1:1);
  const cut=days?Date.now()-days*864e5:0;
  let c=0;
  return rows.filter(x=>!cut||Date.parse(x.t_close)>=cut).map(x=>({c:(c+=x.profit),p:x.profit,x}));
}
function renderEquity(list){
  const plot=$('eqplot'), svg=$('eqsvg'); if(!plot||!svg)return;
  const pts=eqSeries(list,EQ.d);
  if(pts.length<2){svg.innerHTML='';$('eqstats').innerHTML='';$('eqnote').textContent='';
    plot.querySelector&&0; $('eqtip').hidden=true;
    svg.setAttribute('viewBox','0 0 600 120');
    svg.innerHTML='<text x="300" y="60" text-anchor="middle" class="ax" style="font-size:13px;font-family:inherit">برای این بازه هنوز معامله‌ای بسته نشده</text>';
    EQ.key='';return;}
  const w=Math.round(Math.max(280,plot.clientWidth||600)), h=(typeof innerWidth==='number'&&innerWidth<560)?210:290, pad=22;
  const key=EQ.d+'|'+pts.length+'|'+pts[pts.length-1].c.toFixed(2)+'|'+w;
  if(EQ.key===key)return; EQ.key=key;
  const animate=EQ.drawn!==EQ.d; EQ.drawn=EQ.d;
  const S=[{c:0,p:0,x:null},...pts], n=S.length;
  const vals=S.map(v=>v.c), mx=Math.max(...vals,0), mn=Math.min(...vals,0), span=(mx-mn)||1;
  const X=i=>i/(n-1)*w, Y=v=>pad+(mx-v)/span*(h-2*pad), zy=Y(0);
  const d='M'+S.map((v,i)=>X(i).toFixed(1)+' '+Y(v.c).toFixed(1)).join(' L');
  const area=d+` L${w} ${zy.toFixed(1)} L0 ${zy.toFixed(1)} Z`;
  const last=S[n-1], ex=X(n-1), ey=Y(last.c);
  svg.setAttribute('viewBox',`0 0 ${w} ${h}`);
  svg.innerHTML=`<defs>
    <linearGradient id="eqg1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5BCB9C" stop-opacity=".34"/><stop offset="1" stop-color="#5BCB9C" stop-opacity="0"/></linearGradient>
    <linearGradient id="eqg2" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#F07D6E" stop-opacity=".34"/><stop offset="1" stop-color="#F07D6E" stop-opacity="0"/></linearGradient>
    <clipPath id="eqcu"><rect x="0" y="0" width="${w}" height="${zy.toFixed(1)}"/></clipPath><clipPath id="eqcd"><rect x="0" y="${zy.toFixed(1)}" width="${w}" height="${(h-zy).toFixed(1)}"/></clipPath></defs>
    ${[0.25,0.5,0.75].map(f=>`<line class="eqgrid" x1="0" x2="${w}" y1="${(pad+f*(h-2*pad)).toFixed(1)}" y2="${(pad+f*(h-2*pad)).toFixed(1)}"/>`).join('')}
    <path class="eqarea" d="${area}" fill="url(#eqg1)" clip-path="url(#eqcu)"/><path class="eqarea" d="${area}" fill="url(#eqg2)" clip-path="url(#eqcd)"/>
    <line class="eqzero" x1="0" x2="${w}" y1="${zy.toFixed(1)}" y2="${zy.toFixed(1)}"/>
    <text class="ax" x="4" y="${Math.max(11,zy-5).toFixed(1)}">$0</text>
    <text class="ax" x="4" y="${(Y(mx)+12).toFixed(1)}">${mx>0&&(zy-Y(mx))>26?'$'+mx.toFixed(0):''}</text>
    <text class="ax" x="4" y="${(Y(mn)-5).toFixed(1)}">${mn<0&&(Y(mn)-zy)>26?'−$'+Math.abs(mn).toFixed(0):''}</text>
    <path class="eqline" id="eqline" d="${d}"/>
    <line class="eqx" id="eqx" y1="0" y2="${h}" x1="0" x2="0" style="display:none"/><circle class="eqhov" id="eqhov" r="5" style="display:none"/>
    <circle class="eqping" cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="5"/><circle class="eqend" cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="4.5"/>`;
  const line=$('eqline');
  if(animate&&line&&line.getTotalLength&&typeof requestAnimationFrame==='function'){
    const L=line.getTotalLength(); line.style.strokeDasharray=L; line.style.strokeDashoffset=L;
    requestAnimationFrame(()=>requestAnimationFrame(()=>{line.style.transition='stroke-dashoffset 1.7s cubic-bezier(.22,1,.36,1)';line.style.strokeDashoffset=0;}));
  }
  // آمار
  let peak=0,dd=0,run=0,best=null; S.forEach(v=>{peak=Math.max(peak,v.c);dd=Math.max(dd,peak-v.c);if(v.x&&(best===null||v.p>best))best=v.p;});
  let streak=0,sg=0; for(let i=pts.length-1;i>=0;i--){const g=pts[i].p>0?1:pts[i].p<0?-1:0; if(!g)break; if(!sg)sg=g; if(g!==sg)break; streak++;}
  const tot=last.c, wins=pts.filter(v=>v.p>0).length;
  $('eqstats').innerHTML=[
    ['جمع این بازه',`<b class="num ${cls(tot)}">${money(tot)}</b>`],
    ['برد',`<b class="num">${Math.round(100*wins/pts.length)}٪</b>`],
    ['اوج',`<b class="num pos">${money(peak)}</b>`],
    ['بیشترین افت',`<b class="num ${dd>0?'neg':''}">${dd>0?'−$'+dd.toFixed(2):'$0.00'}</b>`],
    ['بهترین معامله',`<b class="num pos">${money(best)}</b>`],
    ['سری فعلی',`<b class="num ${sg>0?'pos':sg<0?'neg':''}">${streak?streak+' '+(sg>0?'برد':'باخت')+' پیاپی':'—'}</b>`],
  ].map(([k,v])=>`<div><small>${k}</small>${v}</div>`).join('');
  $('eqnote').textContent=pts.length+' معامله';
  EQ.S=S;EQ.w=w;EQ.h=h;EQ.X=X;EQ.Y=Y;
}
function eqHover(e){
  const S=EQ.S; if(!S||!$('eqplot').getBoundingClientRect)return;
  const r=$('eqplot').getBoundingClientRect(); if(!r.width)return;
  const f=Math.min(1,Math.max(0,((e.clientX-r.left)/r.width)));
  const i=Math.round(f*(S.length-1)), v=S[i], px=EQ.X(i), py=EQ.Y(v.c);
  const xl=$('eqx'), hv=$('eqhov'), tip=$('eqtip'); if(!xl||!hv)return;
  xl.setAttribute('x1',px);xl.setAttribute('x2',px);xl.style.display='';
  hv.setAttribute('cx',px);hv.setAttribute('cy',py);hv.style.display='';
  const x=v.x; const sub=x?`<small>#${esc(x.ticket)} · ${x.side==='BUY'?'خرید':'فروش'} · <span class="num ${cls(v.p)}">${money(v.p)}</span></small><small>${esc(x.day_fa)} · ${esc(x.close_hm)} · ${esc(x.opener||'')}</small>`:'<small>شروع بازه</small>';
  tip.innerHTML=`<b class="num ${cls(v.c)}">${money(v.c)}</b>${sub}`; tip.hidden=false;
  const pct=Math.min(86,Math.max(14,px/EQ.w*100)); tip.style.left=pct+'%';
}
function eqLeave(){const xl=$('eqx'),hv=$('eqhov');if(xl)xl.style.display='none';if(hv)hv.style.display='none';$('eqtip').hidden=true;}
$('eqplot').onpointermove=eqHover; $('eqplot').onpointerleave=eqLeave;
$('eqchips').onclick=e=>{const b=e.target&&e.target.closest?e.target.closest('[data-d]'):null;if(!b)return;
  EQ.d=+b.dataset.d;[...$('eqchips').children].forEach(c=>c.classList.toggle('on',c===b));renderEquity(LAST.trades)};
let eqT=null; addEventListener('resize',()=>{clearTimeout(eqT);eqT=setTimeout(()=>{EQ.key='';renderEquity(LAST.trades)},180)});

/* اعداد کارت‌های بالا با شمارنده می‌آیند (فقط بار اول) */
function countUp(el){
  if(typeof requestAnimationFrame!=='function'||(typeof matchMedia==='function'&&matchMedia('(prefers-reduced-motion:reduce)').matches))return;
  const t=el.textContent, m=t.match(/\d[\d,]*(\.\d+)?/); if(!m)return;
  const end=parseFloat(m[0].replace(/,/g,'')); if(!isFinite(end)||end===0)return;
  const dec=m[1]?m[1].length-1:0, grp=m[0].includes(','), t0=performance.now(), D=1100;
  const fmt=v=>grp?v.toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec}):v.toFixed(dec);
  const step=now=>{const k=Math.min(1,(now-t0)/D), e=k===1?1:1-Math.pow(2,-10*k); el.textContent=t.replace(m[0],fmt(end*e)); if(k<1)requestAnimationFrame(step);};
  requestAnimationFrame(step);
}
/* نور دنبال‌کنندهٔ ماوس روی کارت‌ها */
addEventListener('pointermove',e=>{const c=e.target&&e.target.closest?e.target.closest('.card,.glass,.pod'):null;
  if(c){const r=c.getBoundingClientRect();c.style.setProperty('--mx',(e.clientX-r.left)+'px');c.style.setProperty('--my',(e.clientY-r.top)+'px');}},{passive:true});

let PP={key:'avgR',dir:1};
$('pphead').onclick=e=>{const th=e.target&&e.target.closest?e.target.closest('th[data-k]'):null;if(!th)return;
  const k=th.dataset.k;if(PP.key===k)PP.dir=-PP.dir;else{PP.key=k;PP.dir=1;} renderPeople(LAST.trades)};
$('podium').onclick=e=>{const c=e.target&&e.target.closest?e.target.closest('[data-who]'):null;if(!c)return;
  TR.who=c.dataset.who;TR.sort='';renderTrades(LAST.trades);$('trbody').scrollIntoView&&$('trbody').scrollIntoView({behavior:'smooth',block:'start'})};
$('ppbody').onclick=e=>{const tr=e.target&&e.target.closest?e.target.closest('tr[data-who]'):null;if(!tr)return;
  TR.who=tr.dataset.who;TR.sort='';renderTrades(LAST.trades);$('trbody').scrollIntoView&&$('trbody').scrollIntoView({behavior:'smooth',block:'start'})};
function renderTrades(list){
  list=list||[];
  renderPeople(list);
  const days=[...new Set(list.map(x=>x.day))];
  if(TR.day&&!days.includes(TR.day))TR.day='';
  const names=[...new Set(list.map(x=>x.opener||'نام ثبت نشده'))];
  if(TR.who&&!names.includes(TR.who))TR.who='';
  $('trday').innerHTML='<option value="">همهٔ روزها</option>'+days.map(d=>{const x=list.find(y=>y.day===d);
    return `<option value="${esc(d)}"${TR.day===d?' selected':''}>${esc(x.day_fa)} · ${esc(d)}</option>`}).join('');
  $('trwho').innerHTML='<option value="">همهٔ نفرات</option>'+names.map(nm=>`<option value="${esc(nm)}"${TR.who===nm?' selected':''}>${esc(nm)} (${list.filter(y=>(y.opener||'نام ثبت نشده')===nm).length})</option>`).join('');
  $('trres').value=TR.res; $('trside').value=TR.side; $('trsort').value=TR.sort;
  let rows=list.filter(x=>(!TR.day||x.day===TR.day)&&(!TR.side||x.side===TR.side)&&(!TR.who||(x.opener||'نام ثبت نشده')===TR.who)
    &&(!TR.res||(TR.res==='win'?x.profit>0:x.profit<0)));
  const flat=!!TR.sort;
  if(TR.sort==='best')rows=[...rows].sort((a,b)=>b.profit-a.profit);
  else if(TR.sort==='worst')rows=[...rows].sort((a,b)=>a.profit-b.profit);
  else if(TR.sort==='r')rows=[...rows].sort((a,b)=>(b.r==null?-1e9:b.r)-(a.r==null?-1e9:a.r));
  else if(TR.sort==='person')rows=[...rows].sort((a,b)=>String(a.opener).localeCompare(String(b.opener),'fa')||(a.t_close<b.t_close?1:-1));
  const n=rows.length, pnl=rows.reduce((a,x)=>a+x.profit,0), wins=rows.filter(x=>x.profit>0).length;
  const rs=rows.filter(x=>x.r!=null), sr=rs.reduce((a,x)=>a+x.r,0);
  $('trtiles').innerHTML=[['معاملات',fa(n),list.length!==n?('از '+list.length+' کل'):'همهٔ دفتر','' ],
    ['جمع سود و ضرر',money(pnl),'',cls(pnl)],['برد',n?Math.round(100*wins/n)+'٪':'—',`${wins} سودده · ${n-wins} غیر سودده`,''],
    ['جمع R',rs.length?(sr>0?'+':'')+sr.toFixed(1)+'R':'—','',cls(sr)]]
    .map(([k,v,sub,c])=>`<div class="tile"><div class="k">${k}</div><div class="v num ${c}">${v}</div><div class="s">${sub}</div></div>`).join('');
  $('trnote').textContent=list.length?`${list.length} معامله در دفتر`:'';
  let html='',cur=null;
  for(const x of rows){
    if(!flat&&x.day!==cur){cur=x.day;const dr=rows.filter(y=>y.day===cur);const dp=dr.reduce((a,y)=>a+y.profit,0);
      html+=`<tr class="dayrow"><td colspan="10">${esc(x.day_fa)} · <span class="num">${esc(cur)}</span> · ${dr.length} معامله · <span class="num ${cls(dp)}">${money(dp)}</span></td></tr>`;}
    const od=flat?x.day.slice(5).replace('-','/')+' ':(x.open_day&&x.open_day!==x.day?x.open_day.slice(5).replace('-','/')+' ':'');
    const tags=[x.held?'<span class="tag">⏸ نگه داشته</span>':'',x.contra?'<span class="tag">↔️ خلاف سیگنال</span>':'',x.locked?'<span class="tag">🔒 بی‌ضرر شد</span>':''].join(' ');
    html+=`<tr><td class="mono nw muted">#${esc(x.ticket)}</td>
      <td class="n nw">${esc(od+x.open_hm)} → ${esc(x.close_hm)}<span class="sub">${dur(x.secs)}</span></td>
      <td class="nw">${SIDE_FA[x.side]||esc(x.side)}</td><td class="mono setup">${esc(setupFa(x.setup)).replace(/\+/g,' + ').replace(/_/g,'_<wbr>')}</td>
      <td class="n">${x.lot!=null?Number(x.lot).toFixed(2):'—'}</td>
      <td class="n nw">${x.entry!=null?x.entry.toFixed(2):'—'} → ${x.exit!=null?x.exit.toFixed(2):'—'}</td>
      <td class="n nw ${cls(x.profit)}"><b>${money(x.profit)}</b><span class="sub">${x.r!=null?(x.r>0?'+':'')+x.r.toFixed(2)+'R':''}</span></td>
      <td class="who"><b>${esc(x.opener||'')}</b><small>${esc(x.opened_by)}</small></td><td class="who">${esc(x.closed_by)}<small>${esc(x.closed_how)}</small></td><td>${tags}</td></tr>`;
  }
  $('trbody').innerHTML=html||'<tr><td colspan="10" class="muted">'+(list.length?'با این فیلترها معامله‌ای نیست':'هنوز معامله بسته‌شده‌ای نیست')+'</td></tr>';
}
let TR={day:'',res:'',side:'',who:'',sort:''};
$('trday').onchange=e=>{TR.day=e.target.value;renderTrades(LAST.trades)};
$('trres').onchange=e=>{TR.res=e.target.value;renderTrades(LAST.trades)};
$('trside').onchange=e=>{TR.side=e.target.value;renderTrades(LAST.trades)};
$('trwho').onchange=e=>{TR.who=e.target.value;renderTrades(LAST.trades)};
$('trsort').onchange=e=>{TR.sort=e.target.value;renderTrades(LAST.trades)};
const AUH='<tr><th>گروه</th><th>نمونه</th><th>اول ‎+1R</th><th>‎+1R / ‎-1R</th><th>۱۵ دقیقه در جهت</th><th>۶۰ دقیقه در جهت</th><th>میانگین R در ۶۰ دقیقه</th><th>حرکت به نفع / به ضرر (R)</th></tr>';
const pct=v=>v==null?'—':Math.round(v*100)+'٪';
function auRow(x,label,inverse){
  const f=x.first, few=(x.n||0)<5;
  const good=f==null||few?'':(inverse?(f<0.5?'pos':'neg'):(f>=0.6?'pos':f<0.5?'neg':''));
  return `<tr class="${few?'muted':''}"><td class="${label===x.key?'mono':''}">${esc(label)}</td><td class="n">${x.n||0}</td>
    <td class="n ${good}"><b>${pct(f)}</b></td><td class="n">${x.n?`${x.plus??0} / ${x.minus??0}`:'—'}</td>
    <td class="n">${pct(x.up15)}</td><td class="n">${pct(x.up60)}</td><td class="n ${cls(x.r60)}">${rfmt(x.r60)}</td>
    <td class="n">${x.mfe!=null?x.mfe.toFixed(2)+' / '+x.mae.toFixed(2):'—'}</td></tr>`;
}
function renderAudit(a){
  document.querySelectorAll('.auh').forEach(h=>h.innerHTML=AUH); $('auhead').innerHTML=AUH;
  if(!a||!a.all){$('autiles').innerHTML='<div class="empty" style="grid-column:1/-1">کارنامه هنوز حساب نشده؛ چند دقیقه بعد از روشن شدن اسکات می‌آید.</div>';
    ['aulabel','ausetup','auhour','audec','auside','auweek','aucond'].forEach(id=>$(id).innerHTML='');$('aunote').textContent='';return;}
  const x=a.all;
  $('aunote').textContent=`${a.days} روز اخیر · به‌روز ${a.updated||''}`;
  $('autiles').innerHTML=[
    ['سیگنال‌ها',fa(x.n),`${fa(x.plus)} اول ‎+1R · ${fa(x.minus)} اول ‎-1R`,''],
    ['اول ‎+1R',pct(x.first),'از سیگنال‌هایی که به یکی رسیدند',x.first>=0.5?'pos':'neg'],
    ['۱۵ دقیقه بعد',pct(x.up15),'در جهت سیگنال',''],
    ['۶۰ دقیقه بعد',pct(x.up60),'در جهت سیگنال',''],
    ['ساعت اول',x.mfe!=null?x.mfe.toFixed(2)+'R':'—',`بیشترین به نفع · به ضرر ${x.mae!=null?x.mae.toFixed(2)+'R':'—'}`,''],
    ['لغزش ورود',a.slip&&a.slip.avg!=null?(a.slip.avg>0?'+':'')+a.slip.avg.toFixed(1)+' پوینت':'—',
     a.slip&&a.slip.n?`میانگین ${a.slip.n} ورود · مثبت = بدتر از قیمت هشدار`:'هنوز داده‌ای نیست',''],
  ].map(([k,v,sub,c])=>`<div class="tile"><div class="k">${k}</div><div class="v num ${c}">${v}</div><div class="s">${sub}</div></div>`).join('');
  $('aulabel').innerHTML=(a.label||[]).map(r=>auRow(r,r.fa)).join('')||'<tr><td colspan="8" class="muted">داده‌ای نیست</td></tr>';
  if(a.tight!=null)$('autight').textContent=String(a.tight);
  $('ausetup').innerHTML=(a.setup||[]).map(r=>auRow(r,r.key)).join('')||'<tr><td colspan="8" class="muted">داده‌ای نیست</td></tr>';
  $('auhour').innerHTML=(a.hour||[]).map(r=>auRow(r,r.key.replace('-',' تا '))).join('');
  $('audec').innerHTML=(a.decision||[]).map(r=>auRow(r,r.fa||r.key,r.key==='rejected')).join('');
  $('auside').innerHTML=(a.side||[]).map(r=>auRow(r,r.key==='BUY'?'خرید':r.key==='SELL'?'فروش':r.key)).join('');
  $('auweek').innerHTML=(a.weekday||[]).map(r=>auRow(r,r.fa||r.key)).join('')||'<tr><td colspan="8" class="muted">داده‌ای نیست</td></tr>';
  $('aucond').innerHTML=[...(a.late||[]),...(a.spread||[]),...(a.news||[])].map(r=>auRow(r,r.fa||r.key)).join('')||'<tr><td colspan="8" class="muted">داده‌ای نیست</td></tr>';
}
const PANES=['dash','trades','audit','guide'];
function show(tab){
  if(!PANES.includes(tab))tab='dash';
  PANES.forEach(k=>{$('p-'+k).hidden=k!==tab;$('t-'+k).setAttribute('aria-selected',String(k===tab));});
  try{history.replaceState(null,'',location.pathname+location.search+(tab==='dash'?'':'#'+tab));}catch(e){}
  if(tab!=='dash')window.scrollTo(0,0);
  observeReveals();
}
PANES.forEach(k=>$('t-'+k).onclick=()=>show(k));
document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>show(b.dataset.go));

/* scroll reveal (once) — fail-safe: everything becomes visible after 2.5s even without IntersectionObserver */
let io=null;
function observeReveals(){
  const els=[...document.querySelectorAll('.reveal:not(.in)')];
  if(!('IntersectionObserver' in window)){els.forEach(e=>e.classList.add('in'));return;}
  io=io||new IntersectionObserver(es=>es.forEach(en=>{if(en.isIntersecting){en.target.classList.add('in');io.unobserve(en.target);}}),{threshold:.12,rootMargin:'0px 0px -5% 0px'});
  els.forEach(e=>io.observe(e));
}
setTimeout(()=>document.querySelectorAll('.reveal').forEach(e=>e.classList.add('in')),2500);
const topEl=$('top');addEventListener('scroll',()=>topEl.classList.toggle('scrolled',scrollY>8),{passive:true});

show((location.hash||'#dash').slice(1));
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
