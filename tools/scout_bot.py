"""SCOUT BOT — هر ستاپ را به تلگرام می‌فرستد و منتظر تأیید یا رد شما می‌ماند.

- خودش وارد نمی‌شود. فقط بعد از «تأیید» شما سفارش می‌گذارد.
- حد ضرر نرم (scout.soft_stop، پیش‌فرض روشن): حد ضرر سیگنال فقط «هشدار» است و بستن فقط با
  شماست. روی بروکر فقط یک حد ضرر اضطراری دور (scout.emergency_sl_mult × فاصلهٔ حد ضرر سیگنال)
  گذاشته می‌شود تا قطعی VPS/اینترنت ضرر را بی‌سقف نکند. حد سود گذاشته نمی‌شود.
- رسیدن به حد ضرر سیگنال: پیام با دکمهٔ «بستن / نگه دار»؛ تا تصمیم نگیرید هر
  scout.sl_reminder_seconds یادآوری می‌شود.
- گزارش زنده: یک پیام وضعیت برای هر پوزیشن تأییدشده، هر scout.status_every_seconds
  (پیش‌فرض ۵ دقیقه) به‌روز می‌شود. رسیدن به ۱ برابر ریسک و شکست ساختار پیام جداگانه دارند.
- پوزیشن‌های باز در scout.state_file نگه داشته می‌شوند و بعد از ری‌استارت دوباره دنبال می‌شوند.
- بستن با دکمهٔ «بستن» یا دستور /close <ticket>.
- مهلت پاسخ به هر هشدار: scout.expiry_seconds (پیش‌فرض ۳۰ دقیقه).
- در لحظهٔ تأیید، ورود با قیمت همان لحظه است و حد ضرر با همان فاصلهٔ هشدار از این قیمت
  گذاشته می‌شود؛ اگر قیمت از حد ضرر هشدار رد شده باشد، سفارشی ثبت نمی‌شود.
- تا وقتی پوزیشن اسکات باز است، هشدار جدید فرستاده نمی‌شود؛ ستاپ‌ها بی‌صدا در ژورنال ثبت
  می‌شوند (رویداد suppressed) و هشدارهای بی‌جوابِ قبلی لغو می‌شوند.
- همه‌چیز در data/scout_journal_v2.csv با ستون‌های ثابت ثبت می‌شود؛ آمار: tools/scout_stats.py

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
from core.mt5_client import MT5Client, MT5Config, safe_comment  # noqa: E402

try:                                   # ماژول تشخیص، کنار همین فایل یا در ریشه
    from tools.scout_setups import indicators, detect
except Exception:                      # pragma: no cover
    from scout_setups import indicators, detect
try:
    from tools import scout_tracker as trk
except Exception:                      # pragma: no cover
    import scout_tracker as trk

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

    def edit(self, message_id: int, text: str, buttons: Optional[list] = None) -> bool:
        kw: Dict[str, Any] = {"chat_id": self.chat, "message_id": message_id,
                              "text": text, "parse_mode": "HTML"}
        if buttons:
            kw["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("editMessageText", **kw) is not None

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
        self.tg = TG(token, (s.get("telegram.admin_ids") or [0])[0])
        self.pending: Dict[str, dict] = {}
        self.open: Dict[int, dict] = {}
        self.last_bar: Optional[pd.Timestamp] = None
        self.n = 0
        self.block_hedge = bool(s.get("scout.block_hedge", True))
        self.merge_setups = bool(s.get("scout.merge_setups", True))
        self.beat_path = Path(str(s.get("scout.heartbeat", "data/heartbeat_scout.txt")))
        self._last_beat = 0.0
        # حد ضرر نرم + گزارش زنده
        self.soft_stop = bool(s.get("scout.soft_stop", True))
        self.emerg_mult = float(s.get("scout.emergency_sl_mult", 3.0))
        self.status_every = float(s.get("scout.status_every_seconds", 300))
        self.remind_every = float(s.get("scout.sl_reminder_seconds", 300))
        self.state_path = Path(str(s.get("scout.state_file", "data/scout_state.json")))

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

    def record_silent(self, r: pd.Series, spread_pts: float) -> None:
        """معامله باز است: هشدار فرستاده نمی‌شود، فقط برای آمار ثبت می‌شود."""
        entry, sl, tp, risk, n_set = self.levels(r)
        self.log(event="suppressed", setup=str(r.get("setups_all", r.setup)), n_setups=n_set,
                 side=r.side, bar=str(r.bar), price=entry, sl=round(sl, 2), tp=round(tp, 2),
                 atr=round(r.atr, 3), spread=spread_pts, risk_usd=risk)

    def alert(self, r: pd.Series, spread_pts: float) -> None:
        self.n += 1
        aid = f"{self.n}"
        entry, sl, tp, risk, n_set = self.levels(r)
        head = f"  ({n_set} ستاپ هم‌زمان)" if n_set > 1 else ""
        txt = (f"<b>{'🟢 BUY' if r.side=='BUY' else '🔴 SELL'}</b> · <code>{r.setup}</code>{head}\n"
               f"کندل: {r.bar:%H:%M}  |  قیمت: <b>{entry:.2f}</b>\n"
               f"حد ضرر: {sl:.2f}  ({r.sl_dist:.2f} = {r.sl_dist/r.atr:.1f}×ATR)\n"
               f"هدف پیشنهادی: {tp:.2f}  (۲ برابر ریسک)\n"
               f"ریسک: ${risk} · ATR {r.atr:.2f} · اسپرد {spread_pts:.0f}\n"
               f"<i>{self.expiry // 60} دقیقه فرصت پاسخ</i>")
        mid = self.tg.send(txt, [[{"text": "✅ تأیید", "callback_data": f"a|{aid}|y"},
                                  {"text": "❌ رد", "callback_data": f"a|{aid}|n"}]])
        self.pending[aid] = {"row": r, "sl": sl, "tp": tp, "mid": mid, "t": time.time(), "txt": txt}
        self.log(event="alert", alert=aid, setup=str(r.get("setups_all", r.setup)),
                 n_setups=n_set, side=r.side, bar=str(r.bar),
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
        opposite = [tk for tk, i in self.open.items() if i["side"] != r.side]
        if self.block_hedge and opposite and not self.dry:
            names = "، ".join(f"#{t}" for t in opposite)
            self.tg.ack(cb, "پوزیشن مخالف باز است")
            self.tg.edit(p["mid"], p["txt"] +
                         f"\n\n🚫 <b>ثبت نشد — پوزیشن مخالف باز است</b> ({names})\n"
                         "خرید و فروش هم‌زمان همدیگر را خنثی می‌کنند و فقط اسپرد می‌دهید. "
                         "اول آن را ببندید.")
            self.log(event="blocked_hedge", alert=aid, setup=r.setup, side=r.side,
                     note=names)
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
        broker_sl = (trk.emergency_sl(r.side, px, sl_now, self.emerg_mult)
                     if self.soft_stop else round(sl_now, 2))
        res = self.client.send_market_order(r.side, self.lot, sl=broker_sl,
                                            tp=None, comment=scout_comment(r.setup))
        if not res.ok:
            self.tg.edit(p["mid"], p["txt"] + f"\n\n⚠️ <b>ثبت نشد</b>: {res.comment}")
            self.log(event="order_failed", alert=aid, setup=r.setup, side=r.side, note=res.comment)
            return
        tk = res.position or res.order
        self.open[tk] = {"setup": r.setup, "side": r.side, "entry": res.price,
                         "sl": round(sl_now, 2), "risk": abs(res.price - sl_now), "r1": False,
                         "opened": time.time(), "alert": aid,
                         "emerg": broker_sl if self.soft_stop else None,
                         "status_mid": None, "below": False, "hold": False, "warned": False}
        self._save_state()
        self.tg.edit(p["mid"], p["txt"] + f"\n\n✅ <b>باز شد</b> #{tk} @ {res.price:.2f}\n{info_line}")
        soft_note = (f"\nحد ضرر سیگنال ({sl_now:.2f}) فقط هشدار است — بستن فقط با شما. "
                     f"حد ضرر اضطراری روی بروکر: {broker_sl:.2f}" if self.soft_stop else "")
        self.tg.send(f"پوزیشن #{tk} باز است. تا بسته نشود هشدار جدیدی نمی‌آید.{soft_note}\n"
                     f"وضعیت هر {int(self.status_every // 60)} دقیقه در یک پیام زنده به‌روز می‌شود.",
                     [[{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]])
        self.log(event="opened", alert=aid, setup=r.setup, side=r.side, ticket=tk,
                 price=res.price, sl=round(sl_now, 2),
                 note=f"emergency_sl={broker_sl}" if self.soft_stop else None)
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

    def close(self, tk: int, cb: Optional[str] = None) -> None:
        res = self.client.close_position(int(tk), comment="scout manual")
        if cb:
            self.tg.ack(cb, "بسته شد" if res.ok else f"خطا: {res.comment}")
        info = self.open.get(int(tk), {})
        self.tg.send(f"{'✅' if res.ok else '⚠️'} بستن #{tk}: {res.comment or 'OK'} @ {res.price:.2f}")
        self.log(event="closed", ticket=tk, setup=info.get("setup"), side=info.get("side"),
                 price=res.price, ok=res.ok)

    # -------------------------------------------------------------- monitoring
    def _close_buttons(self, tk: int, hold: bool = False) -> list:
        row = [{"text": "🔻 بستن", "callback_data": f"c|{tk}"}]
        if hold:
            row.append({"text": "⏸ نگه دار", "callback_data": f"h|{tk}"})
        return [row]

    def monitor(self, f: pd.DataFrame) -> None:
        live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        now = time.time()
        server_now: Optional[float] = None
        for tk in list(self.open):
            if tk not in live:                       # دستی یا با حد ضرر اضطراری بسته شده
                pnl, px = self.realized(tk)
                if pnl is None and now - self.open[tk].get("gone_t", now) < 30:
                    self.open[tk].setdefault("gone_t", now)   # تاریخچه هنوز نیامده
                    continue
                info = self.open.pop(tk)
                self._save_state()
                res_txt = f" · نتیجه ${pnl:+.2f}" if pnl is not None else ""
                emerg = info.get("emerg")
                by_emerg = (emerg and px and info.get("risk")
                            and abs(float(px) - float(emerg)) <= 0.5 * float(info["risk"]))
                why = "\n🛑 با حد ضرر اضطراری بروکر بسته شد." if by_emerg else ""
                self.tg.send(f"ℹ️ پوزیشن #{tk} ({info['setup']}) بسته شد{res_txt}.{why}"
                             + ("\n🔔 هشدارها دوباره فعال شد." if not self.open else ""))
                if info.get("status_mid"):
                    self.tg.edit(info["status_mid"],
                                 f"📊 <b>#{tk}</b> · <code>{info['setup']}</code>\n"
                                 f"✅ <b>بسته شد</b>{res_txt}" + (f" @ {px:.2f}" if px else ""))
                self.log(event="gone", alert=info.get("alert"), ticket=tk, setup=info["setup"],
                         side=info["side"], entry=round(float(info["entry"]), 2), price=px, profit=pnl,
                         note="emergency_sl" if by_emerg else None)
                continue
            p, info = live[tk], self.open[tk]
            profit = float(getattr(p, "profit", 0.0))
            swap = float(getattr(p, "swap", 0.0) or 0.0)
            price = float(getattr(p, "price_current", 0.0))
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
            if self.soft_stop and price > 0:
                self._soft_stop(tk, info, price, profit, now)
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

    def _soft_stop(self, tk: int, info: dict, price: float, profit: float, now: float) -> None:
        """حد ضرر سیگنال = هشدار. هیچ‌وقت خودش نمی‌بندد."""
        if trk.beyond_soft_sl(info["side"], price, float(info["sl"])):
            first = not info.get("below")
            due = (not info.get("hold")) and now - float(info.get("remind_t", 0.0)) >= self.remind_every
            if first or due:
                info["below"], info["remind_t"] = True, now
                if first:
                    info["hold"] = False
                head = "🛑 <b>قیمت به حد ضرر سیگنال رسید</b>" if first else "⏰ <b>یادآوری: هنوز زیر حد ضرر سیگنال</b>"
                self.tg.send(f"{head}\n#{tk} · <code>{info['setup']}</code> · الان {price:.2f} · "
                             f"حد ضرر سیگنال {float(info['sl']):.2f} · زیان ${profit:+.2f}\n"
                             f"پوزیشن باز می‌ماند تا شما تصمیم بگیرید"
                             + (f" (حد ضرر اضطراری {float(info['emerg']):.2f})." if info.get("emerg") else "."),
                             self._close_buttons(tk, hold=True))
                if first:
                    self.log(event="soft_sl_hit", ticket=tk, setup=info["setup"], side=info["side"],
                             price=price, sl=info["sl"], profit=profit)
                self._save_state()
        elif info.get("below") and trk.recovered(info["side"], price, float(info["sl"]), float(info["risk"])):
            info["below"], info["hold"] = False, False       # دوباره مسلح: عبور بعدی دوباره خبر می‌دهد
            self._save_state()

    def _push_status(self, tk: int, info: dict, price: float, profit: float, swap: float,
                     secs: Optional[float], server_now: Optional[float], now: float) -> None:
        info["status_t"] = now
        text = trk.status_text(tk, info, price, profit, swap, secs, server_now)
        buttons = self._close_buttons(tk)
        mid = info.get("status_mid")
        if mid and self.tg.edit(mid, text, buttons):
            return
        new_mid = self.tg.send(text, buttons)
        if new_mid:
            info["status_mid"] = new_mid
            self._save_state()

    def hold(self, tk: int, cb: Optional[str] = None) -> None:
        info = self.open.get(int(tk))
        if info is None:
            if cb:
                self.tg.ack(cb, "این پوزیشن دیگر باز نیست")
            return
        info["hold"] = True
        self._save_state()
        if cb:
            self.tg.ack(cb, "نگه داشته شد")
        self.tg.send(f"⏸ #{tk} نگه داشته شد. تا وقتی زیر حد ضرر سیگنال است دیگر یادآوری نمی‌کنم؛ "
                     "گزارش ۵ دقیقه‌ای ادامه دارد و هر وقت خواستید ببندید.", self._close_buttons(tk))
        self.log(event="hold", ticket=tk, setup=info["setup"], side=info["side"])

    # ------------------------------------------------------------------ state
    def _save_state(self) -> None:
        try:
            trk.save_state(self.state_path, self.open)
        except Exception as exc:                       # pragma: no cover
            logger.warning("scout state save failed: {}", exc)

    def restore(self) -> None:
        """بعد از ری‌استارت: پوزیشن‌های باز اسکات دوباره دنبال می‌شوند."""
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
            info["risk"] = float(info.get("risk") or 0.0)
            self.open[tk] = info
        for tk, p in live.items():
            if tk in self.open:
                continue
            side = "BUY" if int(getattr(p, "type", 0)) == 0 else "SELL"
            entry = float(p.price_open)
            soft = float(getattr(p, "sl", 0.0) or 0.0)
            if soft <= 0:                              # بدون حد ضرر: ۱٪ قیمت به‌عنوان حد ضرر سیگنال
                soft = round(entry * (0.99 if side == "BUY" else 1.01), 2)
            info = {"setup": str(getattr(p, "comment", "") or "scout"), "side": side, "entry": entry,
                    "sl": soft, "risk": abs(entry - soft), "r1": False, "warned": False,
                    "opened": time.time(), "alert": None, "status_mid": None, "below": False,
                    "hold": False, "adopted": True, "emerg": None}
            if self.soft_stop and not self.dry:
                emerg = trk.emergency_sl(side, entry, soft, self.emerg_mult)
                res = self.client.modify_sltp(tk, sl=emerg)
                if res.ok:
                    info["emerg"] = emerg
                else:
                    logger.warning("scout adopt #{}: emergency SL not set ({})", tk, res.comment)
            self.open[tk] = info
            self.log(event="adopted", ticket=tk, setup=info["setup"], side=side, entry=entry,
                     sl=soft, note=f"emergency_sl={info['emerg']}")
            self.tg.send(f"🔁 پوزیشن #{tk} ({side} @ {entry:.2f}) دوباره تحت نظر است.\n"
                         f"حد ضرر سیگنال {soft:.2f} از این به بعد فقط هشدار است"
                         + (f"؛ حد ضرر اضطراری روی بروکر {info['emerg']:.2f}." if info["emerg"] else "."),
                         self._close_buttons(tk))
        if self.open:
            self._save_state()
            logger.info("scout tracking {} open position(s)", len(self.open))

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
        self.tg.send(f"👀 <b>اسکات روشن شد</b>{' (آزمایشی)' if self.dry else ''}\n"
                     f"حساب {getattr(acc,'login','?')} · {getattr(acc,'server','?')} · "
                     f"موجودی ${getattr(acc,'balance',0):.2f}\nلات {self.lot} · انقضای هشدار "
                     f"{self.expiry//60} دقیقه")
        logger.info("scout online")
        self.restore()
        if self.selftest:
            self.fire_selftest()
        while True:
            try:
                self.beat()
                while not self.tg.q.empty():
                    u = self.tg.q.get_nowait()
                    cq = u.get("callback_query")
                    if cq:
                        parts = str(cq.get("data", "")).split("|")
                        if parts[0] == "a" and len(parts) == 3:
                            self.decide(parts[1], parts[2] == "y", cq["id"])
                        elif parts[0] == "c" and len(parts) == 2:
                            self.close(int(parts[1]), cq["id"])
                        elif parts[0] == "h" and len(parts) == 2:
                            self.hold(int(parts[1]), cq["id"])
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
                    rows = ([self.merge(g) for _s, g in sig.groupby("side", sort=False)]
                            if self.merge_setups and not sig.empty
                            else [r for _, r in sig.iterrows()])
                    quiet = self.busy() and not self.dry
                    for r in rows:
                        (self.record_silent if quiet else self.alert)(r, sp)
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
    Scout(st, a.dry or a.selftest, a.selftest).run()


if __name__ == "__main__":
    main()
