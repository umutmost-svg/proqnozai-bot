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


_OWN_TABLES = frozenset({"users", "sessions", "consumed_init_data", "sqlite_sequence"})


class ForeignDatabaseError(RuntimeError):
    """The configured file already holds tables that are not the Mini App's."""


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

    def create_session(self, token: str, user_id: int, init_hash: str, now: int, ttl_s: int) -> Session:
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
            # Opportunistic cleanup keeps both tables bounded.
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now - 7 * 86400,))
            conn.execute("DELETE FROM consumed_init_data WHERE expires_at < ?", (now,))
        return session

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
            conn.execute("DELETE FROM consumed_init_data WHERE expires_at < ?", (now,))
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

    def count_users(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
