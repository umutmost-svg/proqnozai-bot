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
# Same statement as the bot side (/bot_crm.py): older than the retention, or than the user's notice stated.
PURGE_SQL = ("DELETE FROM inquiries WHERE at < ? OR at < ? - COALESCE((SELECT n.days FROM inquiry_notices n "
             "WHERE n.user_id = inquiries.user_id), ?) * 86400")

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
-- `days`: the retention stated in the notice the user received; their messages are never kept longer.
CREATE TABLE IF NOT EXISTS inquiry_notices (
    user_id INTEGER PRIMARY KEY,
    at      INTEGER NOT NULL,
    days    INTEGER NOT NULL
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



def secure_file(path: Path) -> None:
    """The file holds users' messages: exactly 0660 (owner + the other process's group), never readable by other
    local users. Tightened BEFORE it is opened for writing; fails closed when it cannot be made private."""
    if path.exists() and not path.is_file():
        raise IsADirectoryError(f"{path.name} is not a file")
    if path.exists():
        if os.stat(path).st_mode & 0o777 != 0o660:
            try:
                os.chmod(path, 0o660)
            except PermissionError:  # owned by the other process: only its owner can change the mode
                pass
    else:
        try:
            os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o660))
            os.chmod(path, 0o660)  # the umask may have dropped group write
        except FileExistsError:  # the other process created it at the same moment: checked below
            pass
    if os.stat(path).st_mode & 0o007:
        raise PermissionError(f"{path.name} is readable by other local users")

class BotCrmFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Read-write connection: the file's mode is checked (and tightened) before EVERY open."""
        secure_file(self.path)
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def exists(self) -> bool:
        """Anything at the path (a directory there is an error to report, not "nothing collected")."""
        return self.path.exists()

    def init_schema(self) -> None:
        """Only for writes (retention setting): readers never create the file."""
        secure_file(self.path)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def _read(self, sql: str, args: tuple = (), strict: bool = False) -> list[tuple]:
        """Read-only connection (`mode=ro`): a read never creates or changes the file. A missing file or table is
        "nothing collected" (empty). Any other failure is empty for display reads, raised for `strict` ones
        (inquiry texts: an unreadable history must never look like an empty one)."""
        if not self.exists():
            return []
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
            try:
                conn.execute("PRAGMA busy_timeout=1000")
                return conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or not strict:
                return []
            raise
        except sqlite3.Error:
            if not strict:
                return []
            raise

    # ---- referral starts --------------------------------------------------------------------

    def starts(self, user_ids: Iterable[int] | None = None, strict: bool = False) -> dict[int, dict]:
        """`strict`: an unreadable file raises (a segment must never read it as "no referral")."""
        if user_ids is None:
            rows = self._read("SELECT user_id, code, at FROM referral_starts", strict=strict)
        else:
            ids = sorted(set(user_ids))
            rows = []
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i:i + _CHUNK]
                rows += self._read(f"SELECT user_id, code, at FROM referral_starts WHERE user_id IN "
                                   f"({','.join('?' * len(chunk))})", tuple(chunk), strict=strict)
        return {int(u): {"code": c, "at": int(a)} for u, c, a in rows}

    # ---- inquiries --------------------------------------------------------------------------

    def has_table(self, name: str) -> bool:
        """Strict: an unreadable file raises; a missing file or table is False."""
        return bool(self._read("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,), strict=True))

    def collected(self) -> bool:
        return bool(self._read("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inquiries'"))

    def threads(self) -> list[dict]:
        """Every user with retained inquiries (bounded by the retention), newest activity first: count, time and id
        of the latest message. No text: texts are read only by opening a thread (journalled)."""
        rows = self._read("SELECT user_id, COUNT(*), MAX(at), MAX(id) FROM inquiries GROUP BY user_id "
                          "ORDER BY MAX(at) DESC", strict=True)
        return [{"userId": int(u), "count": int(n), "lastAt": int(a), "lastId": int(i)} for u, n, a, i in rows]

    def messages(self, user_id: int, limit: int = 200) -> list[dict]:
        rows = self._read("SELECT id, at, text FROM inquiries WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                          (user_id, limit), strict=True)
        return [{"id": int(i), "at": int(a), "text": t} for i, a, t in rows]

    def notice_at(self, user_id: int) -> int | None:
        rows = self._read("SELECT at FROM inquiry_notices WHERE user_id = ?", (user_id,))
        return int(rows[0][0]) if rows else None

    def retention_days(self) -> int:
        rows = self._read("SELECT value FROM config WHERE key = 'retention_days'")
        return clamp_retention(rows[0][0]) if rows else DEFAULT_RETENTION_DAYS

    def apply_retention(self, days: int, now: int) -> int:
        """Writes the setting (read by the bot too) and purges by it in one transaction; raises on any failure so
        the caller can report it (the admin.db setting stays the source and is applied again later)."""
        self.init_schema()
        days = clamp_retention(days)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT INTO config (key, value) VALUES ('retention_days', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(days),))
                removed = conn.execute(PURGE_SQL, (now - days * DAY, now, days)).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return removed

    def purge(self, now: int) -> int:
        """Deletes inquiries past the retention (admin side: runs even while the bot is down). Raises on failure."""
        if not self.exists():
            return 0
        days = self.retention_days()
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inquiries'").fetchone() is None:
                return 0
            return conn.execute(PURGE_SQL, (now - days * DAY, now, days)).rowcount
