"""Bot side of the admin -> bot message queue `bot-outbox.db` (docs/miniapp/admin-dashboard.md §10).

The admin panel writes jobs (one recipient each, plain text); the bot — the only holder of the token —
claims, sends and records the outcome here. This module is the whole contract on the bot side; wiring it
into the bot's job queue (main.py, behind a flag that is off by default) is a separate step.

Rules: a job is claimed atomically (queued -> sending) right before it is sent, with a claim token that fences
its outcome, so two bot instances never send it twice. Delivery is AT MOST ONCE: a job left in `sending` past
STUCK_S (the bot died mid-send, Telegram's outcome unknown) is marked `failed/interrupted`, never re-sent;
only Telegram's explicit "retry after" (the message was refused, not sent) is retried, up to MAX_ATTEMPTS;
a timeout or network error is `failed/unknown_outcome`. Either one PAUSES the whole outbox (Fable audit 16.11): a
"retry after" for the time Telegram names (the flood limit is per bot, so every other job would be refused too, and
that retry does not use up an attempt — the job's expiry bounds it), a network error for NETWORK_PAUSE_S doubling up
to NETWORK_PAUSE_MAX_S while it lasts, so an outage costs one job its delivery, never the whole batch. Expired jobs
are never sent late; users banned in the bot are skipped; texts are never logged. Retention: texts of finished
jobs are cleared after TEXT_RETENTION_DAYS, rows deleted after ROW_RETENTION_DAYS.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("bot_outbox")


def match_owner(path: Path, conn: sqlite3.Connection) -> None:
    """The bot runs as root while the admin process (uid 10001) shares this file: SQLite's -wal/-shm sidecars the bot
    creates are handed to the database file's owner, so the admin can still open the database. No-op unless root."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()  # opens (creates) the sidecars in WAL mode
        st = os.stat(path)
        for suffix in ("-wal", "-shm"):
            side = Path(f"{path}{suffix}")
            if side.exists() and (side.stat().st_uid, side.stat().st_gid) != (st.st_uid, st.st_gid):
                os.chown(side, st.st_uid, st.st_gid)
    except OSError:
        pass  # best effort: the Mini App's entrypoint also restores ownership of its volume on every start

STUCK_S = 600
MAX_ATTEMPTS = 3
BATCH = 25  # per run: below Telegram's ~30 messages/s; the caller runs it about once a second
NETWORK_PAUSE_S = 30
NETWORK_PAUSE_MAX_S = 300
TEXT_RETENTION_DAYS = 30
ROW_RETENTION_DAYS = 400
DAY = 86400
# Telegram: a bot may delete its own message in a private chat only within 48 h (kept below with a margin).
DELETE_LIMIT_S = 47 * 3600 + 30 * 60
MAX_DELETE_AFTER_S = 47 * 3600

# Same schema as miniapp/admin_web/outbox.py (whichever side starts first creates it; a test keeps them equal).
# cancelled_batches is written by the admin's cancel; claim and retry honour it.
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
    deleted_at      INTEGER,
    parse_mode      TEXT
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
                 ("deleted_at", "INTEGER"), ("parse_mode", "TEXT"))  # parse_mode: 'HTML' for broadcasts, 17.11


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



@dataclass(frozen=True)
class Job:
    id: str
    user_id: int
    text: str
    buttons: list[dict]
    attempts: int
    token: str
    parse_mode: str | None = None  # 'HTML' (broadcasts) or None (plain text)


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
        # Telegram may have accepted the message before the answer was lost: unknown, so never re-sent.
        return Outcome("failed", "unknown_outcome")
    return Outcome("failed", "error")


class BotOutbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        # No job is claimed before this moment (flood limit / network outage). Kept in a file of the bot's own next to
        # the queue (the queue's schema is shared with the admin): a restart, a job enqueued or a batch released
        # during the pause all wait for it too (Codex wave1 r2).
        self._pause_file = path.with_name(path.name + ".pause")
        self.paused_until = self._read_pause()
        self._network_pause = 0  # the current network back-off, doubled per failure in a row

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # mode=rw: never creates the file — the admin process does (as its uid); a missing file is an error here.
        conn = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            match_owner(self.path, conn)
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            add_columns(conn)

    def claim(self, now: int) -> Job | None:
        """The next due job, claimed for sending now (one at a time: a claim never waits behind other sends)."""
        token = secrets.token_hex(16)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Outcome unknown (crashed mid-send): never re-sent — at most once.
                conn.execute("UPDATE jobs SET status = 'failed', last_error_code = 'interrupted', claim_token = NULL "
                             "WHERE status = 'sending' AND claimed_at <= ?", (now - STUCK_S,))
                conn.execute("UPDATE jobs SET status = 'expired' WHERE status = 'queued' AND expires_at <= ?", (now,))
                row = conn.execute(
                    "SELECT id, user_id, text, buttons_json, attempts, parse_mode FROM jobs WHERE status = 'queued' "
                    "AND not_before <= ? AND expires_at > ? AND (retry_at IS NULL OR retry_at <= ?) "
                    "AND (batch_id IS NULL OR batch_id NOT IN (SELECT batch_id FROM cancelled_batches)) "
                    "ORDER BY not_before, created_at LIMIT 1", (now, now, now)).fetchone()
                job = None
                if row is not None and conn.execute(
                        "UPDATE jobs SET status = 'sending', claimed_at = ?, claim_token = ?, attempts = attempts + 1 "
                        "WHERE id = ? AND status = 'queued'", (now, token, row[0])).rowcount:
                    job = Job(row[0], row[1], row[2], json.loads(row[3]), row[4] + 1, token,
                              row[5] if row[5] == "HTML" else None)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return job

    def _read_pause(self) -> int:
        try:
            return int(self._pause_file.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0  # no pause on record (or unreadable): the jobs' own retry_at still holds the refused one

    def _pause(self, until: int) -> None:
        """No job before `until`, also after a restart. Written BEFORE the outcome is recorded: a stop between the two
        leaves the pause in place, never a recorded failure without its pause."""
        # Only ever extended, under an exclusive lock: the file's deadline (another bot process may have written a
        # longer one) is read and kept inside the same critical section, so no writer can shorten it.
        self.paused_until = max(self.paused_until, until)
        tmp = self._pause_file.with_name(f"{self._pause_file.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with open(self._pause_file.with_name(self._pause_file.name + ".lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.paused_until = max(self.paused_until, self._read_pause())
                tmp.write_text(str(self.paused_until))
                os.replace(tmp, self._pause_file)
        except OSError as exc:
            # The file is unusable: the pause is kept in the queue itself instead — every queued job waits for it,
            # also after a restart (Codex wave1 r4) — and in memory for this process. Accepted limit (Codex wave1 r5):
            # a job enqueued after this AND a restart before the pause ends may be tried once early; a flood limit then
            # refuses it again (requeued, no attempt used, the pause re-set), a network outage costs that one job.
            # Closing it fully needs a queue-wide deadline in the schema shared with the admin — not worth it.
            logger.warning("event=outbox_pause_not_saved type=%s", type(exc).__name__)
            with self._connect() as conn:
                conn.execute("UPDATE jobs SET retry_at = ? WHERE status = 'queued' AND (retry_at IS NULL OR retry_at < ?)",
                             (self.paused_until, self.paused_until))
        finally:
            tmp.unlink(missing_ok=True)

    def finish(self, job: Job, outcome: Outcome, now: int) -> bool:
        """Records the outcome only for the claim that is still current (fenced by its token)."""
        fence = "WHERE id = ? AND status = 'sending' AND claim_token = ?"
        with self._connect() as conn:
            if outcome.kind == "sent":
                cur = conn.execute(f"UPDATE jobs SET status = 'sent', sent_at = ?, message_id = ?, last_error_code = NULL, "
                                   f"claim_token = NULL {fence}", (now, outcome.message_id, job.id, job.token))
            elif outcome.kind == "retry" and (outcome.code == "rate_limited" or job.attempts < MAX_ATTEMPTS):
                # A batch cancelled while this job was being sent: never queued again. A flood limit refused the
                # message (nothing was sent) and pauses the whole outbox: it gives its attempt back.
                given_back = 1 if outcome.code == "rate_limited" else 0
                cur = conn.execute(f"UPDATE jobs SET status = CASE WHEN batch_id IN (SELECT batch_id FROM "
                                   f"cancelled_batches) THEN 'cancelled' ELSE 'queued' END, retry_at = ?, "
                                   f"attempts = attempts - ?, last_error_code = ?, claim_token = NULL {fence}",
                                   (now + max(1, outcome.retry_after), given_back, outcome.code, job.id, job.token))
            else:
                cur = conn.execute(f"UPDATE jobs SET status = 'failed', last_error_code = ?, claim_token = NULL {fence}",
                                   (outcome.code or "error", job.id, job.token))
        return cur.rowcount > 0

    async def run_once(self, send: Callable[[Job], Awaitable[Outcome]], now: Callable[[], int],
                       is_banned: Callable[[int], bool] = lambda user_id: False) -> dict[str, int]:
        """Claims due jobs and sends them through `send` (the bot's own sender). Returns counts by outcome."""
        counts: dict[str, int] = {}
        for _ in range(BATCH):
            # Before every claim: another bot process may have paused the outbox since the last one.
            self.paused_until = max(self.paused_until, self._read_pause())
            if now() < self.paused_until:
                break
            job = self.claim(now())
            if job is None:
                break
            if is_banned(job.user_id):
                outcome = Outcome("failed", "bot_ban")
            else:
                try:
                    outcome = await send(job)
                except Exception as exc:  # noqa: BLE001 — classified by type, never logged with its text
                    outcome = classify(exc)
            stop = outcome.code in ("rate_limited", "unknown_outcome")
            if outcome.code == "rate_limited":
                self._pause(now() + max(1, outcome.retry_after))
            elif outcome.code == "unknown_outcome":
                self._network_pause = min(NETWORK_PAUSE_MAX_S, max(NETWORK_PAUSE_S, self._network_pause * 2))
                self._pause(now() + self._network_pause)
                logger.warning("event=outbox_paused reason=network seconds=%s", self._network_pause)
            self.finish(job, outcome, now())
            counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
            if stop:
                break
            if outcome.kind == "sent":
                self._network_pause = 0
        if counts:
            logger.info("event=outbox_run %s", " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return counts

    async def run_deletes(self, delete: Callable[[int, int], Awaitable[None]], now: Callable[[], int]) -> dict[str, int]:
        """Auto-delete (the admin's "delete after"): sent messages whose time has come are deleted in the user's chat,
        AT MOST ONCE — a row is claimed (`deleting`) before Telegram is called, so neither a second loop nor a restart
        after a crash tries it again. Telegram lets a bot delete its own message only within 48 h: one older than
        DELETE_LIMIT_S at the moment of the call is `too_old`, never tried; a sent job without a message id (sent by an
        older bot) is `no_message_id`. Outcomes: deleted / failed. Texts are never logged."""
        counts: dict[str, int] = {}
        self.paused_until = max(self.paused_until, self._read_pause())
        if now() < self.paused_until:
            return counts  # a flood limit or an outage pauses deletes too (CRM audit P1: they were lost for good)
        with self._connect() as conn:
            # A claim left by a crash mid-call: tried again (deleting a deleted message only fails, harmlessly).
            conn.execute("UPDATE jobs SET delete_status = NULL WHERE delete_status = 'deleting' AND deleted_at <= ?",
                         (now() - STUCK_S,))
            conn.execute("UPDATE jobs SET delete_status = 'no_message_id', deleted_at = ? WHERE status = 'sent' "
                         "AND delete_after_s IS NOT NULL AND delete_status IS NULL AND message_id IS NULL", (now(),))
            rows = conn.execute(
                "SELECT id, user_id, message_id, sent_at FROM jobs WHERE status = 'sent' AND delete_after_s IS NOT NULL "
                "AND delete_status IS NULL AND message_id IS NOT NULL AND sent_at + delete_after_s <= ? "
                "ORDER BY sent_at LIMIT ?", (now(), BATCH)).fetchall()
        for job_id, user_id, message_id, sent_at in rows:
            t = now()
            state = "too_old" if t - sent_at > DELETE_LIMIT_S else "deleting"
            with self._connect() as conn:
                claimed = conn.execute("UPDATE jobs SET delete_status = ?, deleted_at = ? WHERE id = ? "
                                       "AND delete_status IS NULL", (state, t, job_id)).rowcount
            if not claimed or state == "too_old":
                if claimed:
                    counts[state] = counts.get(state, 0) + 1
                continue
            transient = None
            try:
                await delete(user_id, message_id)
                state = "deleted"
            except Exception as exc:  # noqa: BLE001 — classified by type, never logged with its text
                transient = classify(exc)
                if transient.code in ("rate_limited", "unknown_outcome"):
                    state = None  # Telegram's limit or the network: tried again after the pause (before 48 h ends)
                else:
                    transient, state = None, "failed"  # the user deleted the chat, the message is gone, …
                logger.info("event=outbox_delete_failed type=%s", type(exc).__name__)
            with self._connect() as conn:
                conn.execute("UPDATE jobs SET delete_status = ?, deleted_at = ? WHERE id = ? AND delete_status = 'deleting'",
                             (state, now(), job_id))
            counts[state or "retry"] = counts.get(state or "retry", 0) + 1
            if transient is not None:
                if transient.code == "rate_limited":
                    self._pause(now() + max(1, transient.retry_after))
                else:
                    self._network_pause = min(NETWORK_PAUSE_MAX_S, max(NETWORK_PAUSE_S, self._network_pause * 2))
                    self._pause(now() + self._network_pause)
                break
        if counts:
            logger.info("event=outbox_deletes %s", " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return counts

    def purge(self, now: int) -> None:
        finished = "('sent', 'failed', 'expired', 'cancelled')"
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET text = '', buttons_json = '[]' WHERE status IN {finished} "
                         "AND created_at < ? AND text != ''", (now - TEXT_RETENTION_DAYS * DAY,))
            conn.execute(f"DELETE FROM jobs WHERE status IN {finished} AND created_at < ?",
                         (now - ROW_RETENTION_DAYS * DAY,))
