"""Resilient MetaTrader5 wrapper: heartbeat, auto-reconnect, typed helpers.

Design notes
------------
* The ``MetaTrader5`` package is a *singleton* C-extension binding to one
  terminal.  It is **not** thread-safe, so every call in this module is taken
  under a single re-entrant lock.  The live loop and the Telegram thread can
  therefore both query the terminal safely.
* Position/deal read failures raise MT5ReadError: an unknown state must never
  be mistaken for an empty account. Other reads retain their optional results.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from loguru import logger

try:  # pragma: no cover - import guard keeps tooling/CI usable off-Windows
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False


# MT5 validates the order comment client-side; spaces, '+' and other symbols
# (or a long text) make order_send() return None with last_error == -2
# ("Invalid \"comment\" argument") -- the request never leaves the terminal.
COMMENT_MAX_LEN = 25
RES_E_INVALID_PARAMS = -2


def safe_comment(text: Optional[str], fallback: str = "bot", limit: int = COMMENT_MAX_LEN) -> str:
    """Return an order comment MT5 always accepts: [A-Za-z0-9_], <= ``limit`` chars."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(text or "")).strip("_")[:limit].strip("_")
    if cleaned:
        return cleaned
    fb = re.sub(r"[^A-Za-z0-9_]+", "_", str(fallback or "")).strip("_")[:limit].strip("_")
    return fb or "bot"
    logger.warning("MetaTrader5 package unavailable ({}); client is inert", exc)

__all__ = [
    "MT5_AVAILABLE",
    "MT5Client",
    "MT5Config",
    "MT5ReadError",
    "OrderResult",
    "TIMEFRAME_SECONDS",
    "timeframe_to_mt5",
]

TIMEFRAME_SECONDS: Dict[str, int] = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360,
    "M10": 600, "M12": 720, "M15": 900, "M20": 1200, "M30": 1800,
    "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200, "D1": 86400, "W1": 604800, "MN1": 2592000,
}

_RATE_COLUMNS: Tuple[str, ...] = (
    "time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume",
)


def timeframe_to_mt5(name: str) -> int:
    """Map ``"M5"`` -> ``mt5.TIMEFRAME_M5``."""
    key = str(name).upper().strip()
    if key not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported timeframe: {name!r}")
    if not MT5_AVAILABLE:
        return TIMEFRAME_SECONDS[key]
    attr = getattr(mt5, f"TIMEFRAME_{key}", None)
    if attr is None:
        raise ValueError(f"MetaTrader5 has no TIMEFRAME_{key}")
    return int(attr)


class MT5ReadError(RuntimeError):
    """The broker state could not be read after bounded retries."""


@dataclass
class MT5Config:
    """Everything the client needs to talk to a terminal."""

    login: int = 0
    password: str = ""
    server: str = ""
    terminal_path: str = ""
    timeout_ms: int = 60_000
    portable: bool = False
    symbol: str = "XAUUSD"
    fallback_symbols: List[str] = field(default_factory=list)
    magic: int = 553311
    deviation_points: int = 30
    comment: str = "gold_m5_bot"
    heartbeat_seconds: int = 30
    reconnect_backoff: List[int] = field(default_factory=lambda: [5, 10, 20, 40, 60])
    max_send_retries: int = 3

    @classmethod
    def from_settings(cls, settings: Any) -> "MT5Config":
        return cls(
            login=int(settings.get("mt5.login", 0) or 0),
            password=str(settings.get("mt5.password", "") or ""),
            server=str(settings.get("mt5.server", "") or ""),
            terminal_path=str(settings.get("mt5.terminal_path", "") or ""),
            timeout_ms=int(settings.get("mt5.timeout_ms", 60_000)),
            portable=bool(settings.get("mt5.portable", False)),
            symbol=str(settings.get("symbol.name", "XAUUSD")),
            fallback_symbols=list(settings.get("symbol.fallback_names", []) or []),
            magic=int(settings.get("symbol.magic", 553311)),
            deviation_points=int(settings.get("symbol.deviation_points", 30)),
            comment=str(settings.get("symbol.comment", "gold_m5_bot")),
            heartbeat_seconds=int(settings.get("engine.heartbeat_seconds", 30)),
            reconnect_backoff=list(
                settings.get("engine.reconnect_backoff_seconds", [5, 10, 20, 40, 60]) or [5]
            ),
            max_send_retries=int(settings.get("engine.max_send_retries", 3)),
        )


@dataclass
class OrderResult:
    """Normalised result of any trade request."""

    ok: bool
    retcode: int = -1
    comment: str = ""
    order: int = 0
    deal: int = 0
    position: int = 0
    price: float = 0.0
    volume: float = 0.0
    request: Dict[str, Any] = field(default_factory=dict)
    uncertain: bool = False

    def __str__(self) -> str:
        state = "OK" if self.ok else "FAIL"
        return f"[{state}] retcode={self.retcode} {self.comment} order={self.order} price={self.price}"


class MT5Client:
    """Thread-safe, self-healing facade over the MetaTrader5 API."""

    def __init__(
        self,
        config: MT5Config,
        on_state_change: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._connected = False
        self._resolved_symbol: Optional[str] = None
        self._symbol_info_cache: Optional[Any] = None
        self._symbol_cache_ts: float = 0.0
        self._on_state_change = on_state_change
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._last_error: Tuple[int, str] = (0, "")
        self.reconnect_count = 0

    # ------------------------------------------------------------------ props
    @property
    def symbol(self) -> str:
        return self._resolved_symbol or self.config.symbol

    @property
    def magic(self) -> int:
        return self.config.magic

    @property
    def last_error(self) -> Tuple[int, str]:
        return self._last_error

    @property
    def is_connected(self) -> bool:
        """True only when the terminal object reports an active broker link."""
        if not MT5_AVAILABLE:
            return False
        with self._lock:
            try:
                info = mt5.terminal_info()
                if info is None or not bool(getattr(info, "connected", False)):
                    self._connected = False
                    return False
                if mt5.account_info() is None:
                    self._connected = False
                    return False
                self._connected = True
                return True
            except Exception as exc:  # terminal killed mid-call
                logger.debug("is_connected probe failed: {}", exc)
                self._connected = False
                return False

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> bool:
        """Initialise the terminal, log in and select the gold symbol."""
        if not MT5_AVAILABLE:
            logger.error("cannot connect: MetaTrader5 package not importable")
            return False
        with self._lock:
            kwargs: Dict[str, Any] = {"timeout": self.config.timeout_ms}
            if self.config.portable:
                kwargs["portable"] = True
            if self.config.login:
                kwargs.update(
                    login=int(self.config.login),
                    password=self.config.password,
                    server=self.config.server,
                )
            try:
                ok = (
                    mt5.initialize(self.config.terminal_path, **kwargs)
                    if self.config.terminal_path
                    else mt5.initialize(**kwargs)
                )
            except Exception as exc:
                logger.error("mt5.initialize raised: {}", exc)
                return False

            if not ok:
                self._capture_error("initialize")
                return False

            if self.config.login:
                try:
                    if not mt5.login(
                        int(self.config.login),
                        password=self.config.password,
                        server=self.config.server,
                        timeout=self.config.timeout_ms,
                    ):
                        self._capture_error("login")
                        mt5.shutdown()
                        return False
                except Exception as exc:
                    logger.error("mt5.login raised: {}", exc)
                    return False

            if not self._resolve_symbol():
                logger.error("could not resolve a tradable gold symbol")
                return False

            acc = mt5.account_info()
            term = mt5.terminal_info()
            self._connected = bool(term and getattr(term, "connected", False))
            logger.success(
                "MT5 connected | login={} server={} balance={:.2f} {} | symbol={} | algo_trading={}",
                getattr(acc, "login", "?"),
                getattr(acc, "server", "?"),
                float(getattr(acc, "balance", 0.0)),
                getattr(acc, "currency", ""),
                self.symbol,
                getattr(term, "trade_allowed", "?"),
            )
            if term is not None and not getattr(term, "trade_allowed", True):
                logger.warning("Algo Trading is DISABLED in the terminal; orders will be rejected")
            self._notify_state(True, "connected")
            return True

    def _resolve_symbol(self) -> bool:
        candidates: List[str] = [self.config.symbol, *self.config.fallback_symbols]
        for name in candidates:
            if not name:
                continue
            info = mt5.symbol_info(name)
            if info is None:
                continue
            if not info.visible and not mt5.symbol_select(name, True):
                continue
            info = mt5.symbol_info(name)
            if info is None:
                continue
            self._resolved_symbol = name
            self._symbol_info_cache = info
            self._symbol_cache_ts = time.time()
            if name != self.config.symbol:
                logger.warning("symbol '{}' unavailable -> using '{}'", self.config.symbol, name)
            return True

        # last resort: fuzzy scan of the broker's whole symbol table
        try:
            for info in mt5.symbols_get() or []:
                upper = info.name.upper()
                if "XAU" in upper and "USD" in upper:
                    if info.visible or mt5.symbol_select(info.name, True):
                        self._resolved_symbol = info.name
                        self._symbol_info_cache = mt5.symbol_info(info.name)
                        self._symbol_cache_ts = time.time()
                        logger.warning("fuzzy-matched gold symbol -> '{}'", info.name)
                        return True
        except Exception as exc:
            logger.error("symbol scan failed: {}", exc)
        return False

    def shutdown(self) -> None:
        self._stop_heartbeat.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        with self._lock:
            if MT5_AVAILABLE:
                try:
                    mt5.shutdown()
                except Exception as exc:  # pragma: no cover
                    logger.debug("shutdown raised: {}", exc)
            self._connected = False
        logger.info("MT5 client shut down")

    def reconnect(self) -> bool:
        """Safely tear the terminal link down and rebuild it. Never raises."""
        with self._lock:
            self.reconnect_count += 1
            logger.warning("reconnect attempt #{} starting", self.reconnect_count)
            self._notify_state(False, "reconnecting")
            try:
                if MT5_AVAILABLE:
                    mt5.shutdown()
            except Exception as exc:
                logger.debug("shutdown during reconnect raised: {}", exc)
            self._connected = False
            self._symbol_info_cache = None

            backoff = self.config.reconnect_backoff or [5]
            for attempt, delay in enumerate(backoff, start=1):
                time.sleep(float(delay))
                try:
                    if self.connect():
                        logger.success("reconnected after {} attempt(s)", attempt)
                        return True
                except Exception as exc:
                    logger.error("reconnect attempt {} raised: {}", attempt, exc)
                logger.warning("reconnect attempt {}/{} failed", attempt, len(backoff))
            logger.error("reconnect exhausted backoff ladder; staying offline")
            return False

    def ensure_connected(self) -> bool:
        """Cheap guard for the top of the live loop."""
        if self.is_connected:
            return True
        return self.reconnect()

    # ------------------------------------------------------------- heartbeat
    def start_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="mt5-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        logger.info("heartbeat thread started ({}s)", self.config.heartbeat_seconds)

    def _heartbeat_loop(self) -> None:
        was_up = True
        interval = max(5, int(self.config.heartbeat_seconds))
        while not self._stop_heartbeat.wait(interval):
            up = self.is_connected
            if up and not was_up:
                logger.success("heartbeat: link RESTORED")
                self._notify_state(True, "restored")
            elif not up and was_up:
                logger.error("heartbeat: link LOST")
                self._notify_state(False, "lost")
            was_up = up

    def _notify_state(self, connected: bool, reason: str) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(connected, reason)
        except Exception as exc:  # a broken callback must never kill the client
            logger.error("state-change callback failed: {}", exc)

    # ------------------------------------------------------- internal guards
    def _capture_error(self, where: str) -> None:
        try:
            code, text = mt5.last_error()
        except Exception:
            code, text = -1, "unknown"
        self._last_error = (int(code), str(text))
        logger.error("MT5 {} failed: ({}) {}", where, code, text)

    def _guarded(self, label: str, fn: Callable[[], Any], retries: int = 2) -> Any:
        """Run ``fn`` under the lock, retrying and reconnecting when needed."""
        if not MT5_AVAILABLE:
            return None
        for attempt in range(1, retries + 2):
            with self._lock:
                try:
                    result = fn()
                except Exception as exc:
                    logger.error("{} raised on attempt {}: {}", label, attempt, exc)
                    result = None
                if result is not None:
                    return result
                self._capture_error(label)
            if attempt <= retries:
                if not self.is_connected:
                    self.reconnect()
                else:
                    time.sleep(0.35 * attempt)
        return None

    # ----------------------------------------------------------------- reads
    def account_info(self) -> Optional[Any]:
        return self._guarded("account_info", lambda: mt5.account_info())

    def balance(self) -> float:
        acc = self.account_info()
        return float(getattr(acc, "balance", 0.0)) if acc else 0.0

    def equity(self) -> float:
        acc = self.account_info()
        return float(getattr(acc, "equity", 0.0)) if acc else 0.0

    def symbol_info(self, refresh: bool = False) -> Optional[Any]:
        if (
            not refresh
            and self._symbol_info_cache is not None
            and time.time() - self._symbol_cache_ts < 30.0
        ):
            return self._symbol_info_cache
        info = self._guarded("symbol_info", lambda: mt5.symbol_info(self.symbol))
        if info is not None:
            self._symbol_info_cache = info
            self._symbol_cache_ts = time.time()
        return info

    def get_tick(self) -> Optional[Any]:
        return self._guarded("symbol_info_tick", lambda: mt5.symbol_info_tick(self.symbol))

    def spread_points(self) -> Optional[float]:
        tick = self.get_tick()
        info = self.symbol_info()
        if tick is None or info is None or not info.point:
            return None
        return float((tick.ask - tick.bid) / info.point)

    def server_time(self) -> Optional[datetime]:
        """Broker server clock, taken from the freshest tick.

        ``tick.time`` is the broker wall clock encoded as an epoch, so it must
        be decoded WITHOUT the machine's local timezone. Plain
        ``datetime.fromtimestamp`` applies the host TZ on top: on the VPS
        (UTC-7) that returned broker time minus 7 hours, which shifted the
        weekday gate and the daily guard window.
        """
        tick = self.get_tick()
        if tick is None:
            return None
        return datetime.fromtimestamp(int(tick.time), tz=timezone.utc).replace(tzinfo=None)

    def get_rates(self, timeframe: str, count: int, start_pos: int = 0) -> pd.DataFrame:
        """``copy_rates_from_pos`` -> tidy, time-indexed DataFrame."""
        tf = timeframe_to_mt5(timeframe)
        raw = self._guarded(
            f"copy_rates_from_pos[{timeframe}]",
            lambda: mt5.copy_rates_from_pos(self.symbol, tf, int(start_pos), int(count)),
        )
        return self._rates_to_frame(raw, timeframe)

    def get_rates_range(self, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        """``copy_rates_range`` -> tidy, time-indexed DataFrame."""
        tf = timeframe_to_mt5(timeframe)
        raw = self._guarded(
            f"copy_rates_range[{timeframe}]",
            lambda: mt5.copy_rates_range(self.symbol, tf, start, end),
        )
        return self._rates_to_frame(raw, timeframe)

    def get_history_bars(self, timeframe: str, bars: int, chunk: int = 20_000) -> pd.DataFrame:
        """Pull a deep history in chunks (terminals cap single requests)."""
        frames: List[pd.DataFrame] = []
        remaining = int(bars)
        pos = 0
        while remaining > 0:
            take = min(chunk, remaining)
            part = self.get_rates(timeframe, take, start_pos=pos)
            if part.empty:
                break
            frames.append(part)
            pos += take
            remaining -= take
            if len(part) < take:
                break
        if not frames:
            return pd.DataFrame(columns=list(_RATE_COLUMNS[1:]))
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    @staticmethod
    def _rates_to_frame(raw: Any, timeframe: str) -> pd.DataFrame:
        if raw is None or len(raw) == 0:
            logger.debug("no rates returned for {}", timeframe)
            return pd.DataFrame(columns=list(_RATE_COLUMNS[1:]))
        df = pd.DataFrame(raw)
        if "time" not in df.columns:
            return pd.DataFrame(columns=list(_RATE_COLUMNS[1:]))
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time").sort_index()
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype("float64")
        if "tick_volume" in df.columns:
            df["volume"] = df["tick_volume"].astype("float64")
        else:
            df["volume"] = 0.0
        return df[~df.index.duplicated(keep="last")]

    def positions(self, magic_only: bool = True) -> List[Any]:
        raw = self._guarded("positions_get", lambda: mt5.positions_get(symbol=self.symbol))
        if raw is None:
            raise MT5ReadError(f"positions unavailable: {self.last_error}")
        items = list(raw)
        if magic_only:
            items = [p for p in items if int(getattr(p, "magic", 0)) == self.magic]
        return items

    def position_by_ticket(self, ticket: int) -> Optional[Any]:
        raw = self._guarded("positions_get_ticket", lambda: mt5.positions_get(ticket=int(ticket)))
        if raw is None:
            raise MT5ReadError(f"position {ticket} unavailable: {self.last_error}")
        items = list(raw) if raw else []
        return items[0] if items else None

    def open_position_count(self) -> int:
        return len(self.positions(magic_only=True))

    def deals_since(self, since: datetime) -> List[Any]:
        raw = self._guarded(
            "history_deals_get",
            lambda: mt5.history_deals_get(since, datetime.now() + timedelta(days=1)),
        )
        if raw is None:
            raise MT5ReadError(f"deal history unavailable: {self.last_error}")
        return [d for d in raw if int(getattr(d, "magic", 0)) == self.magic]

    def deals_for_position(self, position_id: int) -> List[Any]:
        """Complete position history, including manual exits with another magic."""
        raw = self._guarded(
            "history_deals_get_position",
            lambda: mt5.history_deals_get(position=int(position_id)),
        )
        if raw is None:
            raise MT5ReadError(f"position history unavailable: {self.last_error}")
        return [d for d in raw if int(getattr(d, "position_id", 0)) == int(position_id)]

    def position_id_for_deal(self, deal_ticket: int) -> Optional[int]:
        """Resolve a market-order deal to its broker position identifier."""
        raw = self._guarded(
            "history_deals_get_ticket",
            lambda: mt5.history_deals_get(ticket=int(deal_ticket)),
        )
        if raw is None:
            raise MT5ReadError(f"deal {deal_ticket} unavailable: {self.last_error}")
        for deal in raw:
            position_id = int(getattr(deal, "position_id", 0) or 0)
            if position_id > 0:
                return position_id
        return None

    # ---------------------------------------------------------------- writes
    def _filling_modes(self) -> List[int]:
        """Broker-supported filling modes, best first."""
        if not MT5_AVAILABLE:
            return []
        info = self.symbol_info()
        modes: List[int] = []
        mask = int(getattr(info, "filling_mode", 0)) if info else 0
        # bit 1 = FOK, bit 2 = IOC (MT5 SYMBOL_FILLING_* flags)
        if mask & 1:
            modes.append(int(mt5.ORDER_FILLING_FOK))
        if mask & 2:
            modes.append(int(mt5.ORDER_FILLING_IOC))
        for fallback in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            if int(fallback) not in modes:
                modes.append(int(fallback))
        return modes

    def normalize_price(self, price: float) -> float:
        info = self.symbol_info()
        digits = int(getattr(info, "digits", 2)) if info else 2
        return round(float(price), digits)

    def normalize_volume(self, volume: float) -> float:
        info = self.symbol_info()
        if info is None:
            return round(float(volume), 2)
        step = float(getattr(info, "volume_step", 0.01)) or 0.01
        vmin = float(getattr(info, "volume_min", 0.01))
        vmax = float(getattr(info, "volume_max", 100.0))
        steps = round(float(volume) / step)
        vol = steps * step
        vol = max(vmin, min(vmax, vol))
        decimals = max(0, len(str(step).split(".")[-1])) if "." in str(step) else 0
        return round(vol, decimals)

    def send_market_order(
        self,
        side: str,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: Optional[str] = None,
    ) -> OrderResult:
        """TRADE_ACTION_DEAL market order with filling-mode fallback."""
        if not MT5_AVAILABLE:
            return OrderResult(False, comment="MetaTrader5 unavailable")
        if not self.ensure_connected():
            return OrderResult(False, comment="offline")

        side_u = side.upper()
        if side_u not in ("BUY", "SELL"):
            return OrderResult(False, comment=f"bad side {side!r}")

        info = self.symbol_info(refresh=True)
        tick = self.get_tick()
        if info is None or tick is None:
            return OrderResult(False, comment="no quote")

        order_type = mt5.ORDER_TYPE_BUY if side_u == "BUY" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if side_u == "BUY" else tick.bid)
        vol = self.normalize_volume(volume)

        base: Dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": vol,
            "type": order_type,
            "price": self.normalize_price(price),
            "deviation": int(self.config.deviation_points),
            "magic": int(self.magic),
            "comment": safe_comment(comment or self.config.comment, self.config.comment),
            "type_time": mt5.ORDER_TIME_GTC,
        }
        if sl is not None and sl > 0:
            base["sl"] = self.normalize_price(sl)
        if tp is not None and tp > 0:
            base["tp"] = self.normalize_price(tp)

        last: Optional[Any] = None
        for filling in self._filling_modes():
            request = dict(base, type_filling=filling)
            for attempt in range(1, int(self.config.max_send_retries) + 1):
                raised = False
                with self._lock:
                    try:
                        last = mt5.order_send(request)
                    except Exception as exc:
                        logger.error("order_send raised: {}", exc)
                        last, raised = None, True
                if last is None:
                    self._capture_error("order_send")
                    if not raised and self._last_error[0] == RES_E_INVALID_PARAMS:
                        # rejected by the terminal's own argument check: nothing was sent
                        return OrderResult(False, RES_E_INVALID_PARAMS,
                                           f"rejected before send: {self._last_error[1]}",
                                           request=request)
                    return OrderResult(False, comment="order outcome unknown; do not resend",
                                       request=request, uncertain=True)
                if last.retcode in (10008, 10012, 10028, 10031):
                    return OrderResult(False, int(last.retcode), str(last.comment),
                                       request=request, uncertain=True)
                if last.retcode in (mt5.TRADE_RETCODE_DONE, 10010):
                    logger.success(
                        "{} {} lots @ {} | sl={} tp={} ticket={}",
                        side_u, vol, last.price, base.get("sl"), base.get("tp"), last.order,
                    )
                    return OrderResult(
                        True, int(last.retcode), str(last.comment), int(last.order),
                        int(last.deal), int(getattr(last, "position", 0) or 0),
                        float(last.price), float(last.volume), request,
                        uncertain=(last.retcode == 10010),
                    )
                if last.retcode in (
                    mt5.TRADE_RETCODE_REQUOTE,
                    mt5.TRADE_RETCODE_PRICE_CHANGED,
                    mt5.TRADE_RETCODE_PRICE_OFF,
                ):
                    fresh = self.get_tick()
                    if fresh is not None:
                        request["price"] = self.normalize_price(
                            fresh.ask if side_u == "BUY" else fresh.bid
                        )
                    logger.warning("requote on attempt {}, retrying", attempt)
                    time.sleep(0.25)
                    continue
                if last.retcode in (
                    mt5.TRADE_RETCODE_INVALID_FILL,
                    getattr(mt5, "TRADE_RETCODE_UNSUPPORTED_FILL_POLICY", 10030),
                ):
                    logger.warning("filling mode {} rejected, trying next", filling)
                    break
                logger.error("order rejected: ({}) {}", last.retcode, last.comment)
                return OrderResult(
                    False, int(last.retcode), str(last.comment), request=request
                )

        code = int(getattr(last, "retcode", -1)) if last is not None else -1
        text = str(getattr(last, "comment", "send failed")) if last is not None else "send failed"
        return OrderResult(False, code, text, request=base)

    def modify_sltp(
        self,
        ticket: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> OrderResult:
        """TRADE_ACTION_SLTP on an open position."""
        if not MT5_AVAILABLE:
            return OrderResult(False, comment="MetaTrader5 unavailable")
        pos = self.position_by_ticket(ticket)
        if pos is None:
            return OrderResult(False, comment=f"position {ticket} not found")

        request: Dict[str, Any] = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": int(ticket),
            "sl": self.normalize_price(sl if sl is not None else pos.sl),
            "tp": self.normalize_price(tp if tp is not None else pos.tp),
            "magic": int(self.magic),
        }
        with self._lock:
            try:
                res = mt5.order_send(request)
            except Exception as exc:
                logger.error("modify_sltp raised: {}", exc)
                return OrderResult(False, comment=str(exc), request=request)

        if res is None:
            self._capture_error("order_send[SLTP]")
            return OrderResult(False, comment="no response", request=request)
        ok = res.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            logger.info("SLTP updated | ticket={} sl={} tp={}", ticket, request["sl"], request["tp"])
        else:
            logger.warning("SLTP rejected | ticket={} ({}) {}", ticket, res.retcode, res.comment)
        return OrderResult(ok, int(res.retcode), str(res.comment), request=request)

    def close_position(self, ticket: int, comment: str = "manual close") -> OrderResult:
        """Close an open position at market with an opposite deal."""
        if not MT5_AVAILABLE:
            return OrderResult(False, comment="MetaTrader5 unavailable")
        pos = self.position_by_ticket(ticket)
        if pos is None:
            return OrderResult(False, comment=f"position {ticket} not found")
        tick = self.get_tick()
        if tick is None:
            return OrderResult(False, comment="no quote")

        is_buy = int(pos.type) == int(mt5.ORDER_TYPE_BUY)
        request: Dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": float(pos.volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": int(ticket),
            "price": self.normalize_price(tick.bid if is_buy else tick.ask),
            "deviation": int(self.config.deviation_points),
            "magic": int(self.magic),
            "comment": safe_comment(comment, "close"),
            "type_time": mt5.ORDER_TIME_GTC,
        }
        for filling in self._filling_modes():
            with self._lock:
                try:
                    res = mt5.order_send(dict(request, type_filling=filling))
                except Exception as exc:
                    logger.error("close_position raised: {}", exc)
                    return OrderResult(False, comment=str(exc), request=request, uncertain=True)
            if res is None or res.retcode in (10008, 10012, 10028, 10031):
                return OrderResult(False, comment="close outcome unknown; reconcile before retry",
                                   request=request, uncertain=True)
            if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.success("closed position {} ({} lots)", ticket, pos.volume)
                return OrderResult(True, int(res.retcode), str(res.comment), price=float(res.price))
            if res.retcode != 10030:
                return OrderResult(False, int(res.retcode), str(res.comment), request=request,
                                   uncertain=(res.retcode == 10010))
        return OrderResult(False, comment="close failed", request=request)

    # ------------------------------------------------------------- telemetry
    def telemetry(self) -> Dict[str, Any]:
        """Snapshot used by the Telegram admin panel."""
        acc = self.account_info()
        tick = self.get_tick()
        info = self.symbol_info()
        positions = self.positions()
        return {
            "connected": self.is_connected,
            "symbol": self.symbol,
            "login": int(getattr(acc, "login", 0)) if acc else 0,
            "server": str(getattr(acc, "server", "-")) if acc else "-",
            "currency": str(getattr(acc, "currency", "USD")) if acc else "USD",
            "balance": float(getattr(acc, "balance", 0.0)) if acc else 0.0,
            "equity": float(getattr(acc, "equity", 0.0)) if acc else 0.0,
            "margin": float(getattr(acc, "margin", 0.0)) if acc else 0.0,
            "margin_free": float(getattr(acc, "margin_free", 0.0)) if acc else 0.0,
            "margin_level": float(getattr(acc, "margin_level", 0.0)) if acc else 0.0,
            "profit": float(getattr(acc, "profit", 0.0)) if acc else 0.0,
            "leverage": int(getattr(acc, "leverage", 0)) if acc else 0,
            "bid": float(getattr(tick, "bid", 0.0)) if tick else 0.0,
            "ask": float(getattr(tick, "ask", 0.0)) if tick else 0.0,
            "spread_points": (
                float((tick.ask - tick.bid) / info.point)
                if tick and info and info.point
                else 0.0
            ),
            "server_time": (
                datetime.fromtimestamp(int(tick.time), tz=timezone.utc)
                .replace(tzinfo=None)
                .strftime("%Y-%m-%d %H:%M:%S")
                if tick else "-"
            ),
            "positions": [
                {
                    "ticket": int(p.ticket),
                    "side": "BUY" if int(p.type) == 0 else "SELL",
                    "volume": float(p.volume),
                    "price_open": float(p.price_open),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "profit": float(p.profit),
                }
                for p in positions
            ],
            "reconnects": self.reconnect_count,
        }

    # -------------------------------------------------------- context manager
    def __enter__(self) -> "MT5Client":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()
