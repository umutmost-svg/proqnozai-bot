"""Bot side of the admin -> bot message queue `bot-outbox.db` (docs/miniapp/admin-dashboard.md §10).

The admin panel writes jobs (one recipient each, plain text); the bot — the only holder of the token —
claims, sends and records the outcome here. This module is the whole contract on the bot side; wiring it
into the bot's job queue (main.py, behind a flag that is off by default) is a separate step.

Rules: a job is claimed atomically (queued -> sending), so two bot instances never send it twice; a job
left in `sending` (crash) returns to the queue after STUCK_S, at most MAX_ATTEMPTS times; expired jobs are
never sent late; users banned in the bot are skipped; texts are never logged. Retention: texts of finished
jobs are cleared after TEXT_RETENTION_DAYS, rows deleted after ROW_RETENTION_DAYS.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("bot_outbox")

STUCK_S = 600
MAX_ATTEMPTS = 3
BATCH = 25  # per run: below Telegram's ~30 messages/s; the caller runs it about once a second
TEXT_RETENTION_DAYS = 30
ROW_RETENTION_DAYS = 400
DAY = 86400

# Same schema as miniapp/admin_web/outbox.py (whichever side starts first creates it; a test keeps them equal).
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


@dataclass(frozen=True)
class Job:
    id: str
    user_id: int
    text: str
    buttons: list[dict]
    attempts: int


@dataclass(frozen=True)
class Outcome:
    """What sending did: `sent`, `retry` (rate limit, network: try again later) or `failed` (permanent)."""
    kind: str
    code: str | None = None
    retry_after: int = 0
    message_id: int | None = None


def classify(exc: BaseException) -> Outcome:
    """python-telegram-bot errors -> outcome codes (codes only: the error text may echo user data)."""
    from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

    if isinstance(exc, RetryAfter):
        wait = exc.retry_after
        seconds = int(wait.total_seconds()) if hasattr(wait, "total_seconds") else int(wait)
        return Outcome("retry", "rate_limited", max(1, seconds))
    if isinstance(exc, Forbidden):
        return Outcome("failed", "blocked_by_user")
    if isinstance(exc, BadRequest):
        return Outcome("failed", "bad_request")
    if isinstance(exc, (TimedOut, NetworkError)):
        return Outcome("retry", "network", 30)
    return Outcome("failed", "error")


class BotOutbox:
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

    def claim(self, now: int, limit: int = BATCH) -> list[Job]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE jobs SET status = 'expired' WHERE status = 'queued' AND expires_at <= ?", (now,))
                # Stuck in `sending` (the bot died mid-send): back to the queue, or failed after MAX_ATTEMPTS.
                conn.execute("UPDATE jobs SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END, "
                             "last_error_code = 'stuck' WHERE status = 'sending' AND claimed_at <= ?",
                             (MAX_ATTEMPTS, now - STUCK_S))
                rows = conn.execute(
                    "SELECT id, user_id, text, buttons_json, attempts FROM jobs WHERE status = 'queued' "
                    "AND not_before <= ? AND (retry_at IS NULL OR retry_at <= ?) ORDER BY not_before, created_at "
                    "LIMIT ?", (now, now, limit)).fetchall()
                jobs = []
                for job_id, user_id, text, buttons, attempts in rows:
                    if conn.execute("UPDATE jobs SET status = 'sending', claimed_at = ?, attempts = attempts + 1 "
                                    "WHERE id = ? AND status = 'queued'", (now, job_id)).rowcount:
                        jobs.append(Job(job_id, user_id, text, json.loads(buttons), attempts + 1))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return jobs

    def finish(self, job: Job, outcome: Outcome, now: int) -> None:
        with self._connect() as conn:
            if outcome.kind == "sent":
                conn.execute("UPDATE jobs SET status = 'sent', sent_at = ?, last_error_code = NULL WHERE id = ? "
                             "AND status = 'sending'", (now, job.id))
            elif outcome.kind == "retry" and job.attempts < MAX_ATTEMPTS:
                conn.execute("UPDATE jobs SET status = 'queued', retry_at = ?, last_error_code = ? WHERE id = ? "
                             "AND status = 'sending'", (now + max(1, outcome.retry_after), outcome.code, job.id))
            else:
                conn.execute("UPDATE jobs SET status = 'failed', last_error_code = ? WHERE id = ? AND status = 'sending'",
                             (outcome.code or "error", job.id))

    async def run_once(self, send: Callable[[Job], Awaitable[Outcome]], now: Callable[[], int],
                       is_banned: Callable[[int], bool] = lambda user_id: False) -> dict[str, int]:
        """Claims due jobs and sends them through `send` (the bot's own sender). Returns counts by outcome."""
        counts: dict[str, int] = {}
        for job in self.claim(now()):
            if is_banned(job.user_id):
                outcome = Outcome("failed", "bot_ban")
            else:
                try:
                    outcome = await send(job)
                except Exception as exc:  # noqa: BLE001 — classified by type, never logged with its text
                    outcome = classify(exc)
            self.finish(job, outcome, now())
            counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
        if counts:
            logger.info("event=outbox_run %s", " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return counts

    def purge(self, now: int) -> None:
        finished = "('sent', 'failed', 'expired', 'cancelled')"
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET text = '', buttons_json = '[]' WHERE status IN {finished} "
                         "AND created_at < ? AND text != ''", (now - TEXT_RETENTION_DAYS * DAY,))
            conn.execute(f"DELETE FROM jobs WHERE status IN {finished} AND created_at < ?",
                         (now - ROW_RETENTION_DAYS * DAY,))
