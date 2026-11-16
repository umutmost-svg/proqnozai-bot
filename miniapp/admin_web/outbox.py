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
STATUSES = ("queued", "sending", "sent", "failed", "expired", "cancelled")
# A queued job with this retry_at is held: the bot claims only `retry_at <= now`, so it is invisible to the bot
# until `release_batch` (a broadcast's status is re-checked first; see crm_routes.queue_broadcast).
HOLD = 2**62
# The bot side's limits (bot_outbox.py; the bot package is not importable here, a test keeps them equal).
BOT_LIMITS = {"batchPerRun": 25, "maxAttempts": 3, "stuckS": 600, "textRetentionDays": 30, "rowRetentionDays": 400,
              "jobTtlS": JOB_TTL_S}

# Shared with bot_outbox.py (the bot creates the same schema if it starts first). `cancelled_batches`: a batch
# cancelled in THIS database. Enqueue, release, cancel and the bot's claim/retry all run in this one file, so SQLite
# orders them: a marked batch never gets new jobs, its held jobs are never released, and the bot never claims or
# re-queues one of its jobs (a retry after the cancel ends as cancelled).
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
    claim_token     TEXT,
    sent_at         INTEGER,
    delete_after_s  INTEGER,
    message_id      INTEGER,
    delete_status   TEXT,
    deleted_at      INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, not_before);
CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id);
CREATE TABLE IF NOT EXISTS cancelled_batches (
    batch_id     TEXT PRIMARY KEY,
    cancelled_at INTEGER NOT NULL
);
"""

# Columns added after the first release (auto-delete, 15.11): added to existing files by both sides' init_schema.
ADDED_COLUMNS = (("delete_after_s", "INTEGER"), ("message_id", "INTEGER"), ("delete_status", "TEXT"),
                 ("deleted_at", "INTEGER"))


def add_columns(conn) -> None:
    """The admin and the bot may start together on an older file: the check and the ALTERs run under one write lock,
    and a column the other side added meanwhile is not an error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        for name, kind in ADDED_COLUMNS:
            if name not in have:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise



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
            add_columns(conn)
        if fresh:
            os.chmod(self.path, 0o660)  # admin writes jobs, the bot writes outcomes (one group on the volume)

    def enqueue(self, jobs: Iterable[tuple[str, int, str, list[dict]]], *, batch_id: str | None, not_before: int,
                admin_event_id: int | None, now: int, hold: bool = False, delete_after_s: int | None = None) -> int:
        """jobs: (idempotency_key, user_id, text, buttons). One transaction; existing keys are left as they are.
        `hold`: the new jobs stay unclaimable until `release_batch`. Returns how many new jobs were queued."""
        added = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if batch_id is not None and conn.execute("SELECT 1 FROM cancelled_batches WHERE batch_id = ?",
                                                         (batch_id,)).fetchone():
                    conn.execute("COMMIT")
                    return 0  # cancelled already: nothing new is queued
                for key, user_id, text, buttons in jobs:
                    if not 0 < len(text) <= MAX_TEXT:
                        raise ValueError("text")
                    added += conn.execute(
                        "INSERT OR IGNORE INTO jobs (id, idempotency_key, user_id, batch_id, text, buttons_json, "
                        "not_before, expires_at, status, retry_at, admin_event_id, created_at, delete_after_s) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                        (str(uuid.uuid4()), key, user_id, batch_id, text, json.dumps(buttons), not_before,
                         not_before + JOB_TTL_S, HOLD if hold else None, admin_event_id, now,
                         delete_after_s)).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return added

    def release_batch(self, batch_id: str) -> int:
        """Makes a batch's held jobs claimable unless the batch is cancelled here (a real retry time set by the
        bot is never touched)."""
        with self._connect() as conn:
            return conn.execute("UPDATE jobs SET retry_at = NULL WHERE batch_id = ? AND status = 'queued' "
                                "AND retry_at = ? AND NOT EXISTS (SELECT 1 FROM cancelled_batches WHERE batch_id = ?)",
                                (batch_id, HOLD, batch_id)).rowcount

    def cancel_batch(self, batch_id: str, now: int) -> int:
        """Cancels a batch in this database (the point of no return for the bot): marks it, and cancels its jobs
        the bot has not claimed yet, in one transaction. Idempotent."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT OR IGNORE INTO cancelled_batches (batch_id, cancelled_at) VALUES (?, ?)",
                             (batch_id, now))
                n = conn.execute("UPDATE jobs SET status = 'cancelled' WHERE batch_id = ? AND status = 'queued'",
                                 (batch_id,)).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return n

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

    # ---- queue view (admin panel; no texts leave these methods) ------------------------------

    def summary(self, now: int) -> dict:
        """Jobs by status (all; and outcomes of the last 24 h by when they happened: sent by sent_at, failed by the
        last attempt, expired by expiry, the others by creation), the next due time and the last send."""
        if not self.path.exists():
            return {"connected": False, "byStatus": {}, "last24h": {}, "nextDueAt": None, "lastSentAt": None}
        with self._connect() as conn:
            by_status = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
            # Failures by their last attempt (claimed_at), expiry by expires_at; cancellations have no own
            # time in the shared schema, so they stay by creation.
            recent = dict(conn.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE (status = 'failed' AND COALESCE(claimed_at, created_at) >= ?) "
                "OR (status = 'expired' AND expires_at >= ?) OR (status IN ('queued', 'sending', 'cancelled') "
                "AND created_at >= ?) GROUP BY status", (now - 86400, now - 86400, now - 86400)).fetchall())
            recent["sent"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'sent' AND sent_at >= ?",
                                          (now - 86400,)).fetchone()[0]
            next_due = conn.execute("SELECT MIN(MAX(not_before, COALESCE(retry_at, 0))) FROM jobs "
                                    "WHERE status = 'queued' AND COALESCE(retry_at, 0) != ?", (HOLD,)).fetchone()[0]
            last_sent = conn.execute("SELECT MAX(sent_at) FROM jobs WHERE status = 'sent'").fetchone()[0]
        return {"connected": True, "byStatus": by_status, "last24h": recent, "nextDueAt": next_due,
                "lastSentAt": last_sent}

    def recent(self, limit: int = 100, status: str | None = None) -> list[dict]:
        """Newest jobs without their text: kind (message / broadcast), recipient, status, attempts, times."""
        if not self.path.exists():
            return []
        where, args = ("WHERE status = ?", [status]) if status in STATUSES else ("", [])
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT idempotency_key, user_id, batch_id, status, attempts, last_error_code, not_before, created_at, "
                f"sent_at FROM jobs {where} ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"key": r[0], "kind": "broadcast" if r[2] else "message", "userId": r[1], "batch": r[2],
                 "status": r[3], "attempts": r[4], "error": r[5], "notBefore": r[6], "createdAt": r[7], "sentAt": r[8]}
                for r in rows]
