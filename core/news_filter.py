#!/usr/bin/env python3
"""Economic News Filter for XAUUSD M5 bot — Production v2.0.

Prevents trade entry during high-impact economic news windows.
Fetches weekly events from Forex Factory JSON calendar with native ISO-8601
parsing, intelligent all-day event filtering, and thread-safe fail-safe caching.

Architecture:
  * Synchronous cold-start: immediate calendar load before background refresh
  * ISO-8601 native parsing: automatic DST & timezone handling
  * Network-resilient: exponential backoff, logs warning, continues trading on failure
  * Thread-safe: RLock across all event reads/writes
  * All-day event filtering: skips bank holidays & non-actionable entries
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from loguru import logger


@dataclass
class EconomicEvent:
    """Single high-impact economic news event with UTC timestamp."""

    title: str
    currency: str
    impact: str  # "High", "Medium", "Low"
    event_time_utc: datetime
    forecast: Optional[str] = None
    previous: Optional[str] = None

    def __hash__(self) -> int:
        return hash((self.title, self.currency, self.event_time_utc))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EconomicEvent):
            return NotImplemented
        return (
            self.title == other.title
            and self.currency == other.currency
            and self.event_time_utc == other.event_time_utc
        )


@dataclass
class NewsFilterConfig:
    """Configuration for the economic news filter."""

    enabled: bool = True
    currencies: List[str] = field(default_factory=lambda: ["USD"])
    min_impact: str = "High"  # "High", "Medium", "Low"
    pause_minutes_before: int = 5
    pause_minutes_after: int = 5
    # FIX(NFP): رویدادهای درجه‌یک (NFP/CPI/FOMC/پاول) ساعت‌ها بازار را آشفته
    # نگه می‌دارند. پنجرهٔ بعد از این رویدادها جداگانه و طولانی‌تر است؛
    # صفر = غیرفعال (رفتار قبلی).
    pause_minutes_after_tier1: int = 0
    tier1_patterns: List[str] = field(default_factory=lambda: [
        "non-farm", "nfp", "cpi", "fomc", "federal funds", "powell",
    ])
    cache_refresh_hours: float = 1.0
    network_timeout_seconds: float = 5.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    # FIX(news): وقتی endpoint لایو در دسترس نیست (429/شبکه)، داده‌ها از
    # فایل محلی تاریخی (مثل بک‌تست) جایگزین می‌شوند — دیگر «بی‌داده» نمی‌مانیم.
    historical_file: str = "data/historical_news.json"
    # FIX(news): وقتی هیچ داده‌ای (شبکه و فایل) در دسترس نیست، به‌جای رفتار
    # «Clear/مجاز»، به‌صورت محافظه‌کارانه ورود مسدود شود (در صورت true).
    block_on_stale: bool = False
    # FIX(tz): زمان کندل‌های MT5 به وقت «سرور بروکر» است (مثلاً UTC+3 برای
    # MetaQuotes-Demo) ولی زمان رویدادهای تقویم UTC است. این آفست قبل از
    # مقایسه از زمان سرور کم می‌شود تا پنجره‌های بلاک دقیق باشند.
    # صفر یعنی «سرور = UTC» (رفتار قدیمی معیوب).
    server_utc_offset_hours: float = 0.0


class NewsFilter:
    """Thread-safe economic news filter with ISO-8601 parsing and fail-safe resilience."""

    # Forex Factory impact level mapping
    IMPACT_LEVELS = {"high": "High", "medium": "Medium", "low": "Low"}

    def __init__(self, config: NewsFilterConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._events: List[EconomicEvent] = []
        self._last_refresh: Optional[datetime] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_refresh = threading.Event()
        self._started = False
        self._calendar_url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        self._source: str = "none"   # "network" | "historical" | "none"

    def start(self) -> None:
        """Start news filter: synchronous cold-start, then background refresh thread.
        
        Cold-start ensures calendar is populated before trading begins, eliminating
        race condition where describe() shows empty cache at bot startup.
        """
        if not self.config.enabled:
            logger.info("news filter disabled in config")
            return

        logger.info("news filter cold-start: fetching calendar synchronously...")
        try:
            success = self.update_calendar()
            if success:
                logger.success("cold-start successful: calendar loaded")
            else:
                logger.warning("cold-start failed to fetch: will retry in background")
        except Exception as exc:
            logger.error("cold-start exception: {} | will retry in background", exc)

        # Now launch background refresh thread
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(
            target=self._background_refresh,
            daemon=True,
            name="news-filter-refresh",
        )
        self._refresh_thread.start()
        self._started = True
        logger.info("news filter background refresh started (interval: {:.1f}h)", self.config.cache_refresh_hours)

    def stop(self) -> None:
        """Stop background refresh thread gracefully."""
        if not self._started:
            return
        self._stop_refresh.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=5.0)
        logger.info("news filter stopped")

    def update_calendar(self) -> bool:
        """Fetch and parse events from Forex Factory calendar endpoint.

        On failure (network / HTTP error including rate-limit 429/5xx) the method
        automatically falls back to the local historical calendar
        (``config.historical_file``) so the live path never runs "blind".

        Returns:
            True if events are available (network OR fallback), False otherwise.
        """
        if not self.config.enabled:
            return True

        # ---------------------------------------------------------- network
        try:
            response = requests.get(
                self._calendar_url,
                timeout=self.config.network_timeout_seconds,
                headers={"User-Agent": "Mozilla/5.0 (XAUUSD gold-m5-bot; contact: owner)"},
            )
            response.raise_for_status()
            raw_events = response.json()

            parsed = self._parse_events(raw_events)
            if parsed:
                with self._lock:
                    self._events = parsed
                    self._last_refresh = datetime.now(timezone.utc)
                    self._source = "network"
                logger.success(
                    "calendar refreshed: {} high-impact {} events loaded (network)",
                    len(parsed), ",".join(self.config.currencies),
                )
                return True

            logger.warning("network calendar parsed but empty; falling back to historical file")
        except requests.RequestException as exc:
            logger.warning(
                "calendar fetch failed ({}); falling back to historical file",
                exc if not isinstance(exc, requests.HTTPError) or exc.response is None else f"HTTP {exc.response.status_code}",
            )
        except (ValueError, KeyError) as exc:
            logger.warning("calendar parse error ({}); falling back to historical file", exc)

        # ------------------------------------------------------ fallback file
        if self._load_historical():
            logger.success(
                "using local historical calendar {} (fallback after network failure)",
                self.config.historical_file,
            )
            return True

        logger.error("no news data available (network + local fallback both empty)")
        return False

    def _load_historical(self) -> bool:
        """Load events from the local historical calendar (same source as backtest).

        Only entries whose ``currency`` and ``impact`` pass the configured
        filters are kept, matching the network path.  The historical file has
        no timezone offset (UTC ISO-8601), so no DST pitfalls here.
        """
        path = Path(self.config.historical_file)
        if not path.exists():
            logger.warning("historical news file not found: {}", path)
            return False

        try:
            with path.open("r", encoding="utf-8") as fh:
                rows = json.load(fh)
        except Exception as exc:
            logger.error("failed loading historical news from {}: {}", path, exc)
            return False

        events: List[EconomicEvent] = []
        for item in rows if isinstance(rows, list) else []:
            try:
                title = str(item.get("title", "")).strip()
                currency = str(item.get("currency") or item.get("country") or "").strip().upper()
                impact = str(item.get("impact", "")).lower()
                date_str = str(item.get("time") or item.get("date") or "").strip()
                if not title or not currency or not date_str:
                    continue
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                event = EconomicEvent(
                    title=title,
                    currency=currency,
                    impact=self.IMPACT_LEVELS.get(impact, "Low"),
                    event_time_utc=dt.astimezone(timezone.utc),
                )
                if self._matches_filter(event):
                    events.append(event)
            except Exception as exc:
                logger.debug("error parsing historical event: {}", exc)
                continue

        if not events:
            logger.warning("historical file {} has no matching events", path)
            return False

        with self._lock:
            self._events = events
            self._last_refresh = datetime.now(timezone.utc)
            self._source = "historical"
        logger.info(
            "loaded {} matching events from historical calendar {}", len(events), path
        )
        return True

    def is_news_active(
        self,
        ts: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Check if ``ts`` (default: now UTC) is in a news blackout window.

        The caller may pass the *bar time* a signal was evaluated on so the
        decision is made against the exact bar, matching backtest behaviour.
        """
        if not self.config.enabled:
            return False, "News filter disabled"

        with self._lock:
            events = self._events.copy()

        # FIX(news): اگر هیچ داده‌ای (شبکه + fallback فایل) در دسترس نیست،
        # به‌جای «Clear/مجاز»، به‌صورت محافظه‌کارانه رفتار کنیم — بسته به
        # block_on_stale. پیش‌فرض فعلی false = رفتار قبلی (مجاز). با true:
        # ربات بدون تقویم خبری وارد معامله نمی‌شود.
        if not events:
            if self.config.block_on_stale:
                logger.warning("no news data available → trading blocked (block_on_stale)")
                return True, "No news calendar available — trading paused (block_on_stale)"
            return False, "Calendar empty (warming up, will update soon)"

        if ts is None:
            now_utc = datetime.now(timezone.utc)
        else:
            # FIX(tz): ts (زمان کندل سیگنال) به وقت «سرور بروکر» است (naive).
            # ابتدا با آفست پیکربندی‌شده به UTC تبدیل شود، وگرنه پنجره‌های
            # بلاک به اندازه‌ی اختلاف سرور با UTC جابه‌جا می‌شوند.
            if ts.tzinfo is None:
                ts = ts - timedelta(hours=self.config.server_utc_offset_hours)
                ts = ts.replace(tzinfo=timezone.utc)
            now_utc = ts.astimezone(timezone.utc)
        before_buffer = timedelta(minutes=self.config.pause_minutes_before)
        after_buffer = timedelta(minutes=self.config.pause_minutes_after)

        # Check all events for active blackout windows
        for event in sorted(events, key=lambda e: e.event_time_utc):
            # FIX(NFP): پنجرهٔ after برای رویدادهای درجه‌یک جدا و طولانی‌تر است.
            ev_after = after_buffer
            if (
                self.config.pause_minutes_after_tier1 > 0
                and self._is_tier1(event.title)
            ):
                ev_after = timedelta(minutes=self.config.pause_minutes_after_tier1)
            window_start = event.event_time_utc - before_buffer
            window_end = event.event_time_utc + ev_after

            if window_start <= now_utc <= window_end:
                time_to_event = (event.event_time_utc - now_utc).total_seconds() / 60
                if time_to_event > 0:
                    return (
                        True,
                        f"⚠️ {time_to_event:.0f} mins before {event.impact}-Impact {event.currency}: {event.title}",
                    )
                else:
                    mins_after = -time_to_event
                    return (
                        True,
                        f"⚠️ {mins_after:.0f} mins after {event.title} (Volatility Cooldown)",
                    )

        return False, "Clear"

    def get_next_event(self) -> Optional[Dict[str, Any]]:
        """Return next upcoming high-impact USD event with time remaining.

        Returns:
            Dict with: title, currency, impact, time_until_minutes, event_time_utc
            or None if no upcoming events within next 7 days.
        """
        if not self.config.enabled:
            return None

        with self._lock:
            events = self._events.copy()

        if not events:
            return None

        now_utc = datetime.now(timezone.utc)
        upcoming = [e for e in events if e.event_time_utc > now_utc]

        if not upcoming:
            return None

        next_event = min(upcoming, key=lambda e: e.event_time_utc)
        time_remaining = (next_event.event_time_utc - now_utc).total_seconds() / 60

        return {
            "title": next_event.title,
            "currency": next_event.currency,
            "impact": next_event.impact,
            "time_until_minutes": round(time_remaining, 1),
            "event_time_utc": next_event.event_time_utc.isoformat(),
        }

    def describe(self) -> Dict[str, Any]:
        """Return complete filter status for logs and Telegram updates."""
        with self._lock:
            event_count = len([e for e in self._events if self._matches_filter(e)])
            source = self._source
            last_refresh_str = (
                self._last_refresh.strftime("%Y-%m-%d %H:%M:%S UTC")
                if self._last_refresh
                else "pending (cold-start in progress)"
            )

        is_active, reason = self.is_news_active()
        next_event = self.get_next_event()

        return {
            "enabled": self.config.enabled,
            "status": "ACTIVE" if is_active else "CLEAR",
            "reason": reason,
            "source": source,
            "cached_high_impact_events": event_count,
            "last_refresh_utc": last_refresh_str,
            "next_event": next_event,
            "buffer_before_mins": self.config.pause_minutes_before,
            "buffer_after_mins": self.config.pause_minutes_after,
        }

    # ================================================================ private
    def _background_refresh(self) -> None:
        """Background daemon thread: refresh calendar with sane pacing.

        FIX(news): نسخه قبلی در صورت موفقیت (شبکه *یا* fallback) فقط
        ``backoff_base`` (۱ ثانیه) صبر می‌کرد → ربات هر ثانیه به endpoint
        درخواست می‌زد و خودش باعث 429 می‌شد! الان:
          * موفقیت شبکه → صبر کامل = cache_refresh_hours (پیش‌فرض ۱ ساعت)
          * شکست شبکه (حتی اگر fallback جواب داده) → exponential backoff
            2s, 4s, 8s, ... تا سقف cache_refresh_hours
        """
        consecutive_failures = 0

        while not self._stop_refresh.is_set():
            try:
                # FIX(news): اول صبر، بعد fetch — وگرنه ترد بلافاصله بعد از
                # cold-start یک درخواست دوباره می‌زند (double-tap در استارت).
                with self._lock:
                    network_ok = self._source == "network"

                if network_ok:
                    # داده شبکه تازه است → کامل به اندازه cache_refresh_hours صبر
                    self._stop_refresh.wait(self.config.cache_refresh_hours * 3600.0)
                    if self._stop_refresh.is_set():
                        break
                    consecutive_failures = 0
                else:
                    # آخرین تلاش شکست خورده (یا هنوز داده‌ای نیست) → backoff کوتاه
                    consecutive_failures += 1
                    wait_seconds = min(
                        self.config.cache_refresh_hours * 3600.0,
                        self.config.backoff_base_seconds
                        * (2 ** consecutive_failures),
                    )
                    if consecutive_failures <= 6:
                        logger.info(
                            "news network refresh failed ({} consecutive); next try in {:.0f}s "
                            "(historical fallback active in the meantime)",
                            consecutive_failures, wait_seconds,
                        )
                    self._stop_refresh.wait(wait_seconds)

                self.update_calendar()

            except Exception as exc:
                logger.exception("unexpected error in news filter refresh loop: {}", exc)
                self._stop_refresh.wait(30.0)

    def _parse_events(self, raw: Any) -> List[EconomicEvent]:
        """Parse Forex Factory JSON response with native ISO-8601 timezone handling.

        Expected format from FF endpoint:
          [
            {
              "date": "2026-08-19T08:30:00-04:00",  # ISO-8601 with TZ offset
              "title": "Non-Farm Payrolls",
              "currency": "USD",
              "impact": "high",  # "high", "medium", "low"
              "forecast": "...",
              "previous": "..."
            },
            ...
          ]

        Handles:
          * ISO-8601 strings with embedded timezone (EDT, EST, etc.)
          * All-day events, bank holidays (skipped)
          * Malformed entries (logged, skipped)
        """
        events: List[EconomicEvent] = []

        if not isinstance(raw, list):
            logger.warning("calendar response not a list")
            return events

        for item in raw:
            try:
                # Extract required fields
                title = str(item.get("title", "")).strip()
                # خط اصلاح‌شده و ضد باگ:
                currency = str(item.get("country") or item.get("currency") or "").strip().upper()
                impact_raw = str(item.get("impact", "low")).lower()
                date_str = str(item.get("date", "")).strip()

                if not title or not currency or not date_str:
                    continue

                # Skip all-day events, bank holidays, non-actionable entries
                if self._is_all_day_or_non_actionable(title, date_str):
                    logger.debug("skipping non-actionable event: {}", title)
                    continue

                # Parse ISO-8601 datetime with timezone info
                try:
                    event_time_utc = self._parse_iso8601(date_str)
                    if event_time_utc is None:
                        logger.debug("skipping event with unparseable time: {}", date_str)
                        continue
                except Exception as exc:
                    logger.debug("time parse error for {}: {}", title, exc)
                    continue

                # Map impact level
                impact = self.IMPACT_LEVELS.get(impact_raw, "Low")

                # Filter by currency and impact level
                if not self._matches_filter_fields(currency, impact):
                    continue

                # Create event
                event = EconomicEvent(
                    title=title,
                    currency=currency,
                    impact=impact,
                    event_time_utc=event_time_utc,
                    forecast=item.get("forecast"),
                    previous=item.get("previous"),
                )
                events.append(event)

            except Exception as exc:
                logger.debug("error parsing event: {}", exc)
                continue

        return events

    def _is_all_day_or_non_actionable(self, title: str, date_str: str) -> bool:
        """Filter out bank holidays, all-day events, and non-actionable entries."""
        title_lower = title.lower()
        date_lower = date_str.lower()

        # All-day markers
        if any(marker in title_lower for marker in ["all day", "holiday", "bank holiday", "weekend"]):
            return True

        if any(marker in date_lower for marker in ["all day", "00:00:00"]):
            return True

        # Bank holiday patterns
        if any(holiday in title_lower for holiday in ["independence day", "christmas", "thanksgiving", "new year", "easter", "labour day"]):
            return True

        return False

    def _parse_iso8601(self, date_str: str) -> Optional[datetime]:
        """Parse ISO-8601 string with timezone to UTC datetime.

        Handles formats like:
          2026-08-19T08:30:00-04:00
          2026-08-19T08:30:00+00:00
          2026-08-19T08:30:00Z
        """
        try:
            # Standard Python 3.7+ method: fromisoformat handles ISO-8601 + TZ
            dt = datetime.fromisoformat(date_str)

            # Ensure timezone-aware and convert to UTC
            if dt.tzinfo is None:
                logger.warning("timezone-naive event time: {}, assuming UTC", date_str)
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(timezone.utc)

        except ValueError:
            # Try alternative parsing if fromisoformat fails
            try:
                # Handle Z suffix (UTC)
                if date_str.endswith("Z"):
                    date_str = date_str[:-1] + "+00:00"

                dt = datetime.fromisoformat(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                return dt.astimezone(timezone.utc)

            except ValueError:
                return None

    def _matches_filter(self, event: EconomicEvent) -> bool:
        """Check if event passes currency and impact filters."""
        return self._matches_filter_fields(event.currency, event.impact)

    def _is_tier1(self, title: str) -> bool:
        """Check if an event title matches a tier-1 (market-moving) pattern."""
        title_lower = title.lower()
        return any(p in title_lower for p in self.config.tier1_patterns)

    def _matches_filter_fields(self, currency: str, impact: str) -> bool:
        """Check currency and impact against config."""
        if currency not in self.config.currencies:
            return False

        impact_order = {"Low": 0, "Medium": 1, "High": 2}
        min_level = impact_order.get(self.config.min_impact, 2)
        current_level = impact_order.get(impact, 0)

        return current_level >= min_level
