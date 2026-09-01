"""Headless multi-panel chart renderer (TradingView dark) -> io.BytesIO.

The Agg backend is selected *before* pyplot is imported, which is mandatory on
a Windows VPS running without a desktop session.  Nothing is ever written to
disk: every figure is serialised into an in-memory PNG buffer and the figure is
explicitly closed to keep the renderer leak-free across thousands of signals.
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg", force=True)   # MUST precede pyplot / mplfinance imports

import matplotlib.pyplot as plt     # noqa: E402
import mplfinance as mpf            # noqa: E402
import numpy as np                  # noqa: E402
import pandas as pd                 # noqa: E402
from loguru import logger           # noqa: E402

__all__ = ["ChartGenerator", "ChartTheme"]

# matplotlib is not thread-safe; the Telegram thread and the live loop can both
# ask for a chart, so serialise all rendering.
_RENDER_LOCK = threading.Lock()


@dataclass(frozen=True)
class ChartTheme:
    """TradingView-ish dark palette."""

    background: str = "#131722"
    panel_background: str = "#131722"
    grid: str = "#2A2E39"
    text: str = "#D1D4DC"
    up: str = "#26A69A"
    down: str = "#EF5350"
    ema_fast: str = "#F5C542"
    ema_slow: str = "#42A5F5"
    ema_extra: str = "#AB47BC"
    entry: str = "#FFFFFF"
    take_profit: str = "#26A69A"
    stop_loss: str = "#EF5350"
    macd_line: str = "#42A5F5"
    macd_signal: str = "#FF7043"
    rsi_line: str = "#BA68C8"
    band: str = "#5D6D7E"
    watermark: str = "#3A4050"


class ChartGenerator:
    """Renders signal charts entirely in RAM."""

    def __init__(
        self,
        theme: Optional[ChartTheme] = None,
        bars: int = 120,
        dpi: int = 130,
        figsize: Tuple[float, float] = (12.8, 8.0),
        brand: str = "GOLD M5 VIP",
    ) -> None:
        self.theme = theme or ChartTheme()
        self.bars = int(bars)
        self.dpi = int(dpi)
        self.figsize = figsize
        self.brand = brand
        self._style = self._build_style()

    # ------------------------------------------------------------------ style
    def _build_style(self) -> Dict[str, Any]:
        t = self.theme
        marketcolors = mpf.make_marketcolors(
            up=t.up,
            down=t.down,
            edge={"up": t.up, "down": t.down},
            wick={"up": t.up, "down": t.down},
            volume={"up": t.up, "down": t.down},
            ohlc={"up": t.up, "down": t.down},
            alpha=0.95,
        )
        return mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=marketcolors,
            facecolor=t.panel_background,
            figcolor=t.background,
            edgecolor=t.grid,
            gridcolor=t.grid,
            gridstyle="--",
            gridaxis="both",
            y_on_right=True,
            rc={
                "axes.labelcolor": t.text,
                "axes.edgecolor": t.grid,
                "xtick.color": t.text,
                "ytick.color": t.text,
                "text.color": t.text,
                "axes.titlecolor": t.text,
                "font.size": 9.5,
                "figure.facecolor": t.background,
                "savefig.facecolor": t.background,
                "legend.facecolor": t.panel_background,
                "legend.edgecolor": t.grid,
                "grid.alpha": 0.35,
            },
        )

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _ohlc_frame(df: pd.DataFrame) -> pd.DataFrame:
        """mplfinance wants title-case columns and a DatetimeIndex."""
        needed = {"open", "high", "low", "close"}
        if not needed.issubset(df.columns):
            raise KeyError(f"frame must contain {sorted(needed)}")
        out = df.rename(
            columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            }
        )
        if "Volume" not in out.columns:
            out["Volume"] = 0.0
        if not isinstance(out.index, pd.DatetimeIndex):
            out.index = pd.to_datetime(out.index)
        return out[["Open", "High", "Low", "Close", "Volume"]]

    def _ema_addplots(self, window: pd.DataFrame, ema_columns: Sequence[str]) -> List[Any]:
        colors = [self.theme.ema_fast, self.theme.ema_slow, self.theme.ema_extra]
        plots: List[Any] = []
        for idx, col in enumerate(ema_columns):
            if col not in window.columns or window[col].isna().all():
                continue
            plots.append(
                mpf.make_addplot(
                    window[col],
                    panel=0,
                    color=colors[idx % len(colors)],
                    width=1.25,
                    label=col.upper().replace("_", " "),
                )
            )
        return plots

    def _band_addplots(self, window: pd.DataFrame) -> List[Any]:
        plots: List[Any] = []
        for col, style in (("bb_upper", "--"), ("bb_mid", ":"), ("bb_lower", "--")):
            if col in window.columns and not window[col].isna().all():
                plots.append(
                    mpf.make_addplot(
                        window[col],
                        panel=0,
                        color=self.theme.band,
                        width=0.9,
                        linestyle=style,
                    )
                )
        return plots

    def _oscillator_addplots(self, window: pd.DataFrame, oscillator: str) -> List[Any]:
        t = self.theme
        plots: List[Any] = []
        if oscillator == "macd" and "macd" in window.columns:
            hist = window["macd_hist"].fillna(0.0)
            colors = [t.up if v >= 0 else t.down for v in hist.to_numpy()]
            plots.extend(
                [
                    mpf.make_addplot(
                        hist, panel=1, type="bar", width=0.7, color=colors,
                        alpha=0.75, ylabel="MACD",
                    ),
                    mpf.make_addplot(window["macd"], panel=1, color=t.macd_line, width=1.2),
                    mpf.make_addplot(
                        window["macd_signal"], panel=1, color=t.macd_signal, width=1.1
                    ),
                ]
            )
        elif "rsi" in window.columns:
            level_70 = pd.Series(70.0, index=window.index)
            level_30 = pd.Series(30.0, index=window.index)
            level_50 = pd.Series(50.0, index=window.index)
            plots.extend(
                [
                    mpf.make_addplot(
                        window["rsi"], panel=1, color=t.rsi_line, width=1.35, ylabel="RSI(14)"
                    ),
                    mpf.make_addplot(level_70, panel=1, color=t.down, width=0.8, linestyle="--"),
                    mpf.make_addplot(level_50, panel=1, color=t.grid, width=0.7, linestyle=":"),
                    mpf.make_addplot(level_30, panel=1, color=t.up, width=0.8, linestyle="--"),
                ]
            )
        return plots

    # ----------------------------------------------------------------- public
    def render_signal_chart(
        self,
        df: pd.DataFrame,
        *,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        side: Optional[str] = None,
        entry: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        ema_columns: Sequence[str] = ("ema_9", "ema_21"),
        oscillator: str = "macd",
        strategy_name: str = "",
        subtitle: str = "",
        bars: Optional[int] = None,
        score: Optional[int] = None,
    ) -> Optional[io.BytesIO]:
        """Render a 2-panel dark chart and return a rewound PNG buffer.

        Panel 0: candles + EMAs (+ Bollinger bands when present) + entry/SL/TP
                 horizontal lines with right-edge price labels, tinted profit /
                 risk zones and an R-multiple ladder.
        Panel 1: MACD histogram + lines, or the RSI oscillator with 70/30 rails.
        Returns ``None`` (never raises) if rendering fails, so a chart problem
        can never block a live signal.
        """
        try:
            with _RENDER_LOCK:
                return self._render(
                    df, symbol=symbol, timeframe=timeframe, side=side, entry=entry,
                    sl=sl, tp=tp, ema_columns=ema_columns, oscillator=oscillator,
                    strategy_name=strategy_name, subtitle=subtitle,
                    bars=bars or self.bars, score=score,
                )
        except Exception as exc:
            logger.exception("chart rendering failed: {}", exc)
            return None

    def _render(
        self,
        df: pd.DataFrame,
        *,
        symbol: str,
        timeframe: str,
        side: Optional[str],
        entry: Optional[float],
        sl: Optional[float],
        tp: Optional[float],
        ema_columns: Sequence[str],
        oscillator: str,
        strategy_name: str,
        subtitle: str,
        bars: int,
        score: Optional[int] = None,
    ) -> io.BytesIO:
        window = df.iloc[-int(bars):].copy()
        ohlc = self._ohlc_frame(window)

        addplots: List[Any] = []
        addplots.extend(self._ema_addplots(window, ema_columns))
        addplots.extend(self._band_addplots(window))
        addplots.extend(self._oscillator_addplots(window, oscillator))

        t = self.theme
        hlines: List[float] = []
        hcolors: List[str] = []
        hstyles: List[str] = []
        labels: List[Tuple[float, str, str]] = []
        for value, color, tag in (
            (entry, t.entry, "ENTRY"),
            (tp, t.take_profit, "TP"),
            (sl, t.stop_loss, "SL"),
        ):
            if value is not None and float(value) > 0:
                hlines.append(float(value))
                hcolors.append(color)
                hstyles.append("-" if tag == "ENTRY" else "--")
                labels.append((float(value), tag, color))

        direction = (side or "").upper()
        arrow = "▲" if direction == "BUY" else ("▼" if direction == "SELL" else "")
        title = f"  {symbol}  ·  {timeframe}  {arrow} {direction}".rstrip()
        if strategy_name:
            title += f"   [{strategy_name}]"
        if score is not None:
            title += f"   ·  SCORE {int(score)}/100"

        plot_kwargs: Dict[str, Any] = dict(
            type="candle",
            style=self._style,
            volume=False,
            figsize=self.figsize,
            figscale=1.0,
            returnfig=True,
            tight_layout=True,
            xrotation=15,
            datetime_format="%m-%d %H:%M",
            ylabel="Price (USD)",
            title=title,
            warn_too_much_data=len(ohlc) + 10,
        )
        if addplots:
            plot_kwargs["addplot"] = addplots
            plot_kwargs["panel_ratios"] = (3, 1)
        if hlines:
            plot_kwargs["hlines"] = dict(
                hlines=hlines, colors=hcolors, linestyle=hstyles, linewidths=1.1, alpha=0.95
            )

        fig, axlist = mpf.plot(ohlc, **plot_kwargs)
        try:
            main_ax = axlist[0]
            main_ax.set_facecolor(t.panel_background)

            # -------------------------------------------- cinematic overlays
            entry_v = float(entry) if entry else None
            sl_v = float(sl) if sl else None
            tp_v = float(tp) if tp else None
            if entry_v and sl_v and tp_v:
                risk = abs(entry_v - sl_v)
                # tinted profit / risk zones
                main_ax.axhspan(
                    min(entry_v, tp_v), max(entry_v, tp_v),
                    color=t.take_profit, alpha=0.07, zorder=0,
                )
                main_ax.axhspan(
                    min(entry_v, sl_v), max(entry_v, sl_v),
                    color=t.stop_loss, alpha=0.09, zorder=0,
                )
                # R-multiple ladder between SL and TP
                if risk > 0:
                    lo, hi = min(sl_v, tp_v), max(sl_v, tp_v)
                    for k in (1, 2, 3):
                        for sign, lvl in ((1, entry_v + k * risk), (-1, entry_v - k * risk)):
                            if lo < lvl < hi and abs(lvl - entry_v) > 1e-9:
                                main_ax.axhline(
                                    lvl, color=t.grid, linewidth=0.7,
                                    linestyle=":", alpha=0.65, zorder=1,
                                )
                                main_ax.text(
                                    0.6, lvl, f"+{k}R" if sign > 0 else f"-{k}R",
                                    color=t.band, fontsize=7.5, va="center", ha="left",
                                    alpha=0.9, zorder=2,
                                    fontweight="bold",
                                )
                # direction badge on the last candle
                if direction in ("BUY", "SELL") and len(ohlc):
                    last_i = len(ohlc) - 1
                    last_close = float(ohlc["Close"].iloc[-1])
                    colour = t.up if direction == "BUY" else t.down
                    offset = risk * 0.35 if risk > 0 else abs(last_close) * 0.001
                    y_pos = last_close - offset if direction == "BUY" else last_close + offset
                    main_ax.text(
                        last_i - 1, y_pos, f"{arrow} {direction}",
                        color=colour, fontsize=13, fontweight="bold",
                        ha="right", va="center", alpha=0.95, zorder=3,
                        bbox=dict(
                            boxstyle="round,pad=0.25", facecolor="#0B0E14",
                            edgecolor=colour, alpha=0.55,
                        ),
                    )

            # right-edge price tags for entry / TP / SL
            x_right = len(ohlc) - 1
            for price, tag, color in labels:
                main_ax.text(
                    x_right + 0.6,
                    price,
                    f" {tag} {price:,.2f} ",
                    color="#0B0E14",
                    fontsize=8.5,
                    fontweight="bold",
                    va="center",
                    ha="left",
                    bbox=dict(boxstyle="round,pad=0.28", facecolor=color, edgecolor="none", alpha=0.95),
                    clip_on=False,
                )

            if subtitle:
                fig.text(
                    0.012, 0.965, subtitle, color=t.text, fontsize=8.8,
                    ha="left", va="top", alpha=0.85,
                )
            fig.text(
                0.5, 0.5, self.brand, color=t.watermark, fontsize=42,
                ha="center", va="center", alpha=0.13, zorder=0, fontweight="bold",
            )
            for ax in axlist:
                ax.tick_params(colors=t.text, labelsize=8.5)

            buffer = io.BytesIO()
            fig.savefig(
                buffer,
                format="png",
                dpi=self.dpi,
                facecolor=t.background,
                bbox_inches="tight",
                pad_inches=0.22,
            )
            buffer.seek(0)
            logger.debug("chart rendered in-memory ({:.1f} KB)", len(buffer.getbuffer()) / 1024)
            return buffer
        finally:
            plt.close(fig)   # non-negotiable: Agg figures leak otherwise

    def render_from_signal(
        self,
        df: pd.DataFrame,
        signal: Any,
        *,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        ema_columns: Sequence[str] = ("ema_9", "ema_21"),
        subtitle: str = "",
    ) -> Optional[io.BytesIO]:
        """Convenience wrapper taking a :class:`strategies.base.Signal`."""
        meta = getattr(signal, "meta", None) or {}
        return self.render_signal_chart(
            df,
            symbol=symbol,
            timeframe=timeframe,
            side=getattr(signal, "side", None),
            entry=getattr(signal, "entry", 0.0) or None,
            sl=getattr(signal, "sl", 0.0) or None,
            tp=getattr(signal, "tp", 0.0) or None,
            ema_columns=ema_columns,
            oscillator=getattr(signal, "oscillator", "macd"),
            strategy_name=getattr(signal, "strategy", ""),
            subtitle=subtitle or getattr(signal, "reason", ""),
            score=meta.get("score"),
        )

    def render_equity_curve(
        self,
        equity: Sequence[float],
        *,
        title: str = "Backtest Equity Curve",
        initial_balance: float = 533.0,
    ) -> Optional[io.BytesIO]:
        """Small dark equity/drawdown chart for the Telegram backtest card."""
        try:
            with _RENDER_LOCK:
                t = self.theme
                series = np.asarray(list(equity), dtype="float64")
                if series.size == 0:
                    return None
                peak = np.maximum.accumulate(series)
                drawdown = np.where(peak > 0, (series - peak) / peak * 100.0, 0.0)

                fig, (ax1, ax2) = plt.subplots(
                    2, 1, figsize=(11.0, 6.4), sharex=True,
                    gridspec_kw={"height_ratios": [3, 1]},
                    facecolor=t.background,
                )
                for ax in (ax1, ax2):
                    ax.set_facecolor(t.panel_background)
                    ax.grid(color=t.grid, linestyle="--", alpha=0.35)
                    ax.tick_params(colors=t.text, labelsize=8.5)
                    for spine in ax.spines.values():
                        spine.set_color(t.grid)

                colour = t.up if series[-1] >= initial_balance else t.down
                ax1.plot(series, color=colour, linewidth=1.6)
                ax1.fill_between(range(series.size), initial_balance, series, color=colour, alpha=0.14)
                ax1.axhline(initial_balance, color=t.text, linewidth=0.9, linestyle=":", alpha=0.7)
                ax1.set_title(title, color=t.text, fontsize=12, fontweight="bold")
                ax1.set_ylabel("Balance ($)", color=t.text)

                ax2.fill_between(range(drawdown.size), drawdown, 0.0, color=t.down, alpha=0.45)
                ax2.set_ylabel("DD (%)", color=t.text)
                ax2.set_xlabel("Closed trades", color=t.text)

                fig.tight_layout()
                buffer = io.BytesIO()
                fig.savefig(buffer, format="png", dpi=self.dpi, facecolor=t.background, bbox_inches="tight")
                plt.close(fig)
                buffer.seek(0)
                return buffer
        except Exception as exc:
            logger.exception("equity curve rendering failed: {}", exc)
            return None
