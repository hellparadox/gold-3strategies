"""SCOUT BOT — هر ستاپ را به تلگرام می‌فرستد و منتظر تأیید یا رد شما می‌ماند.
 
- خودش وارد نمی‌شود. فقط بعد از «تأیید» شما سفارش می‌گذارد.
- حد ضرر همیشه روی بروکر گذاشته می‌شود؛ حد سود گذاشته نمی‌شود (خروج با شماست).
- بعد از ورود، در رسیدن به ۱ برابر ریسک و در شکست ساختار خبر می‌دهد، ولی نمی‌بندد.
- بستن با دکمهٔ «بستن» یا دستور /close <ticket>.
- همه‌چیز در data/scout_journal.csv ثبت می‌شود.
 
اجرا:
    py -3.11 tools/scout_bot.py --config config/settings_scout.yaml
    py -3.11 tools/scout_bot.py --config config/settings_scout.yaml --dry   # بدون ثبت سفارش
"""
from __future__ import annotations
 
import argparse
import csv
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
 
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
 
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from loguru import logger  # noqa: E402
 
from core import Settings  # noqa: E402
from core.mt5_client import MT5Client, MT5Config  # noqa: E402
 
try:                                   # ماژول تشخیص، کنار همین فایل یا در ریشه
    from tools.scout_setups import indicators, detect
except Exception:                      # pragma: no cover
    from scout_setups import indicators, detect
 
API = "https://api.telegram.org/bot{}/{}"
 
 
# --------------------------------------------------------------------- telegram
class TG:
    def __init__(self, token: str, chat_id: int) -> None:
        self.token, self.chat = token, int(chat_id)
        self.q: "queue.Queue[dict]" = queue.Queue()
        self._offset = 0
        self._stop = threading.Event()
 
    def _call(self, method: str, **kw: Any) -> Optional[dict]:
        try:
            r = requests.post(API.format(self.token, method), json=kw, timeout=20)
            d = r.json()
            return d.get("result") if d.get("ok") else None
        except Exception as exc:
            logger.warning("telegram {} failed: {}", method, exc)
            return None
 
    def send(self, text: str, buttons: Optional[list] = None) -> Optional[int]:
        kw: Dict[str, Any] = {"chat_id": self.chat, "text": text, "parse_mode": "HTML"}
        if buttons:
            kw["reply_markup"] = {"inline_keyboard": buttons}
        r = self._call("sendMessage", **kw)
        return r.get("message_id") if r else None
 
    def edit(self, message_id: int, text: str) -> None:
        self._call("editMessageText", chat_id=self.chat, message_id=message_id,
                   text=text, parse_mode="HTML")
 
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
    def __init__(self, s: Settings, dry: bool) -> None:
        self.s, self.dry = s, dry
        self.client = MT5Client(MT5Config.from_settings(s))
        self.lot = float(s.get("scout.lot", 0.01))
        self.expiry = int(s.get("scout.expiry_seconds", 300))
        self.journal = Path(s.get("scout.journal", "data/scout_journal.csv"))
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        token = os.environ.get(str(s.get("telegram.token_env", "TELEGRAM_TOKEN_SCOUT")), "")
        if not token:
            raise SystemExit("توکن تلگرام تنظیم نشده (TELEGRAM_TOKEN_SCOUT)")
        self.tg = TG(token, (s.get("telegram.admin_ids") or [0])[0])
        self.pending: Dict[str, dict] = {}
        self.open: Dict[int, dict] = {}
        self.last_bar: Optional[pd.Timestamp] = None
        self.n = 0
 
    # ---------------------------------------------------------------- journal
    def log(self, **row: Any) -> None:
        row.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        new = not self.journal.exists()
        with self.journal.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
 
    # ----------------------------------------------------------------- alerts
    def alert(self, r: pd.Series, spread_pts: float) -> None:
        self.n += 1
        aid = f"{self.n}"
        entry = float(r.ref_close)
        sl = entry - r.sl_dist if r.side == "BUY" else entry + r.sl_dist
        tp = entry + r.tp_dist if r.side == "BUY" else entry - r.tp_dist
        risk = round(r.sl_dist * 100 * self.lot, 2)
        txt = (f"<b>{'🟢 BUY' if r.side=='BUY' else '🔴 SELL'}</b> · <code>{r.setup}</code>\n"
               f"کندل: {r.bar:%H:%M}  |  قیمت: <b>{entry:.2f}</b>\n"
               f"حد ضرر: {sl:.2f}  ({r.sl_dist:.2f} = {r.sl_dist/r.atr:.1f}×ATR)\n"
               f"هدف پیشنهادی: {tp:.2f}  (۲ برابر ریسک)\n"
               f"ریسک: ${risk} · ATR {r.atr:.2f} · اسپرد {spread_pts:.0f}\n"
               f"<i>۵ دقیقه فرصت پاسخ</i>")
        mid = self.tg.send(txt, [[{"text": "✅ تأیید", "callback_data": f"a|{aid}|y"},
                                  {"text": "❌ رد", "callback_data": f"a|{aid}|n"}]])
        self.pending[aid] = {"row": r, "sl": sl, "tp": tp, "mid": mid, "t": time.time(), "txt": txt}
        self.log(event="alert", alert=aid, setup=r.setup, side=r.side, bar=str(r.bar),
                 price=entry, sl=round(sl, 2), tp=round(tp, 2), atr=round(r.atr, 3),
                 spread=spread_pts, risk_usd=risk)
 
    def expire(self) -> None:
        now = time.time()
        for aid in [k for k, v in self.pending.items() if now - v["t"] > self.expiry]:
            p = self.pending.pop(aid)
            if p["mid"]:
                self.tg.edit(p["mid"], p["txt"] + "\n\n⏳ <b>منقضی شد</b>")
            self.log(event="expired", alert=aid, setup=p["row"].setup, side=p["row"].side)
 
    # ---------------------------------------------------------------- decision
    def decide(self, aid: str, yes: bool, cb: str) -> None:
        p = self.pending.pop(aid, None)
        if p is None:
            self.tg.ack(cb, "این هشدار دیگر معتبر نیست")
            return
        r = p["row"]
        if not yes:
            self.tg.ack(cb, "رد شد")
            self.tg.edit(p["mid"], p["txt"] + "\n\n❌ <b>رد شد</b>")
            self.log(event="rejected", alert=aid, setup=r.setup, side=r.side)
            return
        self.tg.ack(cb, "در حال ثبت سفارش...")
        if self.dry:
            self.tg.edit(p["mid"], p["txt"] + "\n\n🧪 <b>حالت آزمایشی — سفارشی ثبت نشد</b>")
            self.log(event="approved_dry", alert=aid, setup=r.setup, side=r.side)
            return
        res = self.client.send_market_order(r.side, self.lot, sl=round(p["sl"], 2),
                                            tp=None, comment=f"scout:{r.setup}"[:31])
        if not res.ok:
            self.tg.edit(p["mid"], p["txt"] + f"\n\n⚠️ <b>ثبت نشد</b>: {res.comment}")
            self.log(event="order_failed", alert=aid, setup=r.setup, side=r.side, note=res.comment)
            return
        tk = res.position or res.order
        self.open[tk] = {"setup": r.setup, "side": r.side, "entry": res.price,
                         "sl": p["sl"], "risk": abs(res.price - p["sl"]), "r1": False,
                         "opened": time.time()}
        self.tg.edit(p["mid"], p["txt"] + f"\n\n✅ <b>باز شد</b> #{tk} @ {res.price:.2f}")
        self.tg.send(f"پوزیشن #{tk} باز است. هر وقت خواستید ببندید:",
                     [[{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]])
        self.log(event="opened", alert=aid, setup=r.setup, side=r.side, ticket=tk,
                 price=res.price, sl=round(p["sl"], 2))
 
    def close(self, tk: int, cb: Optional[str] = None) -> None:
        res = self.client.close_position(int(tk), comment="scout manual")
        if cb:
            self.tg.ack(cb, "بسته شد" if res.ok else f"خطا: {res.comment}")
        info = self.open.pop(int(tk), {})
        self.tg.send(f"{'✅' if res.ok else '⚠️'} بستن #{tk}: {res.comment or 'OK'} @ {res.price:.2f}")
        self.log(event="closed", ticket=tk, setup=info.get("setup"), side=info.get("side"),
                 price=res.price, ok=res.ok)
 
    # -------------------------------------------------------------- monitoring
    def monitor(self, f: pd.DataFrame) -> None:
        live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        for tk in list(self.open):
            if tk not in live:                       # با حد ضرر یا دستی بسته شده
                info = self.open.pop(tk)
                self.tg.send(f"ℹ️ پوزیشن #{tk} ({info['setup']}) دیگر باز نیست.")
                self.log(event="gone", ticket=tk, setup=info["setup"])
                continue
            p, info = live[tk], self.open[tk]
            profit = float(getattr(p, "profit", 0.0))
            price = float(getattr(p, "price_current", 0.0))
            d = (price - info["entry"]) if info["side"] == "BUY" else (info["entry"] - price)
            if not info["r1"] and info["risk"] > 0 and d >= info["risk"]:
                info["r1"] = True
                self.tg.send(f"🎯 #{tk} ({info['setup']}) به ۱ برابر ریسک رسید. سود فعلی ${profit:.2f}",
                             [[{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]])
                self.log(event="reached_1R", ticket=tk, setup=info["setup"], profit=profit)
            last = f.iloc[-1]
            broke = (info["side"] == "BUY" and last.close < last.tenkan) or \
                    (info["side"] == "SELL" and last.close > last.tenkan)
            if broke and not info.get("warned"):
                info["warned"] = True
                self.tg.send(f"⚠️ #{tk} ({info['setup']}) ساختار شکست — بسته شدن کندل خلاف جهت تنکان. "
                             f"سود فعلی ${profit:.2f}",
                             [[{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]])
                self.log(event="structure_break", ticket=tk, setup=info["setup"], profit=profit)
 
    # -------------------------------------------------------------------- loop
    def run(self) -> None:
        if not self.client.connect():
            raise SystemExit("MT5 connect failed")
        acc = self.client.account_info()
        self.tg.start()
        self.tg.send(f"👀 <b>اسکات روشن شد</b>{' (آزمایشی)' if self.dry else ''}\n"
                     f"حساب {getattr(acc,'login','?')} · {getattr(acc,'server','?')} · "
                     f"موجودی ${getattr(acc,'balance',0):.2f}\nلات {self.lot} · انقضای هشدار "
                     f"{self.expiry//60} دقیقه")
        logger.info("scout online")
        while True:
            try:
                while not self.tg.q.empty():
                    u = self.tg.q.get_nowait()
                    cq = u.get("callback_query")
                    if cq:
                        parts = str(cq.get("data", "")).split("|")
                        if parts[0] == "a" and len(parts) == 3:
                            self.decide(parts[1], parts[2] == "y", cq["id"])
                        elif parts[0] == "c" and len(parts) == 2:
                            self.close(int(parts[1]), cq["id"])
                    msg = (u.get("message") or {}).get("text", "")
                    if msg.startswith("/close"):
                        bits = msg.split()
                        if len(bits) > 1 and bits[1].isdigit():
                            self.close(int(bits[1]))
 
                self.expire()
                m15 = self.client.get_rates("M15", 700)
                if m15 is None or m15.empty:
                    time.sleep(2); continue
                m15 = m15.iloc[:-1]                      # فقط کندل‌های بسته
                h1 = self.client.get_rates("H1", 300)
                f = indicators(m15, h1, int(self.s.get("scout.h1_ema_period", 20)))
                self.monitor(f)
                bar = f.index[-1]
                if self.last_bar is None:
                    self.last_bar = bar
                elif bar != self.last_bar:
                    self.last_bar = bar
                    sig = detect(f)
                    sig = sig[sig.bar == bar]
                    sp = self.client.spread_points() or 0.0
                    for _, r in sig.iterrows():
                        self.alert(r, sp)
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
    a = ap.parse_args()
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
    Scout(Settings.load(a.config), a.dry).run()
 
 
if __name__ == "__main__":
    main()
