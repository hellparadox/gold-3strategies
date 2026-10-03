"""نمودار هشدار اسکات — PNG در حافظه، بدون pyplot (امن برای نخ پس‌زمینه).

ظاهر عمداً با نمودار ORB/ایچیموکو فرق دارد: «ابزار پوزیشن» روی کندل‌های ۱۵ دقیقه‌ای —
- محدودهٔ سود (سبز) از ورود تا هدف 2R، محدودهٔ ضرر (قرمز) از ورود تا حد ضرر،
- محدودهٔ خنثی (کهربایی) دور ورود: جایی که بستن تقریباً سر به سر است (اسپرد/هزینه)،
- ابر و خطوط ایچیموکو کم‌رنگ برای زمینه، کندل هشدار علامت‌دار، برچسب قیمت‌ها کنار محور.
متن داخل تصویر انگلیسی/عدد است (فونت فارسی روی VPS تضمینی نیست)؛ توضیح فارسی در کپشن می‌آید.
"""
from __future__ import annotations

import io
import threading
from typing import Optional

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch, Rectangle

_LOCK = threading.Lock()                     # matplotlib در چند نخ هم‌زمان امن نیست

INK = "#0d1117"
PANEL = "#111823"
GRID = "#1f2a37"
TEXT = "#c9d4e0"
MUTED = "#7d8b99"
UP = "#3ddc97"
DOWN = "#ff6b6b"
PROFIT = "#22c55e"
LOSS = "#ef4444"
NEUTRAL = "#f5b942"
GOLD = "#e2b64e"
TENKAN = "#4ea8ff"
KIJUN = "#b48cff"
EMA = "#9aa5b1"


def neutral_half_width(risk: float, spread_price: float) -> float:
    """نیم‌پهنای محدودهٔ خنثی: اسپرد واقعی، ولی دست‌کم ۸٪ ریسک تا روی نمودار دیده شود."""
    return max(float(spread_price or 0.0), 0.08 * float(risk))


def render_alert_chart(frame: pd.DataFrame, side: str, entry: float, sl: float, tp: float,
                       setup: str, risk_usd: float, spread_price: float = 0.0,
                       bars: int = 64, ahead: int = 14, alert_id: Optional[str] = None) -> bytes:
    """frame: کندل‌های بستهٔ M15 با open/high/low/close و (اختیاری) tenkan/kijun/cloud_top/cloud_bot/ema200."""
    f = frame.tail(bars).copy()
    if f.empty:
        raise ValueError("no bars to draw")
    n = len(f)
    x = np.arange(n)
    o, h, l, c = (f[k].astype(float).to_numpy() for k in ("open", "high", "low", "close"))
    buy = str(side).upper() == "BUY"
    risk = abs(float(entry) - float(sl)) or 1e-9
    half = neutral_half_width(risk, spread_price)

    with _LOCK:
        fig = Figure(figsize=(10.8, 6.2), dpi=110, facecolor=INK)
        FigureCanvasAgg(fig)
        ax = fig.add_axes((0.045, 0.10, 0.745, 0.74), facecolor=PANEL)

        # زمینهٔ ایچیموکو (کم‌رنگ)
        if {"cloud_top", "cloud_bot"} <= set(f.columns):
            top, bot = f["cloud_top"].astype(float).to_numpy(), f["cloud_bot"].astype(float).to_numpy()
            ax.fill_between(x, bot, top, color="#5b6b7d", alpha=0.13, linewidth=0, zorder=1)
        for col, colr, lw, ls in (("tenkan", TENKAN, 1.1, "-"), ("kijun", KIJUN, 1.1, "-"),
                                  ("ema200", EMA, 1.0, (0, (4, 3)))):
            if col in f.columns:
                ax.plot(x, f[col].astype(float).to_numpy(), color=colr, lw=lw, ls=ls, alpha=0.75, zorder=2)

        # کندل‌ها
        w = 0.62
        for i in range(n):
            up = c[i] >= o[i]
            colr = UP if up else DOWN
            ax.vlines(x[i], l[i], h[i], color=colr, lw=1.0, alpha=0.9, zorder=3)
            body_lo, body_h = min(o[i], c[i]), max(abs(c[i] - o[i]), risk * 0.004)
            ax.add_patch(Rectangle((x[i] - w / 2, body_lo), w, body_h, facecolor=colr if up else colr,
                                   edgecolor=colr, alpha=0.95 if up else 0.85, zorder=4))

        # ابزار پوزیشن: از کندل هشدار تا جلوتر
        x0, x1 = n - 0.5, n - 0.5 + ahead
        prof_lo, prof_hi = (entry + half, tp) if buy else (tp, entry - half)
        loss_lo, loss_hi = (sl, entry - half) if buy else (entry + half, sl)
        ax.add_patch(Rectangle((x0, prof_lo), x1 - x0, prof_hi - prof_lo, facecolor=PROFIT, alpha=0.20,
                               edgecolor=PROFIT, lw=1.2, zorder=2))
        ax.add_patch(Rectangle((x0, loss_lo), x1 - x0, loss_hi - loss_lo, facecolor=LOSS, alpha=0.20,
                               edgecolor=LOSS, lw=1.2, zorder=2))
        ax.add_patch(Rectangle((x0, entry - half), x1 - x0, 2 * half, facecolor=NEUTRAL, alpha=0.30,
                               edgecolor=NEUTRAL, lw=0.8, zorder=2))
        # پژواک کم‌رنگ محدوده‌ها روی گذشته، برای دیدن جای قیمت نسبت به سطح‌ها
        ax.axhspan(prof_lo, prof_hi, xmin=0, xmax=1, color=PROFIT, alpha=0.025, zorder=0)
        ax.axhspan(loss_lo, loss_hi, xmin=0, xmax=1, color=LOSS, alpha=0.025, zorder=0)
        for lvl, colr, ls in ((tp, PROFIT, "-"), (sl, LOSS, "-"), (entry, GOLD, (0, (6, 3)))):
            ax.hlines(lvl, -0.5, x1, color=colr, lw=1.3 if lvl != entry else 1.1, ls=ls, alpha=0.95, zorder=5)
        mid_r1 = entry + risk if buy else entry - risk        # 1R داخل محدودهٔ سود
        ax.hlines(mid_r1, x0, x1, color=PROFIT, lw=0.8, ls=(0, (2, 3)), alpha=0.8, zorder=5)

        # کندل هشدار
        mark_y = (l[-1] - risk * 0.18) if buy else (h[-1] + risk * 0.18)
        ax.scatter([x[-1]], [mark_y], marker="^" if buy else "v", s=110, color=GOLD, zorder=6,
                   edgecolors=INK, linewidths=0.8)

        # محدودهٔ عمودی
        lo_all = min(np.nanmin(l), sl, tp) if not buy else min(np.nanmin(l), sl)
        hi_all = max(np.nanmax(h), tp) if buy else max(np.nanmax(h), sl)
        lo_all, hi_all = min(lo_all, tp, sl), max(hi_all, tp, sl)
        pad = (hi_all - lo_all) * 0.06
        ax.set_ylim(lo_all - pad, hi_all + pad)
        ax.set_xlim(-0.8, x1 + 0.5)

        # برچسب‌ها کنار محور راست
        def tag(y, text, colr, dy=0.0):
            ax.annotate(text, xy=(1.0, y), xycoords=("axes fraction", "data"), xytext=(6, dy),
                        textcoords="offset points", va="center", ha="left", fontsize=8.6, color=INK,
                        fontweight="bold", annotation_clip=False,
                        bbox=dict(boxstyle="round,pad=0.28", fc=colr, ec="none"))
        usd_r = float(risk_usd)
        tag(tp, f"TARGET 2R  {tp:.2f}   +${2 * usd_r:.2f}", PROFIT)
        tag(mid_r1, f"1R  {mid_r1:.2f}   +${usd_r:.2f}", "#86efac")
        tag(entry, f"ENTRY  {entry:.2f}", GOLD)
        tag(sl, f"STOP  {sl:.2f}   -${usd_r:.2f}", LOSS)

        # محور زمان (ساعت سرور)
        idx = pd.DatetimeIndex(f.index)
        step = max(1, n // 8)
        ticks = list(range(0, n, step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([idx[i].strftime("%H:%M") if i else idx[i].strftime("%m/%d %H:%M") for i in ticks],
                           fontsize=8, color=MUTED)
        ax.tick_params(axis="y", colors=MUTED, labelsize=8, length=0)
        ax.tick_params(axis="x", length=0)
        ax.yaxis.tick_left()
        ax.grid(True, color=GRID, lw=0.6, alpha=0.9)
        for sp in ax.spines.values():
            sp.set_visible(False)

        # سربرگ
        head = f"SCOUT  ·  {'▲ BUY' if buy else '▼ SELL'}  ·  {setup}"
        fig.text(0.045, 0.935, head, color=UP if buy else DOWN, fontsize=15, fontweight="bold", ha="left")
        sub = (f"XAUUSD M15   ·   risk ${usd_r:.2f}   ·   R:R 1:2   ·   spread {spread_price:.2f}"
               + (f"   ·   alert #{alert_id}" if alert_id else ""))
        fig.text(0.045, 0.895, sub, color=MUTED, fontsize=9.5, ha="left")
        # راهنمای رنگ‌ها
        lx = 0.045
        for colr, label in ((PROFIT, "profit zone"), (NEUTRAL, "break-even zone"), (LOSS, "loss zone"),
                            (TENKAN, "tenkan"), (KIJUN, "kijun"), (EMA, "EMA200")):
            fig.patches.append(FancyBboxPatch((lx, 0.035), 0.012, 0.022, boxstyle="round,pad=0.002",
                                              transform=fig.transFigure, fc=colr, ec="none", alpha=0.9))
            fig.text(lx + 0.017, 0.04, label, color=MUTED, fontsize=8.2, ha="left", va="bottom")
            lx += 0.026 + 0.0068 * len(label)
        fig.text(0.965, 0.04, "server time", color=MUTED, fontsize=7.5, ha="right", va="bottom")
        fig.text(0.45, 0.47, "SCOUT", color="#ffffff", alpha=0.035, fontsize=70, fontweight="bold",
                 ha="center", va="center")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=INK)
        return buf.getvalue()
