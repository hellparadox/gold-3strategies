#!/usr/bin/env python3
"""Live trading supervisor for the XAUUSD M5 bot.

Threads
-------
* main thread        : the 1-second execution loop (MT5 calls only)
* mt5-heartbeat      : link watchdog inside :class:`MT5Client`
* telegram-loop      : PTB v20 application + its own asyncio loop
* ai-gate            : optional LLM pre-trade gate worker (core/ai_gate)

The only cross-thread traffic is (a) the Telegram controller pulling telemetry
through :class:`BotBridge` hooks, and (b) this module pushing broadcasts into
the bot loop.  Shared mutable state lives behind ``self._lock``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal as os_signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from core import Settings, setup_logging
from core.ai_gate import AIGate, GateResult
from core.ai_gate.context import TradePlan, build_context, eet_offset_hours, server_offset_hours
from core.ai_gate.evaluate import ExitRules
from core.ai_gate.gate import hash_key
from core.chart_generator import ChartGenerator
from core.database import Database
from core.dashboard import DashboardServer
from core.daily_digest import build_daily_stats, render_daily_card
from core.mt5_client import TIMEFRAME_SECONDS, MT5Client, MT5Config, MT5ReadError
from core.risk_manager import (
    ManageAction,
    PositionView,
    RiskConfig,
    RiskManager,
    SymbolSpec,
    TradeLevels,
)
from core.signal_score import SignalScorer
from core.telegram_bot import BotBridge, TelegramConfig, TelegramController
from strategies import available_strategies, build_from_settings, build_strategy
from strategies.base import BaseStrategy, Signal


def _completed(result: Any) -> "Future[Any]":
    """A finished Future holding ``result`` (uniform handling of instant verdicts)."""
    fut: Future = Future()
    fut.set_result(result)
    return fut


def shadow_pl(side: str, entry: float, exit_price: float, lot: float = 0.01) -> float:
    """Signed P/L if the position had been held to ``exit_price``.

    BUY  → exit − entry
    SELL → entry − exit
    Contract size 100 (XAUUSD): dollars = price_diff × lot × 100.
    """
    if str(side).upper() == "BUY":
        diff = float(exit_price) - float(entry)
    else:
        diff = float(entry) - float(exit_price)
    return diff * float(lot) * 100.0


def shadow_outcome(pl: float) -> str:
    if pl > 0.01:
        return "WIN"
    if pl < -0.01:
        return "LOSS"
    return "BE"


def shadow_verdict_text(actual_profit: float, shadow_profit: float) -> str:
    """Compare manual-close P/L vs held-to-SL/TP P/L. |diff| < $0.5 → equal."""
    diff = float(actual_profit) - float(shadow_profit)
    if abs(diff) < 0.5:
        return "تفاوتی نداشت"
    if diff > 0:
        return "بستن دستی بهتر بود"
    return "نگه داشتن بهتر بود"


def format_shadow_verdict(actual_profit: float, shadow_profit: float) -> str:
    """Both numbers + comparison verdict on one line (auditable)."""
    verdict = shadow_verdict_text(actual_profit, shadow_profit)
    return (
        f"بستن دستی: {float(actual_profit):+,.2f}  |  "
        f"اگر نگه می‌داشتی: {float(shadow_profit):+,.2f}  →  {verdict}"
    )


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
    position_id: int = 0

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


@dataclass
class PlannedTrade:
    """Levels and execution identity for one signal, before any order is sent."""

    levels: TradeLevels
    entry_price: float
    tick: Any
    point: float
    scope: str
    signal_key: str


@dataclass
class PendingEntry:
    """A live-mode entry waiting for the AI gate verdict (main loop keeps running)."""

    signal: Signal
    planned: PlannedTrade
    prepared: Any
    future: Any
    key: str
    deadline: float


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
        # IMPROVE (2026-09-15): چاپ وضعیت مؤثر گیت‌های سمت در شروع برنامه تا
        # معلوم شود yaml واقعاً خوانده شده. هر دو Instance این فایل را اجرا
        # می‌کنند؛ استراتژی‌های بدون این کلیدها چیزی لاگ نمی‌کنند.
        _side_gates = {k: self.strategy.params.get(k) for k in
                       ("enable_kijun_short", "enable_tenkan_short")
                       if k in self.strategy.params}
        if _side_gates:
            logger.info("⚡ side gates effective | strategy={} | {}",
                        self.strategy.name, _side_gates)
        # سوییچ لحظه‌ای /extension — آخرین تصمیم ادمین از yaml مهم‌تر است و
        # بعد از ری‌استارت هم می‌ماند (فایل per-strategy تا دوInstance تداخل نکنند).
        self._extension_state_path = os.path.join(
            "data", f"extension_filter_{self.strategy.name}.json"
        )
        self._load_extension_state()
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
            pause_minutes_after_tier1=int(
                news_sec.get("pause_minutes_after_tier1", 0)
            ),
            tier1_patterns=list(
                news_sec.get(
                    "tier1_patterns",
                    ["non-farm", "nfp", "cpi", "fomc", "federal funds", "powell"],
                )
            ),
            cache_refresh_hours=float(news_sec.get("cache_refresh_hours", 1.0)),
            network_timeout_seconds=float(news_sec.get("network_timeout_seconds", 5.0)),
            server_utc_offset_hours=float(news_sec.get("server_utc_offset_hours", 0.0)),
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

        # --- signal quality scorer (degrades to neutral 50 without stats) ----
        self.scorer = SignalScorer(
            str(settings.get("signal_score.file", "data/signal_scores.json"))
        )

        # --- AI pre-trade gate (config/ai_gate.yaml; off = no effect at all) ---
        self.ai_gate: AIGate = AIGate.create(
            self.strategy.name,
            notify=self.telegram.notify_admins if self.telegram is not None else None,
        )
        self._pending_entry: Optional[PendingEntry] = None
        self._ai_sim_last = 0.0

        # --- live web dashboard ----------------------------------------------
        self.dashboard: Optional[DashboardServer] = None
        if bool(settings.get("dashboard.enabled", False)):
            # token از env هم قابل تزریق است (DASHBOARD_TOKEN) — تا کانفیگ دست‌نخورده بماند
            dash_token = os.environ.get("DASHBOARD_TOKEN") or str(
                settings.get("dashboard.token", "") or ""
            )
            self.dashboard = DashboardServer(
                telemetry_provider=self._hook_telemetry,
                db=self.db,
                host=str(settings.get("dashboard.host", "0.0.0.0")),
                port=int(settings.get("dashboard.port", 8080)),
                brand="GOLD M5 VIP",
                token=dash_token,
                log_path=str(settings.get("logging.path", "logs/bot_{time:YYYY-MM-DD}.log")),
                ai_gate_status=self.ai_gate.status,
                ai_gate_report=self.ai_gate.dashboard_report,
            )

        # --- automatic daily digest state -------------------------------------
        self.digest_enabled = bool(settings.get("daily_digest.enabled", True))
        self._last_digest_day: Optional[str] = None

        # --- manual-close "conscience mirror": بعد از هر بستن دستی، قیمت را
        # دنبال می‌کنیم تا معلوم شود نگه‌داشتن WIN می‌شد یا LOSS.
        self._shadows: List[Dict[str, Any]] = []

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
            get_extension=self._hook_get_extension,
            set_extension=self._hook_set_extension,
            bar_minutes=lambda: int(getattr(self.strategy, "bar_minutes", 5)),
            request_close=self._request_manual_close,
            open_position_count=self._hook_open_position_count,
            get_ai_gate=self._hook_get_ai_gate,
            set_ai_gate=self._hook_set_ai_gate,
        )

    def _hook_get_ai_gate(self) -> Optional[Dict[str, Any]]:
        gate = getattr(self, "ai_gate", None)
        return None if gate is None else gate.status()

    def _hook_set_ai_gate(self, mode: str) -> Optional[Dict[str, Any]]:
        gate = getattr(self, "ai_gate", None)
        return None if gate is None else gate.set_mode(mode)

    def _hook_open_position_count(self) -> int:
        """Broker positions_get for this symbol + this bot's magic only."""
        try:
            return int(self.client.open_position_count())
        except Exception as exc:
            logger.warning("open_position_count failed: {}", exc)
            raise

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

    # -------------------------------------------------- extension filter
    # مثل cooldown: پارامتر در prepare() خوانده می‌شود پس تغییر زندهٔ آن
    # بدون ری‌استارت اثر می‌گذارد. تصمیم ادمین در فایل JSON ذخیره می‌شود.
    def _extension_supported(self) -> bool:
        return "extension_filter_enabled" in self.strategy.params

    def _hook_get_extension(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._extension_supported():
                return None
            return {
                "enabled": bool(self.strategy.params["extension_filter_enabled"]),
                "max_atr": float(self.strategy.params.get("extension_max_atr", 4.0)),
                "blocks": int(getattr(self.strategy, "extension_blocks", 0)),
            }

    def _hook_set_extension(
        self, enabled: Optional[bool], max_atr: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._extension_supported():
                return None
            if max_atr is not None:
                self.strategy.params["extension_max_atr"] = max(
                    0.5, min(float(max_atr), 20.0)
                )
            if enabled is not None:
                self.strategy.params["extension_filter_enabled"] = bool(enabled)
            state = {
                "enabled": bool(self.strategy.params["extension_filter_enabled"]),
                "max_atr": float(self.strategy.params.get("extension_max_atr", 4.0)),
                "blocks": int(getattr(self.strategy, "extension_blocks", 0)),
            }
        self._persist_extension_state(state)
        logger.info(
            "extension filter -> enabled={} max_atr={:.1f}",
            state["enabled"],
            state["max_atr"],
        )
        return state

    def _load_extension_state(self) -> None:
        """آخرین تصمیم /extension بعد از ری‌استارت هم برقرار می‌ماند."""
        try:
            with open(self._extension_state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return  # فایل نیست → همان پیش‌فرض yaml/کد
        if not self._extension_supported():
            return
        if "enabled" in state:
            self.strategy.params["extension_filter_enabled"] = bool(state["enabled"])
        if "max_atr" in state:
            try:
                self.strategy.params["extension_max_atr"] = max(
                    0.5, min(float(state["max_atr"]), 20.0)
                )
            except (TypeError, ValueError):
                pass
        logger.info(
            "extension filter state restored: enabled={} max_atr={}",
            self.strategy.params["extension_filter_enabled"],
            self.strategy.params.get("extension_max_atr"),
        )

    def _persist_extension_state(self, state: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self._extension_state_path) or ".", exist_ok=True)
            with open(self._extension_state_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "enabled": bool(state["enabled"]),
                        "max_atr": float(state["max_atr"]),
                    },
                    fh,
                    indent=2,
                )
        except OSError as exc:
            logger.error("cannot persist extension filter state: {}", exc)

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
            "server_time": datetime.fromtimestamp(int(tick.time), tz=timezone.utc)
            .replace(tzinfo=None)
            .strftime("%Y-%m-%d %H:%M:%S"),
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
                # FIX(2026-09-05): آرگومان‌های راه‌اندازی اصلی (مثل --config
                # settings_ichimoku.yaml) به پروسه‌ی جدید منتقل شوند تا
                # ری‌استارت خودکار هرگز با کانفیگ/استراتژی/توکنِ غلط بالا نیاید.
                relaunch = [sys.executable, "main_live.py", *sys.argv[1:]]
                logger.warning("relaunch args: {}", " ".join(relaunch[1:]) or "(default config)")
                subprocess.Popen(
                    relaunch,
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
    def _sync_positions(self) -> bool:
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
                    tracked.volume = float(pos.volume)
                    tracked.position_id = int(getattr(pos, "identifier", pos.ticket))
                else:
                    self.tracked[ticket] = TrackedPosition(
                        ticket=ticket,
                        side="BUY" if int(pos.type) == 0 else "SELL",
                        volume=float(pos.volume),
                        price_open=float(pos.price_open),
                        sl=float(pos.sl or 0.0),
                        tp=float(pos.tp or 0.0),
                        atr=self.current_atr,
                        position_id=int(getattr(pos, "identifier", pos.ticket)),
                    )
                    logger.info("adopted existing position #{}", ticket)

        complete = True
        for ticket in known - set(live):
            if not self._handle_closed_position(ticket):
                complete = False
        return complete

    def _handle_closed_position(self, ticket: int) -> bool:
        with self._lock:
            tracked = self.tracked.get(ticket)
        if tracked is None:
            return True
        deals = self.client.deals_for_position(tracked.position_id or ticket)
        entries = [d for d in deals if int(getattr(d, "entry", -1)) == 0]
        exits = [d for d in deals if int(getattr(d, "entry", -1)) in (1, 3)]
        volume_in = sum(float(d.volume) for d in entries)
        volume_out = sum(float(d.volume) for d in exits)
        # Empty/successful history can still lag behind positions_get. Partial
        # exits alone are not proof of a full close. Keep the mirror and retry.
        if not exits or volume_in <= 0 or volume_out + 1e-8 < volume_in:
            logger.warning("closure #{} awaiting complete broker deal history", ticket)
            return False
        last_exit = max(exits, key=lambda d: (getattr(d, "time_msc", 0), getattr(d, "ticket", 0)))
        close_price = float(last_exit.price)
        if close_price <= 0:
            return False
        profit = sum(float(getattr(d, key, 0.0)) for d in deals
                     for key in ("profit", "commission", "swap", "fee"))
        outcome = "WIN" if profit > 0 else ("BE" if abs(profit) < 0.01 else "LOSS")
        # Rebuild day-specific P/L before committing the closure. A history
        # failure leaves the mirror intact for the next reconciliation cycle.
        self._sync_daily()
        self.db.close_signal(ticket, close_price, profit, outcome)
        gate = getattr(self, "ai_gate", None)
        if gate is not None:
            gate.record_outcome(ticket, profit)
        # Fill actual manual-close P/L into any armed shadow for this ticket.
        with self._lock:
            for s in self._shadows:
                if int(s.get("ticket", -1)) == int(ticket) and s.get("actual_profit") is None:
                    s["actual_profit"] = profit
                    break
            self.tracked.pop(ticket, None)
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

        return True

    # =================================================== manual close (admin)
    def _request_manual_close(self, ticket_arg: str) -> str:
        """بستن اضطراری پوزیشن باز توسط ادمین + ثبت «سایه» برای داوری بعدی."""
        arg = (ticket_arg or "").strip()
        with self._lock:
            tracked_list = list(self.tracked.values())
        if not tracked_list:
            return "ℹ️ پوزیشن بازی برای بستن وجود ندارد."
        target = None
        if arg.isdigit():
            for tracked in tracked_list:
                if tracked.ticket == int(arg):
                    target = tracked
                    break
            if target is None:
                return f"❌ پوزیشن #{arg} پیدا نشد. (باز: {', '.join('#' + str(t.ticket) for t in tracked_list)})"
        elif len(tracked_list) == 1:
            target = tracked_list[0]
        else:
            return (
                "چند پوزیشن باز است — تیکت را مشخص کن:\n"
                + "\n".join(f"<code>/close {t.ticket}</code> · {t.side} {t.volume}" for t in tracked_list)
            )

        # Arm shadow BEFORE close so a fast main-loop sync cannot miss it.
        # actual_profit is filled by _handle_closed_position once deals land.
        shadow = {
            "ticket": target.ticket,
            "side": target.side,
            "entry": float(target.price_open),
            "sl": float(target.sl or 0.0),
            "tp": float(target.tp or 0.0),
            "volume": float(target.volume),
            "actual_profit": None,
        }
        with self._lock:
            self._shadows.append(shadow)

        result = self.client.close_position(target.ticket, comment="admin manual close")
        if not result.ok:
            with self._lock:
                self._shadows = [s for s in self._shadows if s is not shadow]
            return f"❌ بستن #‌{target.ticket} ناموفق بود: {result.comment}"

        logger.success("manual close #{} by admin (shadow armed)", target.ticket)
        return (
            f"✅ دستور بستن #‌{target.ticket} اجرا شد — کارت بسته‌شدن به‌زودی می‌آید.\n"
            "🪞 <i>سایه فعال شد: ربات قیمت را دنبال می‌کند تا معلوم شود "
            "اگر نگه می‌داشتی WIN می‌شد یا LOSS — و بهت می‌گوید.</i>"
        )

    def _check_shadows(self) -> None:
        """داوری سایه‌ها: اولین برخورد قیمت با TP یا SLِ اصلیِ معامله‌ی دست‌بسته.

        Verdict needs BOTH (1) price hit original SL/TP and (2) actual manual-close
        P/L from broker deals — then compares the two numbers (not win/loss label).
        """
        if not self._shadows:
            return
        tick = self.client.get_tick()
        if tick is None:
            return
        bid, ask = float(tick.bid), float(tick.ask)
        still_open: List[Dict[str, Any]] = []
        for shadow in self._shadows:
            side = shadow["side"]
            entry, sl, tp = shadow["entry"], shadow["sl"], shadow["tp"]
            lot = shadow["volume"]
            exit_price: Optional[float] = None
            if side == "BUY":
                if tp > 0 and bid >= tp:
                    exit_price = tp
                elif sl > 0 and bid <= sl:
                    exit_price = sl
            else:
                if tp > 0 and ask <= tp:
                    exit_price = tp
                elif sl > 0 and ask >= sl:
                    exit_price = sl
            if exit_price is None:
                still_open.append(shadow)
                continue
            actual = shadow.get("actual_profit")
            if actual is None:
                # Price hit, but broker deals not landed yet — wait one more cycle.
                still_open.append(shadow)
                continue
            money = shadow_pl(side, entry, exit_price, lot)
            outcome = shadow_outcome(money)
            emoji = "\U0001F3C1" if outcome == "WIN" else ("\U000026AA" if outcome == "BE" else "\U0001F534")
            comparison = format_shadow_verdict(float(actual), money)
            if self.telegram is not None:
                self.telegram.notify_admins(
                    f"🪞 <b>داوری معامله‌ی دستی #{shadow['ticket']}</b>\n"
                    f"اگر نگه می‌داشتی: {emoji} <b>{outcome}</b> "
                    f"(${money:+,.2f})\n"
                    f"{comparison}"
                )
            logger.info(
                "shadow verdict #{}: held={} actual={} | {}",
                shadow["ticket"], money, actual, comparison,
            )
        self._shadows = still_open

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
        """Rebuild today's counters from confirmed history, including restarts."""
        server_now = self.client.server_time()
        if server_now is None:
            raise MT5ReadError("server time unavailable for daily guard")
        day_start = server_now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = [d for d in self.client.deals_since(day_start)
                 if str(getattr(d, "symbol", "")) == self.client.symbol]
        pnl = sum(float(getattr(d, key, 0.0)) for d in deals
                  for key in ("profit", "commission", "swap", "fee"))
        entries = {int(getattr(d, "position_id", 0) or getattr(d, "order", 0) or d.ticket)
                   for d in deals if int(getattr(d, "entry", -1)) in (0, 2)}
        account = self.client.account_info()
        if account is None:
            raise MT5ReadError("account unavailable for daily guard")
        with self._lock:
            if self.daily_date != server_now.date():
                self.day_start_balance = float(account.balance) - pnl
                self.daily_halt_notified = False
            self.daily_date = server_now.date()
            self.daily_realized_pnl = pnl
            self.daily_trades = len(entries)

    # ============================================================ daily digest
    def _check_daily_digest(self) -> None:
        """Broadcast the previous UTC day's digest right after UTC midnight.

        The digest window matches the database (UTC timestamps), and the
        first loop iteration after startup only primes the date so restarts
        never duplicate a digest.
        """
        if not self.digest_enabled or self.telegram is None:
            return
        today = datetime.utcnow().date()
        if self._last_digest_day == str(today):
            return
        first_call = self._last_digest_day is None
        self._last_digest_day = str(today)
        if first_call:
            return
        previous = today - timedelta(days=1)
        open_now: Optional[int] = None
        try:
            open_now = int(self.client.open_position_count())
        except Exception as exc:
            logger.warning("open_position_count failed; digest falls back: {}", exc)
            open_now = None
        try:
            stats = build_daily_stats(self.db, previous, open_positions=open_now)
        except Exception as exc:
            logger.warning("daily digest build failed: {}", exc)
            return
        if stats.get("trades", 0) == 0 and stats.get("opened", 0) == 0:
            logger.info("daily digest skipped — nothing traded on {}", previous)
            return
        card = render_daily_card(stats)
        self.telegram.broadcast_daily(stats, card)
        logger.success("daily digest for {} broadcast", previous)

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
        # FIX (2026-09-16, H1 timing — incident 106282597): the H1 frame must
        # be passed WITH its forming candle. The strategy's internal shift(1)
        # already drops the forming H1 row and lands every decision on the
        # last FULLY CLOSED H1 candle — exactly the backtest alignment.
        # Stripping the forming candle here as well made live skip one MORE
        # closed candle: on :00/:15/:30 bars the bot decided with an H1 candle
        # up to one hour older than backtest (h1_trend_bull lagged 45-60 min
        # behind the actual H1 close). ORB ignores h1 entirely -> ichimoku only.
        h1 = frames.get("h1").copy() if frames.get("h1") is not None and not frames.get("h1").empty else None

        with self._lock:
            strategy = self.strategy

        prepared = strategy.prepare(m5, m15, h1)
        closed_atr = float(prepared["atr"].iloc[-1]) if "atr" in prepared and len(prepared) >= 1 else 0.0
        with self._lock:
            self.current_atr = closed_atr if closed_atr > 0 else self.current_atr

        if not self.running:
            return

        self._sync_daily()
        self._check_daily_digest()
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

        if getattr(self, "_pending_entry", None) is not None:
            logger.info("AI gate: a live decision is still pending; new signal ignored")
            return

        gate = getattr(self, "ai_gate", None)
        gate_inputs = {"prepared": prepared, "h1": h1}
        if gate is not None and gate.active_mode() == "live":
            # AI gate live: plan now, decide asynchronously, execute from the main
            # loop (_process_pending_entry) — position management never waits.
            self._begin_gated_entry(signal, gate_inputs)
            return

        # FIX(#4): اول سفارش با سطوح واقعی ریسک‌منیجر ثبت می‌شود و بعد همان
        # سطوحِ اجراشده (entry/sl/tp/lot/ticket واقعی) به تلگرام می‌رود.
        # قبلاً broadcast سطوح متای استراتژی را با rr هاردکد ۲.۰ نشان می‌داد
        # که با معامله‌ای که واقعاً ثبت می‌شد یکی نبود؛ سفارش‌های ردشده هم
        # بی‌جهت به VIP مخابره می‌شدند.
        levels = self._open_trade(signal, gate_inputs=gate_inputs)
        if levels is None:
            return
        self._broadcast_entry(signal, levels, prepared)

    def _broadcast_entry(self, signal: Signal, levels: TradeLevels, prepared: pd.DataFrame) -> None:
        # 🟢 مخابره سیگنال با سطوح واقعی به همراه چارت
        if self.telegram is not None:
            ref_time = getattr(signal, "ref_time", None)
            score = self.scorer.score(
                strategy=signal.strategy,
                side=signal.side,
                hour=int(getattr(ref_time, "hour", 12) or 12),
                atr=float(signal.atr or 0.0),
            )
            signal.meta["score"] = score
            logger.info("signal score {}/100 | {}", score, self.scorer.describe(
                strategy=signal.strategy, side=signal.side,
                hour=int(getattr(ref_time, "hour", 12) or 12), atr=float(signal.atr or 0.0),
            )["bucket"])
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
                score=score,
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

    def _open_trade(self, signal: Signal,
                    gate_inputs: Optional[Dict[str, Any]] = None) -> Optional[TradeLevels]:
        """سفارش را با سطوح ریسک‌منیجر ثبت می‌کند؛ در موفقیت levels برمی‌گرداند.

        AI gate در حالت shadow: نظر در پس‌زمینه خواسته می‌شود و سفارش منتظر آن نمی‌ماند.
        """
        planned = self._plan_trade(signal)
        if planned is None:
            return None
        gate = getattr(self, "ai_gate", None)
        gate_key = None
        if gate_inputs is not None and gate is not None and gate.active_mode() == "shadow":
            gate_key, _ = self._gate_submit(signal, planned, gate_inputs)
        levels = self._execute_trade(signal, planned)
        if levels is not None and gate_key is not None:
            gate.attach_ticket(gate_key, signal.ticket, signal.entry)
        return levels

    def _plan_trade(self, signal: Signal) -> Optional[PlannedTrade]:
        """Levels from the RiskManager at the current tick; no order is sent."""
        info = self.client.symbol_info(refresh=True)
        if info is None:
            logger.error("cannot size trade: no symbol info")
            return None
        self.risk.update_spec(SymbolSpec.from_mt5(info))

        tick = self.client.get_tick()
        if tick is None:
            return None
        entry_price = float(tick.ask if signal.is_long else tick.bid)
        account = self.client.account_info()
        if account is None or float(account.balance) <= 0:
            logger.error("cannot size trade without a positive confirmed balance")
            return None
        balance = float(account.balance)
        levels = self.risk.build_levels(signal.side, entry_price, signal.atr, balance)
        if levels is None:
            return None

        scope = json.dumps([str(getattr(account, "server", self.client.config.server)),
                            int(account.login), self.client.symbol, self.client.magic])
        signal_key = json.dumps([scope, signal.strategy, str(signal.ref_time), signal.side])
        return PlannedTrade(levels=levels, entry_price=entry_price, tick=tick,
                            point=float(getattr(info, "point", 0.01) or 0.01),
                            scope=scope, signal_key=signal_key)

    def _execute_trade(self, signal: Signal, planned: PlannedTrade) -> Optional[TradeLevels]:
        """Claim the signal in the execution journal and send the market order."""
        levels, scope, signal_key = planned.levels, planned.scope, planned.signal_key
        if not self.db.claim_execution(scope, signal_key):
            logger.warning("entry skipped: duplicate signal or unresolved order; inspect execution journal")
            return None
        try:
            execution_tag = hashlib.sha256(signal_key.encode("utf-8")).hexdigest()[:8]
            execution_comment = f"{signal.strategy}:{execution_tag}"[:31]
            result = self.client.send_market_order(
                signal.side, levels.lot, sl=levels.sl, tp=levels.tp,
                comment=execution_comment,
            )
        except Exception:
            self.db.finish_execution(signal_key, "uncertain", "exception during send")
            raise
        if not result.ok:
            status = "uncertain" if result.uncertain else "rejected"
            self.db.finish_execution(signal_key, status, str(result))
        if result.uncertain and not result.ok:
            logger.error("order needs broker verification; new entries blocked persistently")
            if self.telegram is not None:
                self.telegram.notify_admins(
                    "⚠️ نتیجه یا حجم نهایی سفارش نیاز به بررسی در MT5 دارد. "
                    "ورود جدید تا بررسی دفتر سفارش‌ها متوقف است؛ مدیریت پوزیشن‌ها ادامه دارد."
                )
        if not result.ok:
            logger.warning("order not confirmed: {}", result.comment)
            return None

        # در صورت موفقیت‌آمیز بودن معامله لایو
        ticket = int(result.position or 0)
        position_id = ticket
        if position_id <= 0 and result.deal:
            try:
                position_id = int(self.client.position_id_for_deal(result.deal) or 0)
                ticket = position_id
            except MT5ReadError:
                logger.warning("confirmed deal exists; position id not available yet")
        try:
            for candidate in self.client.positions():
                candidate_comment = str(getattr(candidate, "comment", "") or "")
                candidate_identifier = int(getattr(candidate, "identifier", 0) or 0)
                if ((ticket > 0 and int(candidate.ticket) == ticket)
                        or (position_id > 0 and candidate_identifier == position_id)
                        or candidate_comment == execution_comment):
                    ticket = int(candidate.ticket)
                    position_id = int(getattr(candidate, "identifier", ticket))
                    break
        except MT5ReadError:
            logger.warning("order confirmed; using returned ticket until broker sync recovers")
        if ticket <= 0 or position_id <= 0:
            self.db.finish_execution(signal_key, "uncertain",
                                     f"confirmed order but unresolved position; {result}")
            logger.error("confirmed order could not be mapped to a position; new entries blocked")
            if self.telegram is not None:
                self.telegram.notify_admins(
                    "⚠️ سفارش در بروکر تأیید شد اما شناسهٔ پوزیشن هنوز مشخص نیست. "
                    "SL/TP داخل سفارش است؛ ورودهای جدید تا بررسی MT5 متوقف شدند."
                )
            return None
        self.db.finish_execution(signal_key, "uncertain" if result.uncertain else "accepted",
                                 f"position={position_id}; {result}")
        if result.uncertain:
            logger.error("partially completed order requires broker verification; new entries blocked")
            if self.telegram is not None:
                self.telegram.notify_admins(
                    "⚠️ سفارش فقط بخشی اجرا شده یا وضعیت نهایی آن قطعی نیست. "
                    "حجم اجراشده مدیریت می‌شود؛ ورودهای جدید تا بررسی MT5 متوقف شدند."
                )
        filled_lot = float(result.volume or levels.lot)
        fill = float(result.price or levels.entry)
        actual_risk = self.risk.spec.money_per_lot(abs(fill - levels.sl)) * filled_lot
        actual_risk += self.risk_config.commission_per_lot * filled_lot
        reward = self.risk.spec.money_per_lot(abs(levels.tp - fill)) * filled_lot
        reward -= self.risk_config.commission_per_lot * filled_lot
        levels = replace(levels, entry=fill, lot=filled_lot, risk_money=actual_risk,
                         reward_money=reward, rr=reward / actual_risk if actual_risk > 0 else 0.0)

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
                position_id=position_id,
            )
            self.signals_sent += 1
            self.daily_trades += 1

        self.db.log_signal(
            ticket=int(ticket), symbol=self.client.symbol, side=signal.side,
            strategy=signal.strategy, lot=levels.lot, entry=signal.entry,
            sl=levels.sl, tp=levels.tp, atr=signal.atr, reason=signal.reason,
        )
        return levels

    # ================================================================ AI gate
    def _gate_submit(self, signal: Signal, planned: PlannedTrade,
                     gate_inputs: Dict[str, Any]) -> tuple:
        """Build the context and queue the request. Returns (journal_key, future|None).

        Never raises: a context failure is logged and yields a ``None`` future.
        """
        key = hash_key(planned.signal_key)
        try:
            context, meta = self._gate_context(signal, planned, gate_inputs)
        except Exception as exc:
            logger.warning("AI gate context unavailable: {}", exc)
            return key, None
        return key, self.ai_gate.submit(context, key, meta)

    def _gate_context(self, signal: Signal, planned: PlannedTrade,
                      gate_inputs: Dict[str, Any]) -> tuple:
        cfg = self.ai_gate.config
        tick = planned.tick
        tick_time = float(getattr(tick, "time", 0) or 0)
        offset = server_offset_hours(tick_time, time.time()) if tick_time > 0 else None
        if offset is None:
            offset = eet_offset_hours(datetime.now(timezone.utc))
            logger.warning("AI gate: broker clock offset not measurable (stale tick); using UTC{:+g}", offset)
        spread_points = (float(tick.ask) - float(tick.bid)) / planned.point if planned.point else 0.0
        levels = planned.levels
        plan = TradePlan(
            side=signal.side, entry=float(levels.entry), sl=float(levels.sl), tp=float(levels.tp),
            atr=float(signal.atr), risk_percent=float(self.risk_config.risk_percent),
            be_trigger_atr=float(self.risk_config.breakeven_trigger_atr),
            be_offset_points=float(self.risk_config.breakeven_buffer_points),
            trail_trigger_atr=float(self.risk_config.trailing_trigger_atr),
            trail_dist_atr=float(self.risk_config.trailing_distance_atr),
            spread_points=spread_points, point=planned.point,
        )
        news = None
        if getattr(self, "news_filter", None) is not None:
            try:
                news = self.news_filter.get_next_event()
            except Exception:
                news = None
        recent = None
        if cfg.context.include_recent_trades and cfg.context.recent_trades > 0:
            recent = self.db.recent_signals(limit=max(200, cfg.context.recent_trades * 5))
        context = build_context(
            strategy=signal.strategy, layer=signal.reason, ref_time=signal.ref_time,
            trigger=gate_inputs["prepared"], trigger_minutes=int(getattr(self.strategy, "bar_minutes", 5)),
            h1=gate_inputs.get("h1"), plan=plan, offset_hours=offset,
            decision_time_utc=datetime.now(timezone.utc),
            trigger_bars=cfg.context.trigger_bars, h1_bars=cfg.context.h1_bars,
            next_news=news, recent_trades=recent, recent_limit=cfg.context.recent_trades,
            base_rates=cfg.base_rates,
        )
        meta = {
            "strategy": signal.strategy, "side": signal.side, "layer": signal.reason,
            "ref_time_server": str(signal.ref_time), "ref_time_utc": context["signal_bar_close_utc"],
            "entry": float(levels.entry), "sl": float(levels.sl), "tp": float(levels.tp),
            "atr": float(signal.atr), "spread_points": round(spread_points, 1),
        }
        return context, meta

    def _begin_gated_entry(self, signal: Signal, gate_inputs: Dict[str, Any]) -> None:
        planned = self._plan_trade(signal)
        if planned is None:
            return
        key, future = self._gate_submit(signal, planned, gate_inputs)
        if future is None:
            future = _completed(GateResult(key, "live", "error", error="context unavailable"))
        timeout = float(self.ai_gate.config.timeout_seconds)
        self._pending_entry = PendingEntry(
            signal=signal, planned=planned, prepared=gate_inputs.get("prepared"),
            future=future, key=key, deadline=time.time() + timeout + 2.0,
        )
        logger.info("AI gate [live]: waiting for verdict on {} {} (max {:.0f}s)",
                    signal.side, signal.reason, timeout)

    def _entry_guard_reason(self) -> str:
        """Why a delayed live entry must not be sent now ('' = all clear)."""
        if not self.running:
            return "engine paused"
        halted, why = self._daily_guard()
        if halted:
            return f"daily guard: {why}"
        if not self._session_open(self.client.server_time()) or not self._spread_ok():
            return "session closed or spread too wide"
        if getattr(self, "news_filter", None) is not None:
            blocked, why = self.news_filter.is_news_active()
            if blocked:
                return f"news: {why}"
        if self.client.open_position_count() >= self.risk_config.max_positions_per_symbol:
            return "position limit reached"
        return ""

    def _process_pending_entry(self) -> None:
        """Main-loop step: act on a finished (or overdue) live AI-gate decision."""
        pending = getattr(self, "_pending_entry", None)
        if pending is None:
            return
        if not pending.future.done() and time.time() < pending.deadline:
            return
        self._pending_entry = None
        gate = self.ai_gate
        if pending.future.done():
            try:
                result = pending.future.result()
            except Exception as exc:  # worker crashed: treat as a gate error
                result = GateResult(pending.key, "live", "error", error=str(exc)[:200])
        else:
            pending.future.cancel()
            result = gate.timeout_result(pending.key, "live")

        allow, action = gate.decide_live(result)
        if not allow:
            gate.finalize(result, action)
            return
        final = "cancelled_guard"
        try:
            reason = self._entry_guard_reason()
            if reason:
                logger.info("AI gate: delayed entry cancelled ({})", reason)
                return
            fresh = self._plan_trade(pending.signal)
            if fresh is None:
                return
            drift = abs(float(fresh.entry_price) - float(pending.planned.entry_price))
            limit = float(gate.config.max_entry_drift_atr) * float(pending.signal.atr or 0.0)
            if limit > 0 and drift > limit:
                final = "cancelled_drift"
                logger.info("AI gate: delayed entry cancelled, price drift {:.2f} > {:.2f}", drift, limit)
                return
            levels = self._execute_trade(pending.signal, fresh)
            if levels is None:
                final = "order_failed"
                return
            final = action
            gate.attach_ticket(pending.key, pending.signal.ticket, pending.signal.entry)
            self._broadcast_entry(pending.signal, levels, pending.prepared)
        finally:
            gate.finalize(result, final)

    AI_SIM_INTERVAL_S = 300.0

    def _ai_gate_housekeeping(self) -> None:
        """Every few minutes: simulate AI-gate verdict outcomes on broker M1 bars.

        Runs in the main (MT5) thread; results are stored in the gate journal so the
        dashboard can show accuracy without touching MT5.  Never raises.
        """
        gate = getattr(self, "ai_gate", None)
        if gate is None or not gate.has_journal():
            return
        now = time.time()
        if now - getattr(self, "_ai_sim_last", 0.0) < self.AI_SIM_INTERVAL_S:
            return
        self._ai_sim_last = now
        try:
            server_now = self.client.server_time()
            if server_now is None:
                return
            rc = self.risk_config
            rules = ExitRules(rc.breakeven_trigger_atr, rc.breakeven_buffer_points,
                              rc.trailing_trigger_atr, rc.trailing_distance_atr,
                              point=float(getattr(self.risk.spec, "point", 0.01) or 0.01))

            def fetch(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
                # MT5 python treats datetimes as UTC epochs while bar times are the
                # server clock: pass the server wall clock tagged as UTC.
                return self.client.get_rates_range(
                    "M1", start.to_pydatetime().replace(tzinfo=timezone.utc),
                    end.to_pydatetime().replace(tzinfo=timezone.utc))

            updated = gate.simulate_pending(fetch, rules, server_now)
            if updated:
                logger.debug("AI gate: {} verdict simulation(s) updated", updated)
        except Exception as exc:
            logger.warning("AI gate evaluation step failed: {}", exc)

    def _cancel_pending_entry(self) -> None:
        pending = getattr(self, "_pending_entry", None)
        if pending is None:
            return
        self._pending_entry = None
        if pending.future.done():
            try:
                result = pending.future.result()
            except Exception as exc:
                result = GateResult(pending.key, "live", "error", error=str(exc)[:200])
        else:
            pending.future.cancel()
            result = self.ai_gate.timeout_result(pending.key, "live")
        self.ai_gate.finalize(result, "cancelled_guard")

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
                try:
                    self._sync_daily()
                except MT5ReadError as exc:
                    logger.warning("daily guard not ready: {}", exc)
                if self.daily_date is not None:
                    break
                if attempt < 3:
                    logger.warning("daily guard init attempt {} returned no deals; retrying in 5s", attempt)
                    time.sleep(5.0)
        self.client.start_heartbeat()

        if self.dashboard is not None:
            self.dashboard.start()

        if self.telegram is not None:
            self.telegram.start()
            self.telegram.notify_admins(
                "\U0001F680 <b>GOLD M5 BOT started</b>\n"
                f"Symbol: <code>{self.client.symbol}</code>\n"
                f"Strategy: <b>{self.strategy.name}</b>\n"
                f"Risk: <b>{self.risk_config.risk_percent:.2f}%</b>\n"
                f"AI gate: <b>{self.ai_gate.mode}</b>"
                + ("" if self.ai_gate.mode == "off" or self.ai_gate.active_mode() != "off"
                   else " (inactive: API key/model missing)")
            )

        unresolved = self.db.unresolved_executions()
        if unresolved:
            logger.critical(
                "{} unresolved execution(s) require broker-history review; "
                "new entries for the affected account/symbol are blocked",
                len(unresolved),
            )
            if self.telegram is not None:
                self.telegram.notify_admins(
                    "\u26a0\ufe0f <b>Execution review required</b>\n"
                    f"Unresolved attempts: <b>{len(unresolved)}</b>\n"
                    "New entries are blocked until broker history is checked."
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
                reconciled = self._sync_positions()
                self._manage_open_positions()
                self._check_shadows()

                if not reconciled:
                    self._stop.wait(self.loop_sleep)
                    continue

                # (1b) live AI gate: act on a finished verdict (never waits here)
                self._process_pending_entry()
                # (1c) AI gate evaluation: simulate verdict outcomes on M1 (every 5 min)
                self._ai_gate_housekeeping()

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

            except MT5ReadError as exc:
                logger.warning("broker state unknown; entry cycle skipped: {}", exc)
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
        if getattr(self, "ai_gate", None) is not None:
            try:
                self._cancel_pending_entry()
            finally:
                self.ai_gate.shutdown()
        if self.dashboard is not None:
            self.dashboard.stop()
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
