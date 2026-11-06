"""SQLite storage of the Mini App: its own file, never the bot's bot.db.

Only hashes of session tokens are stored; the raw token lives solely in the
client's HttpOnly cookie. Times are unix seconds supplied by the caller's
clock so tests control expiry.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from miniapp.telegram_auth import TelegramUser

Lang = Literal["az", "ru", "en"]
SUPPORTED_LANGS: tuple[Lang, ...] = ("az", "ru", "en")
DEFAULT_LANG: Lang = "ru"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    first_name       TEXT,
    last_name        TEXT,
    username         TEXT,
    language_code    TEXT,
    lang             TEXT NOT NULL,
    lang_source      TEXT NOT NULL CHECK (lang_source IN ('telegram', 'user')),
    created_at       INTEGER NOT NULL,
    last_login_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(telegram_user_id),
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    revoked_at   INTEGER,
    init_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
-- initData whose session was logged out: it cannot be exchanged again.
CREATE TABLE IF NOT EXISTS consumed_init_data (
    init_hash   TEXT PRIMARY KEY,
    expires_at  INTEGER NOT NULL
);
"""


_USER_COLUMNS = ("telegram_user_id, first_name, last_name, username, language_code, lang, lang_source, "
                 "created_at, last_login_at")
_SESSION_COLUMNS = "token_hash, user_id, created_at, expires_at, revoked_at, init_hash"
_OWN_TABLES = frozenset({"users", "sessions", "consumed_init_data", "sqlite_sequence"})


class ForeignDatabaseError(RuntimeError):
    """The configured file already holds tables that are not the Mini App's."""


SESSION_RETENTION_S = 7 * 86400


@dataclass(frozen=True)
class BurnWindow:
    """Which sessions' initData to burn on revocation, and until when.

    `fresh_since`: sessions created at or after it may hold still-exchangeable
    initData (auth_date <= created_at + skew, fresh while >= now - max_age).
    """

    fresh_since: int
    until: int

    @classmethod
    def for_policy(cls, now: int, max_age_s: int, future_skew_s: int) -> BurnWindow:
        return cls(fresh_since=now - max_age_s - future_skew_s, until=now + max_age_s + future_skew_s)


class InitDataConsumedError(Exception):
    """The initData was burned by a logout; no session may be created from it."""


def lang_from_telegram(code: str | None) -> Lang:
    base = (code or "").lower().split("-")[0]
    return base if base in SUPPORTED_LANGS else DEFAULT_LANG  # type: ignore[return-value]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class User:
    telegram_user_id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    lang: Lang
    lang_source: str


@dataclass(frozen=True)
class Session:
    user_id: int
    created_at: int
    expires_at: int
    revoked_at: int | None
    init_hash: str


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A hard link has its own name and, if the target is still empty, no foreign
        # tables: refuse any multiply linked file instead (ours is never linked).
        if self.path.exists() and os.stat(self.path).st_nlink > 1:
            raise ForeignDatabaseError("the database file has several hard links")
        with self._connect() as conn:
            # Read-only look before any write: a hard link or a copy of bot.db under
            # another name would otherwise get our tables and WAL mode added to it.
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if tables - _OWN_TABLES:
                raise ForeignDatabaseError("the database file is not a Mini App database")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def upsert_user(self, tg: TelegramUser, now: int) -> User:
        """Atomic insert-or-update keyed by the verified Telegram id.

        Profile fields follow Telegram on every login; `lang` follows Telegram's
        language_code only until the user picks a language explicitly.
        """
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_user_id, first_name, last_name, username, language_code,
                                   lang, lang_source, created_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, ?, 'telegram', ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    username = excluded.username,
                    language_code = excluded.language_code,
                    last_login_at = excluded.last_login_at,
                    lang = CASE WHEN users.lang_source = 'user' THEN users.lang ELSE excluded.lang END
                """,
                (tg.id, tg.first_name, tg.last_name, tg.username, tg.language_code,
                 lang_from_telegram(tg.language_code), now, now),
            )
        user = self.get_user(tg.id)
        assert user is not None
        return user

    def get_user(self, user_id: int) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT telegram_user_id, first_name, last_name, username, lang, lang_source "
                "FROM users WHERE telegram_user_id = ?",
                (user_id,),
            ).fetchone()
        return User(*row) if row else None

    def set_language(self, user_id: int, lang: Lang) -> None:
        if lang not in SUPPORTED_LANGS:
            raise ValueError("unsupported language")
        with self._write() as conn:
            conn.execute(
                "UPDATE users SET lang = ?, lang_source = 'user' WHERE telegram_user_id = ?", (lang, user_id)
            )

    def create_session(
        self, token: str, user_id: int, init_hash: str, now: int, ttl_s: int,
        max_active: int | None = None, burn: BurnWindow | None = None,
    ) -> tuple[Session, int]:
        """Creates a session; returns it and how many older sessions it evicted.

        `max_active`: cap of active sessions of the user including the new one;
        the oldest active ones beyond it are revoked in the same transaction and
        their still-fresh initData is burned (`burn`), like any other revocation.
        """
        if max_active is not None and burn is None:
            raise ValueError("evicting sessions requires a burn window")
        session = Session(user_id=user_id, created_at=now, expires_at=now + ttl_s, revoked_at=None,
                          init_hash=digest(init_hash))
        with self._write() as conn:
            # Checked inside the same IMMEDIATE transaction as the insert: a logout
            # that burns this initData is serialized either fully before (rejected
            # here) or fully after (it cannot see this session's token anyway).
            if conn.execute(
                "SELECT 1 FROM consumed_init_data WHERE init_hash = ? AND expires_at >= ?", (session.init_hash, now)
            ).fetchone():
                raise InitDataConsumedError
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, init_hash) VALUES (?, ?, ?, ?, ?)",
                (digest(token), user_id, session.created_at, session.expires_at, session.init_hash),
            )
            evicted = 0
            if max_active is not None and burn is not None:
                # Same transaction as the insert: two parallel logins cannot both
                # see room for one more session. rowid breaks ties within a second.
                # An evicted device is signed out like after logout: a reload with
                # its launch must not sign in again (and evict someone else).
                evicted = self._revoke_where(
                    conn,
                    "token_hash IN (SELECT token_hash FROM sessions WHERE user_id = ? AND revoked_at IS NULL"
                    " AND expires_at > ? ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                    (user_id, now, max_active), now, burn,
                )
            self._purge(conn, now)
        return session, evicted

    def get_session(self, token: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, created_at, expires_at, revoked_at, init_hash FROM sessions WHERE token_hash = ?",
                (digest(token),),
            ).fetchone()
        return Session(*row) if row else None

    def revoke_session(
        self, token: str, now: int, consumed_until: int, also_consume: Iterable[str] = ()
    ) -> bool:
        """Revokes the session and burns the initData it was created from.

        `also_consume`: received `hash` values of further verified initData of the
        same user (the current launch), burned in the same transaction.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT init_hash FROM sessions WHERE token_hash = ? AND revoked_at IS NULL", (digest(token),)
            ).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE sessions SET revoked_at = ? WHERE token_hash = ?", (now, digest(token)))
            for init_hash in (row[0], *(digest(h) for h in also_consume)):
                conn.execute(
                    "INSERT INTO consumed_init_data (init_hash, expires_at) VALUES (?, ?) "
                    "ON CONFLICT(init_hash) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)",
                    (init_hash, consumed_until),
                )
            self._purge(conn, now)
        return True

    def consume_init_data(self, init_hash: str, now: int, until: int) -> None:
        """Burns a received initData `hash` (no session involved)."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO consumed_init_data (init_hash, expires_at) VALUES (?, ?) "
                "ON CONFLICT(init_hash) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)",
                (digest(init_hash), until),
            )
            # Every burn path prunes expired burns, so the table stays bounded
            # even when no new session is ever created.
            conn.execute("DELETE FROM consumed_init_data WHERE expires_at < ?", (now,))

    def is_init_data_consumed(self, init_hash: str, now: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM consumed_init_data WHERE init_hash = ? AND expires_at >= ?", (digest(init_hash), now)
            ).fetchone()
        return row is not None

    def ping(self) -> None:
        """Raises sqlite3.Error if the database cannot be read."""
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchall()

    def count_users(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def revoke_user_sessions(
        self, user_id: int, now: int, burn: BurnWindow, also_consume: Iterable[str] = ()
    ) -> int:
        """Revokes every active session of the user; returns how many.

        `also_consume`: received `hash` values of verified initData of this user
        (the current launch), burned in the same transaction.
        """
        with self._write() as conn:
            for init_hash in also_consume:
                conn.execute(
                    "INSERT INTO consumed_init_data (init_hash, expires_at) VALUES (?, ?) "
                    "ON CONFLICT(init_hash) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)",
                    (digest(init_hash), burn.until),
                )
            return self._revoke_where(conn, "user_id = ?", (user_id,), now, burn)

    def revoke_all_sessions(self, now: int, burn: BurnWindow) -> int:
        """Emergency: revokes every active session of every user."""
        with self._write() as conn:
            return self._revoke_where(conn, "1 = 1", (), now, burn)

    def purge_expired(self, now: int) -> None:
        with self._write() as conn:
            self._purge(conn, now)

    def stats(self, now: int) -> dict[str, int]:
        """Counts only: no identifiers, nothing that could be replayed."""
        with self._connect() as conn:
            def count(sql: str, *args: object) -> int:
                return int(conn.execute(sql, args).fetchone()[0])

            return {
                "users": count("SELECT COUNT(*) FROM users"),
                "active_sessions": count(
                    "SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL AND expires_at > ?", now),
                "stored_sessions": count("SELECT COUNT(*) FROM sessions"),
                "burned_launches": count("SELECT COUNT(*) FROM consumed_init_data WHERE expires_at >= ?", now),
            }

    def backup(self, destination: Path) -> None:
        """Consistent online copy (SQLite backup API) into a new file."""
        if destination.exists():
            raise FileExistsError("backup destination already exists")
        if destination.expanduser().resolve() == self.path.expanduser().resolve():
            raise ValueError("backup destination is the database itself")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as src:
            dst = sqlite3.connect(destination)
            try:
                src.backup(dst)
            finally:
                dst.close()

    def restore_from(self, source: Path, now: int, burn: BurnWindow) -> int:
        """Replaces users and sessions with a backup in ONE transaction; returns
        how many restored sessions were revoked.

        Atomic for the API: it sees either the old data or the restored data with
        every session already revoked (a backup may hold sessions that were ended
        after it was taken). The burn ledger is merged, never replaced, so a launch
        burned after the backup stays burned. The source is opened read-only and
        must be a Mini App database.
        """
        if source.expanduser().resolve() == self.path.expanduser().resolve():
            raise ValueError("restore source is the database itself")
        source_uri = f"{source.expanduser().resolve().as_uri()}?mode=ro"
        src = sqlite3.connect(source_uri, uri=True)
        try:
            tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            src.close()
        if not {"users", "sessions", "consumed_init_data"} <= tables or tables - _OWN_TABLES:
            raise ForeignDatabaseError("the restore source is not a Mini App database")
        with self._connect() as conn:
            conn.execute("ATTACH DATABASE ? AS src", (source_uri,))
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Live sessions (some created after the backup) end like any
                    # revocation: their fresh launches are burned before the rows go.
                    self._revoke_where(conn, "1 = 1", (), now, burn)
                    conn.execute("DELETE FROM sessions")
                    conn.execute("DELETE FROM users")
                    conn.execute(f"INSERT INTO users ({_USER_COLUMNS}) SELECT {_USER_COLUMNS} FROM src.users")
                    conn.execute(
                        f"INSERT INTO sessions ({_SESSION_COLUMNS}) SELECT {_SESSION_COLUMNS} FROM src.sessions")
                    conn.execute(
                        "INSERT INTO consumed_init_data (init_hash, expires_at) "
                        "SELECT init_hash, expires_at FROM src.consumed_init_data WHERE true "
                        "ON CONFLICT(init_hash) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)"
                    )
                    revoked = self._revoke_where(conn, "1 = 1", (), now, burn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.execute("DETACH DATABASE src")
        return revoked

    @staticmethod
    def _revoke_where(conn: sqlite3.Connection, where: str, args: tuple[object, ...], now: int,
                      burn: BurnWindow) -> int:
        """Revokes the active sessions matching `where`; returns how many.

        Every matching session created inside the freshness window has its
        initData burned, whatever its state (active, expired, revoked earlier):
        a reload with that launch must not sign straight back in. `where` is a
        fixed SQL fragment chosen by this module, never user input.
        """
        conn.execute(
            "INSERT INTO consumed_init_data (init_hash, expires_at) "
            f"SELECT init_hash, ? FROM sessions WHERE ({where}) AND created_at >= ? "
            "ON CONFLICT(init_hash) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)",
            (burn.until, *args, burn.fresh_since),
        )
        revoked = conn.execute(
            f"UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL AND expires_at > ? AND ({where})",
            (now, now, *args),
        ).rowcount
        Store._purge(conn, now)
        return revoked

    @staticmethod
    def _purge(conn: sqlite3.Connection, now: int) -> None:
        # Ended sessions are kept SESSION_RETENTION_S for incident review, then dropped,
        # so the tables stay bounded even if no one ever runs the CLI.
        cutoff = now - SESSION_RETENTION_S
        conn.execute("DELETE FROM sessions WHERE expires_at < ? OR revoked_at < ?", (cutoff, cutoff))
        conn.execute("DELETE FROM consumed_init_data WHERE expires_at < ?", (now,))
