"""Admin panel -> Mini App commands (docs/miniapp/credits.md): `admin-commands.db` next to the Mini App database.

The admin process enqueues (credits grant/debit/unlimited, MB ID decisions, the credits switch); the Mini App
applies each command once (by its key, recorded in miniapp.db) and writes the result back here so the panel can
show it. Nothing personal beyond the Telegram user id, an MB ID and the owner's short reason is stored.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

FILE_NAME = "admin-commands.db"
KINDS = ("grant", "debit", "unlimited", "mb_approve", "mb_reject", "mb_set", "flag")
BATCH = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    user_id    INTEGER,
    payload    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    applied_at INTEGER,
    result     TEXT
);
CREATE INDEX IF NOT EXISTS commands_pending ON commands(applied_at, created_at);
"""


class CommandQueue:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def enqueue(self, key: str, kind: str, user_id: int | None, payload: dict, now: int) -> bool:
        """False when the key was already enqueued (a retried admin action)."""
        if kind not in KINDS:
            raise ValueError("unknown command kind")
        with self._connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO commands (key, kind, user_id, payload, created_at) "
                               "VALUES (?, ?, ?, ?, ?)", (key, kind, user_id, json.dumps(payload), now))
        return cur.rowcount == 1

    def results(self, keys: list[str]) -> dict[str, dict]:
        if not keys or not self.path.exists():
            return {}
        with self._connect() as conn:
            rows = conn.execute(f"SELECT key, applied_at, result FROM commands WHERE key IN ({','.join('?' * len(keys))})",
                                keys).fetchall()
        return {k: {"appliedAt": a, "result": r} for k, a, r in rows}

    def pending_count(self) -> int:
        if not self.path.exists():
            return 0
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM commands WHERE applied_at IS NULL").fetchone()[0]

    def apply_pending(self, apply: Callable[[str, str, int | None, dict], str], now: int) -> int:
        """Mini App side: applies the oldest pending commands in order; a command is marked only after `apply`
        returned (it is idempotent by key on the Mini App side, so a crash in between repeats nothing)."""
        if not self.path.exists():
            return 0
        with self._connect() as conn:
            rows = conn.execute("SELECT key, kind, user_id, payload FROM commands WHERE applied_at IS NULL "
                                "ORDER BY created_at, rowid LIMIT ?", (BATCH,)).fetchall()
        done = 0
        for key, kind, user_id, payload in rows:
            try:
                data = json.loads(payload)
            except ValueError:
                data = None
            result = apply(key, kind, user_id, data) if isinstance(data, dict) else "invalid"
            with self._connect() as conn:
                conn.execute("UPDATE commands SET applied_at = ?, result = ? WHERE key = ? AND applied_at IS NULL",
                             (now, result, key))
            done += 1
        return done
