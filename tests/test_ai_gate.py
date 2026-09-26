"""AI pre-trade gate: config, context (look-ahead), parsing, provider, gate, live/shadow wiring.

All tests are offline: the provider is a fake or ``requests`` is mocked.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger  # noqa: E402

from core import Settings  # noqa: E402
from core.ai_gate import AIGate, GateResult, parse_decision  # noqa: E402
from core.ai_gate.config import (  # noqa: E402
    AIGateConfig, ConfigError, ContextConfig, load_config, read_state, resolve_mode, state_path,
    write_state,
)
from core.ai_gate.context import (  # noqa: E402
    ContextError, TradePlan, build_context, server_offset_hours, session_name,
)
from core.ai_gate.decision import InvalidDecision  # noqa: E402
from core.ai_gate.gate import MAX_INFLIGHT, hash_key  # noqa: E402
from core.ai_gate.provider import (  # noqa: E402
    FakeProvider, OpenAICompatibleProvider, ProviderError, redact,
)
from core.database import Database  # noqa: E402
from core.mt5_client import MT5Config, OrderResult  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager, SymbolSpec  # noqa: E402
from main_live import LiveBot, PendingEntry, TrackedPosition  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = os.path.join(ROOT, "config", "ai_gate_prompt.md")
TAKE = json.dumps({"decision": "TAKE", "confidence": 70, "p_protect": 0.6,
                   "reasons": ["روند ساعتی هم‌جهت است"], "risk_flags": []})
SKIP_HI = json.dumps({"decision": "SKIP", "confidence": 80, "p_protect": 0.3,
                      "reasons": ["فاصله از کیجون 2.4 ATR"], "risk_flags": ["stretched_from_kijun"]})
SKIP_LO = json.dumps({"decision": "SKIP", "confidence": 50, "p_protect": 0.45,
                      "reasons": ["شواهد مختلط"], "risk_flags": []})


def write_yaml(folder, text):
    path = os.path.join(folder, "ai_gate.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def cfg(tmp, **kw):
    values = dict(mode="shadow", model="m", prompt_file=PROMPT, journal_dir=tmp)
    values.update(kw)
    return AIGateConfig(**values).validate()


def wait(fut, timeout=5.0):
    return fut.result(timeout=timeout)


# ============================================================================ config
class ConfigTests(unittest.TestCase):
    def test_missing_file_is_off(self):
        self.assertEqual(load_config("ichimoku_m15", "/nonexistent/ai_gate.yaml").mode, "off")

    def test_repo_config_is_valid(self):
        path = os.path.join(ROOT, "config", "ai_gate.yaml")
        self.assertEqual(load_config("ichimoku_m15", path).mode, "shadow")
        self.assertEqual(load_config("orb_gold", path).mode, "off")
        self.assertEqual(load_config("unknown_strategy", path).mode, "off")
        self.assertEqual(load_config("ichimoku_m15", path).model, "")   # disabled until configured

    def test_defaults_merged_with_bot_override(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write_yaml(tmp, "defaults:\n  mode: shadow\n  timeout_seconds: 9\n  context:\n"
                                   "    trigger_bars: 30\nbots:\n  a:\n    mode: live\n    context:\n"
                                   "      h1_bars: 10\n")
            a = load_config("a", path)
            self.assertEqual((a.mode, a.timeout_seconds, a.context.trigger_bars, a.context.h1_bars),
                             ("live", 9, 30, 10))
            self.assertEqual(load_config("b", path).mode, "shadow")

    def test_bare_yaml_off_is_off(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write_yaml(tmp, "defaults:\n  mode: off\n")
            self.assertEqual(load_config("x", path).mode, "off")
            path = write_yaml(tmp, "defaults:\n  mode: on\n")
            with self.assertRaises(ConfigError):
                load_config("x", path)

    def test_invalid_values_rejected(self):
        bad = [dict(mode="auto"), dict(timeout_seconds=0), dict(block_min_confidence=101),
               dict(fail_policy="maybe"), dict(max_retries=9), dict(temperature=3),
               dict(base_url="ftp://x"), dict(max_entry_drift_atr=0), dict(max_calls_per_day=-1),
               dict(price_input_per_mtok=-1), dict(context=ContextConfig(trigger_bars=5)),
               dict(api_key_env="BAD NAME")]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ConfigError):
                AIGateConfig(**kw).validate()

    def test_unknown_keys_rejected(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with self.assertRaises(ConfigError):
                load_config("x", write_yaml(tmp, "defaults:\n  modee: live\n"))
            with self.assertRaises(ConfigError):
                load_config("x", write_yaml(tmp, "defaults:\n  context:\n    bars: 3\n"))

    def test_runtime_override_wins_while_yaml_unchanged(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = state_path("s", tmp)
            write_state(path, "live", "shadow")
            self.assertEqual(resolve_mode("shadow", path), ("live", "runtime"))

    def test_yaml_edit_beats_old_override(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = state_path("s", tmp)
            write_state(path, "live", "shadow")
            self.assertEqual(resolve_mode("off", path), ("off", "yaml"))
            self.assertEqual(read_state(path)["mode"], "off")          # rewritten, not deleted

    def test_state_write_is_atomic(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = state_path("s", tmp)
            write_state(path, "shadow", "off")
            write_state(path, "live", "off")
            self.assertEqual(os.listdir(tmp), [os.path.basename(path)])  # no temp leftovers
            self.assertEqual(read_state(path)["mode"], "live")
            with self.assertRaises(ConfigError):
                write_state(path, "turbo", "off")


# ========================================================================== decision
class DecisionTests(unittest.TestCase):
    def test_valid(self):
        d = parse_decision(SKIP_HI)
        self.assertEqual((d.decision, d.confidence, d.p_protect), ("SKIP", 80, 0.3))
        self.assertEqual(d.risk_flags, ["stretched_from_kijun"])

    def test_fenced_and_surrounding_prose(self):
        self.assertEqual(parse_decision("```json\n" + TAKE + "\n```").decision, "TAKE")
        self.assertEqual(parse_decision("Here you go: " + TAKE + " thanks").decision, "TAKE")

    def test_lowercase_decision_normalised_and_extra_keys_ignored(self):
        obj = json.loads(TAKE)
        obj.update(decision="take", extra=1)
        self.assertEqual(parse_decision(json.dumps(obj)).decision, "TAKE")

    def test_rejections(self):
        base = json.loads(TAKE)
        cases = [dict(decision="WAIT"), dict(confidence=72.5), dict(confidence=101),
                 dict(confidence=True), dict(confidence="70"), dict(p_protect=1.2),
                 dict(p_protect=None), dict(reasons="x"), dict(risk_flags=[1])]
        for change in cases:
            obj = dict(base, **change)
            with self.subTest(change=change), self.assertRaises(InvalidDecision):
                parse_decision(json.dumps(obj))
        for text in ("", "no json here", "[1,2]"):
            with self.subTest(text=text), self.assertRaises(InvalidDecision):
                parse_decision(text)

    def test_long_reasons_and_flags_trimmed(self):
        obj = dict(json.loads(TAKE), reasons=["x" * 500] * 9, risk_flags=["Wide Spread!"] * 12)
        d = parse_decision(json.dumps(obj))
        self.assertEqual(len(d.reasons), 5)
        self.assertEqual(len(d.reasons[0]), 160)
        self.assertEqual(len(d.risk_flags), 8)
        self.assertEqual(d.risk_flags[0], "wide_spread")


# =========================================================================== context
def make_trigger(ref="2026-09-09 12:00", n=120, future=10, minutes=15, with_ind=True):
    idx = pd.date_range(end=pd.Timestamp(ref) + timedelta(minutes=minutes * future),
                        periods=n + future, freq=f"{minutes}min")
    base = 100 + np.arange(len(idx)) * 0.01
    df = pd.DataFrame({"open": base, "high": base + 0.5, "low": base - 0.5, "close": base + 0.1,
                       "tick_volume": 100, "spread": 27}, index=idx)
    df.loc[idx > pd.Timestamp(ref), "close"] = 999.0            # poison: future values
    if with_ind:
        df["atr"] = 1.0
        df["tenkan"], df["kijun"] = base - 0.2, base - 0.5
        df["senkou_a"], df["senkou_b"] = base - 1.0, base - 1.5
        df["cloud_top"], df["cloud_bottom"] = base - 1.0, base - 1.5
        df["ema_200"] = base - 2
        df["kijun_slope"] = 0.01
        df["h1_trend_bull"], df["h1_trend_bear"], df["regime_trending"] = True, False, True
        df["swing_high"], df["swing_low"] = base + 2.0, base - 3.0
    return df


def make_h1(ref="2026-09-09 12:00", n=80):
    # hourly bars up to and including the forming one plus two future ones
    end = pd.Timestamp(ref).floor("h") + timedelta(hours=2)
    idx = pd.date_range(end=end, periods=n, freq="h")
    base = 100 + np.arange(n) * 0.1
    return pd.DataFrame({"open": base, "high": base + 1, "low": base - 1, "close": base,
                         "tick_volume": 500}, index=idx)


def plan(side="BUY", entry=101.28):
    return TradePlan(side=side, entry=entry, sl=entry - 1.6 if side == "BUY" else entry + 1.6,
                     tp=entry + 4.8 if side == "BUY" else entry - 4.8, atr=1.0, risk_percent=1.0,
                     be_trigger_atr=1.0, be_offset_points=5, trail_trigger_atr=1.3,
                     trail_dist_atr=0.4, spread_points=27)


def ctx(**kw):
    args = dict(strategy="ichimoku_m15", layer="Kijun pullback", ref_time="2026-09-09 12:00",
                trigger=make_trigger(), trigger_minutes=15, h1=make_h1(), plan=plan(),
                offset_hours=3.0, decision_time_utc=datetime(2026, 9, 9, 9, 16, tzinfo=timezone.utc))
    args.update(kw)
    return build_context(**args)


class ContextTests(unittest.TestCase):
    def test_no_future_trigger_rows(self):
        c = ctx()
        self.assertEqual(len(c["trigger_bars"]), 48)
        self.assertEqual(c["trigger_bars"][-1][0], "2026-09-09T09:00Z")    # 12:00 server - 3h
        self.assertNotIn(999.0, [b[4] for b in c["trigger_bars"]])
        self.assertEqual(c["signal_bar_close_utc"], "2026-09-09T09:15Z")

    def test_signal_bar_must_be_last_closed_bar(self):
        with self.assertRaises(ContextError):
            ctx(ref_time="2026-09-09 12:07")
        with self.assertRaises(ContextError):
            ctx(trigger=make_trigger().loc[:"2026-09-09 11:45"])

    def test_h1_only_fully_closed_candles(self):
        c = ctx()
        last_open = c["h1_bars"][-1][0]
        # signal closes 12:15 server -> last closed H1 opened 11:00 server = 08:00 UTC
        self.assertEqual(last_open, "2026-09-09T08:00Z")
        self.assertEqual(len(c["h1_bars"]), 48)

    def test_winter_offset(self):
        c = ctx(offset_hours=2.0)
        self.assertEqual(c["signal_bar_close_utc"], "2026-09-09T10:15Z")
        self.assertEqual(c["session"], "london")

    def test_server_offset_and_session(self):
        now = 1_790_000_000.0
        self.assertEqual(server_offset_hours(now + 3 * 3600 + 40, now), 3.0)
        self.assertEqual(server_offset_hours(now + 2 * 3600 - 70, now), 2.0)
        self.assertIsNone(server_offset_hours(now - 19 * 3600, now))      # weekend: tick ~19 h old
        self.assertIsNone(server_offset_hours(now + 20 * 3600, now))
        self.assertEqual([session_name(h) for h in (3, 8, 13, 17, 22)],
                         ["asia", "london", "overlap", "newyork", "late"])

    def test_eet_fallback_follows_eu_dst(self):
        from core.ai_gate.context import eet_offset_hours
        cases = [(datetime(2026, 1, 15, 12), 2.0), (datetime(2026, 3, 29, 0, 59), 2.0),
                 (datetime(2026, 3, 29, 1, 0), 3.0), (datetime(2026, 9, 26, 12), 3.0),
                 (datetime(2026, 10, 25, 0, 59), 3.0), (datetime(2026, 10, 25, 1, 0), 2.0),
                 (datetime(2026, 12, 31, 23), 2.0)]
        for when, expected in cases:
            with self.subTest(when=when):
                self.assertEqual(eet_offset_hours(when.replace(tzinfo=timezone.utc)), expected)

    def test_live_context_falls_back_when_tick_is_stale(self):
        b = LiveBot.__new__(LiveBot)
        b.news_filter = None
        b.db = Database(":memory:")
        self.addCleanup(b.db.close)
        b.risk_config = RiskConfig.from_settings(Settings.load(os.path.join(ROOT, "config", "settings_ichimoku.yaml")))
        b.strategy = NS(bar_minutes=15)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            b.ai_gate = AIGate(cfg(tmp), "ichimoku_m15", provider=FakeProvider(TAKE))
            self.addCleanup(b.ai_gate.shutdown)
            from main_live import PlannedTrade
            levels = NS(entry=101.28, sl=99.68, tp=106.08)
            stale = NS(bid=101.2, ask=101.28, time=time.time() - 19 * 3600)
            planned = PlannedTrade(levels, 101.28, stale, 0.01, "s", "k")
            sig = NS(side="BUY", atr=1.0, strategy="ichimoku_m15", reason="Kijun pullback",
                     ref_time=pd.Timestamp("2026-09-09 12:00"))
            with patch("main_live.datetime") as fake_dt:
                fake_dt.now.return_value = datetime(2026, 9, 9, 9, 20, tzinfo=timezone.utc)
                c, _ = b._gate_context(sig, planned, {"prepared": make_trigger(), "h1": make_h1()})
            self.assertEqual(c["signal_bar_close_utc"], "2026-09-09T09:15Z")     # EEST fallback = +3

    def test_missing_indicator_columns_are_null(self):
        c = ctx(trigger=make_trigger(with_ind=False))
        ind = c["indicators_at_signal"]
        self.assertIsNone(ind["kijun"])
        self.assertIsNone(ind["close_minus_kijun_atr"])
        self.assertIsNone(ind["h1_trend_bull"])
        self.assertEqual(ind["atr"], 1.0)

    def test_derived_values(self):
        ind = ctx()["indicators_at_signal"]
        self.assertAlmostEqual(ind["close_minus_kijun_atr"], 0.6, places=3)
        self.assertGreater(ind["close_vs_cloud_atr"], 0)
        self.assertAlmostEqual(ind["cloud_thickness_atr"], 0.5, places=3)
        c = ctx()
        self.assertAlmostEqual(c["plan"]["sl_atr"], 1.6, places=3)
        self.assertAlmostEqual(c["plan"]["spread_vs_median"], 1.0, places=3)

    def test_no_account_identity(self):
        text = json.dumps(ctx(recent_trades=[]), ensure_ascii=False).lower()
        for word in ("login", "server\"", "balance", "equity", "token", "chat_id", "20277252"):
            self.assertNotIn(word, text)

    def test_recent_trades_only_before_signal_close(self):
        trades = [
            {"side": "BUY", "reason": "Kijun pullback", "profit": 5.0, "closed_at": "2026-09-09 08:00:00"},
            {"side": "SELL", "reason": "Tenkan momentum", "profit": -3.0, "closed_at": "2026-09-09 09:00:00"},
            {"side": "BUY", "reason": "Kijun pullback", "profit": 9.0, "closed_at": "2026-09-09 09:30:00"},
            {"side": "BUY", "reason": "Kijun pullback", "profit": None, "closed_at": None},
        ]
        perf = ctx(recent_trades=trades)["recent_performance"]
        self.assertEqual(perf["all"]["n"], 2)                 # 09:30 UTC is after 09:15 close
        self.assertEqual(perf["same_side_layer"], {"n": 1, "wins": 1, "losses": 0, "be": 0, "net_usd": 5.0})

    def test_news_window(self):
        self.assertEqual(ctx(next_news={"title": "CPI", "time_until_minutes": 42.4})["news"],
                         {"next_high_impact_usd": {"title": "CPI", "minutes_until": 42}})
        self.assertIsNone(ctx(next_news={"title": "NFP", "time_until_minutes": 3000})
                          ["news"]["next_high_impact_usd"])

    def test_size_is_bounded(self):
        self.assertLess(len(json.dumps(ctx(), ensure_ascii=False)), 12_000)


# ========================================================================== provider
def http(status, body=None, headers=None, text=""):
    resp = Mock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text or json.dumps(body or {})
    resp.json = Mock(return_value=body) if body is not None else Mock(side_effect=ValueError)
    return resp


def ok_body(text=TAKE):
    return {"model": "vendor/m", "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 90}}


class ProviderTests(unittest.TestCase):
    KEY = "sk-secret-123456789"

    def make(self, responses, retries=2):
        session = Mock()
        session.post = Mock(side_effect=responses)
        self.sleeps = []
        return OpenAICompatibleProvider("https://api.example/v1", self.KEY, "m", max_retries=retries,
                                        session=session, sleep=self.sleeps.append), session

    def test_success(self):
        p, session = self.make([http(200, ok_body())])
        r = p.complete("sys", "user", time.time() + 10)
        self.assertEqual((r.tokens_in, r.tokens_out, r.model), (1200, 90, "vendor/m"))
        kwargs = session.post.call_args.kwargs
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {self.KEY}")
        self.assertEqual(session.post.call_args.args[0], "https://api.example/v1/chat/completions")

    def test_retry_on_429_then_success(self):
        p, session = self.make([http(429, text="slow down"), http(200, ok_body())])
        p.complete("s", "u", time.time() + 10)
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_5xx_until_retries_exhausted(self):
        p, session = self.make([http(500, text="x")] * 3)
        with self.assertRaises(ProviderError) as cm:
            p.complete("s", "u", time.time() + 10)
        self.assertEqual((cm.exception.kind, cm.exception.http_status), ("http_error", 500))
        self.assertEqual(session.post.call_count, 3)

    def test_client_error_not_retried(self):
        p, session = self.make([http(401, text="bad key")])
        with self.assertRaises(ProviderError):
            p.complete("s", "u", time.time() + 10)
        self.assertEqual(session.post.call_count, 1)

    def test_timeout(self):
        p, _ = self.make([requests.Timeout("read timed out")])
        with self.assertRaises(ProviderError) as cm:
            p.complete("s", "u", time.time() + 10)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_retry_after_beyond_deadline_is_not_retried(self):
        p, session = self.make([http(429, headers={"Retry-After": "30"}, text="x")])
        with self.assertRaises(ProviderError):
            p.complete("s", "u", time.time() + 5)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_deadline_already_passed(self):
        p, session = self.make([http(200, ok_body())])
        with self.assertRaises(ProviderError) as cm:
            p.complete("s", "u", time.time() - 1)
        self.assertEqual(cm.exception.kind, "timeout")
        session.post.assert_not_called()

    def test_key_never_in_errors_or_logs(self):
        seen = []
        sink = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
        try:
            echo = f"invalid key {self.KEY} Authorization: Bearer {self.KEY}"
            p, _ = self.make([http(403, text=echo)])
            with self.assertRaises(ProviderError) as cm:
                p.complete("s", "u", time.time() + 10)
            self.assertNotIn(self.KEY, str(cm.exception))
            p, _ = self.make([requests.ConnectionError(f"proxy said {self.KEY}")], retries=0)
            with self.assertRaises(ProviderError) as cm:
                p.complete("s", "u", time.time() + 10)
            self.assertNotIn(self.KEY, str(cm.exception))
        finally:
            logger.remove(sink)
        self.assertFalse(any(self.KEY in s for s in seen))
        self.assertEqual(redact("Bearer abcdefghijkl", ""), "Bearer ***")

    def test_real_http_roundtrip_on_localhost(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen["auth"] = self.headers.get("Authorization")
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                out = json.dumps(ok_body()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        session = requests.Session()
        session.trust_env = False                      # no proxy for localhost
        p = OpenAICompatibleProvider(f"http://127.0.0.1:{srv.server_address[1]}/v1", self.KEY, "m",
                                     session=session)
        r = p.complete("system text", "user text", time.time() + 10)
        self.assertEqual(parse_decision(r.text).decision, "TAKE")
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["auth"], f"Bearer {self.KEY}")
        self.assertEqual(seen["body"]["messages"][0], {"role": "system", "content": "system text"})
        self.assertEqual(seen["body"]["temperature"], 0.0)

    def test_malformed_body(self):
        p, _ = self.make([http(200, {"choices": []})])
        with self.assertRaises(ProviderError):
            p.complete("s", "u", time.time() + 10)


# ============================================================================== gate
META = {"strategy": "ichimoku_m15", "side": "BUY", "layer": "Kijun pullback",
        "ref_time_server": "2026-09-09 12:00:00", "ref_time_utc": "2026-09-09T09:15Z",
        "entry": 101.28, "sl": 99.68, "tp": 106.08, "atr": 1.0, "spread_points": 27}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.notes = []
        self.lines = []
        self.sink = logger.add(lambda m: self.lines.append(str(m)), level="INFO", format="{message}")
        self.addCleanup(logger.remove, self.sink)

    def gate(self, provider=None, **kw):
        g = AIGate(cfg(self.tmp.name, **kw), "ichimoku_m15", notify=self.notes.append,
                   provider=provider if provider is not None else FakeProvider(TAKE))
        self.addCleanup(g.shutdown)
        return g

    def test_off_creates_nothing(self):
        provider = FakeProvider(TAKE)
        g = self.gate(provider, mode="off")
        self.assertEqual(g.active_mode(), "off")
        self.assertIsNone(g.submit(ctx(), "k", META))
        self.assertIsNone(g._executor)
        self.assertEqual(provider.calls, [])
        self.assertEqual(os.listdir(self.tmp.name), [])
        g.record_outcome(1, 2.0)                                     # no journal -> no file
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_missing_key_disables_with_one_warning(self):
        c = cfg(self.tmp.name, api_key_env="AI_GATE_TEST_KEY_ABSENT")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_GATE_TEST_KEY_ABSENT", None)
            g = AIGate(c, "ichimoku_m15")
            self.addCleanup(g.shutdown)
            g._ensure_ready()
        self.assertEqual(g.active_mode(), "off")
        self.assertEqual(g.status()["disabled_reason"], "no_key")
        self.assertEqual(sum("AI gate disabled" in s for s in self.lines), 1)
        self.assertIsNone(g.submit(ctx(), "k", META))

    def test_shadow_journal_log_and_notify(self):
        g = self.gate()
        r = wait(g.submit(ctx(), "k1", META))
        self.assertTrue(r.ok)
        self.assertEqual((r.decision, r.confidence), ("TAKE", 70))
        time.sleep(0.05)
        row = g._open_journal().get("k1")
        self.assertEqual((row["status"], row["action"], row["mode"]), ("ok", "shadow_logged", "shadow"))
        self.assertEqual(json.loads(row["context_json"])["schema"], "ai_gate_ctx/1")
        self.assertTrue(any(s.startswith("🤖 AI gate [shadow] TAKE 70% → shadow_logged | ") for s in self.lines))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("نظر هوش مصنوعی", self.notes[0])

    def test_dedupe_same_signal(self):
        provider = FakeProvider(TAKE)
        g = self.gate(provider)
        wait(g.submit(ctx(), "k1", META))
        r2 = wait(g.submit(ctx(), "k1", META))
        self.assertTrue(r2.cached)
        self.assertEqual(len(provider.calls), 1)

    def test_budget_calls_and_cost(self):
        provider = FakeProvider(TAKE)
        g = self.gate(provider, max_calls_per_day=1)
        self.assertTrue(wait(g.submit(ctx(), "a", META)).ok)
        self.assertEqual(wait(g.submit(ctx(), "b", META)).status, "budget")
        self.assertEqual(len(provider.calls), 1)
        g2 = AIGate(cfg(tempfile.mkdtemp(dir=self.tmp.name), max_cost_usd_per_day=0.001,
                        price_input_per_mtok=1.0, price_output_per_mtok=1.0),
                    "ichimoku_m15", provider=FakeProvider(TAKE, tokens=(1000, 100)))
        self.addCleanup(g2.shutdown)
        self.assertAlmostEqual(wait(g2.submit(ctx(), "a", META)).cost_usd, 0.0011, places=6)
        self.assertEqual(wait(g2.submit(ctx(), "b", META)).status, "budget")

    def test_queue_full(self):
        g = self.gate(FakeProvider(TAKE, delay=0.3))
        futures = [g.submit(ctx(), f"q{i}", META) for i in range(MAX_INFLIGHT + 2)]
        statuses = [wait(f).status for f in futures]
        self.assertEqual(statuses.count("queue_full"), 2)
        self.assertEqual(statuses.count("ok"), MAX_INFLIGHT)

    def test_invalid_reply_and_provider_error(self):
        g = self.gate(FakeProvider("I think you should buy"))
        r = wait(g.submit(ctx(), "i", META))
        self.assertEqual((r.status, r.label), ("invalid", "INVALID"))
        g = AIGate(cfg(tempfile.mkdtemp(dir=self.tmp.name)), "ichimoku_m15",
                   provider=FakeProvider(error=ProviderError("timeout", "request timed out")))
        self.addCleanup(g.shutdown)
        r = wait(g.submit(ctx(), "t", META))
        self.assertEqual((r.status, r.label), ("timeout", "TIMEOUT"))

    def test_decide_live_matrix(self):
        g = self.gate(mode="live")
        mk = lambda **kw: GateResult("k", "live", **kw)  # noqa: E731
        self.assertEqual(g.decide_live(mk(status="ok", decision="TAKE", confidence=90)), (True, "taken"))
        self.assertEqual(g.decide_live(mk(status="ok", decision="SKIP", confidence=60)), (False, "blocked"))
        self.assertEqual(g.decide_live(mk(status="ok", decision="SKIP", confidence=59)), (True, "taken"))
        self.assertEqual(g.decide_live(mk(status="timeout")), (True, "taken_fail_open"))
        closed = AIGate(cfg(self.tmp.name, mode="live", fail_policy="closed"), "ichimoku_m15",
                        provider=FakeProvider(TAKE))
        self.addCleanup(closed.shutdown)
        self.assertEqual(closed.decide_live(mk(status="http_error")), (False, "blocked_fail_closed"))

    def test_late_result_is_marked_late(self):
        g = self.gate(FakeProvider(TAKE, delay=0.3), mode="live")
        fut = g.submit(ctx(), "late", META)
        g.timeout_result("late", "live")
        wait(fut)
        row = g._open_journal().get("late")
        self.assertEqual(row["status"], "late")
        self.assertEqual(row["decision"], "TAKE")

    def test_ticket_and_outcome(self):
        g = self.gate()
        wait(g.submit(ctx(), "k", META))
        g.attach_ticket("k", 555, 101.3)
        g.record_outcome(555, -12.9)
        row = g._open_journal().get("k")
        self.assertEqual((row["ticket"], row["profit"], row["result"]), (555, -12.9, "loss"))

    def test_set_mode_persists_and_reports_source(self):
        g = self.gate(mode="shadow")
        state = g.set_mode("live")
        self.assertEqual((state["mode"], state["source"], state["active_mode"]), ("live", "runtime", "live"))
        again = AIGate(cfg(self.tmp.name, mode="shadow"), "ichimoku_m15", provider=FakeProvider(TAKE))
        self.addCleanup(again.shutdown)
        self.assertEqual(again.mode, "live")
        with self.assertRaises(ConfigError):
            g.set_mode("turbo")

    def test_create_with_invalid_config_is_off(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            g = AIGate.create("x", config_path=write_yaml(tmp, "defaults:\n  mode: turbo\n"))
            self.assertEqual(g.active_mode(), "off")

    def test_status_summary(self):
        g = self.gate()
        wait(g.submit(ctx(), "a", META))
        s = g.status()
        self.assertEqual(s["journal"]["today"]["take"]["n"], 1)
        self.assertEqual(s["journal"]["last"]["decision"], "TAKE")


# ===================================================================== main_live wiring
class LiveWiringBase(unittest.TestCase):
    MODE = "shadow"
    REPLY = TAKE
    DELAY = 0.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        b = self.bot = LiveBot.__new__(LiveBot)
        b._lock = threading.RLock()
        b.db = Database(":memory:")
        self.addCleanup(b.db.close)
        b.risk_config = RiskConfig.from_settings(Settings.load(os.path.join(ROOT, "config", "settings_ichimoku.yaml")))
        b.risk = RiskManager(b.risk_config, SymbolSpec.gold_default())
        b.tracked = {}; b.signals_sent = 0; b.daily_trades = 0; b.telegram = None
        b.news_filter = None; b.running = True
        b.strategy = NS(name="ichimoku_m15", bar_minutes=15)
        b.client = Mock()
        b.client.config = MT5Config()
        b.client.symbol = "XAUUSD"; b.client.magic = 735511
        b.client.symbol_info.return_value = NS(name="XAUUSD", point=.01, digits=2,
            trade_tick_value=1., trade_tick_size=.01, trade_contract_size=100.,
            volume_min=.01, volume_max=100., volume_step=.01, trade_stops_level=0)
        b.client.get_tick.return_value = NS(bid=101.2, ask=101.28)
        b.client.account_info.return_value = NS(balance=533., login=123, server="demo")
        b.client.positions.return_value = []
        b.client.open_position_count.return_value = 0
        b.client.send_market_order.return_value = OrderResult(True, order=123, position=123,
                                                              price=101.28, volume=.01)
        b._daily_guard = Mock(return_value=(False, ""))
        b._session_open = Mock(return_value=True)
        b._spread_ok = Mock(return_value=True)
        b._pending_entry = None
        self.provider = FakeProvider(self.REPLY, delay=self.DELAY)
        b.ai_gate = AIGate(cfg(self.tmp.name, mode=self.MODE), "ichimoku_m15", provider=self.provider)
        self.addCleanup(b.ai_gate.shutdown)
        self.signal = NS(strategy="ichimoku_m15", ref_time=pd.Timestamp("2026-09-09 12:00"), side="BUY",
                         is_long=True, atr=1., reason="Kijun pullback", meta={})
        self.inputs = {"prepared": make_trigger(), "h1": make_h1()}

    def journal_row(self):
        return self.bot.ai_gate._open_journal().rows()[-1]


class OffModeWiring(LiveWiringBase):
    MODE = "off"

    def test_off_is_the_old_path(self):
        levels = self.bot._open_trade(self.signal, gate_inputs=self.inputs)
        self.assertIsNotNone(levels)
        self.bot.client.send_market_order.assert_called_once()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_bot_without_gate_attribute_still_trades(self):
        del self.bot.ai_gate
        self.assertIsNotNone(self.bot._open_trade(self.signal))


class ShadowWiring(LiveWiringBase):
    MODE = "shadow"
    DELAY = 0.5

    def test_order_does_not_wait_for_the_model(self):
        started = time.time()
        levels = self.bot._open_trade(self.signal, gate_inputs=self.inputs)
        self.assertLess(time.time() - started, 0.3)
        self.assertIsNotNone(levels)
        self.bot.client.send_market_order.assert_called_once()
        time.sleep(0.8)
        row = self.journal_row()
        self.assertEqual((row["status"], row["action"], row["ticket"]), ("ok", "shadow_logged", 123))
        self.assertEqual(row["signal_key"], hash_key(json.dumps(
            [json.dumps(["demo", 123, "XAUUSD", 735511]), "ichimoku_m15", "2026-09-09 12:00:00", "BUY"])))

    def test_provider_failure_never_blocks_the_order(self):
        self.provider.error = ProviderError("http_error", "HTTP 500")
        self.assertIsNotNone(self.bot._open_trade(self.signal, gate_inputs=self.inputs))
        self.bot.client.send_market_order.assert_called_once()

    def test_context_failure_never_blocks_the_order(self):
        broken = {"prepared": make_trigger().iloc[:0], "h1": None}
        self.assertIsNotNone(self.bot._open_trade(self.signal, gate_inputs=broken))
        self.bot.client.send_market_order.assert_called_once()
        self.assertEqual(self.provider.calls, [])

    def test_outcome_recorded_on_close(self):
        b = self.bot
        b._open_trade(self.signal, gate_inputs=self.inputs)
        time.sleep(0.8)
        b.tracked = {123: TrackedPosition(123, "BUY", .01, 101.28, 99.68, 106.08, 1., position_id=123)}
        b.client.positions.return_value = []
        deal = lambda entry, profit: NS(position_id=123, entry=entry, volume=.01, profit=profit,  # noqa: E731
                                        commission=0., swap=0., fee=0., price=103., ticket=entry + 1,
                                        time_msc=entry + 1, magic=735511, symbol="XAUUSD", order=123)
        b.client.deals_for_position.return_value = [deal(0, 0.), deal(1, 9.)]
        b._sync_daily = Mock(); b._shadows = []
        self.assertTrue(b._sync_positions())
        self.assertEqual(self.journal_row()["profit"], 9.0)


class LiveWiring(LiveWiringBase):
    MODE = "live"

    def start(self):
        self.bot._begin_gated_entry(self.signal, self.inputs)
        pending = self.bot._pending_entry
        self.assertIsNotNone(pending)
        wait(pending.future)
        self.bot._process_pending_entry()
        self.assertIsNone(self.bot._pending_entry)

    def test_take_executes(self):
        self.start()
        self.bot.client.send_market_order.assert_called_once()
        row = self.journal_row()
        self.assertEqual((row["action"], row["ticket"]), ("taken", 123))

    def test_confident_skip_blocks(self):
        self.provider.text = SKIP_HI
        self.start()
        self.bot.client.send_market_order.assert_not_called()
        self.assertEqual(self.journal_row()["action"], "blocked")
        self.assertEqual(self.bot.db.unresolved_executions(), [])   # nothing claimed

    def test_unconfident_skip_enters(self):
        self.provider.text = SKIP_LO
        self.start()
        self.bot.client.send_market_order.assert_called_once()
        self.assertEqual(self.journal_row()["action"], "taken")

    def test_drift_cancels(self):
        self.bot._begin_gated_entry(self.signal, self.inputs)
        wait(self.bot._pending_entry.future)
        self.bot.client.get_tick.return_value = NS(bid=101.8, ask=101.88)   # +0.6 ATR
        self.bot._process_pending_entry()
        self.bot.client.send_market_order.assert_not_called()
        self.assertEqual(self.journal_row()["action"], "cancelled_drift")

    def test_guard_cancels(self):
        self.bot._begin_gated_entry(self.signal, self.inputs)
        wait(self.bot._pending_entry.future)
        self.bot.running = False
        self.bot._process_pending_entry()
        self.bot.client.send_market_order.assert_not_called()
        self.assertEqual(self.journal_row()["action"], "cancelled_guard")

    def test_order_failure_is_journaled(self):
        self.bot.client.send_market_order.return_value = OrderResult(False, comment="rejected")
        self.start()
        self.assertEqual(self.journal_row()["action"], "order_failed")

    def test_main_loop_never_waits(self):
        self.provider.delay = 1.0
        self.bot._begin_gated_entry(self.signal, self.inputs)
        started = time.time()
        self.bot._process_pending_entry()
        self.assertLess(time.time() - started, 0.05)
        self.assertIsNotNone(self.bot._pending_entry)          # still waiting
        self.bot.client.send_market_order.assert_not_called()

    def test_overdue_decision_uses_fail_policy(self):
        self.provider.delay = 1.0
        self.bot._begin_gated_entry(self.signal, self.inputs)
        self.bot._pending_entry.deadline = time.time() - 1
        self.bot._process_pending_entry()
        self.bot.client.send_market_order.assert_called_once()   # fail_policy open
        row = self.journal_row()
        self.assertEqual((row["status"], row["action"]), ("timeout", "taken_fail_open"))

    def test_context_failure_goes_through_fail_policy(self):
        self.inputs = {"prepared": make_trigger().iloc[:0], "h1": None}
        self.bot._begin_gated_entry(self.signal, self.inputs)
        self.bot._process_pending_entry()
        self.bot.client.send_market_order.assert_called_once()   # fail open
        self.assertEqual(self.provider.calls, [])

    def test_new_signal_ignored_while_pending(self):
        b = self.bot
        b._pending_entry = PendingEntry(self.signal, None, None, Mock(), "k", time.time() + 30)
        b.strategy = Mock(name="strategy")
        b.strategy.prepare.return_value = make_trigger()
        b.strategy.evaluate.return_value = self.signal
        b.current_atr = 1.0
        b._sync_daily = Mock(); b._check_daily_digest = Mock()
        b._open_trade = Mock(); b._begin_gated_entry = Mock()
        frames = {"m5": make_trigger(minutes=5), "m15": make_trigger(), "h1": make_h1()}
        b._on_new_closed_bar(frames)
        b.strategy.evaluate.assert_called_once()
        b._open_trade.assert_not_called()
        b._begin_gated_entry.assert_not_called()

    def test_shutdown_keeps_a_verdict_that_already_arrived(self):
        self.bot._begin_gated_entry(self.signal, self.inputs)
        wait(self.bot._pending_entry.future)
        self.bot._cancel_pending_entry()
        row = self.journal_row()
        self.assertEqual((row["status"], row["action"]), ("ok", "cancelled_guard"))
        self.bot.client.send_market_order.assert_not_called()

    def test_shutdown_cancels_pending(self):
        self.provider.delay = 1.0
        self.bot._begin_gated_entry(self.signal, self.inputs)
        self.bot._cancel_pending_entry()
        self.assertIsNone(self.bot._pending_entry)
        self.assertEqual(self.journal_row()["action"], "cancelled_guard")


# ========================================================================== telegram
class TelegramCommandTests(unittest.TestCase):
    def controller(self, state):
        from core.telegram_bot import BotBridge, TelegramController
        c = TelegramController.__new__(TelegramController)
        c.bridge = BotBridge(get_ai_gate=Mock(return_value=state), set_ai_gate=Mock(return_value=state))
        c._guard_admin = AsyncMock(return_value=True)
        return c

    def run_cmd(self, c, args):
        update = NS(message=NS(reply_text=AsyncMock()))
        asyncio.run(c._cmd_aigate(update, NS(args=args)))
        return update.message.reply_text

    STATE = {"mode": "shadow", "mode_fa": "سایه (فقط ثبت)", "source": "yaml", "active_mode": "off",
             "disabled_reason": "no_key", "model": "", "fail_policy": "open", "block_min_confidence": 60,
             "journal": None}

    def test_status(self):
        c = self.controller(self.STATE)
        reply = self.run_cmd(c, [])
        text = reply.call_args.args[0]
        self.assertIn("سایه", text)
        self.assertIn("AI_GATE_API_KEY", text)

    def test_switch_to_live_warns_and_calls_bridge(self):
        c = self.controller(dict(self.STATE, mode="live", active_mode="live", source="runtime"))
        reply = self.run_cmd(c, ["LIVE"])
        c.bridge.set_ai_gate.assert_called_once_with("live")
        self.assertIn("جلوی ورود را بگیرد", reply.call_args.args[0])

    def test_bad_argument(self):
        c = self.controller(self.STATE)
        reply = self.run_cmd(c, ["turbo"])
        c.bridge.set_ai_gate.assert_not_called()
        self.assertIn("Usage", reply.call_args.args[0])


# ========================================================================= dashboard
class DashboardTests(unittest.TestCase):
    def test_log_lines_translated(self):
        from core.dashboard import IMPORTANT_KINDS, translate_log_event
        ev = translate_log_event("🤖 AI gate [shadow] SKIP 72% → shadow_logged | فاصله از کیجون 2.4 ATR", "INFO")
        self.assertEqual(ev["kind"], "ai")
        self.assertIn("رد با اطمینان 72٪", ev["text"])
        self.assertIn("فقط ثبت شد", ev["text"])
        for action in ("taken", "blocked", "taken_fail_open", "blocked_fail_closed",
                       "cancelled_drift", "cancelled_guard", "order_failed"):
            ev = translate_log_event(f"🤖 AI gate [live] TIMEOUT 0% → {action} | x", "INFO")
            self.assertNotIn(action, ev["text"])
        ev = translate_log_event("AI gate disabled: missing API key (env AI_GATE_API_KEY) or model", "WARNING")
        self.assertIn("کلید API", ev["text"])
        self.assertIn("ai", IMPORTANT_KINDS)

    def test_report_section(self):
        from core.dashboard import build_report
        now = datetime(2026, 9, 25, 10, 0)
        self.assertIsNone(build_report([], [], {"connected": True}, now, now)["ai_gate"])
        state = {"mode": "live", "mode_fa": "فعال", "source": "runtime", "active_mode": "live",
                 "model": "m", "fail_policy": "open", "block_min_confidence": 60,
                 "journal": {"period_days": 30,
                             "today": {"total": 2, "take": {"n": 1}, "skip": {"n": 1}, "errors": 0, "cost": 0.01},
                             "period": {"take": {"n": 5, "closed": 4, "net": 12.0, "avg": 3.0},
                                        "skip": {"n": 3, "closed": 0, "net": 0.0, "avg": None},
                                        "blocked": 3}}}
        sec = build_report([], [], {"connected": True}, now, now, ai_gate=state)["ai_gate"]
        text = "\n".join(sec["lines"])
        self.assertIn("دستور تلگرام", text)
        self.assertIn("≥ 60٪", text)
        self.assertEqual([r["note"] for r in sec["rows"]], ["نمونه کم است", "نمونه کم است"])


# ======================================================================= report tool
class ReportToolTests(unittest.TestCase):
    def setUp(self):
        from tools import ai_gate_report as rt
        self.rt = rt
        self.rules = rt.ExitRules(1.0, 5, 1.3, 0.4)

    def bars(self, rows):
        idx = pd.date_range("2026-09-09 12:15", periods=len(rows), freq="min")
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(spread=0)

    def test_simulation_paths(self):
        sim = lambda side, rows: self.rt.simulate(side, 100.0, 98.4 if side == "BUY" else 101.6,  # noqa: E731
                                                  104.8 if side == "BUY" else 95.2, 1.0,
                                                  self.bars(rows), self.rules, 0)
        r = sim("BUY", [[100, 100.5, 99.8, 100.2], [100.2, 104.9, 100.1, 104.5]])
        self.assertEqual((r.exit, r.profit_usd), ("tp", 4.8))
        r = sim("BUY", [[100, 100.2, 98.3, 98.5]])
        self.assertEqual((r.exit, r.profit_usd, r.protected), ("sl", -1.6, False))
        r = sim("BUY", [[100, 101.1, 99.9, 101.0], [101, 101.1, 99.9, 100]])
        self.assertEqual((r.exit, r.profit_usd, r.protected), ("be", 0.05, True))
        r = sim("BUY", [[100, 102.0, 99.9, 101.9], [101.9, 102.0, 101.5, 101.6]])
        self.assertEqual((r.exit, r.profit_usd), ("trail", 1.6))
        r = sim("BUY", [[100, 105.0, 98.0, 100]])                    # both in one bar -> stop
        self.assertEqual(r.exit, "sl")
        r = sim("SELL", [[100, 100.2, 95.0, 95.5]])
        self.assertEqual((r.exit, r.profit_usd), ("tp", 4.8))
        r = sim("BUY", [[100, 100.3, 99.9, 100.1]])
        self.assertEqual(r.exit, "horizon")
        self.assertEqual(self.rt.simulate("BUY", 100, 98, 104, 1.0, pd.DataFrame(), self.rules, 0).exit, "no_data")

    def test_sell_exit_pays_spread(self):
        bars = self.bars([[100, 100.2, 95.25, 95.5]]).assign(spread=10)     # ask low = 95.35
        r = self.rt.simulate("SELL", 100.0, 101.6, 95.2, 1.0, bars, self.rules, 0)
        self.assertEqual(r.exit, "horizon")

    def test_bootstrap_reproducible_and_verdict(self):
        a = self.rt.bootstrap_uplift([1, 2, -1] * 20, [-2, -1, -3] * 6)
        b = self.rt.bootstrap_uplift([1, 2, -1] * 20, [-2, -1, -3] * 6)
        self.assertEqual(a, b)
        self.assertEqual(a["uplift"], 36.0)
        self.assertGreater(a["lo"], 0)
        v = self.rt.verdict(78, 18, a, -2.0)
        self.assertTrue(v["go_live"])
        v = self.rt.verdict(30, 18, a, -2.0)
        self.assertFalse(v["go_live"])
        self.assertFalse(v["checks"][0]["ok"])

    def test_evaluate_journal_only_and_with_bars(self):
        rows = []
        for i in range(4):
            rows.append({"id": i, "status": "ok", "decision": "SKIP" if i % 2 else "TAKE", "confidence": 65 + i,
                         "p_protect": 0.5, "side": "BUY", "layer": "K", "entry_plan": 100.0, "sl_plan": 98.4,
                         "tp_plan": 104.8, "atr": 1.0, "spread_points": 0, "latency_ms": 900 + i,
                         "cost_usd": 0.001, "profit": 2.0 if i == 0 else None, "action": "shadow_logged",
                         "ticket": 1 if i == 0 else None, "created_utc": "2026-09-09 09:16:00",
                         "ref_time_server": "2026-09-09 12:00:00", "context_json": "{}"})
        rows.append({"id": 9, "status": "timeout", "cost_usd": 0})
        rep = self.rt.evaluate(rows, None, None)
        self.assertFalse(rep["simulated"])
        self.assertEqual(rep["actual"]["TAKE"]["sum"], 2.0)
        self.assertEqual(rep["by_status"], {"ok": 4, "timeout": 1})
        tp_bars = self.bars([[100, 104.9, 99.9, 104.5]])
        sl_bars = self.bars([[100, 100.1, 98.0, 98.2]])
        rep = self.rt.evaluate(rows, lambda r: tp_bars if r["decision"] == "TAKE" else sl_bars, self.rules)
        self.assertEqual(rep["take"]["sum"], 9.6)
        self.assertEqual(rep["skip"]["mean"], -1.6)
        self.assertEqual(rep["uplift"]["uplift"], 3.2)
        text = self.rt.render(rep, "ichimoku_m15")
        self.assertIn("هنوز نه", text)


if __name__ == "__main__":
    unittest.main()


# ================================================================ evaluation in the bot
class StoredEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.gate = AIGate(cfg(self.tmp.name), "ichimoku_m15", provider=FakeProvider(TAKE))
        self.addCleanup(self.gate.shutdown)
        self.rules = __import__("core.ai_gate.evaluate", fromlist=["ExitRules"]).ExitRules(1.0, 5, 1.3, 0.4)

    def add(self, key, text, entry=100.0, side="BUY"):
        self.gate._provider.text = text
        meta = dict(META, side=side, entry=entry, sl=entry - 1.6 if side == "BUY" else entry + 1.6,
                    tp=entry + 4.8 if side == "BUY" else entry - 4.8, atr=1.0, spread_points=0,
                    ref_time_server="2026-09-09 12:00:00")
        wait(self.gate.submit(ctx(), key, meta))

    @staticmethod
    def m1(rows, start="2026-09-09 12:15"):
        idx = pd.date_range(start, periods=len(rows), freq="min")
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(spread=0)

    def test_journal_migrates_v1(self):
        import sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        from core.ai_gate import journal as jm
        v1 = jm._DDL
        for line in ("    sim_profit      REAL,\n", "    sim_exit        TEXT,\n", "    sim_protected   INTEGER,\n",
                     "    sim_done        INTEGER NOT NULL DEFAULT 0,\n"):
            v1 = v1.replace(line, "")
        v1 = v1.replace("    result          TEXT,\n    sim_updated_utc TEXT\n", "    result          TEXT\n")
        self.assertNotIn("sim_", v1)
        con = sqlite3.connect(path)
        con.executescript(v1)
        con.execute("INSERT INTO decisions (signal_key, created_utc, mode, status) VALUES ('a','x','shadow','ok')")
        con.commit(); con.close()
        j = jm.DecisionJournal(path)
        self.addCleanup(j.close)
        cols = {r[1] for r in j._conn.execute("PRAGMA table_info(decisions)")}
        self.assertTrue({"sim_profit", "sim_exit", "sim_protected", "sim_done", "sim_updated_utc"} <= cols)
        self.assertEqual(j.get("a")["sim_done"], 0)
        jm.DecisionJournal(path).close()                               # idempotent

    def test_simulation_lifecycle_and_accuracy(self):
        self.add("take_tp", TAKE)
        self.add("skip_sl", SKIP_HI)
        self.add("skip_open", SKIP_HI)
        tp = self.m1([[100, 104.9, 99.9, 104.5]])
        sl = self.m1([[100, 100.1, 98.0, 98.2]])
        flat = self.m1([[100, 100.2, 99.9, 100.1]] * 30)
        bars = {"take_tp": tp, "skip_sl": sl, "skip_open": flat}
        order = iter(["take_tp", "skip_sl", "skip_open"])
        fetch = lambda s, e: bars[next(order)]  # noqa: E731
        # 5 minutes before the signal bar closes: nothing is due yet
        self.assertEqual(self.gate.simulate_pending(fetch, self.rules, datetime(2026, 9, 9, 12, 10)), 0)
        n = self.gate.simulate_pending(fetch, self.rules, datetime(2026, 9, 9, 13, 0))
        self.assertEqual(n, 3)
        j = self.gate._open_journal()
        self.assertEqual((j.get("take_tp")["sim_exit"], j.get("take_tp")["sim_done"]), ("tp", 1))
        self.assertEqual((j.get("skip_sl")["sim_exit"], j.get("skip_sl")["sim_done"]), ("sl", 1))
        self.assertEqual((j.get("skip_open")["sim_exit"], j.get("skip_open")["sim_done"]), ("horizon", 0))
        # 49 h later the open one is final at the horizon
        n = self.gate.simulate_pending(lambda s, e: flat, self.rules, datetime(2026, 9, 11, 13, 16))
        self.assertEqual(n, 1)
        self.assertEqual(j.get("skip_open")["sim_done"], 1)

        rep = self.gate.dashboard_report()
        r = rep["report"]
        self.assertEqual(r["accuracy"]["right"], 2)                     # TAKE→TP and SKIP→SL
        self.assertEqual(r["accuracy"]["neutral"], 1)
        self.assertEqual(r["accuracy"]["pct"], 100.0)
        self.assertEqual(r["take"]["sum"], 4.8)
        self.assertEqual(r["uplift"]["uplift"], 1.6 - 0.1)              # −(−1.6 + 0.1)
        self.assertEqual(len(rep["items"]), 3)
        self.assertEqual(rep["items"][0]["correct"], "neutral")         # newest first
        self.assertEqual({i["correct"] for i in rep["items"]}, {"right", "neutral"})

    def test_missing_history_eventually_final(self):
        self.add("k", TAKE)
        empty = lambda s, e: pd.DataFrame()  # noqa: E731
        self.assertEqual(self.gate.simulate_pending(empty, self.rules, datetime(2026, 9, 9, 14, 0)), 0)
        self.assertEqual(self.gate.simulate_pending(empty, self.rules, datetime(2026, 9, 20, 0, 0)), 1)
        rep = self.gate.dashboard_report()["report"]
        self.assertEqual(rep["pending"], 1)                             # no_data never counts
        self.assertIsNone(rep["accuracy"]["pct"])

    def test_no_journal_report(self):
        g = AIGate(cfg(tempfile.mkdtemp(dir=self.tmp.name), mode="off"), "orb_gold")
        self.addCleanup(g.shutdown)
        rep = g.dashboard_report()
        self.assertIsNone(rep["report"])
        self.assertEqual(rep["status"]["mode"], "off")
        self.assertEqual(g.simulate_pending(lambda s, e: None, self.rules, datetime(2026, 9, 9)), 0)


class HousekeepingTests(LiveWiringBase):
    MODE = "shadow"

    def test_rate_limited_and_uses_m1_range(self):
        b = self.bot
        b._ai_sim_last = 0.0
        b.client.server_time.return_value = datetime(2026, 9, 9, 13, 0)
        b.client.get_rates_range.return_value = pd.DataFrame()
        b._open_trade(self.signal, gate_inputs=self.inputs)
        time.sleep(0.1)
        b._ai_gate_housekeeping()
        args = b.client.get_rates_range.call_args.args
        self.assertEqual(args[0], "M1")
        self.assertEqual(args[1], datetime(2026, 9, 9, 12, 15, tzinfo=timezone.utc))
        b.client.get_rates_range.reset_mock()
        b._ai_gate_housekeeping()                                       # within 5 minutes: skipped
        b.client.get_rates_range.assert_not_called()

    def test_failures_never_raise(self):
        b = self.bot
        b._ai_sim_last = 0.0
        b.client.server_time.side_effect = RuntimeError("IPC")
        b._open_trade(self.signal, gate_inputs=self.inputs)
        b._ai_gate_housekeeping()                                       # no exception

    def test_off_gate_does_nothing(self):
        del self.bot.ai_gate
        self.bot._ai_gate_housekeeping()


class DashboardAITabTests(unittest.TestCase):
    def test_endpoint(self):
        from core.dashboard import DashboardServer

        class DB:
            def recent_signals(self, limit=10): return []
            def signal_performance(self): return {}
            def stats(self): return {}
        payload = {"status": {"mode": "shadow"}, "report": None, "items": []}
        server = DashboardServer(lambda: {"connected": True}, DB(), host="127.0.0.1", port=0, token="t",
                                 ai_gate_report=lambda: payload)
        self.assertTrue(server.start())
        try:
            base = f"http://127.0.0.1:{server._httpd.server_address[1]}"
            got = json.loads(__import__("urllib.request").request.urlopen(base + "/api/ai?token=t").read())
            self.assertEqual(got["status"]["mode"], "shadow")
            self.assertTrue(got["available"])
            page = __import__("urllib.request").request.urlopen(base + "/?token=t").read().decode()
            self.assertIn('id="tab-ai"', page)
        finally:
            server.stop()
        bare = DashboardServer(lambda: {}, DB(), host="127.0.0.1", port=0)
        self.assertFalse(bare.ai_report()["available"])
