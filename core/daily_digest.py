"""Daily performance digest — stats, dark KPI card and Telegram text.

A digest covers one UTC day of *closed* signals (open positions are counted
separately).  Everything reads from the existing ``signals`` table, so no
schema change is needed.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

__all__ = ["build_daily_stats", "render_daily_card", "format_daily_text"]

import matplotlib

matplotlib.use("Agg", force=True)

import matplotlib.pyplot as plt  # noqa: E402

from core.chart_generator import ChartTheme  # noqa: E402

# chart rendering is process-global mutex-protected in chart_generator; reuse
# the same lock so the digest card and signal charts never collide.
from core.chart_generator import _RENDER_LOCK  # noqa: E402


def _day_bounds(day: date) -> tuple:
    start = f"{day.isoformat()} 00:00:00"
    end = f"{day.isoformat()} 23:59:59"
    return start, end


def build_daily_stats(db: Any, day: Optional[date] = None) -> Dict[str, Any]:
    """Aggregate one day of signal performance from the database."""
    day = day or datetime.utcnow().date()
    rows = db.recent_signals(limit=1000)

    closed: List[Dict[str, Any]] = []
    opened: int = 0
    for r in rows:
        created = str(r.get("created_at") or "")
        closed_at = str(r.get("closed_at") or "")
        if created.startswith(day.isoformat()):
            opened += 1
        if closed_at and closed_at.startswith(day.isoformat()):
            closed.append(r)

    # newest-first from the DB -> chronological for the digest
    closed.reverse()

    profits = [float(r.get("profit") or 0.0) for r in closed]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    net = sum(profits)

    streak = 0
    for p in reversed(profits):
        if p > 0:
            streak += 1
        else:
            break

    # cumulative equity across the whole closed history up to end of day
    all_closed = [
        float(r.get("profit") or 0.0)
        for r in rows
        if r.get("closed_at") and str(r.get("closed_at")) <= f"{day.isoformat()} 23:59:59"
    ]
    all_closed.reverse()
    equity: List[float] = []
    run = 0.0
    for p in all_closed:
        run += p
        equity.append(round(run, 2))

    best = max(profits) if profits else 0.0
    worst = min(profits) if profits else 0.0
    trades = len(profits)
    return {
        "day": day.isoformat(),
        "trades": trades,
        "opened": opened,
        "open_now": opened - trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / trades * 100.0) if trades else 0.0,
        "net": round(net, 2),
        "best": round(best, 2),
        "worst": round(worst, 2),
        "win_streak": streak,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "equity": equity,
        "closed_trades_total": len(all_closed),
    }


def render_daily_card(stats: Dict[str, Any], brand: str = "GOLD M5 VIP") -> Optional[io.BytesIO]:
    """Dark KPI card: big numbers + day equity steps. Never raises."""
    try:
        with _RENDER_LOCK:
            t = ChartTheme()
            fig = plt.figure(figsize=(11.0, 6.2), facecolor=t.background)
            gs = fig.add_gridspec(2, 4, hspace=0.55, wspace=0.25,
                                  left=0.06, right=0.96, top=0.80, bottom=0.14)

            net = float(stats.get("net", 0.0))
            good = net >= 0

            fig.text(
                0.06, 0.93, "DAILY PERFORMANCE", color=t.text,
                fontsize=17, fontweight="bold", va="top",
            )
            fig.text(
                0.06, 0.865, str(stats.get("day", "")), color=t.text,
                fontsize=11, alpha=0.75, va="top",
            )
            fig.text(
                0.96, 0.93, brand, color=t.watermark, fontsize=12,
                ha="right", va="top", fontweight="bold",
            )

            net_color = t.up if good else t.down
            fig.text(
                0.96, 0.855, f"${net:+,.2f}", color=net_color,
                fontsize=26, fontweight="bold", ha="right", va="top",
            )

            def kpi(row: int, col: int, value: str, label: str, color: str) -> None:
                ax = fig.add_subplot(gs[row, col])
                ax.set_facecolor(t.panel_background)
                for spine in ax.spines.values():
                    spine.set_color(t.grid)
                ax.set_xticks([])
                ax.set_yticks([])
                ax.text(0.5, 0.58, value, ha="center", va="center",
                        color=color, fontsize=16, fontweight="bold",
                        transform=ax.transAxes)
                ax.text(0.5, 0.16, label, ha="center", va="center",
                        color=t.text, fontsize=9, alpha=0.8,
                        transform=ax.transAxes)

            kpi(0, 0, f"{stats.get('trades', 0)}", "TRADES", t.text)
            kpi(0, 1, f"{stats.get('win_rate', 0):.0f}%", "WIN RATE", t.up)
            kpi(0, 2, f"{stats.get('wins', 0)}W / {stats.get('losses', 0)}L", "W / L", t.text)
            kpi(0, 3, f"{stats.get('win_streak', 0)}", "WIN STREAK", t.ema_fast)
            kpi(1, 0, f"${stats.get('best', 0):,.2f}", "BEST TRADE", t.up)
            kpi(1, 1, f"${stats.get('worst', 0):,.2f}", "WORST TRADE", t.down)
            kpi(1, 2, f"{stats.get('open_now', 0)}", "STILL OPEN", t.text)
            kpi(1, 3, f"{stats.get('closed_trades_total', 0)}", "ALL-TIME TRADES", t.text)

            # day equity steps inside a slim full-width strip
            eq = stats.get("equity") or []
            if eq:
                ax = fig.add_subplot(gs[:, :])
                ax.set_facecolor("none")
                ax.patch.set_alpha(0.0)
                x = range(len(eq))
                colour = t.up if eq[-1] >= eq[0] else t.down
                ax.plot(x, eq, color=colour, linewidth=1.4, alpha=0.35)
                ax.fill_between(x, eq, min(eq), color=colour, alpha=0.06)
                ax.set_xticks([])
                ax.set_yticks([])
                for spine in ax.spines.values():
                    spine.set_visible(False)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, facecolor=t.background,
                        bbox_inches="tight", pad_inches=0.25)
            plt.close(fig)
            buf.seek(0)
            return buf
    except Exception as exc:
        logger.exception("daily card rendering failed: {}", exc)
        return None


def format_daily_text(stats: Dict[str, Any]) -> str:
    """Persian Telegram summary matching the house card style."""
    net = float(stats.get("net", 0.0))
    net_txt = ("+" if net >= 0 else "-") + f"${abs(net):,.2f}"
    total = float((stats.get("equity") or [0])[-1])
    total_txt = ("+" if total >= 0 else "-") + f"${abs(total):,.2f}"
    mood = "\U0001F3AF" if net > 0 else ("\U0001F534" if net < 0 else "\U000026AA")
    arrow = "\U0001F4C8" if net >= 0 else "\U0001F4C9"
    trades = int(stats.get("trades", 0))
    if trades == 0:
        return (
            f"{mood} <b>گزارش روزانه — {stats.get('day', '')}</b>\n"
            "——————————————\n"
            "\U0001F4ED امروز معامله‌ی بسته‌شده‌ای نداشتیم.\n"
            "\U0001F4E6 پوزیشن‌های باز امروز: "
            f"<b>{stats.get('open_now', 0)}</b>"
        )
    return (
        f"{mood} <b>گزارش روزانه — {stats.get('day', '')}</b>\n"
        "——————————————\n"
        f"{arrow} سود/زیان خالص: <b>{net_txt}</b>\n"
        f"\U0001F3AF معاملات: <b>{trades}</b> "
        f"({stats.get('wins', 0)} برنده / {stats.get('losses', 0)} بازنده)\n"
        f"\u2705 وین‌ریت: <b>{stats.get('win_rate', 0):.1f}%</b>\n"
        f"\U0001F534 بهترین/بدترین: <b>${stats.get('best', 0):,.2f}</b> / "
        f"<b>${stats.get('worst', 0):,.2f}</b>\n"
        f"\U0001F525 استریک برنده: <b>{stats.get('win_streak', 0)}</b>\n"
        f"\U0001F4E6 پوزیشن‌های باز: <b>{stats.get('open_now', 0)}</b>\n"
        "——————————————\n"
        f"\U0001F4B0 کل سود تاریخچه: <b>{total_txt}</b> "
        f"روی {stats.get('closed_trades_total', 0)} معامله"
    )


def yesterday() -> date:
    return datetime.utcnow().date() - timedelta(days=1)
