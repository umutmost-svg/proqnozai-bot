"""Bot side of `bot-crm.db`: what the bot reports to the admin panel (referral starts, user inquiries).

A separate SQLite file on the volume shared with the admin process (like `bot-outbox.db`; never `bot.db`, which
the admin must not open). Everything here is OFF unless its flag is on (config.BOT_REFERRALS_ENABLED,
config.BOT_INQUIRIES_ENABLED) and BOT_CRM_DB names the file; with the flags off the bot behaves exactly as
before and never creates the file.

- Referral starts: `/start ref_<code>` from a user the bot has never seen records (user, code) once; later
  starts, other payloads and existing users record nothing. Unknown codes are kept (the admin shows them).
- Inquiries: NEW inbound private messages, only while the flag is on (nothing older is ever collected). The
  first message of each user is stored only after the bot sent the storage notice (translations
  `inquiry_notice`); texts are capped, never logged; rows older than the retention set in the admin panel
  (`config.retention_days`, default 90) are purged by both sides.

Same schema as miniapp/admin_web/bot_crm.py (whichever side starts first creates it; a test keeps them equal).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from config import BOT_CRM_DB, BOT_REFERRALS_ENABLED

logger = logging.getLogger("bot_crm")

DAY = 86400
REF_PREFIX = "ref_"
MAX_INQUIRY = 2000
DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS, MAX_RETENTION_DAYS = 1, 365
PURGE_EVERY_S = 3600
_CODE = re.compile(r"^[a-z0-9]{3,24}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS referral_starts (
    user_id INTEGER PRIMARY KEY,
    code    TEXT NOT NULL,
    at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS referral_starts_code ON referral_starts(code);
CREATE TABLE IF NOT EXISTS inquiries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    at      INTEGER NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inquiries_user ON inquiries(user_id, id);
CREATE INDEX IF NOT EXISTS inquiries_at ON inquiries(at);
CREATE TABLE IF NOT EXISTS inquiry_notices (
    user_id INTEGER PRIMARY KEY,
    at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def referral_code(payload: str | None) -> str | None:
    """`ref_<code>` -> code (lowercase letters and digits, 3..24); anything else -> None."""
    if not payload or not payload.startswith(REF_PREFIX):
        return None
    code = payload[len(REF_PREFIX):]
    return code if _CODE.match(code) else None


def clamp_retention(value: object) -> int:
    try:
        days = int(str(value))
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return min(MAX_RETENTION_DAYS, max(MIN_RETENTION_DAYS, days))


class BotCrm:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._purge_at = 0

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    # ---- referrals ------------------------------------------------------------------------

    def record_start(self, user_id: int, code: str, now: int) -> bool:
        """First attribution wins; True if this start was recorded."""
        with self._connect() as conn:
            return conn.execute("INSERT OR IGNORE INTO referral_starts (user_id, code, at) VALUES (?, ?, ?)",
                                (user_id, code, now)).rowcount > 0

    # ---- inquiries ------------------------------------------------------------------------

    def notice_sent(self, user_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM inquiry_notices WHERE user_id = ?", (user_id,)).fetchone() is not None

    def mark_notice(self, user_id: int, now: int) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO inquiry_notices (user_id, at) VALUES (?, ?)", (user_id, now))

    def record_inquiry(self, user_id: int, text: str, now: int) -> bool:
        """Stores one message, only for a user who has been shown the notice. Returns True if stored."""
        text = text.strip()[:MAX_INQUIRY]
        if not text:
            return False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                noticed = conn.execute("SELECT 1 FROM inquiry_notices WHERE user_id = ?", (user_id,)).fetchone()
                if noticed is not None:
                    conn.execute("INSERT INTO inquiries (user_id, at, text) VALUES (?, ?, ?)", (user_id, now, text))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        self.maybe_purge(now)
        return noticed is not None

    def retention_days(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM config WHERE key = 'retention_days'").fetchone()
        return clamp_retention(row[0]) if row else DEFAULT_RETENTION_DAYS

    def purge(self, now: int) -> int:
        cutoff = now - self.retention_days() * DAY
        with self._connect() as conn:
            return conn.execute("DELETE FROM inquiries WHERE at < ?", (cutoff,)).rowcount

    def maybe_purge(self, now: int) -> None:
        if now < self._purge_at:
            return
        self._purge_at = now + PURGE_EVERY_S
        try:
            removed = self.purge(now)
        except sqlite3.Error as exc:
            logger.warning("event=inquiry_purge_failed type=%s", type(exc).__name__)
            return
        if removed:
            logger.info("event=inquiry_purge removed=%d", removed)


# ---- bot wiring (flags read from config; patched in tests) ---------------------------------------

_instance: BotCrm | None = None


def crm() -> BotCrm:
    """The shared file, created on first use (only ever reached with a flag on)."""
    global _instance
    if _instance is None:
        instance = BotCrm(Path(BOT_CRM_DB))
        instance.init_schema()
        _instance = instance
    return _instance


def note_start(user_id: int, args: list[str] | None, is_new: bool, now: int) -> None:
    """`/start ref_<code>` of a user the bot has never seen: recorded once. Never raises (start must answer)."""
    if not BOT_REFERRALS_ENABLED or not is_new or not args:
        return
    code = referral_code(args[0])
    if code is None:
        return
    try:
        crm().record_start(user_id, code, now)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("event=referral_start_failed type=%s", type(exc).__name__)
