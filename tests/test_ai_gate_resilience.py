"""AI gate resilience: fallback model on overload, automatic shadow re-asks (503/429/timeout)."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.ai_gate import AIGate
from core.ai_gate.config import AIGateConfig, ConfigError, load_config
from core.ai_gate.journal import DecisionJournal
from core.ai_gate.provider import FakeProvider, ProviderError
from tests.test_ai_gate import META, PROMPT, SKIP_HI, TAKE, ctx, write_yaml

E503 = ProviderError("http_error", 'HTTP 503: {"error": {"code": 503, "status": "UNAVAILABLE"}}', 503)
E401 = ProviderError("http_error", "HTTP 401: unauthorized", 401)
ETO = ProviderError("timeout", "request timed out")


class Seq:
    """Provider that raises/answers from a script: each item is an error or a text."""

    def __init__(self, *script, model="gemini-main"):
        self.script, self.model, self.calls = list(script), model, 0

    def complete(self, system, user, deadline):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, ProviderError):
            raise item
        return FakeProvider(item, model=self.model).complete(system, user, deadline)


def cfg(tmp, **kw):
    v = dict(mode="shadow", model="gemini-main", prompt_file=PROMPT, journal_dir=tmp,
             shadow_retry_minutes=(5, 15, 60))
    v.update(kw)
    return AIGateConfig(**v).validate()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.notes = []

    def gate(self, provider, fallbacks=(), **kw):
        g = AIGate(cfg(self.tmp.name, **kw), "ichimoku_m15", notify=self.notes.append,
                   provider=provider, fallback_providers=list(fallbacks))
        self.addCleanup(g.shutdown)
        return g

    def row(self, g, key):
        return g._open_journal().get(key)

    def make_due(self, g, key):
        with g._open_journal()._cursor() as cur:
            cur.execute("UPDATE decisions SET retry_after_utc='2000-01-01 00:00:00' WHERE signal_key=?", (key,))

    def drain(self, g):
        g._executor.submit(lambda: None).result(timeout=5)


class ConfigTests(unittest.TestCase):
    def test_yaml_lists_parsed_and_validated(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write_yaml(tmp, "defaults:\n  fallback_models: [gemini-b, gemini-c]\n"
                                   "  shadow_retry_minutes: [2, 10]\n")
            c = load_config("x", path)
            self.assertEqual(c.fallback_models, ("gemini-b", "gemini-c"))
            self.assertEqual(c.shadow_retry_minutes, (2, 10))
            path = write_yaml(tmp, "defaults:\n  shadow_retry_minutes: []\n  fallback_models:\n")
            c = load_config("x", path)
            self.assertEqual((c.shadow_retry_minutes, c.fallback_models), ((), ()))
            with self.assertRaises(ConfigError):
                load_config("x", write_yaml(tmp, "defaults:\n  shadow_retry_minutes: [0]\n"))
        with self.assertRaises(ConfigError):
            AIGateConfig(fallback_models=("",)).validate()

    def test_repo_yaml_still_valid(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for bot in ("ichimoku_m15", "orb_gold"):
            load_config(bot, os.path.join(root, "config", "ai_gate.yaml"))


class Fallback(Base):
    def test_503_falls_back_to_second_model(self):
        main, backup = Seq(E503, model="gemini-main"), Seq(TAKE, model="gemini-backup")
        g = self.gate(main, [backup])
        r = g.submit(ctx(), "k", META).result(timeout=5)
        self.assertTrue(r.ok)
        self.assertEqual(r.model, "gemini-backup")
        self.assertEqual((main.calls, backup.calls), (1, 1))
        self.assertEqual(self.row(g, "k")["retry_after_utc"], None)
        self.assertEqual(len(self.notes), 1)
        self.assertNotIn("تلاش دوباره", self.notes[0])

    def test_timeout_leaves_time_for_fallback(self):
        seen = []

        class Hang(Seq):
            def complete(self, system, user, deadline):
                seen.append(deadline)
                raise ETO
        main, backup = Hang(ETO), Seq(TAKE, model="gemini-backup")
        g = self.gate(main, [backup], timeout_seconds=50)
        g._clock = lambda: 1000.0
        r = g._run("k0", "live", ctx(), 1050.0)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(seen[0], 1030.0)                      # 60% of 50 s for the primary

    def test_permanent_error_does_not_fall_back(self):
        main, backup = Seq(E401), Seq(TAKE, model="b")
        g = self.gate(main, [backup])
        r = g.submit(ctx(), "k", META).result(timeout=5)
        self.assertFalse(r.ok)
        self.assertEqual(backup.calls, 0)
        self.assertIsNone(self.row(g, "k")["retry_after_utc"])     # 401 is not re-asked
        self.assertNotIn("تلاش دوباره", self.notes[0])


class ShadowReask(Base):
    def test_failed_verdict_is_reasked_with_stored_context(self):
        prov = Seq(E503, SKIP_HI)
        g = self.gate(prov)
        r = g.submit(ctx(), "k", META).result(timeout=5)
        self.assertEqual(r.status, "http_error")
        self.assertIn("تلاش دوباره‌ی خودکار حدود 5 دقیقه", self.notes[0])
        row = self.row(g, "k")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["retry_after_utc"])
        self.assertEqual(g.retry_failed_shadow(), 0)                # not due yet
        self.make_due(g, "k")
        self.assertEqual(g.retry_failed_shadow(), 1)
        self.drain(g)
        row = self.row(g, "k")
        self.assertEqual((row["status"], row["decision"], row["attempts"]), ("ok", "SKIP", 2))
        self.assertIsNone(row["retry_after_utc"])
        self.assertIn("نظر دیرهنگام", self.notes[-1])
        self.assertIn("تلاش 2", self.notes[-1])
        self.assertEqual(len(self.notes), 2)
        self.assertEqual(g.retry_failed_shadow(), 0)                # done: nothing left

    def test_gives_up_after_all_attempts_with_one_message(self):
        prov = Seq(ETO)
        g = self.gate(prov, shadow_retry_minutes=(5, 15))
        g.submit(ctx(), "k", META).result(timeout=5)
        for _ in range(2):
            self.make_due(g, "k")
            self.assertEqual(g.retry_failed_shadow(), 1)
            self.drain(g)
        self.make_due(g, "k")
        self.assertEqual(g.retry_failed_shadow(), 0)
        self.assertEqual(prov.calls, 3)
        self.assertEqual(self.row(g, "k")["attempts"], 3)
        self.assertEqual(len(self.notes), 2)                         # first failure + give-up
        self.assertIn("دیگر تلاش نمی‌شود", self.notes[-1])

    def test_live_rows_and_old_rows_are_not_reasked(self):
        g = self.gate(Seq(E503), mode="live")
        g.submit(ctx(), "live-k", META).result(timeout=5)
        self.make_due(g, "live-k")
        self.assertEqual(g.retry_failed_shadow(), 0)
        g2 = self.gate(Seq(E503))
        g2.submit(ctx(), "old", META).result(timeout=5)
        with g2._open_journal()._cursor() as cur:
            cur.execute("UPDATE decisions SET created_utc='2000-01-01 00:00:00', "
                        "retry_after_utc='2000-01-01 00:00:00' WHERE signal_key='old'")
        self.assertEqual(g2.retry_failed_shadow(), 0)

    def test_disabled_when_no_delays(self):
        g = self.gate(Seq(E503), shadow_retry_minutes=())
        g.submit(ctx(), "k", META).result(timeout=5)
        self.assertNotIn("تلاش دوباره", self.notes[0])
        self.make_due(g, "k")
        self.assertEqual(g.retry_failed_shadow(), 0)


class Migration(unittest.TestCase):
    def test_v2_journal_gets_retry_columns_and_old_failures_become_due(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "ai_gate_ichimoku_m15.db")
            j = DecisionJournal(path)
            j._conn.execute("ALTER TABLE decisions DROP COLUMN attempts")
            j._conn.execute("ALTER TABLE decisions DROP COLUMN retry_after_utc")
            j.insert_pending("old", "shadow", META, {"x": 1}, "m", "p")
            j.complete("old", status="http_error", error="HTTP 503: overloaded")
            j.close()
            j = DecisionJournal(path)
            now = datetime.now(timezone.utc)
            due = j.due_retries(now.strftime("%Y-%m-%d %H:%M:%S"),
                                (now - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S"), 4)
            self.assertEqual([r["signal_key"] for r in due], ["old"])
            self.assertEqual(due[0]["attempts"], 1)
            self.assertEqual(sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0], 3)
            j.close()


if __name__ == "__main__":
    unittest.main()
