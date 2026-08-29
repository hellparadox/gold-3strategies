# -*- coding: utf-8 -*-
"""Isolated news filter validation — runs WITHOUT the bot / MT5.

Covers:
  1. network fetch fails (429) -> automatic fallback to data/historical_news.json
  2. is_news_active(ts) blocks within ±pause minutes, Clear outside
  3. block_on_stale=True blocks when NO data at all
  4. block_on_stale=False keeps legacy permissive behaviour (no data -> Clear)
  5. enabled=False -> always Clear, no network/no file touched
"""
import sys
from datetime import datetime, timedelta, timezone

import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import core.news_filter as nfmod
from core.news_filter import EconomicEvent, NewsFilter, NewsFilterConfig
from loguru import logger
from core.news_filter import NewsFilter, NewsFilterConfig
from loguru import logger

logger.remove()  # keep output clean in this test

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✅" if cond else "❌"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"{mark} {label}" + (f"  — {detail}" if detail else ""))


def make_filter(**over) -> NewsFilter:
    cfg = NewsFilterConfig(
        enabled=over.get("enabled", True),
        currencies=["USD"],
        min_impact="High",
        pause_minutes_before=int(over.get("pause_minutes_before", 5)),
        pause_minutes_after=int(over.get("pause_minutes_after", 5)),
        network_timeout_seconds=float(over.get("network_timeout_seconds", 4.0)),
        max_retries=1,
        historical_file=over.get("historical_file", "data/historical_news.json"),
        block_on_stale=bool(over.get("block_on_stale", False)),
    )
    return NewsFilter(cfg)


NOW = datetime.now(timezone.utc)

print("=" * 66)
print("NEWS FILTER — isolated validation (no bot, no MT5)")
print("=" * 66)

# ---- 1. fallback from broken network to local historical file ----
print("\n[1] fallback · network fails -> local historical file")
nf = make_filter(network_timeout_seconds=2.0)
# Deterministic network failure (connection refused) — do NOT depend on the
# real endpoint; we want to prove the local fallback path explicitly.
nf._calendar_url = "http://127.0.0.1:1/force_network_fail"
ok = nf.update_calendar()
check("update_calendar() returns True (fallback active)", ok)
check("events are loaded from historical data", len(nf._events) > 0, f"{len(nf._events)} events")
check("source == 'historical' (network was down)", nf._source == "historical", nf._source)

# ---- 2. exact-bar blocking (± 5 min) using a real historical event ----
print("\n[2] exact-bar blackout window (±5 min)")
if nf._events:
    ev = nf._events[0]
    ev_t = ev.event_time_utc
    check("blocked 4 min BEFORE event", bool(nf.is_news_active(ev_t - timedelta(minutes=4))[0]))
    check("blocked 3 min AFTER event", bool(nf.is_news_active(ev_t + timedelta(minutes=3))[0]))
    check("clear 30 min BEFORE event", not nf.is_news_active(ev_t - timedelta(minutes=30))[0])
    check("clear 30 min AFTER event", not nf.is_news_active(ev_t + timedelta(minutes=30))[0])
    check("blocked EXACTLY at event time", bool(nf.is_news_active(ev_t)[0]))
else:
    check("(skip: no events)", False)

# ---- 3. wall-clock API still works and returns tuple ----
print("\n[3] wall-clock api")
r = nf.is_news_active()
check("is_news_active() returns (bool, str)", isinstance(r, tuple) and isinstance(r[0], bool) and isinstance(r[1], str),
      str(r))

# ---- 4. block_on_stale ----
print("\n[4] block_on_stale (no data anywhere)")
nf_stale = make_filter(historical_file="data/DOES_NOT_EXIST.json", block_on_stale=True, network_timeout_seconds=1.0)
nf_stale._events = []          # simulate both sources empty
nf_stale._source = "none"
b, why = nf_stale.is_news_active()
check("block_on_stale=True -> blocked when empty", b, why)

nf_perm = make_filter(historical_file="data/DOES_NOT_EXIST.json", block_on_stale=False, network_timeout_seconds=1.0)
nf_perm._events = []
b2, why2 = nf_perm.is_news_active()
check("block_on_stale=False -> permissive when empty (legacy)", not b2, why2)

# ---- 5. disabled filter ----
# ---- 6. signal simulation · news INJECTED near the signal time ----
print("\n[6] signal simulation · news injected 2 min ahead (must BLOCK)")
nf_sig = make_filter()
nf_sig._events = [
    EconomicEvent(
        title="Simulated Non-Farm Payrolls",
        currency="USD",
        impact="High",
        event_time_utc=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
]
b_sig, why_sig = nf_sig.is_news_active()  # wall-clock = now → inside ±5min window
check("signal BLOCKED 2 min before injected news", b_sig, why_sig)
check("reason mentions the injected event", "Non-Farm Payrolls" in why_sig, why_sig)

# ---- 7. normal simulation · no news near signal time ----
print("\n[7] normal simulation · next news 30 min away (must ALLOW)")
nf_norm = make_filter()
nf_norm._events = [
    EconomicEvent(
        title="Simulated CPI",
        currency="USD",
        impact="High",
        event_time_utc=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
]
b_norm, why_norm = nf_norm.is_news_active()
check("signal ALLOWED when news is 30 min away", not b_norm, why_norm)

# ---- 8. pacing · background refresh must NOT hammer the endpoint ----
print("\n[8] pacing · refresh thread no longer requests every ~1s")


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


_ev_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
_payload = [{"title": "Fake CPI", "currency": "USD", "impact": "high", "date": _ev_iso}]
_orig_get = nfmod.requests.get

# 8a. network OK → exactly ONE fetch, then wait cache_refresh_hours (3600s)
_calls_ok = {"n": 0}


def _fake_get_ok(url, timeout=None, headers=None):
    _calls_ok["n"] += 1
    return _FakeResp(_payload)


nfmod.requests.get = _fake_get_ok
try:
    nf_ok = make_filter()
    nf_ok.start()
    time.sleep(2.5)          # old buggy code would refetch every ~1s (≈3 fetches)
    nf_ok.stop()
finally:
    nfmod.requests.get = _orig_get
check("network OK → 1 fetch in 2.5s (not hammered)", _calls_ok["n"] == 1, f"requests={_calls_ok['n']}")
check("source == 'network'", nf_ok._source == "network", nf_ok._source)
check("injected future event parsed", any("Fake CPI" in e.title for e in nf_ok._events))
check("event 2h ahead → wall-clock Clear", not nf_ok.is_news_active()[0])

# 8b. network FAILS + fallback OK → exponential backoff (2s, 4s, …) not 1s loop
_calls_bad = {"n": 0}


def _fake_get_bad(url, timeout=None, headers=None):
    _calls_bad["n"] += 1
    raise nfmod.requests.exceptions.ConnectionError("simulated outage")


# ---- 9. timezone · server(+3) candle time vs UTC events (the 3-hour bug) ----
print("\n[9] timezone · server-time bars vs UTC events (offset fix)")
from backtest.engine import HistoricalNewsChecker

ev_utc = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)   # خبر: 12:30 UTC
server_bar = datetime(2026, 8, 28, 15, 30)                    # همان لحظه روی بروکر UTC+3 (naive)

# مسیر لایو: is_news_active با زمانِ کندل سرور
nf_off = make_filter()
nf_off.config.server_utc_offset_hours = 3.0
nf_off._events = [EconomicEvent(title="TZ NFP", currency="USD", impact="High", event_time_utc=ev_utc)]
b_tz, why_tz = nf_off.is_news_active(server_bar)
check("live: server 15:30 (=12:30 UTC) BLOCKED with offset=3", b_tz, why_tz)

nf_bug = make_filter()          # آفست پیش‌فرض 0.0 = رفتار قدیمی معیوب
nf_bug._events = list(nf_off._events)
b_bug, _ = nf_bug.is_news_active(server_bar)
check("live: offset=0 reproduces the old bug (block missed)", not b_bug)

# مسیر بک‌تست: HistoricalNewsChecker
h_off = HistoricalNewsChecker(enabled=True, server_utc_offset_hours=3.0)
h_off.event_timestamps = [ev_utc.timestamp()]
check("engine: server bar BLOCKED with offset=3", h_off.is_blocked(pd.Timestamp(server_bar)))

h_bug = HistoricalNewsChecker(enabled=True)   # آفست 0.0 = قدیمی
h_bug.event_timestamps = [ev_utc.timestamp()]
check("engine: offset=0 misses the block (documents bug)", not h_bug.is_blocked(pd.Timestamp(server_bar)))

# wall-clock نباید تحت تأثیر آفست باشد (ts=None)
b_wall, _ = nf_off.is_news_active()   # الان خبری نیست → Clear
check("wall-clock path unaffected by offset", isinstance(b_wall, bool))

nfmod.requests.get = _fake_get_bad
try:
    nf_bad = make_filter()
    nf_bad.start()
    time.sleep(6.0)          # new: attempts @0s,2s,6s = 3 · old buggy: ≈6 fetches
    nf_bad.stop()
finally:
    nfmod.requests.get = _orig_get
check("network down → ≤4 attempts in 6s (backoff works)", _calls_bad["n"] <= 4, f"requests={_calls_bad['n']}")
check("fallback still active during outage", nf_bad._source == "historical", nf_bad._source)
check("fallback data available (not blind)", len(nf_bad._events) > 0, f"{len(nf_bad._events)} events")


print("\n[5] disabled filter")
nf_dis = make_filter(enabled=False)
ok_d = nf_dis.update_calendar()
b3, why3 = nf_dis.is_news_active()
check("update_calendar with enabled=False returns True", ok_d)
check("enabled=False -> never blocked", not b3, why3)

# ---- summary ----
print("\n" + "=" * 66)
print(f"RESULT: {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)