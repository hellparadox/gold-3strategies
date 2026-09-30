"""SQLite journal of every gate decision (one row per signal)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

SCHEMA_VERSION = 3
_BE_BAND = 0.5

_DDL = """
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key      TEXT NOT NULL UNIQUE,
    created_utc     TEXT NOT NULL,
    mode            TEXT NOT NULL,
    strategy        TEXT,
    side            TEXT,
    layer           TEXT,
    ref_time_server TEXT,
    ref_time_utc    TEXT,
    entry_plan      REAL,
    sl_plan         REAL,
    tp_plan         REAL,
    atr             REAL,
    spread_points   REAL,
    model           TEXT,
    prompt_version  TEXT,
    context_json    TEXT,
    response_text   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    error           TEXT,
    decision        TEXT,
    confidence      INTEGER,
    p_protect       REAL,
    reasons_json    TEXT,
    risk_flags_json TEXT,
    latency_ms      INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    action          TEXT,
    ticket          INTEGER,
    entry_fill      REAL,
    closed_utc      TEXT,
    profit          REAL,
    result          TEXT,
    sim_profit      REAL,
    sim_exit        TEXT,
    sim_protected   INTEGER,
    sim_done        INTEGER NOT NULL DEFAULT 0,
    sim_updated_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_created ON decisions (created_utc);
CREATE INDEX IF NOT EXISTS idx_ai_ticket  ON decisions (ticket);
"""


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def result_label(profit: float) -> str:
    if profit >= _BE_BAND:
        return "win"
    if profit <= -_BE_BAND:
        return "loss"
    return "be"


class DecisionJournal:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_DDL)
            self._migrate()
            self._conn.commit()

    _V2_COLUMNS = (("sim_profit", "REAL"), ("sim_exit", "TEXT"), ("sim_protected", "INTEGER"),
                   ("sim_done", "INTEGER NOT NULL DEFAULT 0"), ("sim_updated_utc", "TEXT"))
    # v3: provider-call attempts per signal and the earliest time of the next shadow re-ask
    _V3_COLUMNS = (("attempts", "INTEGER NOT NULL DEFAULT 1"), ("retry_after_utc", "TEXT"))

    def _migrate(self) -> None:
        """v1 → v2 → v3: add the simulation and retry columns (idempotent)."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(decisions)")}
        for name, decl in self._V2_COLUMNS + self._V3_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {decl}")
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ writes
    def insert_pending(self, signal_key: str, mode: str, meta: Dict[str, Any],
                       context: Dict[str, Any], model: str, prompt_version: str) -> bool:
        """False when the signal already has a row (dedupe)."""
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO decisions
                   (signal_key, created_utc, mode, strategy, side, layer, ref_time_server,
                    ref_time_utc, entry_plan, sl_plan, tp_plan, atr, spread_points,
                    model, prompt_version, context_json, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                (signal_key, utc_now_str(), mode, meta.get("strategy"), meta.get("side"),
                 meta.get("layer"), meta.get("ref_time_server"), meta.get("ref_time_utc"),
                 meta.get("entry"), meta.get("sl"), meta.get("tp"), meta.get("atr"),
                 meta.get("spread_points"), model, prompt_version,
                 json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
            )
            return cur.rowcount == 1

    def complete(self, signal_key: str, *, status: str, response_text: str = "",
                 error: str = "", decision: Optional[str] = None,
                 confidence: Optional[int] = None, p_protect: Optional[float] = None,
                 reasons: Optional[List[str]] = None, risk_flags: Optional[List[str]] = None,
                 latency_ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                 cost_usd: float = 0.0, model: Optional[str] = None) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE decisions SET status=?, response_text=?, error=?, decision=?,
                   confidence=?, p_protect=?, reasons_json=?, risk_flags_json=?,
                   latency_ms=?, tokens_in=?, tokens_out=?, cost_usd=?,
                   model=COALESCE(?, model) WHERE signal_key=?""",
                (status, response_text[:4000], error[:500], decision, confidence, p_protect,
                 json.dumps(reasons or [], ensure_ascii=False),
                 json.dumps(risk_flags or [], ensure_ascii=False),
                 int(latency_ms), int(tokens_in), int(tokens_out), float(cost_usd),
                 model, signal_key),
            )

    def set_action(self, signal_key: str, action: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE decisions SET action=? WHERE signal_key=?", (action, signal_key))

    def attach_ticket(self, signal_key: str, ticket: int, fill: float) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE decisions SET ticket=?, entry_fill=? WHERE signal_key=?",
                        (int(ticket), float(fill), signal_key))

    def record_outcome(self, ticket: int, profit: float, closed_utc: Optional[str] = None) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE decisions SET closed_utc=?, profit=?, result=?
                   WHERE ticket=? AND closed_utc IS NULL""",
                (closed_utc or utc_now_str(), round(float(profit), 2),
                 result_label(float(profit)), int(ticket)),
            )
            return cur.rowcount > 0

    def store_sim(self, signal_key: str, profit: float, exit_kind: str, protected: bool, done: bool) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE decisions SET sim_profit=?, sim_exit=?, sim_protected=?, sim_done=?,
                   sim_updated_utc=? WHERE signal_key=?""",
                (round(float(profit), 2), exit_kind, int(bool(protected)), int(bool(done)),
                 utc_now_str(), signal_key),
            )

    def schedule_retry(self, signal_key: str, attempts: int, retry_after_utc: Optional[str]) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE decisions SET attempts=?, retry_after_utc=? WHERE signal_key=?",
                        (int(attempts), retry_after_utc, signal_key))

    # ------------------------------------------------------------------- reads
    RETRYABLE_STATUSES = ("http_error", "timeout")

    def due_retries(self, now_utc: str, since_utc: str, max_attempts: int,
                    limit: int = 2) -> List[Dict[str, Any]]:
        """Shadow rows whose provider call failed transiently and whose re-ask is due."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM decisions WHERE mode='shadow' AND status IN ('http_error','timeout')
                   AND context_json IS NOT NULL AND created_utc >= ? AND attempts < ?
                   AND (retry_after_utc IS NULL OR retry_after_utc <= ?) ORDER BY id LIMIT ?""",
                (since_utc, int(max_attempts), now_utc, int(limit)))
            return [dict(r) for r in cur.fetchall()]

    def pending_sims(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Verdicts (status ok or late) whose outcome simulation is not final yet, oldest first."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM decisions WHERE status IN ('ok','late') AND sim_done = 0
                   AND entry_plan IS NOT NULL ORDER BY id LIMIT ?""", (int(limit),))
            return [dict(r) for r in cur.fetchall()]

    def get(self, signal_key: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM decisions WHERE signal_key=?", (signal_key,))
            row = cur.fetchone()
        return dict(row) if row else None

    def rows(self, since_utc: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._cursor() as cur:
            if since_utc:
                cur.execute("SELECT * FROM decisions WHERE created_utc >= ? ORDER BY id", (since_utc,))
            else:
                cur.execute("SELECT * FROM decisions ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    def today_usage(self, now: Optional[datetime] = None) -> Tuple[int, float]:
        """(provider calls, cost USD) since 00:00 UTC; rows with tokens count as calls."""
        now = now or datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d 00:00:00")
        with self._cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS c FROM decisions
                   WHERE created_utc >= ? AND status NOT IN ('budget','no_key','disabled','queue_full')""",
                (start,),
            )
            row = cur.fetchone()
        return int(row["n"] or 0), float(row["c"] or 0.0)

    def summary(self, days: int = 30, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d 00:00:00")
        since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        def group(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
            ok = [r for r in rows if r["status"] == "ok"]
            take = [r for r in ok if r["decision"] == "TAKE"]
            skip = [r for r in ok if r["decision"] == "SKIP"]

            def outcome(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
                done = [float(r["profit"]) for r in rs if r["profit"] is not None]
                return {"n": len(rs), "closed": len(done),
                        "net": round(sum(done), 2),
                        "avg": round(sum(done) / len(done), 2) if done else None}
            return {
                "total": len(rows), "ok": len(ok),
                "errors": sum(1 for r in rows if r["status"] not in ("ok", "pending")),
                "take": outcome(take), "skip": outcome(skip),
                "blocked": sum(1 for r in rows if r["action"] in ("blocked", "blocked_fail_closed")),
                "cost": round(sum(float(r["cost_usd"] or 0) for r in rows), 4),
            }
        recent = self.rows(since)
        last = recent[-1] if recent else None
        return {
            "today": group([r for r in recent if r["created_utc"] >= today]),
            "period_days": days,
            "period": group(recent),
            "last": None if last is None else {
                "created_utc": last["created_utc"], "side": last["side"], "status": last["status"],
                "decision": last["decision"], "confidence": last["confidence"],
                "action": last["action"],
                "reason": (json.loads(last["reasons_json"] or "[]") or [""])[0],
            },
        }
