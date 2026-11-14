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
import re
from typing import Literal

from miniapp.telegram_auth import TelegramUser

Lang = Literal["az", "ru", "en", "tr", "uz", "pt-BR", "es"]
# Same set as the frontend (frontend/src/i18n/index.ts LANGS).
SUPPORTED_LANGS: tuple[Lang, ...] = ("az", "ru", "en", "tr", "uz", "pt-BR", "es")
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
CREATE INDEX IF NOT EXISTS users_created ON users(created_at);
-- initData whose session was logged out: it cannot be exchanged again.
CREATE TABLE IF NOT EXISTS consumed_init_data (
    init_hash   TEXT PRIMARY KEY,
    expires_at  INTEGER NOT NULL
);
-- Referral code of the user's FIRST Mini App start (`startapp=ref_<code>`, MINIAPP_REFERRALS_ENABLED); written
-- once in the same transaction that creates the user, never updated. No foreign key: a restore replaces users.
CREATE TABLE IF NOT EXISTS user_attribution (
    user_id    INTEGER PRIMARY KEY,
    code       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
-- Paid AI calls per UTC day (per user and in total): survives restarts.
CREATE TABLE IF NOT EXISTS ai_usage (
    day      TEXT NOT NULL,
    subject  TEXT NOT NULL,
    used     INTEGER NOT NULL,
    PRIMARY KEY (day, subject)
);
"""


_USER_COLUMNS = ("telegram_user_id, first_name, last_name, username, language_code, lang, lang_source, "
                 "created_at, last_login_at")
_SESSION_COLUMNS = "token_hash, user_id, created_at, expires_at, revoked_at, init_hash"
_OWN_TABLES = frozenset({"users", "sessions", "consumed_init_data", "ai_usage", "user_attribution", "sqlite_sequence"})
_REFERRAL = re.compile(r"^ref_([a-z0-9]{3,24})$")


def referral_code(start_param: str | None) -> str | None:
    """`ref_<code>` (lowercase letters and digits, 3..24) -> code; anything else -> None."""
    match = _REFERRAL.match(start_param or "")
    return match.group(1) if match else None


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
    """Telegram language_code -> interface language; any Portuguese is pt-BR (as the frontend)."""
    base = re.split(r"[-_]", (code or "").lower())[0]
    if base == "pt":
        return "pt-BR"
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

    def upsert_user(self, tg: TelegramUser, now: int, referral: str | None = None) -> User:
        """Atomic insert-or-update keyed by the verified Telegram id.

        Profile fields follow Telegram on every login; `lang` follows Telegram's
        language_code only until the user picks a language explicitly. `referral` (a code
        from referral_code) is kept only when this call creates the user.
        """
        with self._write() as conn:
            existed = referral is not None and conn.execute(
                "SELECT 1 FROM users WHERE telegram_user_id = ?", (tg.id,)).fetchone() is not None
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
            if referral is not None and not existed:
                conn.execute("INSERT OR IGNORE INTO user_attribution (user_id, code, created_at) VALUES (?, ?, ?)",
                             (tg.id, referral, now))
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

    def user_created_at(self, user_id: int) -> int | None:
        """Registration time (ads activity segment "new"); read only, the schema is unchanged."""
        with self._connect() as conn:
            row = conn.execute("SELECT created_at FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
        return int(row[0]) if row else None

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

    def reserve_ai_call(self, day: str, limits: dict[str, int]) -> str | None:
        """One paid call against several daily allowances ({subject: limit}), in ONE
        transaction and for ONE day: either every allowance is charged or none is.
        Returns None when charged, else the first subject that is used up."""
        with self._write() as conn:
            conn.execute("DELETE FROM ai_usage WHERE day < ?", (day,))
            for subject, limit in limits.items():
                row = conn.execute("SELECT used FROM ai_usage WHERE day = ? AND subject = ?", (day, subject)).fetchone()
                if row is not None and row[0] >= limit:
                    return subject
            for subject in limits:
                conn.execute(
                    "INSERT INTO ai_usage (day, subject, used) VALUES (?, ?, 1) "
                    "ON CONFLICT(day, subject) DO UPDATE SET used = used + 1",
                    (day, subject),
                )
            return None

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

    # ---- operator reads (admin panel): no tokens or hashes ever leave these methods ----

    _USER_SORTS = {"id": "u.telegram_user_id", "created": "u.created_at", "login": "u.last_login_at",
                   "username": "u.username COLLATE NOCASE", "sessions": "active_sessions"}

    def list_users(self, now: int, *, query: str = "", sort: str = "created", descending: bool = True,
                   limit: int = 50, offset: int = 0, created_since: int | None = None,
                   login_since: int | None = None, login_before: int | None = None,
                   lang: str | None = None) -> tuple[int, list[dict]]:
        """A page of users with their active session count; the total matches the filters."""
        where, args = ["1 = 1"], []
        if query:
            q = query.strip().lstrip("@")
            if q.isdigit():
                where.append("(u.telegram_user_id = ? OR u.username LIKE ? ESCAPE '\\')")
                args += [int(q), _like(q)]
            else:
                where.append("(u.username LIKE ? ESCAPE '\\' OR u.first_name LIKE ? ESCAPE '\\' "
                             "OR u.last_name LIKE ? ESCAPE '\\')")
                args += [_like(q)] * 3
        if created_since is not None:
            where.append("u.created_at >= ?")
            args.append(created_since)
        if login_since is not None:
            where.append("u.last_login_at >= ?")
            args.append(login_since)
        if login_before is not None:
            where.append("u.last_login_at < ?")
            args.append(login_before)
        if lang:
            where.append("u.lang = ?")
            args.append(lang)
        order = self._USER_SORTS.get(sort, self._USER_SORTS["created"])
        direction = "DESC" if descending else "ASC"
        sql_where = " AND ".join(where)
        with self._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM users u WHERE {sql_where}", args).fetchone()[0])
            rows = conn.execute(
                "SELECT u.telegram_user_id, u.username, u.first_name, u.last_name, u.lang, u.created_at, "
                "u.last_login_at, (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.telegram_user_id "
                "AND s.revoked_at IS NULL AND s.expires_at > ?) AS active_sessions "
                f"FROM users u WHERE {sql_where} ORDER BY {order} {direction}, u.telegram_user_id {direction} "
                "LIMIT ? OFFSET ?", (now, *args, limit, offset)).fetchall()
        return total, [_user_row(r) for r in rows]

    def attributions(self) -> dict[int, dict]:
        """Referral codes of first Mini App starts (admin panel; read only). A database the Mini App has not opened
        since this table was added has none: empty, not an error."""
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_attribution'").fetchone() is None:
                return {}
            return {int(u): {"code": c, "at": int(a)} for u, c, a in conn.execute(
                "SELECT user_id, code, created_at FROM user_attribution")}

    def registrations_since(self, since: int) -> list[dict]:
        """Id and registration time of every user registered at or after `since` (cohorts; no other fields)."""
        with self._connect() as conn:
            return [{"id": int(u), "createdAt": int(c)} for u, c in conn.execute(
                "SELECT telegram_user_id, created_at FROM users WHERE created_at >= ?", (since,))]

    def users_by_ids(self, user_ids: list[int], now: int) -> list[dict]:
        """Rows like list_users for the given ids (batched IN queries; unknown ids are skipped)."""
        out: list[dict] = []
        ids = sorted(set(user_ids))
        with self._connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                out += [_user_row(r) for r in conn.execute(
                    "SELECT u.telegram_user_id, u.username, u.first_name, u.last_name, u.lang, u.created_at, "
                    "u.last_login_at, (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.telegram_user_id "
                    "AND s.revoked_at IS NULL AND s.expires_at > ?) FROM users u "
                    f"WHERE u.telegram_user_id IN ({','.join('?' * len(chunk))})", (now, *chunk))]
        return out

    def user_profile(self, user_id: int, now: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT u.telegram_user_id, u.username, u.first_name, u.last_name, u.lang, u.created_at, "
                "u.last_login_at, (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.telegram_user_id "
                "AND s.revoked_at IS NULL AND s.expires_at > ?) FROM users u WHERE u.telegram_user_id = ?",
                (now, user_id)).fetchone()
            if row is None:
                return None
            sessions = conn.execute(
                "SELECT created_at, expires_at, revoked_at FROM sessions WHERE user_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 50", (user_id,)).fetchall()
        profile = _user_row(row)
        profile["sessions"] = [{
            "createdAt": c, "expiresAt": e, "revokedAt": r,
            "state": "revoked" if r is not None else ("expired" if e <= now else "active"),
        } for c, e, r in sessions]
        return profile

    def user_counts(self, now: int, since: int) -> dict[str, int]:
        with self._connect() as conn:
            total, new, logged_in = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(created_at >= ?), 0), COALESCE(SUM(last_login_at >= ?), 0) "
                "FROM users", (since, since)).fetchone()
            active = conn.execute("SELECT COUNT(DISTINCT user_id) FROM sessions WHERE revoked_at IS NULL "
                                  "AND expires_at > ?", (now,)).fetchone()[0]
        return {"total": int(total), "new": int(new), "loggedIn": int(logged_in), "withActiveSession": int(active)}

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
        has_ai_usage = "ai_usage" in tables  # backups made before M3.2 have no AI ledger
        has_attribution = "user_attribution" in tables
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
                    conn.execute("DELETE FROM user_attribution")
                    if has_attribution:
                        conn.execute("INSERT INTO user_attribution (user_id, code, created_at) "
                                     "SELECT user_id, code, created_at FROM src.user_attribution")
                    if has_ai_usage:
                        # Merged like the burn ledger: a restore never gives back spent AI calls.
                        conn.execute(
                            "INSERT INTO ai_usage (day, subject, used) SELECT day, subject, used FROM src.ai_usage "
                            "WHERE true ON CONFLICT(day, subject) DO UPDATE SET used = MAX(used, excluded.used)"
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


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _user_row(r: tuple) -> dict:
    return {"id": r[0], "username": r[1], "firstName": r[2], "lastName": r[3], "lang": r[4], "createdAt": r[5],
            "lastLoginAt": r[6], "activeSessions": int(r[7])}
