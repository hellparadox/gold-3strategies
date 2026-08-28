#!/usr/bin/env python3
"""Live trading supervisor for the XAUUSD M5 bot.

Threads
-------
* main thread        : the 1-second execution loop (MT5 calls only)
* mt5-heartbeat      : link watchdog inside :class:`MT5Client`
* telegram-loop      : PTB v20 application + its own asyncio loop

The only cross-thread traffic is (a) the Telegram controller pulling telemetry
through :class:`BotBridge` hooks, and (b) this module pushing broadcasts into
the bot loop.  Shared mutable state lives behind ``self._lock``.
"""
from __future__ import annotations

import argparse
import os
import re
import signal as os_signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from core import Settings, setup_logging
from core.chart_generator import ChartGenerator
from core.database import Database
from core.mt5_client import TIMEFRAME_SECONDS, MT5Client, MT5Config
from core.risk_manager import (
    ManageAction,
    PositionView,
    RiskConfig,
    RiskManager,
    SymbolSpec,
    TradeLevels,
)
from core.telegram_bot import BotBridge, TelegramConfig, TelegramController
from strategies import available_strategies, build_from_settings, build_strategy
from strategies.base import BaseStrategy, Signal


@dataclass
class TrackedPosition:
    """Local mirror of an open position, carrying our management flags."""

    ticket: int
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    atr: float
    breakeven_done: bool = False
    trailing_active: bool = False
    opened_at: datetime = field(default_factory=datetime.now)

    def to_view(self) -> PositionView:
        return PositionView(
            ticket=self.ticket,
            side="BUY" if self.side == "BUY" else "SELL",
            volume=self.volume,
            price_open=self.price_open,
            sl=self.sl,
            tp=self.tp,
            breakeven_done=self.breakeven_done,
            trailing_active=self.trailing_active,
        )


class LiveBot:
    """The execution engine."""

    def __init__(self, settings: Settings, use_telegram: bool = True) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._stop = threading.Event()

        # --- config -------------------------------------------------------
        self.tf_trigger = str(settings.get("timeframes.trigger", "M5"))
        self.tf_mid = str(settings.get("timeframes.intermediate", "M15"))
        self.tf_macro = str(settings.get("timeframes.macro", "H1"))
        self.bars_to_load = int(settings.get("engine.bars_to_load", 600))
        self.loop_sleep = float(settings.get("engine.loop_sleep_seconds", 1.0))
        self.session_start = int(settings.get("session.start_hour", 2))
        self.session_end = int(settings.get("session.end_hour", 24))
        self.max_spread = float(settings.get("session.max_spread_points", 40))
        self.trade_weekdays = list(settings.get("session.trade_weekdays", [0, 1, 2, 3, 4]) or [])

        # --- components ---------------------------------------------------
        self.db = Database(str(settings.get("database.path", "subscriptions.db")))
        self.risk_config = RiskConfig.from_settings(settings)
        self.risk = RiskManager(self.risk_config, SymbolSpec.gold_default())
        self.strategy: BaseStrategy = build_from_settings(settings)
        self.charts = ChartGenerator()

        self.telegram: Optional[TelegramController] = None
        if use_telegram:
            tg_config = TelegramConfig.from_settings(settings)
            self.telegram = TelegramController(tg_config, self.db, self._build_bridge())

        self.client = MT5Client(
            MT5Config.from_settings(settings),
            on_state_change=self._on_link_change,
        )

        from core.news_filter import NewsFilter, NewsFilterConfig

        # FIX(#3): settings یک شیء Settings است و attribute «raw» ندارد؛
        # خط قبلی همیشه dict خالی برمی‌گرداند و بخش news_filter در yaml
        # بی‌صدا نادیده گرفته می‌شد.
        news_sec = settings.section("news_filter")

        news_config = NewsFilterConfig(
            enabled=bool(news_sec.get("enabled", True)),
            currencies=list(news_sec.get("currencies", ["USD"])),
            min_impact=str(news_sec.get("min_impact", "High")),
            pause_minutes_before=int(news_sec.get("pause_minutes_before", 5)),
            pause_minutes_after=int(news_sec.get("pause_minutes_after", 5)),
            cache_refresh_hours=float(news_sec.get("cache_refresh_hours", 1.0)),
            network_timeout_seconds=float(news_sec.get("network_timeout_seconds", 5.0)),
        )
        self.news_filter = NewsFilter(news_config)
        self.news_filter.start()   # cold-start تقویم اخبار (fail-safe)

        # --- runtime state ------------------------------------------------
        self.running = True                      # kill switch (admin toggle)
        self.last_signal_bar: Optional[pd.Timestamp] = None
        self.tracked: Dict[int, TrackedPosition] = {}
        self.current_atr: float = 0.0
        self.last_backtest: Optional[BacktestResult] = None
        self.cycle = 0
        self.signals_sent = 0
        self.started_at = datetime.now()

        # --- daily guard state -----------------------------------------------
        self.max_daily_loss_pct = float(settings.get("risk.max_daily_loss_pct", 10.0))
        self.max_daily_trades = int(settings.get("risk.max_daily_trades", 10))
        self.daily_date = None
        self.day_start_balance = 0.0
        self.daily_realized_pnl = 0.0
        self.daily_trades = 0
        self.daily_halt_notified = False

    # ==================================================================== hooks
    def _build_bridge(self) -> BotBridge:
        return BotBridge(
            telemetry=self._hook_telemetry,
            price=self._hook_price,
            is_running=lambda: self.running,
            toggle_bot=self._hook_toggle,
            get_strategy=lambda: self.strategy.name,
            set_strategy=self._hook_set_strategy,
            available_strategies=available_strategies,
            strategy_description=lambda: self.strategy.describe(),
            get_risk=lambda: self.risk_config.risk_percent,
            set_risk=self._hook_set_risk,
            run_backtest=self._hook_run_backtest,
            last_backtest=lambda: self.last_backtest,
            equity_chart=self._hook_equity_chart,
            get_cooldown=self._hook_get_cooldown,
            set_cooldown=self._hook_set_cooldown,
        )

    # FIX(#5): /cooldown قبلاً هیچ هوکی در bridge نداشت و بی‌آنکه چیزی تغییر
    # کند «موفق» جواب می‌داد. cooldown_bars در زمان prepare خوانده می‌شود، پس
    # تغییر زندهٔ پارامتر بدون ری‌استارت اثر می‌گذارد.
    def _hook_get_cooldown(self) -> int:
        with self._lock:
            return int(self.strategy.params.get("cooldown_bars", 0) or 0)

    def _hook_set_cooldown(self, bars: int) -> int:
        value = max(0, min(int(bars), 500))
        with self._lock:
            self.strategy.params["cooldown_bars"] = value
        return value

    def _hook_telemetry(self) -> Dict[str, Any]:
        data = self.client.telemetry()
        with self._lock:
            data.update(
                engine_running=self.running,
                strategy=self.strategy.name,
                risk_percent=self.risk_config.risk_percent,
                signals_sent=self.signals_sent,
                uptime=str(datetime.now() - self.started_at).split(".")[0],
                atr=round(self.current_atr, 3),
            )
        return data

    def _hook_price(self) -> Dict[str, Any]:
        tick = self.client.get_tick()
        info = self.client.symbol_info()
        if tick is None or info is None:
            return {}
        return {
            "symbol": self.client.symbol,
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread_points": float((tick.ask - tick.bid) / info.point) if info.point else 0.0,
            "server_time": datetime.fromtimestamp(int(tick.time)).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _hook_toggle(self, state: Optional[bool] = None) -> bool:
        with self._lock:
            self.running = (not self.running) if state is None else bool(state)
            logger.warning("engine kill-switch -> {}", "RUNNING" if self.running else "PAUSED")
            return self.running

    def _persist_active_strategy(self, name: str) -> bool:
        """Rewrite `strategy.active` in settings.yaml, preserving comments."""
        path = getattr(self.settings, "path", None)
        if path is None:
            logger.error("cannot persist strategy: settings path unknown")
            return False
        try:
            text = path.read_text(encoding="utf-8")
            new_text, count = re.subn(
                r'(\n\s*active:\s*")[^"]*(")',
                lambda m: m.group(1) + str(name) + m.group(2),
                text,
                count=1,
            )
            if count != 1:
                logger.error("cannot persist strategy: 'active:' key not found in {}", path)
                return False
            path.write_text(new_text, encoding="utf-8")
            return True
        except Exception as e:
            logger.error("persisting strategy failed: {}", e)
            return False

    def _schedule_restart(self, delay: float = 1.5) -> None:
        """Graceful self-restart: stop main loop, relaunch a fresh process."""

        def _worker() -> None:
            time.sleep(delay)
            logger.warning("RESTART requested -> shutting down and relaunching…")
            self._stop.set()
            threading.main_thread().join(timeout=25.0)
            try:
                flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                subprocess.Popen(
                    [sys.executable, "main_live.py"],
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    creationflags=flags,
                )
                logger.success("new instance launched")
            except Exception as e:
                logger.error("relaunch failed: {}", e)
            time.sleep(1.0)
            os._exit(0)

        threading.Thread(target=_worker, daemon=True).start()

    def _hook_set_strategy(self, name: str) -> bool:
        try:
            params = self.settings.get(f"strategy.params.{name}", {}) or {}
            build_strategy(name, params, self.risk_config.atr_period)
        except KeyError:
            return False

        if not self._persist_active_strategy(name):
            return False

        with self._lock:
            self.settings.set("strategy.active", name)
        logger.warning("strategy -> {} persisted to settings.yaml; restarting…", name)
        if self.telegram is not None:
            self.telegram.notify_admins(
                f"\U0001F504 <b>Strategy changed → {name}</b>\n"
                "Persisted to settings.yaml.\n"
                "<i>Engine is restarting with the new strategy…</i>"
            )
        self._schedule_restart()
        return True

    def _hook_set_risk(self, percent: float) -> float:
        value = max(0.1, min(5.0, float(percent)))
        with self._lock:
            self.risk_config.risk_percent = value
        logger.warning("risk switched -> {:.2f}%", value)
        return value

    def _hook_run_backtest(self, name: Optional[str] = None) -> Optional[BacktestResult]:
        """Runs in a worker thread (dispatched by the Telegram controller)."""
        try:
            strategy = (
                self.strategy
                if not name
                else build_strategy(
                    name,
                    self.settings.get(f"strategy.params.{name}", {}) or {},
                    self.risk_config.atr_period,
                )
            )
            bars = int(self.settings.get("backtest.bars", 45_000))
            m5 = self.client.get_history_bars(self.tf_trigger, bars)
            if m5.empty:
                logger.error("backtest aborted: no history")
                return None
            m15 = self.client.get_history_bars(self.tf_mid, max(2_000, bars // 3))
            h1 = self.client.get_history_bars(self.tf_macro, max(1_000, bars // 12))
            engine = BacktestEngine(
                strategy=strategy,
                risk_config=self.risk_config,
                backtest_config=BacktestConfig.from_settings(self.settings),
                spec=self.risk.spec,
                symbol=self.client.symbol,
                timeframe=self.tf_trigger,
            )
            result = engine.run(m5, m15, h1)
            with self._lock:
                self.last_backtest = result
            return result
        except Exception as exc:
            logger.exception("backtest hook failed: {}", exc)
            return None

    def _hook_equity_chart(self, result: BacktestResult) -> Any:
        return self.charts.render_equity_curve(
            result.equity_curve,
            title=f"Equity · {result.strategy} · {result.bars:,} bars",
            initial_balance=result.initial_balance,
        )

    def _on_link_change(self, connected: bool, reason: str) -> None:
        """Called from the heartbeat thread; must never block."""
        if self.telegram is not None:
            self.telegram.notify_connection(connected, reason)

    # ================================================================== filters
    def _session_open(self, server_time: Optional[datetime]) -> bool:
        if server_time is None:
            return False
        if self.trade_weekdays and server_time.weekday() not in self.trade_weekdays:
            return False
        hour = server_time.hour
        if self.session_end >= 24:
            return hour >= self.session_start
        return self.session_start <= hour < self.session_end

    def _spread_ok(self) -> bool:
        spread = self.client.spread_points()
        if spread is None:
            return False
        if spread > self.max_spread:
            logger.debug("spread {:.0f}pts > {:.0f}pts, standing down", spread, self.max_spread)
            return False
        return True

    # ============================================================ market data
    def _load_frames(self) -> Dict[str, pd.DataFrame]:
        return {
            "m5": self.client.get_rates(self.tf_trigger, self.bars_to_load),
            "m15": self.client.get_rates(self.tf_mid, max(400, self.bars_to_load // 2)),
            "h1": self.client.get_rates(self.tf_macro, max(300, self.bars_to_load // 4)),
        }

    # =========================================================== position mgmt
    def _sync_positions(self) -> None:
        """Reconcile the local mirror with the broker and report closures."""
        live = {int(p.ticket): p for p in self.client.positions()}
        with self._lock:
            known = set(self.tracked)

        for ticket, pos in live.items():
            with self._lock:
                if ticket in self.tracked:
                    tracked = self.tracked[ticket]
                    tracked.sl = float(pos.sl or 0.0)
                    tracked.tp = float(pos.tp or 0.0)
                else:
                    self.tracked[ticket] = TrackedPosition(
                        ticket=ticket,
                        side="BUY" if int(pos.type) == 0 else "SELL",
                        volume=float(pos.volume),
                        price_open=float(pos.price_open),
                        sl=float(pos.sl or 0.0),
                        tp=float(pos.tp or 0.0),
                        atr=self.current_atr,
                    )
                    logger.info("adopted existing position #{}", ticket)

        for ticket in known - set(live):
            self._handle_closed_position(ticket)

    def _handle_closed_position(self, ticket: int) -> None:
        with self._lock:
            tracked = self.tracked.pop(ticket, None)
        if tracked is None:
            return
        profit = 0.0
        close_price = 0.0
        for deal in self.client.deals_since(tracked.opened_at - timedelta(minutes=5)):
            if int(getattr(deal, "position_id", 0)) == ticket:
                profit += float(getattr(deal, "profit", 0.0))
                profit += float(getattr(deal, "commission", 0.0))
                profit += float(getattr(deal, "swap", 0.0))
                if int(getattr(deal, "entry", 0)) == 1:      # DEAL_ENTRY_OUT
                    close_price = float(getattr(deal, "price", 0.0))
        outcome = "WIN" if profit > 0 else ("BE" if abs(profit) < 0.01 else "LOSS")
        with self._lock:
            self.daily_realized_pnl += profit
        self.db.close_signal(ticket, close_price, profit, outcome)
        logger.success("position #{} closed | {} | ${:.2f}", ticket, outcome, profit)
        if self.telegram is not None:
            self.telegram.broadcast_close(
                {
                    "ticket": ticket,
                    "side": tracked.side,
                    "lot": tracked.volume,
                    "profit": profit,
                    "close_price": close_price,
                    "outcome": outcome,
                }
            )

    def _manage_open_positions(self) -> None:
        """Sub-second break-even / trailing evaluation on live ticks."""
        with self._lock:
            tracked_list = list(self.tracked.values())
            atr = self.current_atr
        if not tracked_list or atr <= 0:
            return
        tick = self.client.get_tick()
        if tick is None:
            return

        for tracked in tracked_list:
            action: Optional[ManageAction] = self.risk.evaluate_position(
                tracked.to_view(), float(tick.bid), float(tick.ask), atr
            )
            if action is None:
                continue
            result = self.client.modify_sltp(action.ticket, sl=action.new_sl, tp=tracked.tp)
            if not result.ok:
                logger.warning("SL move rejected for #{}: {}", action.ticket, result.comment)
                continue
            with self._lock:
                current = self.tracked.get(action.ticket)
                if current is not None:
                    current.sl = action.new_sl
                    if action.kind == "breakeven":
                        current.breakeven_done = True
                    else:
                        current.trailing_active = True
                        current.breakeven_done = True
            logger.success("{} #{} SL {:.2f} -> {:.2f}", action.kind, action.ticket, action.old_sl, action.new_sl)
            if self.telegram is not None:
                self.telegram.broadcast_manage(
                    {
                        "kind": action.kind,
                        "ticket": action.ticket,
                        "side": tracked.side,
                        "old_sl": action.old_sl,
                        "new_sl": action.new_sl,
                        "reason": action.reason,
                    }
                )

    # ============================================================= daily guard
    def _sync_daily(self) -> None:
        """Roll counters at server midnight; rebuild today's P/L after a restart."""
        server_now = self.client.server_time()
        if server_now is None:
            return
        if self.daily_date is None:
            day_start = server_now.replace(hour=0, minute=0, second=0, microsecond=0)
            pnl = 0.0
            for deal in self.client.deals_since(day_start):
                if str(getattr(deal, "symbol", "")) == self.client.symbol and int(getattr(deal, "entry", 0)) == 1:
                    pnl += float(getattr(deal, "profit", 0.0))
                    pnl += float(getattr(deal, "commission", 0.0))
                    pnl += float(getattr(deal, "swap", 0.0))
            bal = self.client.balance() or 0.0
            with self._lock:
                self.daily_date = server_now.date()
                self.daily_realized_pnl = pnl
                self.daily_trades = 0
                self.day_start_balance = bal - pnl
                self.daily_halt_notified = False
            logger.info(
                "daily guard init | realized today=${:.2f} | day-start balance=${:.2f} | cap -{:.0f}%/${:.2f}",
                pnl, bal - pnl, self.max_daily_loss_pct, (bal - pnl) * self.max_daily_loss_pct / 100.0,
            )
            if pnl == 0.0 and (server_now - day_start).total_seconds() > 4 * 3600:
                logger.warning(
                    "daily guard init found 0 closed deals for a day >4h old; "
                    "terminal history may not be synced yet"
                )
            return
        if server_now.date() != self.daily_date:
            bal = self.client.balance() or 0.0
            with self._lock:
                self.daily_date = server_now.date()
                self.daily_realized_pnl = 0.0
                self.daily_trades = 0
                self.day_start_balance = bal
                self.daily_halt_notified = False
            logger.info("daily guard reset | new server day {} | balance=${:.2f}", server_now.date(), bal)

    def _daily_guard(self) -> tuple:
        """Return (halted, reason) for the day's loss/trade caps."""
        with self._lock:
            pnl = self.daily_realized_pnl
            trades = self.daily_trades
            start_bal = self.day_start_balance
        cap = start_bal * (self.max_daily_loss_pct / 100.0) if start_bal > 0 else 0.0
        if cap > 0 and pnl <= -cap:
            return True, f"daily loss ${pnl:.2f} hit cap -${cap:.2f} ({self.max_daily_loss_pct:.0f}% of ${start_bal:.2f})"
        if self.max_daily_trades > 0 and trades >= self.max_daily_trades:
            return True, f"daily trade cap reached ({trades}/{self.max_daily_trades})"
        return False, ""

    # ================================================================== entries
    def _on_new_closed_bar(self, frames: Dict[str, pd.DataFrame]) -> None:
        m5 = frames["m5"].iloc[:-1].copy()
        m15 = frames.get("m15").iloc[:-1].copy() if frames.get("m15") is not None and not frames.get("m15").empty else None
        h1 = frames.get("h1").iloc[:-1].copy() if frames.get("h1") is not None and not frames.get("h1").empty else None

        with self._lock:
            strategy = self.strategy

        prepared = strategy.prepare(m5, m15, h1)
        closed_atr = float(prepared["atr"].iloc[-1]) if "atr" in prepared and len(prepared) >= 1 else 0.0
        with self._lock:
            self.current_atr = closed_atr if closed_atr > 0 else self.current_atr

        if not self.running:
            return

        self._sync_daily()
        halted, halt_reason = self._daily_guard()
        if halted:
            with self._lock:
                first = not self.daily_halt_notified
                self.daily_halt_notified = True
            if first:
                logger.warning("DAILY GUARD ACTIVE -> {} | no new entries until next server day", halt_reason)
                if self.telegram is not None:
                    self.telegram.notify_admins(
                        "\U0001F6D1 <b>DAILY GUARD ACTIVE</b>\n"
                        f"{halt_reason}\n"
                        "<i>No new entries until next server day.</i>"
                    )
            return

        if not self._session_open(self.client.server_time()) or not self._spread_ok():
            return

        # FIX(#3): فیلتر اخبار واقعاً در مسیر ورود اعمال شود — موتور بک‌تست
        # اخبار را اعمال می‌کند (backtest.engine._news_ok) ولی لایو قبلاً
        # کاملاً نادیده‌اش می‌گرفت؛ نتیجه: رفتار لایو و بک‌تست یکی نبود.
        if self.news_filter is not None:
            news_blocked, news_reason = self.news_filter.is_news_active()
            if news_blocked:
                logger.info("📰 بلاک خبری فعال — ورود جدید ممنوع | {}", news_reason)
                return

        if self.client.open_position_count() >= self.risk_config.max_positions_per_symbol:
            return
        signal = strategy.evaluate(m5, m15, h1)
        if signal is None:
            return

        logger.info("🎯 سیگنال شناسایی شد: {} → {} ({})", signal.ref_time, signal.side, signal.reason)

        # FIX(news): فیلتر خبر روی دقیقاً همان کندلِ سیگنال اعمال شود تا با
        # بک‌تست یکی باشد؛ `_news_ok` در بک‌تست از timestamp همان bar استفاده
        # می‌کند، اینجا هم باید از signal.ref_time (نه زمان wall-clock).
        if self.news_filter is not None:
            news_blocked, news_reason = self.news_filter.is_news_active(signal.ref_time)
            if news_blocked:
                logger.info("📰 بلاک خبری فعال — ورود جدید ممنوع | {}", news_reason)
                return

        # FIX(#4): اول سفارش با سطوح واقعی ریسک‌منیجر ثبت می‌شود و بعد همان
        # سطوحِ اجراشده (entry/sl/tp/lot/ticket واقعی) به تلگرام می‌رود.
        # قبلاً broadcast سطوح متای استراتژی را با rr هاردکد ۲.۰ نشان می‌داد
        # که با معامله‌ای که واقعاً ثبت می‌شد یکی نبود؛ سفارش‌های ردشده هم
        # بی‌جهت به VIP مخابره می‌شدند.
        levels = self._open_trade(signal)
        if levels is None:
            return

        # 🟢 مخابره سیگنال با سطوح واقعی به همراه چارت
        if self.telegram is not None:
            payload = signal.as_dict()
            payload.update(
                symbol=self.client.symbol,
                entry=signal.entry,
                sl=signal.sl,
                tp=signal.tp,
                lot=signal.lot,
                risk_money=float(levels.risk_money),
                rr=self._signal_rr(signal),
                ticket=signal.ticket,
            )
            try:
                chart = self.charts.render_from_signal(
                    prepared,
                    signal,
                    symbol=self.client.symbol,
                    timeframe=self.tf_trigger,
                    ema_columns=self.strategy.ema_plot_columns(),
                    subtitle=signal.reason,
                )
                self.telegram.broadcast_signal(payload, chart)
                logger.success("📱 سیگنال (سطوح واقعی سفارش) به همراه چارت به تلگرام مخابره شد.")
            except Exception as e:
                logger.error("خطا در رسم چارت یا ارسال تلگرام: {}", e)

    @staticmethod
    def _signal_rr(signal: Signal) -> float:
        """R:R واقعی محاسبه‌شده از روی سطوح اجراشده."""
        sl_dist = abs(signal.entry - signal.sl)
        if sl_dist <= 0:
            return 0.0
        return abs(signal.tp - signal.entry) / sl_dist

    def _open_trade(self, signal: Signal) -> Optional[TradeLevels]:
        """سفارش را با سطوح ریسک‌منیجر ثبت می‌کند؛ در موفقیت levels برمی‌گرداند."""
        info = self.client.symbol_info(refresh=True)
        if info is None:
            logger.error("cannot size trade: no symbol info")
            return None
        self.risk.update_spec(SymbolSpec.from_mt5(info))

        tick = self.client.get_tick()
        if tick is None:
            return None
        entry_price = float(tick.ask if signal.is_long else tick.bid)
        balance = self.client.balance() or self.risk_config.base_balance
        levels = self.risk.build_levels(signal.side, entry_price, signal.atr, balance)
        if levels is None:
            return None

        result = self.client.send_market_order(
            signal.side, levels.lot, sl=levels.sl, tp=levels.tp,
            comment=f"{signal.strategy}",
        )
        if not result.ok:
            logger.warning("اردر در MT5 ثبت نشد (حالت فقط سیگنال یا ریجکت بروکر): {}", result.comment)
            return None

        # در صورت موفقیت‌آمیز بودن معامله لایو
        ticket = result.position or result.order
        for candidate in self.client.positions():
            if int(candidate.ticket) == int(ticket) or int(getattr(candidate, "identifier", 0)) == int(ticket):
                ticket = int(candidate.ticket)
                break

        signal.entry = float(result.price or levels.entry)
        signal.sl, signal.tp, signal.lot = levels.sl, levels.tp, levels.lot
        signal.ticket = int(ticket)

        with self._lock:
            self.tracked[int(ticket)] = TrackedPosition(
                ticket=int(ticket),
                side=signal.side,
                volume=levels.lot,
                price_open=signal.entry,
                sl=levels.sl,
                tp=levels.tp,
                atr=signal.atr,
            )
            self.signals_sent += 1
            self.daily_trades += 1

        self.db.log_signal(
            ticket=int(ticket), symbol=self.client.symbol, side=signal.side,
            strategy=signal.strategy, lot=levels.lot, entry=signal.entry,
            sl=levels.sl, tp=levels.tp, atr=signal.atr, reason=signal.reason,
        )
        return levels

    # ===================================================================== run
    def run(self) -> None:
        setup_logging(self.settings)
        logger.info("=" * 72)
        logger.info("GOLD M5 BOT starting | {}", self.risk.describe())
        logger.info("=" * 72)

        if not self.client.connect():
            logger.error("initial MT5 connection failed; entering reconnect supervisor")
        else:
            self.risk.update_spec(SymbolSpec.from_mt5(self.client.symbol_info(refresh=True)))
            for attempt in range(1, 4):
                self._sync_daily()
                if self.daily_date is not None and self.daily_realized_pnl != 0.0:
                    break
                if attempt < 3:
                    logger.warning("daily guard init attempt {} returned no deals; retrying in 5s", attempt)
                    time.sleep(5.0)
        self.client.start_heartbeat()

        if self.telegram is not None:
            self.telegram.start()
            self.telegram.notify_admins(
                "\U0001F680 <b>GOLD M5 BOT started</b>\n"
                f"Symbol: <code>{self.client.symbol}</code>\n"
                f"Strategy: <b>{self.strategy.name}</b>\n"
                f"Risk: <b>{self.risk_config.risk_percent:.2f}%</b>"
            )

        bar_seconds = TIMEFRAME_SECONDS.get(self.tf_trigger, 300)
        logger.info("entering main loop ({}s cadence, {}s bars)", self.loop_sleep, bar_seconds)

        while not self._stop.is_set():
            cycle_start = time.time()
            self.cycle += 1
            try:
                if not self.client.ensure_connected():
                    logger.error("offline; retrying in 10s")
                    self._stop.wait(10.0)
                    continue

                # (1) every second: reconcile + manage protective stops
                self._sync_positions()
                self._manage_open_positions()

                # (2) bar close only: hunt for new entries
                frames = self._load_frames()
                m5 = frames["m5"]
                if len(m5) >= self.strategy.min_bars():
                    closed_bar_time = m5.index[-2]
                    if closed_bar_time != self.last_signal_bar:
                        self.last_signal_bar = closed_bar_time
                        self._on_new_closed_bar(frames)
                elif self.cycle % 60 == 1:
                    logger.warning(
                        "warming up: {} / {} bars", len(m5), self.strategy.min_bars()
                    )

            except Exception as exc:
                logger.exception("main loop iteration failed: {}", exc)

            elapsed = time.time() - cycle_start
            self._stop.wait(max(0.05, self.loop_sleep - elapsed))

        self.shutdown()

    def stop(self, *_: Any) -> None:
        logger.warning("shutdown requested")
        self._stop.set()

    def shutdown(self) -> None:
        logger.info("shutting down…")
        if self.telegram is not None:
            self.telegram.notify_admins("\U0001F6D1 <b>GOLD M5 BOT stopped</b>")
            time.sleep(1.0)
            self.telegram.stop()
        self.client.shutdown()
        self.db.close()
        logger.success("clean shutdown complete")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="XAUUSD M5 live trading bot")
    parser.add_argument("--config", default=None, help="path to settings.yaml")
    parser.add_argument("--strategy", default=None, help="override the active strategy")
    parser.add_argument("--risk", type=float, default=None, help="override risk percent")
    parser.add_argument("--no-telegram", action="store_true", help="run headless")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    settings = Settings.load(args.config)
    setup_logging(settings)

    if args.strategy:
        settings.set("strategy.active", args.strategy)
    if args.risk is not None:
        settings.set("risk.risk_percent", float(args.risk))

    bot = LiveBot(settings, use_telegram=not args.no_telegram)
    os_signal.signal(os_signal.SIGINT, bot.stop)
    try:
        os_signal.signal(os_signal.SIGTERM, bot.stop)
    except (AttributeError, ValueError):      # not available on some Windows builds
        pass

    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
        bot.shutdown()
    except Exception as exc:
        logger.exception("fatal: {}", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
