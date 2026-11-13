"""Admin side of `bot-crm.db` (bot -> admin: referral starts and user inquiries; the bot side is /bot_crm.py).

The bot writes starts and inquiries; the admin reads them and owns two things only: the inquiry retention
setting (`config.retention_days`, changed through an audited action in admin.db) and the purge by that setting
(run here too, so retention holds even while the bot is down). A missing file means nothing was collected yet:
readers answer empty; the file is never created by a read.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

DAY = 86400
DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS, MAX_RETENTION_DAYS = 1, 365
_CHUNK = 500

# Same schema as /bot_crm.py (a test keeps them equal).
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


def clamp_retention(value: object) -> int:
    try:
        days = int(str(value))
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return min(MAX_RETENTION_DAYS, max(MIN_RETENTION_DAYS, days))


class BotCrmFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def exists(self) -> bool:
        return self.path.is_file()

    def init_schema(self) -> None:
        """Only for writes (retention setting): readers never create the file."""
        fresh = not self.path.exists()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        if fresh:
            os.chmod(self.path, 0o660)  # the bot writes starts and inquiries (one group on the volume)

    def _read(self, sql: str, args: tuple = ()) -> list[tuple]:
        """Read-only connection (`mode=ro`): a read never creates or changes the file."""
        if not self.exists():
            return []
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        except sqlite3.Error:
            return []
        try:
            conn.execute("PRAGMA busy_timeout=1000")
            return conn.execute(sql, args).fetchall()
        except sqlite3.Error:  # no table yet (bot older than the schema), a lock past the timeout
            return []
        finally:
            conn.close()

    # ---- referral starts --------------------------------------------------------------------

    def starts(self, user_ids: Iterable[int] | None = None) -> dict[int, dict]:
        if user_ids is None:
            rows = self._read("SELECT user_id, code, at FROM referral_starts")
        else:
            ids = sorted(set(user_ids))
            rows = []
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i:i + _CHUNK]
                rows += self._read(f"SELECT user_id, code, at FROM referral_starts WHERE user_id IN "
                                   f"({','.join('?' * len(chunk))})", tuple(chunk))
        return {int(u): {"code": c, "at": int(a)} for u, c, a in rows}
