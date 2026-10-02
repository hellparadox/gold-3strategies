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
import html
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
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
except Exception:                      # pragma: no cover
    import scout_tracker as trk
    import scout_ai

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
               f"حد ضرر: {sl:.2f}  ({r.sl_dist:.2f} = {r.sl_dist/r.atr:.1f}×ATR)"
               + (f" · سقف {risk:.0f}$ (ساختار {float(r['sl_dist_orig']):.2f})"
                  if "sl_dist_orig" in r.index else "") + "\n"
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
        emerg = trk.emergency_sl(r.side, px, sl_now, self.emerg_mult, self.emerg_cap)
        draft = {"sl": round(sl_now, 2), "emerg": emerg, "hold": False}
        broker_sl = (round(trk.desired_broker_sl(draft, self.mode), 2)
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
                         "emerg": emerg if self.soft_stop else None,
                         "status_mid": None, "below": False, "hold": False, "warned": False,
                         "approach": False, "ai_next": 0.0, "ai_note": ""}
        self._save_state()
        self.tg.edit(p["mid"], p["txt"] + f"\n\n✅ <b>باز شد</b> #{tk} @ {res.price:.2f}\n{info_line}")
        soft_note = (f"\nحد ضرر {sl_now:.2f} · اگر برسد: <b>{trk.POLICY_FA[self.mode]}</b>"
                     f" (حالت /mode). با «نگه دار» حد ضرر به سطح اضطراری {emerg:.2f} می‌رود."
                     if self.soft_stop else "")
        self.tg.send(f"پوزیشن #{tk} باز است. تا بسته نشود هشدار جدیدی نمی‌آید.{soft_note}\n"
                     f"وضعیت هر {int(self.status_every // 60)} دقیقه در یک پیام زنده به‌روز می‌شود.",
                     self._close_buttons(tk))
        self.log(event="opened", alert=aid, setup=r.setup, side=r.side, ticket=tk,
                 price=res.price, sl=round(sl_now, 2),
                 note=f"broker_sl={broker_sl};emergency_sl={emerg};mode={self.mode}"
                 if self.soft_stop else None)
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

    def close(self, tk: int, cb: Optional[str] = None, why: str = "scout manual") -> None:
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

    def monitor(self, f: pd.DataFrame) -> None:
        live = {int(p.ticket): p for p in self.client.positions(magic_only=True)}
        now = time.time()
        server_now: Optional[float] = None
        for tk in list(self.open):
            if tk not in live:                       # دستی، در حد ضرر یا اضطراری بسته شده
                pnl, px = self.realized(tk)
                if pnl is None and now - self.open[tk].get("gone_t", now) < 30:
                    self.open[tk].setdefault("gone_t", now)   # تاریخچه هنوز نیامده
                    continue
                info = self.open.pop(tk)
                self._ai_futs.pop(tk, None)
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
                if self._manage_stop(tk, info, p, price, profit, now, f):
                    continue                              # همین الان بسته شد
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
            self._close_at_stop(tk, info, price, profit, "رسیدن به حد ضرر سیگنال")
            return True
        # (۳) هوش مصنوعی: در حد ضرر بپرسد (هر ai_recheck یک بار تا وقتی آن طرف است)
        if pol == "ai":
            if self._ai_step(tk, info, price, profit, now, beyond, f):
                return True
        # (۴) حد ضرر بروکر = سطح درستِ همین سیاست
        want = round(trk.desired_broker_sl(info, self.mode), 2)
        have = float(getattr(p, "sl", 0.0) or 0.0)
        if abs(want - have) > 0.009 and now - self._sl_fail_t.get(tk, 0.0) > 60:
            wrong_side = trk.beyond_soft_sl(side, price, want)
            if wrong_side:                              # حتی سطح اضطراری رد شده
                self._close_at_stop(tk, info, price, profit, "عبور از حد ضرر اضطراری")
                return True
            res = self.client.modify_sltp(tk, sl=want)
            if res.ok:
                self._sl_fail_t.pop(tk, None)
                logger.info("scout #{} broker SL {} -> {} ({})", tk, have, want, pol)
            else:
                self._sl_fail_t[tk] = now
                logger.warning("scout #{} broker SL {} not set: {}", tk, want, res.comment)
        return False

    def _close_at_stop(self, tk: int, info: dict, price: float, profit: float, why: str) -> None:
        res = self.client.close_position(int(tk), comment="scout_sl")
        self.log(event="sl_close", ticket=tk, setup=info["setup"], side=info["side"],
                 price=res.price or price, sl=info["sl"], profit=profit, ok=res.ok, note=why)
        if res.ok:
            logger.info("scout #{} closed at stop ({})", tk, why)
        else:
            self._sl_fail_t[tk] = time.time()
            self.tg.send(f"⚠️ بستن #{tk} در حد ضرر انجام نشد: {res.comment}. دوباره تلاش می‌شود؛ "
                         "حد ضرر اضطراری روی بروکر برقرار است.", self._close_buttons(tk))

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
            self.tg.send(f"🤖 <b>#{tk} بسته شد</b> · {html.escape(why)}")
            self._close_at_stop(tk, info, price, profit, why)
            return True
        if beyond and now >= float(info.get("ai_next") or 0.0):
            if self._ai is None:
                self._ai = scout_ai.ScoutAI.from_gate_config(self.ai_timeout) or False
            if not self._ai:
                self.tg.send(f"🤖 #{tk}: هوش مصنوعی تنظیم نیست — طبق قاعده در حد ضرر بسته شد.")
                self._close_at_stop(tk, info, price, profit, "هوش مصنوعی در دسترس نیست")
                return True
            if self._ai_pool is None:
                self._ai_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-ai")
            msg = scout_ai.build_message(info, price, profit, f)
            self._ai_futs[tk] = self._ai_pool.submit(self._ai.decide, msg)
            info["ai_next"] = now + self.ai_recheck
            logger.info("scout #{} at stop: asking AI", tk)
        return False

    def _push_status(self, tk: int, info: dict, price: float, profit: float, swap: float,
                     secs: Optional[float], server_now: Optional[float], now: float) -> None:
        info["status_t"] = now
        text = trk.status_text(tk, info, price, profit, swap, secs, server_now, self.mode)
        buttons = self._close_buttons(tk)
        mid = info.get("status_mid")
        if mid and self.tg.edit(mid, text, buttons):
            return
        new_mid = self.tg.send(text, buttons)
        if new_mid:
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
            if tk in self.open:
                continue
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
                     sl=info["sl"], note=f"emergency_sl={info['emerg']}")
            self.tg.send(f"🔁 پوزیشن #{tk} ({side} @ {entry:.2f}) دوباره تحت نظر است.\n"
                         f"حد ضرر {info['sl']:.2f} · اضطراری {info['emerg']:.2f} · در حد ضرر: "
                         f"<b>{trk.POLICY_FA[self.mode]}</b>", self._close_buttons(tk))
        if self.open:
            self._save_state()
            logger.info("scout tracking {} open position(s) | mode={}", len(self.open), self.mode)

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
                        elif parts[0] == "u" and len(parts) == 2:
                            self.hold(int(parts[1]), cq["id"], on=False)
                        elif parts[0] == "m" and len(parts) == 2:
                            self.set_mode(parts[1], cq["id"])
                    msg = (u.get("message") or {}).get("text", "")
                    if msg.startswith(("/mode", "/busy")):
                        self.mode_menu()
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
                    rows = [self.cap_row(r) for r in rows]
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
