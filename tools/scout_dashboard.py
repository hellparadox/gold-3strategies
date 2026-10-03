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
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


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
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1>اسکات</h1><div class="sub">دیدبان ستاپ‌های طلا · تأیید دستی در تلگرام · فقط خواندنی</div></div>
    <div class="pills" id="pills"><span class="pill"><span class="dot"></span>در حال دریافت…</span></div>
  </header>
  <div id="err" class="err" hidden></div>

  <section><h2>امروز و کل <small id="asof"></small></h2>
    <div class="tiles" id="tiles"></div></section>

  <div class="cols">
    <section><h2>پوزیشن‌های باز</h2><div id="open"></div></section>
    <section><h2>هشدارهای منتظر جواب</h2><div id="pending"></div></section>
  </div>

  <section><h2>سود و زیان روزانه <small>۱۴ روز اخیر، به وقت تهران</small></h2>
    <div class="chart"><div class="scale"><span id="smax"></span><span>دلار</span></div>
      <div class="bars" id="bars"></div><div class="blab" id="blab"></div></div></section>

  <section><h2>ستاپ‌ها <small id="vnote"></small></h2>
    <div class="tablewrap"><table><thead><tr><th>ستاپ</th><th>هشدار</th><th>تأیید</th><th>معامله</th><th>سود واقعی</th><th>میانگین فرضی</th><th>برد فرضی</th></tr></thead><tbody id="setups"></tbody></table></div>
    <div class="cols" id="sides"></div></section>

  <section><h2>رویدادهای اخیر</h2><ul class="ev" id="events"></ul></section>

  <section><h2>قواعد فعلی</h2><div class="rules" id="rules"></div></section>

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
}
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
