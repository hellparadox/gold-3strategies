"""Event-driven backtest engine with broker-realistic cost modelling.

Quote model
-----------
MT5 rates are **bid** based.  We therefore synthesise the ask as
``bid + spread`` and route every fill through the correct side of the book:

============  =================================  ==========================
Event         Trigger (bid series)               Fill price
============  =================================  ==========================
BUY entry     next bar open                      ask = open + spread + slip
BUY SL/TP     low <= sl  /  high >= tp           sl - slip  /  tp
SELL entry    next bar open                      bid = open - slip
SELL SL/TP    high >= sl - spread / low <= tp - spread   sl + slip  /  tp
============  =================================  ==========================

When a bar contains both the SL and the TP level we always assume the **stop**
filled first.  That is the pessimistic assumption and it keeps reported win
rates honest.
"""
from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from core.risk_manager import PositionView, RiskConfig, RiskManager, SymbolSpec
from strategies.base import BaseStrategy

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "BacktestTrade",
    "HistoricalNewsChecker",
]

ExitReason = Literal["tp", "sl", "breakeven", "trailing", "eod", "end_of_data"]


# ============================================================================
# HISTORICAL NEWS CHECKER (Ultra-fast O(log N) binary search lookup)
# ============================================================================
class HistoricalNewsChecker:
    """Fast news blackout checker for historical bars using binary search."""

    def __init__(
        self,
        filepath: str = "data/historical_news.json",
        before_minutes: int = 5,
        after_minutes: int = 5,
        enabled: bool = True,
        server_utc_offset_hours: float = 0.0,
    ) -> None:
        self.enabled = enabled
        self.before_sec = before_minutes * 60
        self.after_sec = after_minutes * 60
        # FIX(tz): کندل‌های MT5 به وقت سرور بروکر هستند (مثلاً UTC+3)؛ برای
        # مقایسه با رویدادهای UTC باید آفست سرور کم شود، وگرنه پنجره‌های
        # بلاک چند ساعت جابه‌جا می‌شوند و فیلتر هیچ‌وقت بلاک نمی‌کند.
        self.offset_sec = server_utc_offset_hours * 3600.0
        self.event_timestamps: List[float] = []

        if not self.enabled:
            return

        p = Path(filepath)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ts_list: List[float] = []
                    for item in data:
                        t_str = item.get("time") or item.get("date")
                        if t_str:
                            dt = datetime.fromisoformat(str(t_str).replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            ts_list.append(dt.timestamp())
                    self.event_timestamps = sorted(ts_list)
                logger.info(
                    "loaded {} historical news events for backtesting blackout",
                    len(self.event_timestamps),
                )
            except Exception as exc:
                logger.warning("failed loading historical news from {}: {}", filepath, exc)
        else:
            logger.debug("historical news file not found at {}", filepath)

    def is_blocked(self, bar_time: pd.Timestamp) -> bool:
        """Check if bar_time falls within any event blackout window."""
        if not self.enabled or not self.event_timestamps:
            return False

        # Get UTC epoch timestamp.
        # FIX(tz): pandas برای Timestamp خالی (naive) epoch را «انگار UTC»
        # می‌گیرد؛ چون مقدار واقعاً ساعت سرور است، آفست سرور کم می‌شود.
        ts = bar_time.timestamp() - self.offset_sec
        idx = bisect.bisect_left(self.event_timestamps, ts)

        # Check candidate timestamps around insertion point
        candidates: List[float] = []
        if idx > 0:
            candidates.append(self.event_timestamps[idx - 1])
        if idx < len(self.event_timestamps):
            candidates.append(self.event_timestamps[idx])
        if idx + 1 < len(self.event_timestamps):
            candidates.append(self.event_timestamps[idx + 1])

        for ev_ts in candidates:
            if (ev_ts - self.before_sec) <= ts <= (ev_ts + self.after_sec):
                return True
        return False


@dataclass
class BacktestConfig:
    initial_balance: float = 533.0
    spread_points: float = 25.0
    slippage_points: float = 3.0
    commission_per_lot: float = 6.0
    simulate_breakeven: bool = True
    simulate_trailing: bool = True
    bars_per_year: int = 74_880
    session_start_hour: int = 2
    session_end_hour: int = 24
    max_spread_points: float = 40.0
    trade_weekdays: Tuple[int, ...] = (0, 1, 2, 3, 4)
    respect_session: bool = True

    # News filter parameters
    news_filter_enabled: bool = True
    news_file_path: str = "data/historical_news.json"
    news_pause_before: int = 5
    news_pause_after: int = 5
    news_server_utc_offset_hours: float = 0.0

    @classmethod
    def from_settings(cls, settings: Any) -> "BacktestConfig":
        # Extract news filter config safely
        news_sec = (
            settings.get("news_filter", {})
            if hasattr(settings, "get")
            else getattr(settings, "news_filter", {})
        )
        if not isinstance(news_sec, dict):
            news_sec = {}

        return cls(
            initial_balance=float(settings.get("backtest.initial_balance", 533.0)),
            spread_points=float(settings.get("backtest.spread_points", 25.0)),
            slippage_points=float(settings.get("backtest.slippage_points", 3.0)),
            commission_per_lot=float(settings.get("backtest.commission_per_lot", 6.0)),
            simulate_breakeven=bool(settings.get("backtest.simulate_breakeven", True)),
            simulate_trailing=bool(settings.get("backtest.simulate_trailing", True)),
            bars_per_year=int(settings.get("backtest.bars_per_year", 74_880)),
            session_start_hour=int(settings.get("session.start_hour", 2)),
            session_end_hour=int(settings.get("session.end_hour", 24)),
            max_spread_points=float(settings.get("session.max_spread_points", 40.0)),
            trade_weekdays=tuple(settings.get("session.trade_weekdays", [0, 1, 2, 3, 4]) or []),
            news_filter_enabled=bool(
                news_sec.get("enabled", settings.get("news_filter.enabled", True))
            ),
            news_file_path=str(
                news_sec.get(
                    "historical_file",
                    settings.get("news_filter.historical_file", "data/historical_news.json"),
                )
            ),
            news_pause_before=int(
                news_sec.get(
                    "pause_minutes_before",
                    settings.get("news_filter.pause_minutes_before", 5),
                )
            ),
            news_pause_after=int(
                news_sec.get(
                    "pause_minutes_after",
                    settings.get("news_filter.pause_minutes_after", 5),
                )
            ),
            news_server_utc_offset_hours=float(
                news_sec.get(
                    "server_utc_offset_hours",
                    settings.get("news_filter.server_utc_offset_hours", 0.0),
                )
            ),
        )


@dataclass
class BacktestTrade:
    ticket: int
    side: Literal["BUY", "SELL"]
    lot: float
    entry_time: pd.Timestamp
    entry_price: float
    sl: float
    tp: float
    atr: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: ExitReason = "end_of_data"
    gross_profit: float = 0.0
    commission: float = 0.0
    net_profit: float = 0.0
    balance_after: float = 0.0
    bars_held: int = 0
    breakeven_done: bool = False
    trailing_active: bool = False
    mfe: float = 0.0     # max favourable excursion, price units
    mae: float = 0.0     # max adverse excursion, price units

    @property
    def is_win(self) -> bool:
        return self.net_profit > 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ticket": self.ticket, "side": self.side, "lot": self.lot,
            "entry_time": str(self.entry_time), "entry_price": self.entry_price,
            "sl": self.sl, "tp": self.tp, "atr": round(self.atr, 3),
            "exit_time": str(self.exit_time), "exit_price": self.exit_price,
            "exit_reason": self.exit_reason, "gross": round(self.gross_profit, 2),
            "commission": round(self.commission, 2), "net": round(self.net_profit, 2),
            "balance_after": round(self.balance_after, 2), "bars_held": self.bars_held,
            "breakeven": self.breakeven_done, "trailing": self.trailing_active,
        }


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    timeframe: str
    bars: int
    period_start: Optional[pd.Timestamp]
    period_end: Optional[pd.Timestamp]
    initial_balance: float
    final_balance: float
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- rendering
    def summary_lines(self) -> List[str]:
        m = self.metrics
        return [
            f"Strategy        : {self.strategy}",
            f"Symbol / TF     : {self.symbol} {self.timeframe}",
            f"Bars tested     : {self.bars:,}",
            f"Period          : {self.period_start} -> {self.period_end}",
            f"Initial balance : ${self.initial_balance:,.2f}",
            f"Net balance     : ${self.final_balance:,.2f}",
            f"Total profit    : ${m.get('total_profit', 0.0):,.2f} "
            f"({m.get('return_pct', 0.0):.2f}%)",
            f"Total trades    : {m.get('total_trades', 0)}",
            f"Wins / Losses   : {m.get('wins', 0)} / {m.get('losses', 0)}",
            f"Win rate        : {m.get('win_rate', 0.0):.2f}%",
            f"Profit factor   : {m.get('profit_factor', 0.0):.2f}",
            f"Expected payoff : ${m.get('expected_payoff', 0.0):,.2f}",
            f"Max drawdown    : {m.get('max_drawdown_pct', 0.0):.2f}% "
            f"(${m.get('max_drawdown_money', 0.0):,.2f})",
            f"Sharpe ratio    : {m.get('sharpe_ratio', 0.0):.2f}",
            f"Avg win / loss  : ${m.get('avg_win', 0.0):,.2f} / ${m.get('avg_loss', 0.0):,.2f}",
            f"Largest win/loss: ${m.get('largest_win', 0.0):,.2f} / "
            f"${m.get('largest_loss', 0.0):,.2f}",
            f"Max consec W/L  : {m.get('max_consecutive_wins', 0)} / "
            f"{m.get('max_consecutive_losses', 0)}",
            f"Commission paid : ${m.get('total_commission', 0.0):,.2f}",
            f"Exits (tp/sl/be/trail): {m.get('exit_tp', 0)}/{m.get('exit_sl', 0)}/"
            f"{m.get('exit_breakeven', 0)}/{m.get('exit_trailing', 0)}",
        ]

    def to_text(self) -> str:
        return "\n".join(self.summary_lines())

    def to_telegram_card(self) -> str:
        m = self.metrics
        verdict = (
            "\U0001F7E9 PROFITABLE" if m.get("total_profit", 0.0) > 0 else "\U0001F7E5 UNPROFITABLE"
        )
        return (
            f"\U0001F9EA <b>Backtest Report · {self.strategy}</b>\n"
            f"<code>{self.symbol} {self.timeframe} · {self.bars:,} bars</code>\n"
            f"————————————————\n"
            f"\U0001F4B0 Balance: <b>${self.initial_balance:,.2f} → ${self.final_balance:,.2f}</b>\n"
            f"\U0001F4C8 Net P/L: <b>${m.get('total_profit', 0.0):,.2f}</b> "
            f"({m.get('return_pct', 0.0):+.2f}%)\n"
            f"\U0001F3AF Trades: <b>{m.get('total_trades', 0)}</b> · "
            f"W/L: {m.get('wins', 0)}/{m.get('losses', 0)}\n"
            f"✅ Win rate: <b>{m.get('win_rate', 0.0):.1f}%</b>\n"
            f"⚖️ Profit factor: <b>{m.get('profit_factor', 0.0):.2f}</b>\n"
            f"\U0001F4C9 Max DD: <b>{m.get('max_drawdown_pct', 0.0):.2f}%</b> "
            f"(${m.get('max_drawdown_money', 0.0):,.2f})\n"
            f"\U0001F4CA Sharpe: <b>{m.get('sharpe_ratio', 0.0):.2f}</b>\n"
            f"\U0001F4B5 Expected payoff: <b>${m.get('expected_payoff', 0.0):,.2f}</b>\n"
            f"————————————————\n"
            f"{verdict}"
        )


class BacktestEngine:
    """Replays a strategy bar-by-bar with live-identical risk arithmetic."""

    def __init__(
        self,
        strategy: BaseStrategy,
        risk_config: RiskConfig,
        backtest_config: Optional[BacktestConfig] = None,
        spec: Optional[SymbolSpec] = None,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
    ) -> None:
        self.strategy = strategy
        self.cfg = backtest_config or BacktestConfig()
        self.spec = spec or SymbolSpec.gold_default()
        self.risk = RiskManager(risk_config, self.spec)
        self.symbol = symbol
        self.timeframe = timeframe

        # Initialize Historical News Blackout Checker
        self.news_checker = HistoricalNewsChecker(
            filepath=self.cfg.news_file_path,
            before_minutes=self.cfg.news_pause_before,
            after_minutes=self.cfg.news_pause_after,
            enabled=self.cfg.news_filter_enabled,
            server_utc_offset_hours=self.cfg.news_server_utc_offset_hours,
        )

    # ------------------------------------------------------------------ helpers
    @property
    def _spread_price(self) -> float:
        return self.spec.price_from_points(self.cfg.spread_points)

    @property
    def _slip_price(self) -> float:
        return self.spec.price_from_points(self.cfg.slippage_points)

    def _session_ok(self, ts: pd.Timestamp) -> bool:
        if not self.cfg.respect_session:
            return True
        if self.cfg.trade_weekdays and ts.weekday() not in self.cfg.trade_weekdays:
            return False
        hour = int(ts.hour)
        start, end = self.cfg.session_start_hour, self.cfg.session_end_hour
        return start <= hour < end if end <= 24 else hour >= start

    def _news_ok(self, ts: pd.Timestamp) -> bool:
        """Return True if bar is outside economic blackout window."""
        return not self.news_checker.is_blocked(ts)

    def _money(self, price_distance: float, lot: float) -> float:
        return self.spec.money_per_lot(price_distance) * float(lot)

    # --------------------------------------------------------------------- run
    def run(
        self,
        m5: pd.DataFrame,
        m15: Optional[pd.DataFrame] = None,
        h1: Optional[pd.DataFrame] = None,
    ) -> BacktestResult:
        if m5 is None or m5.empty:
            raise ValueError("M5 data is required")

        logger.info(
            "backtest start | strategy={} bars={} spread={}pts commission=${}/lot news_filter={}",
            self.strategy.name,
            len(m5),
            self.cfg.spread_points,
            self.cfg.commission_per_lot,
            self.cfg.news_filter_enabled,
        )
        df = self.strategy.prepare_and_sign(m5, m15, h1)

        idx = df.index
        o = df["open"].to_numpy(dtype="float64")
        h = df["high"].to_numpy(dtype="float64")
        l = df["low"].to_numpy(dtype="float64")
        c = df["close"].to_numpy(dtype="float64")
        atr_arr = df["atr"].to_numpy(dtype="float64")
        sig = df["signal"].to_numpy(dtype="int8")

        n = len(df)
        start = max(self.strategy.min_bars(), 2)
        balance = float(self.cfg.initial_balance)
        equity_curve: List[float] = [balance]
        trades: List[BacktestTrade] = []

        spread = self._spread_price
        slip = self._slip_price
        commission = float(self.cfg.commission_per_lot)
        max_spread_price = self.spec.price_from_points(self.cfg.max_spread_points)
        spread_blocked = spread > max_spread_price
        if spread_blocked:
            logger.warning(
                "configured backtest spread ({}pts) exceeds the live filter ({}pts); "
                "live would never trade in these conditions",
                self.cfg.spread_points,
                self.cfg.max_spread_points,
            )

        open_trade: Optional[BacktestTrade] = None
        ticket_seq = 1

        for i in range(start, n):
            bar_time = idx[i]
            bar_open, bar_high, bar_low, bar_close = o[i], h[i], l[i], c[i]
            atr_now = atr_arr[i - 1]  # closed-bar ATR, never the forming one

            # ---------------------------------------------------- manage open
            if open_trade is not None:
                closed = self._process_open_bar(
                    open_trade,
                    bar_time,
                    bar_high,
                    bar_low,
                    bar_close,
                    atr_now,
                    spread,
                    slip,
                )
                if closed:
                    price_delta = (
                        open_trade.exit_price - open_trade.entry_price
                        if open_trade.side == "BUY"
                        else open_trade.entry_price - open_trade.exit_price
                    )
                    open_trade.gross_profit = self._money(price_delta, open_trade.lot) * (
                        1.0 if price_delta >= 0 else -1.0
                    )
                    open_trade.commission = commission * open_trade.lot
                    open_trade.net_profit = open_trade.gross_profit - open_trade.commission
                    balance += open_trade.net_profit
                    open_trade.balance_after = balance
                    trades.append(open_trade)
                    equity_curve.append(balance)
                    open_trade = None
                else:
                    open_trade.bars_held += 1

            # ------------------------------------------------------ new entry
            if open_trade is None and not spread_blocked:
                direction = int(sig[i - 1])  # signal from the CLOSED bar
                if direction != 0 and self._session_ok(bar_time) and self._news_ok(bar_time):
                    atr_entry = atr_arr[i - 1]
                    if np.isfinite(atr_entry) and atr_entry > 0:
                        side: Literal["BUY", "SELL"] = "BUY" if direction > 0 else "SELL"
                        fill = bar_open + spread + slip if side == "BUY" else bar_open - slip
                        levels = self.risk.build_levels(side, fill, float(atr_entry), balance)
                        if levels is not None:
                            open_trade = BacktestTrade(
                                ticket=ticket_seq,
                                side=side,
                                lot=levels.lot,
                                entry_time=bar_time,
                                entry_price=levels.entry,
                                sl=levels.sl,
                                tp=levels.tp,
                                atr=float(atr_entry),
                            )
                            ticket_seq += 1
                            # the entry bar itself can still stop us out
                            if self._process_open_bar(
                                open_trade,
                                bar_time,
                                bar_high,
                                bar_low,
                                bar_close,
                                atr_entry,
                                spread,
                                slip,
                                entry_bar=True,
                            ):
                                price_delta = (
                                    open_trade.exit_price - open_trade.entry_price
                                    if side == "BUY"
                                    else open_trade.entry_price - open_trade.exit_price
                                )
                                open_trade.gross_profit = self._money(
                                    price_delta, open_trade.lot
                                ) * (1.0 if price_delta >= 0 else -1.0)
                                open_trade.commission = commission * open_trade.lot
                                open_trade.net_profit = (
                                    open_trade.gross_profit - open_trade.commission
                                )
                                balance += open_trade.net_profit
                                open_trade.balance_after = balance
                                trades.append(open_trade)
                                equity_curve.append(balance)
                                open_trade = None

        # ------------------------------------------------- force-close leftover
        if open_trade is not None:
            last_close = c[n - 1]
            open_trade.exit_time = idx[n - 1]
            open_trade.exit_price = (
                last_close if open_trade.side == "BUY" else last_close + spread
            )
            open_trade.exit_reason = "end_of_data"
            price_delta = (
                open_trade.exit_price - open_trade.entry_price
                if open_trade.side == "BUY"
                else open_trade.entry_price - open_trade.exit_price
            )
            open_trade.gross_profit = self._money(price_delta, open_trade.lot) * (
                1.0 if price_delta >= 0 else -1.0
            )
            open_trade.commission = commission * open_trade.lot
            open_trade.net_profit = open_trade.gross_profit - open_trade.commission
            balance += open_trade.net_profit
            open_trade.balance_after = balance
            trades.append(open_trade)
            equity_curve.append(balance)

        metrics = self._compute_metrics(trades, equity_curve, n)
        result = BacktestResult(
            strategy=self.strategy.name,
            symbol=self.symbol,
            timeframe=self.timeframe,
            bars=n,
            period_start=idx[0] if n else None,
            period_end=idx[-1] if n else None,
            initial_balance=float(self.cfg.initial_balance),
            final_balance=balance,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
        )
        logger.success(
            "backtest done | {} trades | net ${:.2f} | WR {:.1f}% | PF {:.2f} | maxDD {:.2f}%",
            metrics.get("total_trades", 0),
            metrics.get("total_profit", 0.0),
            metrics.get("win_rate", 0.0),
            metrics.get("profit_factor", 0.0),
            metrics.get("max_drawdown_pct", 0.0),
        )
        return result

    # ----------------------------------------------------- per-bar management
    def _process_open_bar(
        self,
        trade: BacktestTrade,
        bar_time: pd.Timestamp,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        atr_value: float,
        spread: float,
        slip: float,
        entry_bar: bool = False,
    ) -> bool:
        """Resolve SL/TP for one bar, then advance BE/trailing. True == closed.

        Exit checks always use the stop level that was already in force at the
        start of the bar; protective-stop upgrades computed from this bar's
        extremes only take effect from the *next* bar.  That removes the
        optimism of letting a trail lock in profit on the same candle that
        reversed into the stop.
        """
        is_long = trade.side == "BUY"

        # excursions (for diagnostics)
        if is_long:
            trade.mfe = max(trade.mfe, bar_high - trade.entry_price)
            trade.mae = min(trade.mae, bar_low - trade.entry_price)
            sl_hit = bar_low <= trade.sl
            tp_hit = bar_high >= trade.tp
        else:
            trade.mfe = max(trade.mfe, trade.entry_price - (bar_low + spread))
            trade.mae = min(trade.mae, trade.entry_price - (bar_high + spread))
            sl_hit = (bar_high + spread) >= trade.sl
            tp_hit = (bar_low + spread) <= trade.tp

        if sl_hit:
            # pessimistic: stop wins any same-bar ambiguity
            trade.exit_time = bar_time
            trade.exit_price = trade.sl - slip if is_long else trade.sl + slip
            if trade.trailing_active:
                trade.exit_reason = "trailing"
            elif trade.breakeven_done:
                trade.exit_reason = "breakeven"
            else:
                trade.exit_reason = "sl"
            return True

        if tp_hit:
            trade.exit_time = bar_time
            trade.exit_price = trade.tp
            trade.exit_reason = "tp"
            return True

        # ------------------------------------------ protective stop management
        if not (self.cfg.simulate_breakeven or self.cfg.simulate_trailing):
            return False
        if not np.isfinite(atr_value) or atr_value <= 0:
            return False

        # bid/ask at the most favourable point reached inside the bar
        if is_long:
            best_bid, best_ask = bar_high, bar_high + spread
        else:
            best_bid, best_ask = bar_low, bar_low + spread

        view = PositionView(
            ticket=trade.ticket,
            side=trade.side,
            volume=trade.lot,
            price_open=trade.entry_price,
            sl=trade.sl,
            tp=trade.tp,
            breakeven_done=trade.breakeven_done,
            trailing_active=trade.trailing_active,
        )
        action = self.risk.evaluate_position(view, best_bid, best_ask, float(atr_value))
        if action is None:
            return False
        if action.kind == "breakeven" and not self.cfg.simulate_breakeven:
            return False
        if action.kind == "trailing" and not self.cfg.simulate_trailing:
            return False

        trade.sl = action.new_sl
        if action.kind == "breakeven":
            trade.breakeven_done = True
        else:
            trade.trailing_active = True
            trade.breakeven_done = True
        return False

    # ----------------------------------------------------------------- metrics
    def _compute_metrics(
        self,
        trades: Sequence[BacktestTrade],
        equity: Sequence[float],
        bars: int,
    ) -> Dict[str, Any]:
        initial = float(self.cfg.initial_balance)
        curve = np.asarray(list(equity), dtype="float64")
        total = len(trades)

        if total == 0:
            return {
                "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_profit": 0.0, "return_pct": 0.0, "profit_factor": 0.0,
                "expected_payoff": 0.0, "max_drawdown_pct": 0.0,
                "max_drawdown_money": 0.0, "sharpe_ratio": 0.0, "avg_win": 0.0,
                "avg_loss": 0.0, "largest_win": 0.0, "largest_loss": 0.0,
                "gross_profit": 0.0, "gross_loss": 0.0, "total_commission": 0.0,
                "max_consecutive_wins": 0, "max_consecutive_losses": 0,
                "avg_bars_held": 0.0, "exit_tp": 0, "exit_sl": 0,
                "exit_breakeven": 0, "exit_trailing": 0, "trades_per_day": 0.0,
            }

        nets = np.array([t.net_profit for t in trades], dtype="float64")
        wins_mask = nets > 0.0
        wins = int(wins_mask.sum())
        losses = int(total - wins)
        gross_profit = float(nets[wins_mask].sum())
        gross_loss = float(-nets[~wins_mask].sum())
        total_profit = float(nets.sum())

        # drawdown on the closed-trade equity curve
        peaks = np.maximum.accumulate(curve)
        dd_money = peaks - curve
        dd_pct = np.where(peaks > 0, dd_money / peaks * 100.0, 0.0)

        # Sharpe from per-trade returns, annualised by realised trade frequency
        balances_before = np.concatenate(([initial], curve[:-1]))[: len(nets)]
        with np.errstate(divide="ignore", invalid="ignore"):
            returns = np.where(balances_before > 0, nets / balances_before, 0.0)
        std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
        years = max(bars / float(self.cfg.bars_per_year), 1e-9)
        trades_per_year = total / years
        sharpe = (
            float(returns.mean() / std * math.sqrt(max(trades_per_year, 1.0)))
            if std > 0
            else 0.0
        )

        streak_w = streak_l = best_w = best_l = 0
        for is_win in wins_mask:
            if is_win:
                streak_w += 1
                streak_l = 0
                best_w = max(best_w, streak_w)
            else:
                streak_l += 1
                streak_w = 0
                best_l = max(best_l, streak_l)

        reasons = [t.exit_reason for t in trades]
        days = max(bars / 288.0, 1e-9)  # 288 M5 bars per 24h

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total * 100.0,
            "total_profit": total_profit,
            "return_pct": total_profit / initial * 100.0 if initial else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": (gross_profit / gross_loss)
            if gross_loss > 0
            else float("inf")
            if gross_profit > 0
            else 0.0,
            "expected_payoff": total_profit / total,
            "max_drawdown_money": float(dd_money.max()),
            "max_drawdown_pct": float(dd_pct.max()),
            "sharpe_ratio": sharpe,
            "avg_win": float(nets[wins_mask].mean()) if wins else 0.0,
            "avg_loss": float(nets[~wins_mask].mean()) if losses else 0.0,
            "largest_win": float(nets.max()),
            "largest_loss": float(nets.min()),
            "total_commission": float(sum(t.commission for t in trades)),
            "max_consecutive_wins": best_w,
            "max_consecutive_losses": best_l,
            "avg_bars_held": float(np.mean([t.bars_held for t in trades])),
            "exit_tp": reasons.count("tp"),
            "exit_sl": reasons.count("sl"),
            "exit_breakeven": reasons.count("breakeven"),
            "exit_trailing": reasons.count("trailing"),
            "trades_per_day": total / days,
            "final_lot_used": trades[-1].lot,
        }

    # ------------------------------------------------------------------ export
    @staticmethod
    def trades_to_frame(result: BacktestResult) -> pd.DataFrame:
        if not result.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.as_dict() for t in result.trades])