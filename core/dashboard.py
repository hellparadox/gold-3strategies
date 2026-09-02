"""Live web dashboard served from inside the bot process.

Zero new dependencies: a ``ThreadingHTTPServer`` on a daemon thread serving
one embedded dark RTL page plus a JSON API.  Telemetry is pulled through the
same hooks the Telegram panel uses, throttled by a snapshot cache so page
polls can never hammer MT5.

Settings (settings.yaml):
    dashboard:
      enabled: true
      host: "0.0.0.0"
      port: 8080
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from loguru import logger

__all__ = ["DashboardServer"]

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
    ) -> None:
        self._telemetry = telemetry_provider
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
        url = f"http://{self._host}:{self._port}/"
        if self._token:
            url += f"?token={self._token}"
        logger.success("📊 dashboard online → {}", url)
        return True

    def stop(self) -> None:
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
        if path_only.startswith("/api/stats"):
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
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">🥇 GoldBot <span style="color:var(--muted);font-size:12px" id="strategy"></span></div>
    <div class="status"><span class="dot" id="dot"></span><span id="status-text">…</span></div>
  </header>

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
</script>
</body>
</html>
"""
