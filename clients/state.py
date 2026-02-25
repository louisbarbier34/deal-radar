"""
Persistent state store backed by SQLite.
Survives restarts — prevents duplicate Slack alerts and re-processed events.

Usage:
    from clients.state import state
    state.has_processed("a3_email", "msg-id-123")   # → bool
    state.mark_processed("a3_email", "msg-id-123")
    state.get_snapshot("b2_deals")                   # → dict
    state.set_snapshot("b2_deals", {...})
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import config as _config

logger = logging.getLogger(__name__)

DB_PATH = Path(_config.DB_PATH)


class StateStore:
    """
    Thread-safe SQLite-backed state store.
    Two tables:
      - processed_ids(namespace TEXT, id TEXT, created_at TIMESTAMP)
      - snapshots(namespace TEXT, data TEXT, updated_at TIMESTAMP)
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS processed_ids (
                    namespace TEXT NOT NULL,
                    id        TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (namespace, id)
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    namespace  TEXT PRIMARY KEY,
                    data       TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        logger.debug("State DB initialised at %s", self._db_path)

    # ── Processed IDs ────────────────────────────────────────────────────

    def has_processed(self, namespace: str, item_id: str) -> bool:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_ids WHERE namespace=? AND id=?",
                (namespace, item_id),
            ).fetchone()
        return row is not None

    def mark_processed(self, namespace: str, item_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_ids (namespace, id) VALUES (?, ?)",
                (namespace, item_id),
            )

    def mark_many_processed(self, namespace: str, ids: list[str]) -> None:
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO processed_ids (namespace, id) VALUES (?, ?)",
                [(namespace, i) for i in ids],
            )

    def purge_old(self, namespace: str, keep_days: int = 30) -> int:
        """Remove processed IDs older than `keep_days` to keep the DB lean."""
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM processed_ids WHERE namespace=? AND created_at < datetime('now', ?)",
                (namespace, f"-{keep_days} days"),
            )
        return cur.rowcount

    # ── Snapshots (arbitrary JSON blobs) ─────────────────────────────────

    def get_snapshot(self, namespace: str) -> dict:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM snapshots WHERE namespace=?", (namespace,)
            ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return {}

    def set_snapshot(self, namespace: str, data: dict) -> None:
        serialised = json.dumps(data)
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO snapshots (namespace, data, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(namespace) DO UPDATE SET
                     data=excluded.data,
                     updated_at=excluded.updated_at""",
                (namespace, serialised),
            )


# Singleton — import this everywhere
state = StateStore()
