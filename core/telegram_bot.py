"""Threaded, multi-tier async Telegram controller and signal broadcaster.

Threading model
---------------
``TelegramController.start()`` spawns a daemon thread that owns a brand new
``asyncio`` event loop and the ``python-telegram-bot`` v20 ``Application``.
The MT5 execution thread never awaits anything: it calls the *synchronous*
``broadcast_*`` / ``notify_*`` methods, which marshal coroutines onto the bot
loop via :func:`asyncio.run_coroutine_threadsafe`.  Nothing else is shared.

Audience tiers
--------------
* **Admin**    - full telemetry, kill switch, strategy/risk switching, /addvip.
* **VIP**      - full signal + dark multi-panel chart + BE/trailing updates.
* **Free**     - condensed teaser + inline "upgrade to VIP" call to action.
"""
from __future__ import annotations

import asyncio
import io
import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.database import Database

__all__ = ["BotBridge", "TelegramConfig", "TelegramController"]

# ---------------------------------------------------------------- user labels
BTN_MY_SUB = "\U0001F48E وضعیت اشتراک من"
BTN_BUY_VIP = "\U0001F4B3 خرید / تمدید اشتراک VIP"
BTN_LIVE_PRICE = "\U0001FA99 استعلام قیمت زنده"
BTN_BACKTEST_CARD = "\U0001F9EA کارنامه و بک‌تست"

# --------------------------------------------------------------- admin labels
BTN_TELEMETRY = "\U0001F4CA تلمتری حساب"
BTN_POSITIONS = "\U0001F4C8 پوزیشن‌های باز"
BTN_RUN_BACKTEST = "\U0001F501 اجرای بک‌تست"
BTN_STRATEGY = "⚙️ تغییر استراتژی"
BTN_RISK = "\U0001F39A تنطیم ریسک"
BTN_COOLDOWN = "⏱️ وقفه معاملات"
BTN_TOGGLE = "\U0001F6A6 روشن/خاموش کردن ربات"
BTN_USERS = "\U0001F465 آمار کاربران"


@dataclass
class TelegramConfig:
    enabled: bool = True
    token: str = ""
    admin_ids: List[int] = field(default_factory=list)
    vip_channel_ids: List[int] = field(default_factory=list)
    free_channel_ids: List[int] = field(default_factory=list)
    vip_price_usd: float = 49.0
    vip_days_per_purchase: int = 30
    purchase_contact: str = "@YourAdminUsername"
    send_charts_to_vip: bool = True
    send_manage_alerts_to_vip: bool = True

    @classmethod
    def from_settings(cls, settings: Any) -> "TelegramConfig":
        return cls(
            enabled=bool(settings.get("telegram.enabled", True)),
            token=str(settings.get("telegram.token", "") or ""),
            admin_ids=[int(x) for x in (settings.get("telegram.admin_ids", []) or []) if x],
            vip_channel_ids=[int(x) for x in (settings.get("telegram.vip_channel_ids", []) or [])],
            free_channel_ids=[int(x) for x in (settings.get("telegram.free_channel_ids", []) or [])],
            vip_price_usd=float(settings.get("telegram.vip_price_usd", 49)),
            vip_days_per_purchase=int(settings.get("telegram.vip_days_per_purchase", 30)),
            purchase_contact=str(settings.get("telegram.purchase_contact", "@admin")),
            send_charts_to_vip=bool(settings.get("telegram.send_charts_to_vip", True)),
            send_manage_alerts_to_vip=bool(settings.get("telegram.send_manage_alerts_to_vip", True)),
        )


@dataclass
class BotBridge:
    """Callables injected by ``main_live`` so the bot never imports the engine.

    Every hook is optional; the UI degrades gracefully when one is missing.
    All hooks are invoked **inside the bot's event loop**, so implementations
    must be non-blocking or wrapped by the controller in ``to_thread``.
    """

    telemetry: Optional[Callable[[], Dict[str, Any]]] = None
    price: Optional[Callable[[], Dict[str, Any]]] = None
    is_running: Optional[Callable[[], bool]] = None
    toggle_bot: Optional[Callable[[Optional[bool]], bool]] = None
    get_strategy: Optional[Callable[[], str]] = None
    set_strategy: Optional[Callable[[str], bool]] = None
    available_strategies: Optional[Callable[[], List[str]]] = None
    strategy_description: Optional[Callable[[], str]] = None
    get_risk: Optional[Callable[[], float]] = None
    set_risk: Optional[Callable[[float], float]] = None
    get_cooldown: Optional[Callable[[], int]] = None
    set_cooldown: Optional[Callable[[int], int]] = None
    run_backtest: Optional[Callable[[Optional[str]], Any]] = None
    last_backtest: Optional[Callable[[], Any]] = None
    equity_chart: Optional[Callable[[Any], Optional[io.BytesIO]]] = None


class TelegramController:
    """Owns the PTB application on a dedicated thread + event loop."""

    def __init__(
        self,
        config: TelegramConfig,
        database: Database,
        bridge: Optional[BotBridge] = None,
    ) -> None:
        self.config = config
        self.db = database
        self.bridge = bridge or BotBridge()

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._app: Optional[Application] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._bot_username: str = ""
        self._pending_action: Dict[int, str] = {}

    # ================================================================ lifecycle
    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._ready.is_set())

    def start(self, wait_seconds: float = 25.0) -> bool:
        if not self.config.enabled:
            logger.warning("telegram disabled in settings")
            return False
        if not self.config.token:
            logger.error("telegram token missing; controller not started")
            return False
        if self._thread and self._thread.is_alive():
            return True

        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="telegram-loop", daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout=wait_seconds)
        if ok:
            logger.success("telegram controller online as @{}", self._bot_username or "?")
        else:
            logger.error("telegram controller failed to become ready in {}s", wait_seconds)
        return ok

    def stop(self, timeout: float = 15.0) -> None:
        if self._loop and self._stop_event and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._ready.clear()
        logger.info("telegram controller stopped")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            logger.exception("telegram thread crashed: {}", exc)
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._loop = None
            logger.info("telegram event loop closed")

    async def _async_main(self) -> None:
        self._stop_event = asyncio.Event()

        from telegram.request import HTTPXRequest

        # Optional outbound proxy (e.g. http://127.0.0.1:10808 when running
        # behind v2ray on an Iran-based machine). Leave TELEGRAM_PROXY unset
        # on a VPS with direct internet access.
        proxy_url = os.environ.get("TELEGRAM_PROXY", "").strip() or None

        request = HTTPXRequest(
            connection_pool_size=50,
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0,
            proxy=proxy_url,
        )

        # حذف concurrent_updates برای جلوگیری از تداخل تسک‌ها
        app = (
            ApplicationBuilder()
            .token(self.config.token)
            .request(request)
            .build()
        )
        self._app = app
        self._register_handlers(app)

        await app.initialize()
        me = await app.bot.get_me()
        self._bot_username = me.username or ""
        
        await app.start()
        
        # استارت کنترل‌شده Polling برای رفع قطعی Conflict
        await app.updater.start_polling(
            poll_interval=1.0,
            timeout=20,
            drop_pending_updates=True,
            bootstrap_retries=-1
        )
        
        self._ready.set()

        await self._notify_admins_async(
            "✅ <b>Bot controller online</b>\n"
            f"<code>@{self._bot_username}</code>\n"
            f"Strategy: <b>{self._safe(self.bridge.get_strategy, '-')}</b>"
        )

        await self._stop_event.wait()

        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as exc:
            from loguru import logger
            logger.error("telegram shutdown error: {}", exc)
    # ------------------------------------------------- cross-thread submission
    def _submit(
        self,
        coro: Coroutine[Any, Any, Any],
        wait: Optional[float] = None,
    ) -> Optional[Any]:
        """THE thread boundary. Schedules ``coro`` on the bot loop."""
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            logger.debug("telegram loop not running; dropping coroutine")
            return None
        future: Future = asyncio.run_coroutine_threadsafe(coro, loop)
        if wait:
            try:
                return future.result(timeout=wait)
            except Exception as exc:
                logger.error("telegram coroutine failed: {}", exc)
                return None
        future.add_done_callback(self._log_future)
        return future

    @staticmethod
    def _log_future(future: Future) -> None:
        exc = future.exception() if future.done() and not future.cancelled() else None
        if exc is not None:
            logger.error("telegram background task failed: {}", exc)

    @staticmethod
    def _safe(fn: Optional[Callable[..., Any]], default: Any = None, *args: Any) -> Any:
        if fn is None:
            return default
        try:
            return fn(*args)
        except Exception as exc:
            logger.error("bridge hook failed: {}", exc)
            return default

    # ==================================================================== roles
    def is_admin(self, user_id: int) -> bool:
        return int(user_id) in self.config.admin_ids

    def _main_keyboard(self, user_id: int) -> ReplyKeyboardMarkup:
        if self.is_admin(user_id):
            rows = [
                [KeyboardButton(BTN_TELEMETRY), KeyboardButton(BTN_LIVE_PRICE)],
                [KeyboardButton(BTN_POSITIONS), KeyboardButton(BTN_RUN_BACKTEST)],
                [KeyboardButton(BTN_STRATEGY), KeyboardButton(BTN_RISK)],
                [KeyboardButton(BTN_COOLDOWN), KeyboardButton(BTN_TOGGLE)],
                [KeyboardButton(BTN_USERS)],
            ]
        else:
            rows = [
                [KeyboardButton(BTN_MY_SUB), KeyboardButton(BTN_BUY_VIP)],
                [KeyboardButton(BTN_LIVE_PRICE), KeyboardButton(BTN_BACKTEST_CARD)],
            ]
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

    def _vip_cta(self) -> InlineKeyboardMarkup:
        deep_link = (
            f"https://t.me/{self._bot_username}?start=vip"
            if self._bot_username
            else "https://telegram.org"
        )
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    "\U0001F48E دریافت سیگنال کامل (VIP)",
                    url=deep_link,
                )],
                [InlineKeyboardButton(
                    "\U0001F4AC تماس با پشتیبانی",
                    url=f"https://t.me/{self.config.purchase_contact.lstrip('@')}",
                )],
            ]
        )

    # ================================================================ handlers
    def _register_handlers(self, app: Application) -> None:
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("price", self._cmd_price))
        app.add_handler(CommandHandler("addvip", self._cmd_addvip))
        app.add_handler(CommandHandler("removevip", self._cmd_removevip))
        app.add_handler(CommandHandler("users", self._cmd_users))
        app.add_handler(CommandHandler("backtest", self._cmd_backtest))
        app.add_handler(CommandHandler("toggle", self._cmd_toggle))
        app.add_handler(CommandHandler("strategy", self._cmd_strategy))
        app.add_handler(CommandHandler("risk", self._cmd_risk))
        app.add_handler(CommandHandler("cooldown", self._cmd_cooldown))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        app.add_error_handler(self._on_error)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("telegram handler error: {}", context.error)

    def _register_from_update(self, update: Update) -> Tuple[int, bool]:
        user = update.effective_user
        if user is None:
            return 0, False
        self.db.register_user(user.id, user.username, user.first_name)
        return user.id, self.is_admin(user.id)

    # ------------------------------------------------------------------- /start
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid, admin = self._register_from_update(update)
        if uid == 0 or update.message is None:
            return
        if admin:
            engine_state = (
                "\U0001F7E2 RUNNING"
                if self._safe(self.bridge.is_running, False)
                else "\U0001F534 PAUSED"
            )
            text = (
                "\U0001F451 <b>Admin Panel · GOLD M5 BOT</b>\n"
                f"Strategy: <b>{self._safe(self.bridge.get_strategy, '-')}</b>\n"
                f"Engine: {engine_state}\n\n"
                "<code>/addvip [uid] [days]</code> · grant VIP\n"
                "<code>/removevip [uid]</code> · revoke\n"
                "<code>/backtest [strategy]</code> · re-run\n"
                "<code>/risk 1.0</code> · set risk %\n"
                "<code>/cooldown 12</code> · set cooldown bars (12=60m)\n"
                "<code>/toggle</code> · kill switch"
            )
        else:
            status = self.db.get_user_status(uid)
            tier = "\U0001F48E VIP" if status["is_vip"] else "\U0001F193 FREE"
            text = (
                "\U0001F44B <b>به ربات سیگنال طلا خوش آمدید</b>\n"
                f"سطح دسترسی فعلی: <b>{tier}</b>\n\n"
                "\U0001F4C8 سیگنال‌های XAUUSD در تایم‌فریم M5 "
                "با فیلتر روند M15 و H1\n"
                "\U0001F6E1️ مدیریت ریسک خودکار (ریسک فری + تریلینگ)\n"
                "\U0001F4CA نمودار حرفه‌ای همراه هر سیگنال"
            )
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=self._main_keyboard(uid)
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._cmd_start(update, context)

    # ------------------------------------------------------------------ status
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid, _ = self._register_from_update(update)
        if update.message:
            await update.message.reply_text(
                self._subscription_card(uid), parse_mode=ParseMode.HTML
            )

    def _subscription_card(self, user_id: int) -> str:
        s = self.db.get_user_status(user_id)
        if s["is_vip"]:
            return (
                "\U0001F48E <b>اشتراک VIP فعال است</b>\n"
                f"\U0001F464 کاربر: <code>{s['display_name']}</code>\n"
                f"\U0001F194 شناسه: <code>{s['user_id']}</code>\n"
                f"⏳ روزهای باقی‌مانده: <b>{s['days_left']}</b>\n"
                f"\U0001F4C5 انقضا: <code>{s['expire_at']} UTC</code>"
            )
        return (
            "\U0001F193 <b>اشتراک شما: رایگان</b>\n"
            f"\U0001F194 شناسه: <code>{s['user_id']}</code>\n\n"
            "با ارتقا به VIP سیگنال کامل + "
            "نمودار + هشدار مدیریت پوزیشن "
            "دریافت می‌کنید."
        )

    def _pricing_card(self) -> str:
        c = self.config
        return (
            "\U0001F4B3 <b>اشتراک VIP</b>\n"
            "——————————————\n"
            f"\U0001F4B5 قیمت: <b>${c.vip_price_usd:,.0f}</b> / "
            f"{c.vip_days_per_purchase} روز\n\n"
            "✅ سیگنال کامل (Entry / SL / TP / حجم)\n"
            "✅ نمودار چند پنلی حرفه‌ای\n"
            "✅ هشدار ریسک‌فری و تریلینگ "
            "به صورت لحظه‌ای\n"
            "✅ گزارش عملکرد و بک‌تست هفتگی\n\n"
            f"\U0001F4AC برای خرید: {c.purchase_contact}"
        )

    # ------------------------------------------------------------------- price
    async def _cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._register_from_update(update)
        if update.message:
            await update.message.reply_text(self._price_card(), parse_mode=ParseMode.HTML)

    def _price_card(self) -> str:
        data = self._safe(self.bridge.price, None) or {}
        if not data or not data.get("bid"):
            return "⚠️ قیمت در دسترس نیست (اتصال بروکر قطع است)."
        return (
            f"\U0001FA99 <b>{data.get('symbol', 'XAUUSD')} · LIVE</b>\n"
            "——————————————\n"
            f"\U0001F4C9 Bid: <b>{data['bid']:,.2f}</b>\n"
            f"\U0001F4C8 Ask: <b>{data['ask']:,.2f}</b>\n"
            f"↔️ Spread: <b>{data.get('spread_points', 0):.0f}</b> points\n"
            f"\U0001F551 Server: <code>{data.get('server_time', '-')}</code>"
        )

    # ------------------------------------------------------------------ admin
    async def _guard_admin(self, update: Update) -> bool:
        uid, admin = self._register_from_update(update)
        if not admin and update.message:
            await update.message.reply_text(
                "⛔ این دستور مخصوص مدیر است."
            )
        return admin

    async def _cmd_addvip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_admin(update) or update.message is None:
            return
        args = context.args or []
        if len(args) < 2 or not args[0].lstrip("-").isdigit() or not args[1].lstrip("-").isdigit():
            await update.message.reply_text(
                "Usage: <code>/addvip [user_id] [days]</code>", parse_mode=ParseMode.HTML
            )
            return
        target, days = int(args[0]), int(args[1])
        record = self.db.add_vip_days(
            target, days,
            amount=self.config.vip_price_usd if days > 0 else 0.0,
            granted_by=update.effective_user.id if update.effective_user else None,
        )
        await update.message.reply_text(
            f"✅ VIP {days:+d}d → <code>{target}</code>\n"
            f"Plan: <b>{record.plan_type}</b> · expires <code>{record.vip_expire_at} UTC</code>",
            parse_mode=ParseMode.HTML,
        )
        await self._send_text(
            target,
            "\U0001F389 <b>اشتراک VIP شما فعال شد!</b>\n"
            f"⏳ روزهای باقی‌مانده: <b>{record.days_left}</b>",
        )

    async def _cmd_removevip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_admin(update) or update.message is None:
            return
        args = context.args or []
        if not args or not args[0].lstrip("-").isdigit():
            await update.message.reply_text(
                "Usage: <code>/removevip [user_id]</code>", parse_mode=ParseMode.HTML
            )
            return
        self.db.remove_vip(int(args[0]))
        await update.message.reply_text(f"\U0001F5D1 VIP revoked for <code>{args[0]}</code>", parse_mode=ParseMode.HTML)

    async def _cmd_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_admin(update) or update.message is None:
            return
        stats = self.db.stats()
        vips = self.db.get_all_vip_users()
        lines = [
            "\U0001F465 <b>Subscribers</b>",
            f"Total: <b>{stats['total_users']}</b> · VIP: <b>{stats['vip_users']}</b> "
            f"· Free: <b>{stats['free_users']}</b>",
            f"Revenue logged: <b>${stats['revenue']:,.2f}</b>",
            "",
        ]
        for user in vips[:25]:
            lines.append(
                f"• <code>{user.user_id}</code> {user.display_name} — {user.days_left}d"
            )
        if not vips:
            lines.append("<i>no active VIP subscribers</i>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_admin(update) or update.message is None:
            return
        running = bool(self._safe(self.bridge.toggle_bot, False, None))
        await update.message.reply_text(
            "\U0001F7E2 <b>Engine RESUMED</b>" if running else "\U0001F534 <b>Engine PAUSED</b>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_admin(update) or update.message is None:
            return
        args = context.args or []
        if args:
            try:
                value = float(args[0])
            except ValueError:
                await update.message.reply_text("Usage: <code>/risk 1.0</code>", parse_mode=ParseMode.HTML)
                return
            applied = self._safe(self.bridge.set_risk, None, value)
            await update.message.reply_text(
                f"\U0001F39A Risk set to <b>{applied if applied is not None else value:.2f}%</b>",
                parse_mode=ParseMode.HTML,
            )
            return
        await update.message.reply_text(
            "\U0001F39A Select risk per trade:",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(f"{v}%", callback_data=f"risk:{v}")
                    for v in (0.5, 1.0, 1.5, 2.0)
                ]]
            ),
        )

    async def _cmd_cooldown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_admin(update) or update.message is None:
            return
        args = context.args or []
        if args:
            try:
                value = int(args[0])
            except ValueError:
                await update.message.reply_text("Usage: <code>/cooldown 12</code>", parse_mode=ParseMode.HTML)
                return
            applied = self._safe(self.bridge.set_cooldown, value, value)
            mins = int(applied if applied is not None else value) * 5
            await update.message.reply_text(
                f"⏱️ وقفه معاملات روی <b>{applied if applied is not None else value} کندل ({mins} دقیقه)</b> تنظیم شد.",
                parse_mode=ParseMode.HTML,
            )
            return
        current = self._safe(self.bridge.get_cooldown, 12)
        mins_cur = int(current) * 5
        await update.message.reply_text(
            f"⏱️ <b>تنظیم وقفه بین معاملات (Cooldown)</b>\n"
            f"مقدار فعلی: <b>{current} کندل ({mins_cur} دقیقه)</b>\n"
            "یک گزینه را انتخاب کنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("۱۵ دقیقه (3)", callback_data="cd:3"),
                        InlineKeyboardButton("۳۰ دقیقه (6)", callback_data="cd:6"),
                    ],
                    [
                        InlineKeyboardButton("۶۰ دقیقه (12)", callback_data="cd:12"),
                        InlineKeyboardButton("۹۰ دقیقه (18)", callback_data="cd:18"),
                    ],
                ]
            ),
        )

    async def _cmd_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_admin(update) or update.message is None:
            return
        args = context.args or []
        names: List[str] = self._safe(self.bridge.available_strategies, []) or []
        if args:
            ok = bool(self._safe(self.bridge.set_strategy, False, args[0]))
            await update.message.reply_text(
                f"⚙️ Strategy → <b>{args[0]}</b>" if ok else "❌ Unknown strategy",
                parse_mode=ParseMode.HTML,
            )
            return
        current = self._safe(self.bridge.get_strategy, "-")
        description = self._safe(self.bridge.strategy_description, "")
        await update.message.reply_text(
            f"⚙️ Active: <b>{current}</b>\n<i>{description}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(n, callback_data=f"strat:{n}")] for n in names]
            ),
        )

    async def _cmd_backtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid, admin = self._register_from_update(update)
        if update.message is None:
            return
        if not admin:
            await self._send_backtest_card(uid)
            return
        args = context.args or []
        name = args[0] if args else None
        await update.message.reply_text("⏳ Running backtest, this may take a minute…")
        result = await asyncio.to_thread(self._safe, self.bridge.run_backtest, None, name)
        if result is None:
            await update.message.reply_text("❌ Backtest failed (no data or engine offline).")
            return
        await update.message.reply_text(result.to_telegram_card(), parse_mode=ParseMode.HTML)
        chart = self._safe(self.bridge.equity_chart, None, result)
        if chart is not None:
            await update.message.reply_photo(photo=chart, caption="\U0001F4C8 Equity curve")

    async def _send_backtest_card(self, user_id: int) -> None:
        result = self._safe(self.bridge.last_backtest, None)
        if result is None:
            await self._send_text(
                user_id,
                "ℹ️ هنوز کارنامه‌ای "
                "منتشر نشده است.",
            )
            return
        await self._send_text(user_id, result.to_telegram_card())

    # -------------------------------------------------------------- text menu
    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid, admin = self._register_from_update(update)
        if update.message is None or update.message.text is None:
            return
        text = update.message.text.strip()

        if text == BTN_LIVE_PRICE:
            await update.message.reply_text(self._price_card(), parse_mode=ParseMode.HTML)
            return
        if text == BTN_MY_SUB:
            await update.message.reply_text(self._subscription_card(uid), parse_mode=ParseMode.HTML)
            return
        if text == BTN_BUY_VIP:
            await update.message.reply_text(
                self._pricing_card(), parse_mode=ParseMode.HTML, reply_markup=self._vip_cta()
            )
            return
        if text == BTN_BACKTEST_CARD:
            await self._send_backtest_card(uid)
            return

        if not admin:
            await update.message.reply_text(
                "❓ از دکمه‌های منو "
                "استفاده کنید.",
                reply_markup=self._main_keyboard(uid),
            )
            return

        if text == BTN_TELEMETRY:
            await update.message.reply_text(self._telemetry_card(), parse_mode=ParseMode.HTML)
        elif text == BTN_POSITIONS:
            await update.message.reply_text(self._positions_card(), parse_mode=ParseMode.HTML)
        elif text == BTN_USERS:
            await self._cmd_users(update, context)
        elif text == BTN_TOGGLE:
            await self._cmd_toggle(update, context)
        elif text == BTN_RISK:
            await self._cmd_risk(update, context)
        elif text == BTN_COOLDOWN:
            await self._cmd_cooldown(update, context)
        elif text == BTN_STRATEGY:
            await self._cmd_strategy(update, context)
        elif text == BTN_RUN_BACKTEST:
            await self._cmd_backtest(update, context)
        else:
            await update.message.reply_text(
                "❓ Unknown command.", reply_markup=self._main_keyboard(uid)
            )

    def _telemetry_card(self) -> str:
        t = self._safe(self.bridge.telemetry, None) or {}
        if not t:
            return "⚠️ Telemetry unavailable."
        link = "\U0001F7E2 CONNECTED" if t.get("connected") else "\U0001F534 DISCONNECTED"
        engine = "\U0001F7E2 RUNNING" if self._safe(self.bridge.is_running, False) else "\U0001F534 PAUSED"
        return (
            "\U0001F4CA <b>Account Telemetry</b>\n"
            "——————————————\n"
            f"Link: {link} · Engine: {engine}\n"
            f"Account: <code>{t.get('login')}</code> @ {t.get('server')}\n"
            f"\U0001F4B0 Balance: <b>${t.get('balance', 0):,.2f}</b>\n"
            f"\U0001F4B5 Equity: <b>${t.get('equity', 0):,.2f}</b>\n"
            f"\U0001F4CC Margin: ${t.get('margin', 0):,.2f} · Free: ${t.get('margin_free', 0):,.2f}\n"
            f"\U0001F4C8 Floating P/L: <b>${t.get('profit', 0):,.2f}</b>\n"
            f"\U0001FA99 {t.get('symbol')}: {t.get('bid', 0):,.2f} / {t.get('ask', 0):,.2f} "
            f"({t.get('spread_points', 0):.0f} pts)\n"
            f"\U0001F5C2 Open positions: <b>{len(t.get('positions', []))}</b>\n"
            f"⚙️ Strategy: <b>{self._safe(self.bridge.get_strategy, '-')}</b> "
            f"· Risk: <b>{self._safe(self.bridge.get_risk, 0.0):.2f}%</b>\n"
            f"\U0001F504 Reconnects: {t.get('reconnects', 0)}\n"
            f"\U0001F551 Server time: <code>{t.get('server_time', '-')}</code>"
        )

    def _positions_card(self) -> str:
        t = self._safe(self.bridge.telemetry, None) or {}
        positions = t.get("positions", [])
        if not positions:
            return "\U0001F4ED No open positions."
        lines = ["\U0001F4C8 <b>Open Positions</b>"]
        for p in positions:
            lines.append(
                f"• <code>{p['ticket']}</code> {p['side']} {p['volume']} @ "
                f"{p['price_open']:,.2f}\n"
                f"   SL {p['sl']:,.2f} · TP {p['tp']:,.2f} · "
                f"P/L <b>${p['profit']:,.2f}</b>"
            )
        return "\n".join(lines)

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        uid = query.from_user.id
        if not self.is_admin(uid):
            return
        data = query.data
        if data.startswith("strat:"):
            name = data.split(":", 1)[1]
            ok = bool(self._safe(self.bridge.set_strategy, False, name))
            await query.edit_message_text(
                f"⚙️ Strategy → <b>{name}</b>" if ok else "❌ Unknown strategy",
                parse_mode=ParseMode.HTML,
            )
        elif data.startswith("risk:"):
            value = float(data.split(":", 1)[1])
            applied = self._safe(self.bridge.set_risk, value, value)
            await query.edit_message_text(
                f"\U0001F39A Risk → <b>{applied:.2f}%</b>", parse_mode=ParseMode.HTML
            )
        elif data.startswith("cd:"):
            value = int(data.split(":", 1)[1])
            applied = self._safe(self.bridge.set_cooldown, value, value)
            mins = int(applied if applied is not None else value) * 5
            await query.edit_message_text(
                f"⏱️ وقفه معاملات → <b>{applied if applied is not None else value} کندل ({mins} دقیقه)</b>",
                parse_mode=ParseMode.HTML,
            )

    # ============================================================== send layer
    async def _send_text(
        self,
        chat_id: int,
        text: str,
        markup: Optional[InlineKeyboardMarkup] = None,
    ) -> bool:
        if self._app is None:
            return False
        for attempt in range(3):
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return True
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
            except Forbidden:
                logger.warning("chat {} blocked the bot; marking as blocked", chat_id)
                self.db.set_blocked(int(chat_id), True)
                return False
            except (BadRequest, TelegramError) as exc:
                logger.error("send_message to {} failed ({}): {}", chat_id, attempt + 1, exc)
                await asyncio.sleep(0.6 * (attempt + 1))
        return False

    async def _send_photo(
        self,
        chat_id: int,
        image: bytes,
        caption: str,
        markup: Optional[InlineKeyboardMarkup] = None,
    ) -> bool:
        if self._app is None:
            return False
        for attempt in range(3):
            try:
                await self._app.bot.send_photo(
                    chat_id=chat_id,
                    photo=io.BytesIO(image),
                    caption=caption[:1024],
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                return True
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
            except Forbidden:
                self.db.set_blocked(int(chat_id), True)
                return False
            except (BadRequest, TelegramError) as exc:
                logger.error("send_photo to {} failed ({}): {}", chat_id, attempt + 1, exc)
                await asyncio.sleep(0.6 * (attempt + 1))
        return await self._send_text(chat_id, caption, markup)

    def _vip_audience(self) -> List[int]:
        ids = [int(c) for c in self.config.vip_channel_ids]
        ids.extend(u.user_id for u in self.db.get_all_vip_users())
        ids.extend(self.config.admin_ids)
        seen: List[int] = []
        for i in ids:
            if i not in seen:
                seen.append(i)
        return seen

    # ============================================================== formatting
    def format_signal(self, signal: Dict[str, Any], full: bool = True) -> str:
        side = str(signal.get("side", "")).upper()
        badge = "\U0001F7E9 <b>BUY</b>" if side == "BUY" else "\U0001F7E5 <b>SELL</b>"
        symbol = signal.get("symbol", "XAUUSD")
        if not full:
            return (
                f"\U0001F514 <b>سیگنال جدید {symbol}</b>\n"
                f"{badge} · <code>M5</code>\n"
                "——————————————\n"
                "\U0001F512 Entry / SL / TP فقط برای "
                "اعضای <b>VIP</b> ارسال "
                "می‌شود.\n"
                "\U0001F4C8 همراه نمودار "
                "تحلیلی و مدیریت "
                "لحظه‌ای پوزیشن."
            )
        return (
            f"\U0001F4E1 <b>{symbol} · M5 SIGNAL</b>\n"
            f"{badge}\n"
            "——————————————\n"
            f"\U0001F3AF Entry: <b>{signal.get('entry', 0):,.2f}</b>\n"
            f"\U0001F6D1 SL: <b>{signal.get('sl', 0):,.2f}</b>\n"
            f"\U0001F3C1 TP: <b>{signal.get('tp', 0):,.2f}</b>\n"
            f"⚖️ Lot: <b>{signal.get('lot', 0):.2f}</b> · "
            f"Risk: <b>${signal.get('risk_money', 0):,.2f}</b>\n"
            f"\U0001F4CF ATR(14): {signal.get('atr', 0):,.2f} · R:R "
            f"{signal.get('rr', 0):.2f}\n"
            "——————————————\n"
            f"\U0001F9E9 <i>{signal.get('reason', '')}</i>\n"
            f"\U0001F511 <code>#{signal.get('ticket', 0)}</code> · "
            f"{signal.get('strategy', '')}"
        )

    @staticmethod
    def format_manage(action: Dict[str, Any]) -> str:
        kind = action.get("kind", "trailing")
        if kind == "breakeven":
            head = "\U0001F6E1️ <b>RISK-FREE · ریسک صفر شد</b>"
        else:
            head = "\U0001F4C8 <b>TRAILING STOP UPDATED</b>"
        return (
            f"{head}\n"
            f"\U0001F511 <code>#{action.get('ticket', 0)}</code> · "
            f"{action.get('side', '')}\n"
            f"\U0001F6D1 SL: <s>{action.get('old_sl', 0):,.2f}</s> → "
            f"<b>{action.get('new_sl', 0):,.2f}</b>\n"
            f"<i>{action.get('reason', '')}</i>"
        )

    # ====================================================== public sync API
    def broadcast_signal(
        self,
        signal: Dict[str, Any],
        chart: Optional[io.BytesIO] = None,
    ) -> None:
        image = chart.getvalue() if chart is not None else None
        self._submit(self._broadcast_signal_async(dict(signal), image))

    async def _broadcast_signal_async(
        self, signal: Dict[str, Any], image: Optional[bytes]
    ) -> None:
        full = self.format_signal(signal, full=True)
        teaser = self.format_signal(signal, full=False)

        vip_sent = 0
        for chat_id in self._vip_audience():
            ok = (
                await self._send_photo(chat_id, image, full)
                if (image and self.config.send_charts_to_vip)
                else await self._send_text(chat_id, full)
            )
            vip_sent += int(ok)
            await asyncio.sleep(0.05)

        free_sent = 0
        for chat_id in self.config.free_channel_ids:
            free_sent += int(await self._send_text(chat_id, teaser, self._vip_cta()))
            await asyncio.sleep(0.05)

        logger.info("signal broadcast → VIP:{} FREE:{}", vip_sent, free_sent)

    def broadcast_manage(self, action: Dict[str, Any]) -> None:
        if not self.config.send_manage_alerts_to_vip:
            return
        self._submit(self._broadcast_manage_async(dict(action)))

    async def _broadcast_manage_async(self, action: Dict[str, Any]) -> None:
        text = self.format_manage(action)
        for chat_id in self._vip_audience():
            await self._send_text(chat_id, text)
            await asyncio.sleep(0.05)

    def broadcast_close(self, payload: Dict[str, Any]) -> None:
        self._submit(self._broadcast_close_async(dict(payload)))

    async def _broadcast_close_async(self, payload: Dict[str, Any]) -> None:
        profit = float(payload.get("profit", 0.0))
        icon = "✅" if profit >= 0 else "❌"
        text = (
            f"{icon} <b>POSITION CLOSED</b>\n"
            f"\U0001F511 <code>#{payload.get('ticket', 0)}</code> · "
            f"{payload.get('side', '')} {payload.get('lot', 0):.2f}\n"
            f"\U0001F4B5 Result: <b>${profit:,.2f}</b>\n"
            f"\U0001F4CC Exit: {payload.get('close_price', 0):,.2f} "
            f"({payload.get('outcome', '')})"
        )
        for chat_id in self._vip_audience():
            await self._send_text(chat_id, text)
            await asyncio.sleep(0.05)

    def notify_admins(self, text: str) -> None:
        self._submit(self._notify_admins_async(text))

    async def _notify_admins_async(self, text: str) -> None:
        for admin_id in self.config.admin_ids:
            await self._send_text(admin_id, text)

    def notify_connection(self, connected: bool, reason: str) -> None:
        icon = "\U0001F7E2" if connected else "\U0001F534"
        state = "RESTORED" if connected else "LOST"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.notify_admins(
            f"{icon} <b>MT5 connection {state}</b>\n<code>{reason}</code>\n"
            f"<i>{stamp}</i>"
        )