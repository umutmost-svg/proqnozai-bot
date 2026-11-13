"""Admin side of the admin -> bot message queue `bot-outbox.db` (admin-dashboard.md §10).

The admin process never calls Telegram and never holds the bot token: it writes jobs (one recipient each)
into this shared SQLite file; the existing bot claims and sends them (bot_outbox.py) and records the outcome.
Inserts are idempotent by key, so a request retried after a crash never creates a second message. Texts are
plain text only; nothing here logs them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_TEXT = 4000
JOB_TTL_S = 3 * 86400  # a job not sent within three days of its start is expired, not sent late

# Shared with bot_outbox.py (the bot creates the same schema if it starts first).
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    user_id         INTEGER NOT NULL,
    batch_id        TEXT,
    text            TEXT NOT NULL,
    buttons_json    TEXT NOT NULL DEFAULT '[]',
    not_before      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'expired', 'cancelled')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    retry_at        INTEGER,
    admin_event_id  INTEGER,
    created_at      INTEGER NOT NULL,
    claimed_at      INTEGER,
    sent_at         INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, not_before);
CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id);
"""


class Outbox:
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
        fresh = not self.path.exists()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        if fresh:
            os.chmod(self.path, 0o660)  # admin writes jobs, the bot writes outcomes (one group on the volume)

    def enqueue(self, jobs: Iterable[tuple[str, int, str, list[dict]]], *, batch_id: str | None, not_before: int,
                admin_event_id: int | None, now: int) -> int:
        """jobs: (idempotency_key, user_id, text, buttons). One transaction; existing keys are left as they are.
        Returns how many new jobs were queued."""
        added = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for key, user_id, text, buttons in jobs:
                    if not 0 < len(text) <= MAX_TEXT:
                        raise ValueError("text")
                    added += conn.execute(
                        "INSERT OR IGNORE INTO jobs (id, idempotency_key, user_id, batch_id, text, buttons_json, "
                        "not_before, expires_at, status, admin_event_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                        (str(uuid.uuid4()), key, user_id, batch_id, text, json.dumps(buttons), not_before,
                         not_before + JOB_TTL_S, admin_event_id, now)).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return added

    def cancel_batch(self, batch_id: str) -> int:
        """Jobs of a cancelled broadcast that the bot has not claimed yet."""
        with self._connect() as conn:
            return conn.execute("UPDATE jobs SET status = 'cancelled' WHERE batch_id = ? AND status = 'queued'",
                                (batch_id,)).rowcount

    def status_by_keys(self, keys: list[str]) -> dict[str, dict]:
        if not keys or not self.path.exists():
            return {}
        out: dict[str, dict] = {}
        with self._connect() as conn:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                for key, status, code, sent_at in conn.execute(
                        "SELECT idempotency_key, status, last_error_code, sent_at FROM jobs WHERE idempotency_key IN "
                        f"({','.join('?' * len(chunk))})", chunk):
                    out[key] = {"status": status, "error": code, "sentAt": sent_at}
        return out

    def batch_counts(self, batch_id: str) -> dict[str, int]:
        if not self.path.exists():
            return {}
        with self._connect() as conn:
            return {s: n for s, n in conn.execute("SELECT status, COUNT(*) FROM jobs WHERE batch_id = ? GROUP BY status",
                                                  (batch_id,))}
