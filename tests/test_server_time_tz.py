"""FIX (2026-09-16) — server_time() clock-skew regression test.

`MT5Client.server_time()` used ``datetime.fromtimestamp(int(tick.time))``.
``tick.time`` is the broker WALL CLOCK encoded as an epoch, so decoding it with
the machine's local timezone stacked that offset on top of broker time:

    VPS Windows clock = UTC-7  -> server_time() returned broker time MINUS 7h
    dev box (Tehran)  = UTC+3:30 -> returns broker time PLUS 3h30

Two live decision paths consume it and were shifted with it:
  1. ``LiveBot._session_open`` (weekday gate) — live only traded broker
     Mon 07:00 -> Sat 07:00 while ``backtest/engine.py`` checks broker bar
     timestamps and trades from Mon 00:00 (weekly reopen silently skipped).
  2. ``LiveBot._sync_daily`` — the daily guard window ran broker 07:00 -> 07:00
     and ``deals_since(day_start)`` started 7h early.

This test pins:
  1. server_time() == broker wall clock, INDEPENDENT of the host timezone;
  2. no tick -> None;
  3. the weekday gate accepts broker-Monday 03:00 (the window the skew blocked)
     and rejects Saturday, using the real LiveBot._session_open code;
  4. a source guard so the bare (TZ-dependent) conversion cannot come back.
"""
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from core.mt5_client import MT5Client
from main_live import LiveBot

REPO_ROOT = Path(__file__).resolve().parents[1]

# Broker wall clock we want back, and the epoch MT5 would report for it.
BROKER_WALL = datetime(2026, 9, 16, 13, 44, 4)
TICK_EPOCH = int(BROKER_WALL.replace(tzinfo=timezone.utc).timestamp())


def _stub_client(tick):
    """Minimal stand-in exposing only what server_time() touches."""
    return SimpleNamespace(get_tick=lambda: tick)


def _utc_naive(epoch: int) -> datetime:
    """Epoch -> naive broker wall clock (what the fixed code must return)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


class ServerTimeTimezoneTest(unittest.TestCase):
    def test_returns_broker_wall_clock_not_host_clock(self):
        tick = SimpleNamespace(time=TICK_EPOCH)
        got = MT5Client.server_time(_stub_client(tick))
        self.assertEqual(got, BROKER_WALL)
        self.assertEqual(got, _utc_naive(TICK_EPOCH))

    def test_old_expression_was_host_dependent(self):
        """The old form equals the new one PLUS the host's UTC offset."""
        tick = SimpleNamespace(time=TICK_EPOCH)
        old_form = datetime.fromtimestamp(tick.time)
        new_form = MT5Client.server_time(_stub_client(tick))
        host_offset = datetime.fromtimestamp(TICK_EPOCH) - _utc_naive(TICK_EPOCH)
        self.assertEqual(old_form - new_form, host_offset)

    def test_no_tick_returns_none(self):
        self.assertIsNone(MT5Client.server_time(_stub_client(None)))


class SessionGateAlignmentTest(unittest.TestCase):
    """The gate must judge BROKER weekday, matching backtest/engine.py."""

    def _bot(self):
        return SimpleNamespace(
            trade_weekdays=[0, 1, 2, 3, 4], session_start=0, session_end=24
        )

    def test_broker_monday_early_hours_are_tradable(self):
        # 2026-09-14 is a Monday; 03:00 broker = the weekly reopen window that
        # the 7h skew turned into "Sunday 20:00" and silently blocked.
        dt = datetime(2026, 9, 14, 3, 0)
        self.assertEqual(dt.weekday(), 0)
        self.assertTrue(LiveBot._session_open(self._bot(), dt))

    def test_weekend_is_rejected(self):
        self.assertFalse(LiveBot._session_open(self._bot(), datetime(2026, 9, 12, 10, 0)))
        self.assertFalse(LiveBot._session_open(self._bot(), datetime(2026, 9, 13, 10, 0)))

    def test_friday_close_is_tradable(self):
        self.assertTrue(LiveBot._session_open(self._bot(), datetime(2026, 9, 11, 20, 0)))

    def test_none_is_closed(self):
        self.assertFalse(LiveBot._session_open(self._bot(), None))


class NoBareFromtimestampTest(unittest.TestCase):
    """Source guard: every tick.time decode must pin tz=timezone.utc."""

    PATTERN = re.compile(r"fromtimestamp\(\s*int\(tick\.time\)\s*\)")

    def test_no_host_tz_dependent_conversion_left(self):
        offenders = []
        for rel in ("core/mt5_client.py", "main_live.py"):
            for n, line in enumerate((REPO_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
                if self.PATTERN.search(line):
                    offenders.append(f"{rel}:{n}")
        self.assertEqual(offenders, [], f"TZ-dependent tick.time decode at {offenders}")


if __name__ == "__main__":
    unittest.main()
