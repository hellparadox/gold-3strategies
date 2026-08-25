"""SQLite subscription + access-control layer for the VIP signal service.

Thread-safety strategy: one long-lived connection opened with
``check_same_thread=False`` and every statement wrapped in a module-level
``threading.RLock``.  SQLite serialises writes anyway, and this keeps the
Telegram asyncio thread and the MT5 execution thread from tripping over each
other.  WAL journalling keeps concurrent reads fast.

All timestamps are stored as ISO-8601 UTC strings so the DB is portable and
human-readable.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from loguru import logger

__all__ = ["Database", "UserRecord", "utc_now"]

ISO = "%Y-%m-%d %H:%M:%S"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    plan_type     TEXT NOT NULL DEFAULT 'free',
    vip_expire_at TEXT,
    joined_at     TEXT NOT NULL,
    last_seen_at  TEXT,
    is_blocked    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    days       INTEGER NOT NULL,
    amount     REAL NOT NULL DEFAULT 0,
    granted_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket     INTEGER,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    strategy   TEXT NOT NULL,
    lot        REAL NOT NULL,
    entry      REAL NOT NULL,
    sl         REAL NOT NULL,
    tp         REAL NOT NULL,
    atr        REAL,
    reason     TEXT,
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    close_price REAL,
    profit     REAL,
    outcome    TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_plan   ON users (plan_type);
CREATE INDEX IF NOT EXISTS idx_users_expiry ON users (vip_expire_at);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals (created_at);
CREATE INDEX IF NOT EXISTS idx_signals_tkt  ON signals (ticket);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(ISO) if dt else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in (ISO, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    logger.warning("unparseable timestamp in DB: {!r}", value)
    return None


@dataclass
class UserRecord:
    """Typed projection of a row in ``users``."""

    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    plan_type: str = "free"
    vip_expire_at: Optional[datetime] = None
    joined_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    is_blocked: bool = False
    notes: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "UserRecord":
        return cls(
            user_id=int(row["user_id"]),
            username=row["username"],
            first_name=row["first_name"],
            plan_type=str(row["plan_type"] or "free"),
            vip_expire_at=_parse(row["vip_expire_at"]),
            joined_at=_parse(row["joined_at"]),
            last_seen_at=_parse(row["last_seen_at"]),
            is_blocked=bool(row["is_blocked"]),
            notes=row["notes"] if "notes" in row.keys() else None,
        )

    @property
    def is_vip(self) -> bool:
        if self.plan_type != "vip" or self.vip_expire_at is None:
            return False
        return self.vip_expire_at > utc_now()

    @property
    def days_left(self) -> int:
        if not self.is_vip or self.vip_expire_at is None:
            return 0
        delta = self.vip_expire_at - utc_now()
        return max(0, delta.days + (1 if delta.seconds else 0))

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or f"id:{self.user_id}"


class Database:
    """Thread-safe SQLite gateway for users, payments and signal history."""

    def __init__(self, path: str = "subscriptions.db") -> None:
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        logger.info("subscription database ready at {}", os.path.abspath(self.path))

    # ------------------------------------------------------------------ setup
    def _configure(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=10000")
            self._conn.commit()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

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
            try:
                self._conn.close()
            except Exception as exc:  # pragma: no cover
                logger.debug("db close raised: {}", exc)

    # ------------------------------------------------------------------ users
    def register_user(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> UserRecord:
        """Idempotent upsert: creates a free user or refreshes their profile."""
        now = _fmt(utc_now())
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (user_id, username, first_name, plan_type, joined_at, last_seen_at)
                VALUES (?, ?, ?, 'free', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username     = COALESCE(excluded.username, users.username),
                    first_name   = COALESCE(excluded.first_name, users.first_name),
                    last_seen_at = excluded.last_seen_at
                """,
                (int(user_id), username, first_name, now, now),
            )
        record = self.get_user(user_id)
        assert record is not None
        return record

    def get_user(self, user_id: int) -> Optional[UserRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = ?", (int(user_id),))
            row = cur.fetchone()
        return UserRecord.from_row(row) if row else None

    def touch_user(self, user_id: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE users SET last_seen_at = ? WHERE user_id = ?",
                (_fmt(utc_now()), int(user_id)),
            )

    def is_vip(self, user_id: int) -> bool:
        """True only when plan is 'vip' AND the expiry is in the future."""
        user = self.get_user(user_id)
        if user is None:
            return False
        if user.plan_type == "vip" and not user.is_vip:
            self.expire_user(user_id)      # lazy downgrade on read
            return False
        return user.is_vip

    def add_vip_days(
        self,
        user_id: int,
        days: int,
        amount: float = 0.0,
        granted_by: Optional[int] = None,
    ) -> UserRecord:
        """Grant/extend VIP. Extends from the current expiry when still active."""
        if int(days) == 0:
            raise ValueError("days must be non-zero")
        user = self.get_user(user_id) or self.register_user(user_id)
        base = user.vip_expire_at if (user.is_vip and user.vip_expire_at) else utc_now()
        new_expiry = base + timedelta(days=int(days))
        plan = "vip" if new_expiry > utc_now() else "free"
        with self._cursor() as cur:
            cur.execute(
                "UPDATE users SET plan_type = ?, vip_expire_at = ? WHERE user_id = ?",
                (plan, _fmt(new_expiry), int(user_id)),
            )
            cur.execute(
                """INSERT INTO payments (user_id, days, amount, granted_by, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(user_id), int(days), float(amount), granted_by, _fmt(utc_now())),
            )
        logger.info("VIP {:+d} days for {} -> expires {}", int(days), user_id, _fmt(new_expiry))
        updated = self.get_user(user_id)
        assert updated is not None
        return updated

    def expire_user(self, user_id: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE users SET plan_type = 'free' WHERE user_id = ?", (int(user_id),)
            )
        logger.info("user {} downgraded to free", user_id)

    def remove_vip(self, user_id: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE users SET plan_type = 'free', vip_expire_at = NULL WHERE user_id = ?",
                (int(user_id),),
            )

    def set_blocked(self, user_id: int, blocked: bool = True) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE users SET is_blocked = ? WHERE user_id = ?",
                (1 if blocked else 0, int(user_id)),
            )

    def get_user_status(self, user_id: int) -> Dict[str, Any]:
        """Rich status dict used to render the subscription card."""
        user = self.get_user(user_id)
        if user is None:
            return {
                "exists": False, "user_id": int(user_id), "plan_type": "free",
                "is_vip": False, "days_left": 0, "expire_at": None,
                "joined_at": None, "display_name": f"id:{user_id}", "blocked": False,
            }
        active = user.is_vip
        if user.plan_type == "vip" and not active:
            self.expire_user(user_id)
        return {
            "exists": True,
            "user_id": user.user_id,
            "username": user.username,
            "first_name": user.first_name,
            "display_name": user.display_name,
            "plan_type": "vip" if active else "free",
            "is_vip": active,
            "days_left": user.days_left,
            "expire_at": _fmt(user.vip_expire_at),
            "joined_at": _fmt(user.joined_at),
            "blocked": user.is_blocked,
        }

    def get_all_vip_users(self, include_expired: bool = False) -> List[UserRecord]:
        """Active VIP subscribers (the broadcast audience)."""
        query = "SELECT * FROM users WHERE plan_type = 'vip' AND is_blocked = 0"
        params: Tuple[Any, ...] = ()
        if not include_expired:
            query += " AND vip_expire_at IS NOT NULL AND vip_expire_at > ?"
            params = (_fmt(utc_now()),)
        query += " ORDER BY vip_expire_at ASC"
        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [UserRecord.from_row(r) for r in rows]

    def get_all_users(self, limit: int = 1000) -> List[UserRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT ?", (int(limit),))
            rows = cur.fetchall()
        return [UserRecord.from_row(r) for r in rows]

    def expiring_soon(self, days: int = 3) -> List[UserRecord]:
        """VIPs whose subscription lapses within ``days`` (renewal nudges)."""
        horizon = _fmt(utc_now() + timedelta(days=int(days)))
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM users
                   WHERE plan_type = 'vip' AND vip_expire_at IS NOT NULL
                     AND vip_expire_at > ? AND vip_expire_at <= ?
                   ORDER BY vip_expire_at ASC""",
                (_fmt(utc_now()), horizon),
            )
            rows = cur.fetchall()
        return [UserRecord.from_row(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            total = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(*) AS c FROM users WHERE plan_type='vip' AND vip_expire_at > ?",
                (_fmt(utc_now()),),
            )
            vip = int(cur.fetchone()["c"])
            cur.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM payments")
            revenue = float(cur.fetchone()["s"])
            cur.execute("SELECT COUNT(*) AS c FROM signals")
            signals = int(cur.fetchone()["c"])
        return {
            "total_users": total,
            "vip_users": vip,
            "free_users": total - vip,
            "revenue": revenue,
            "signals_logged": signals,
        }

    # ---------------------------------------------------------------- signals
    def log_signal(
        self,
        *,
        ticket: int,
        symbol: str,
        side: str,
        strategy: str,
        lot: float,
        entry: float,
        sl: float,
        tp: float,
        atr: float = 0.0,
        reason: str = "",
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO signals
                   (ticket, symbol, side, strategy, lot, entry, sl, tp, atr, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(ticket), str(symbol), str(side), str(strategy), float(lot),
                    float(entry), float(sl), float(tp), float(atr), str(reason),
                    _fmt(utc_now()),
                ),
            )
            return int(cur.lastrowid)

    def close_signal(
        self, ticket: int, close_price: float, profit: float, outcome: str
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE signals
                   SET closed_at = ?, close_price = ?, profit = ?, outcome = ?
                   WHERE ticket = ? AND closed_at IS NULL""",
                (_fmt(utc_now()), float(close_price), float(profit), str(outcome), int(ticket)),
            )

    def recent_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (int(limit),)
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def signal_performance(self) -> Dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS trades,
                          COALESCE(SUM(profit), 0) AS net,
                          SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN profit <= 0 THEN 1 ELSE 0 END) AS losses
                   FROM signals WHERE closed_at IS NOT NULL"""
            )
            row = cur.fetchone()
        trades = int(row["trades"] or 0)
        wins = int(row["wins"] or 0)
        return {
            "trades": trades,
            "wins": wins,
            "losses": int(row["losses"] or 0),
            "net": float(row["net"] or 0.0),
            "win_rate": (wins / trades * 100.0) if trades else 0.0,
        }

    # ------------------------------------------------------- context manager
    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
