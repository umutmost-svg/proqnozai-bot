"""admin.db: administrator accounts, sessions, the immutable action journal and settings.

Written only by the admin process. Every change and its journal event share ONE
transaction (docs/miniapp/admin-dashboard.md §4.6). Only hashes of session tokens and
passwords are stored; journal rows carry whitelisted fields only, never secrets.

Local pilot limitation: TOTP secrets are stored unencrypted in this 0600 file
(the design encrypts them with MINIAPP_ADMIN_ENC_KEY, stage D2).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from miniapp.admin_web import totp
from miniapp.credits import valid_rates

Role = Literal["owner", "admin", "analyst", "promoter"]
# promoter (owner 16.11): a promotion manager — analytics aggregates and only the referral links they created.
ROLES: tuple[Role, ...] = ("owner", "admin", "analyst", "promoter")

IDLE_S = 30 * 60
ABSOLUTE_S = 12 * 3600
PRE_AUTH_S = 5 * 60
MAX_FAILURES = 5
LOCK_S = 15 * 60
_TOUCH_EVERY_S = 30

# Settings an administrator may change (§3.3 v1 keys) with their bounds; credits are display-only.
SETTINGS: dict[str, dict[str, Any]] = {
    "ai.enabled": {"type": "bool", "default": True},
    "quick.enabled": {"type": "bool", "default": True},
    "ai.per_user_daily": {"type": "int", "default": 5, "min": 0, "max": 50},
    "ai.daily_budget": {"type": "int", "default": 300, "min": 0, "max": 5000},
    # Forecasts require and spend credits (docs/miniapp/credits.md); off until the owner switches it on.
    "credits.enabled": {"type": "bool", "default": False},
    # Partner deposits per forecast by country (credits.valid_rates): the calculator and, later, the report import.
    "credits.rates": {"type": "rates", "default": []},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role           TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'analyst', 'promoter')),
    state          TEXT NOT NULL CHECK (state IN ('active', 'invited', 'disabled')),
    password_hash  TEXT,
    totp_secret    TEXT,
    last_totp_step INTEGER NOT NULL DEFAULT 0,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    last_login_at  INTEGER
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash   TEXT PRIMARY KEY,
    admin_id     INTEGER NOT NULL REFERENCES admin_users(id),
    stage        TEXT NOT NULL CHECK (stage IN ('password', 'full')),
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    revoked_at   INTEGER
);
CREATE INDEX IF NOT EXISTS admin_sessions_admin ON admin_sessions(admin_id);
CREATE TABLE IF NOT EXISTS runtime_settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL REFERENCES admin_users(id)
);
CREATE TABLE IF NOT EXISTS admin_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              INTEGER NOT NULL,
    admin_id        INTEGER,
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    before_json     TEXT,
    after_json      TEXT,
    reason          TEXT,
    idempotency_key TEXT UNIQUE,
    request_id      TEXT,
    reason_digest   TEXT,
    kind            TEXT NOT NULL CHECK (kind IN ('done', 'requested', 'completed', 'denied', 'failed'))
);
CREATE INDEX IF NOT EXISTS admin_events_at ON admin_events(at);
-- Free-text reasons live OUTSIDE the immutable journal (an owner can erase personal text later);
-- admin_events.reason stays NULL for new events and the journal references this row by event id.
CREATE TABLE IF NOT EXISTS admin_event_reasons (
    event_id INTEGER PRIMARY KEY REFERENCES admin_events(id),
    reason   TEXT NOT NULL
);
-- One completion per request: concurrent retries cannot both record a result.
CREATE UNIQUE INDEX IF NOT EXISTS admin_events_one_completion ON admin_events(request_id) WHERE kind = 'completed';
-- Explicit per-admin permissions on top of the role (default deny), e.g. view_dialogs; granted by an owner.
CREATE TABLE IF NOT EXISTS admin_grants (
    admin_id   INTEGER NOT NULL REFERENCES admin_users(id),
    permission TEXT NOT NULL,
    granted_at INTEGER NOT NULL,
    granted_by INTEGER NOT NULL,
    PRIMARY KEY (admin_id, permission)
);
-- One-time invitation links (16.11): only the SHA-256 of the token is stored. The 2FA secret is generated when the
-- invitee first opens the link and becomes the account's secret once a code from it is confirmed.
CREATE TABLE IF NOT EXISTS admin_invites (
    token_hash  TEXT PRIMARY KEY,
    admin_id    INTEGER NOT NULL REFERENCES admin_users(id),
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER,
    revoked_at  INTEGER,
    totp_secret TEXT,
    failures    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS admin_invites_admin ON admin_invites(admin_id);
CREATE TRIGGER IF NOT EXISTS admin_events_no_update BEFORE UPDATE ON admin_events
BEGIN SELECT RAISE(ABORT, 'admin events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS admin_events_no_delete BEFORE DELETE ON admin_events
BEGIN SELECT RAISE(ABORT, 'admin events are immutable'); END;
"""


# SQL expression of an event's reason (row of admin_events / alias e); old rows may still carry the column.
REASON = ("COALESCE((SELECT r.reason FROM admin_event_reasons r WHERE r.event_id = admin_events.id), "
          "admin_events.reason)")
REASON_OF_E = "COALESCE((SELECT r.reason FROM admin_event_reasons r WHERE r.event_id = e.id), e.reason)"


class AuthError(Exception):
    """Login failed; the message is a stable code, identical for a wrong e-mail and password."""


class ActionError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Admin:
    id: int
    email: str
    role: Role
    state: str


@dataclass(frozen=True)
class AdminSession:
    admin: Admin
    token_hash: str
    expires_at: int


def reason_digest(reason: str | None) -> str | None:
    """Binds idempotent replays to the reason without keeping its text in the immutable journal (the text is
    erasable in admin_event_reasons; a replay keeps working after an erasure)."""
    return None if not reason else hashlib.sha256(("reason:" + reason).encode()).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    derived = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Same work for a missing account, so timing does not reveal which e-mails exist.
        hashlib.scrypt(password.encode(), salt=b"0" * 16, n=2**14, r=8, p=1, dklen=32)
        return False
    _, n, r, p, salt, expected = stored.split("$")
    derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    return hmac.compare_digest(derived.hex(), expected)


# Permissions that exist only as explicit grants (never implied by a role, the owner included), and the roles
# that may hold them: reading users' conversations needs both the role and the grant.
GRANTS: dict[str, tuple[str, ...]] = {"view_dialogs": ("owner", "admin")}


# Role -> permission matrix (§4.2), also served read-only to the panel (Settings -> Roles).
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "read": ROLES,
    "read_personal": ("owner", "admin"),
    "manage_users": ("owner", "admin"),
    "manage_settings": ("owner", "admin"),
    "manage_admins": ("owner",),
    # Ads (ads-architecture.md §8): owner — everything; admin — campaigns, groups and publication;
    # analyst — read and analytics only. Partners define the allowed link hosts and countries: owner only.
    "manage_ads": ("owner", "admin"),
    "publish_ads": ("owner", "admin"),
    "manage_partners": ("owner",),
    # Referral links: create and archive (owner, admin); per-link stats are aggregates (analysts too).
    "manage_referrals": ("owner", "admin", "promoter"),  # a promoter: only the links they created
    # MB IDs users enter in the Mini App: the owner approves them personally (owner 16.11, docs/miniapp/credits.md).
    "approve_mb": ("owner",),
    # Inquiries: reading them needs read_personal AND the explicit view_dialogs grant; the retention period of
    # stored inquiries is an owner decision.
    "manage_inquiry_settings": ("owner",),
}


# A promoter reaches only these API routes (default deny, checked in the request guard before any route runs):
# their session, the aggregate analytics pages and their own referral links.
PROMOTER_API: tuple[tuple[str, re.Pattern[str]], ...] = tuple((m, re.compile(p)) for m, p in (
    ("GET", r"^/api/admin/auth/me$"),
    ("POST", r"^/api/admin/auth/logout$"),
    ("GET", r"^/api/admin/(overview|analytics|value|product)$"),
    ("GET", r"^/api/admin/referrals$"),
    ("POST", r"^/api/admin/referrals$"),
    ("POST", r"^/api/admin/referrals/[0-9]+/archive$"),
))

INVITE_TTL_S = 72 * 3600
INVITE_MAX_FAILURES = 5
MIN_PASSWORD = 12


def promoter_may(method: str, path: str) -> bool:
    return any(method == m and p.match(path) for m, p in PROMOTER_API)


def can(role: str, permission: str) -> bool:
    """Server-side role check for every route (§4.2); the UI only hides what is not allowed."""
    return role in PERMISSIONS[permission]


class AdminStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        # key -> fn(value, now, version), called inside the settings transaction (another process must follow)
        self.setting_hooks: dict[str, Any] = {}

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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not self.path.exists()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            try:  # pilot databases created before the column existed
                conn.execute("ALTER TABLE admin_events ADD COLUMN reason_digest TEXT")
            except sqlite3.OperationalError:
                pass
            self._widen_roles(conn)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('csrf_key', ?)", (secrets.token_hex(32),))
        if fresh:
            os.chmod(self.path, 0o600)

    @staticmethod
    def _widen_roles(conn: sqlite3.Connection) -> None:
        """Databases created before the promoter role (16.11): SQLite cannot alter a CHECK, so admin_users is rebuilt
        once with the same columns and rows (the documented table-rebuild procedure; ids are kept, so every
        reference to admin_users(id) stays valid). Runs under an exclusive lock; a second process sees the new
        schema and skips it."""
        def old() -> bool:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'admin_users'").fetchone()
            return sql is not None and "'promoter'" not in sql[0]
        if not old():
            return
        conn.execute("PRAGMA foreign_keys=OFF")  # only outside a transaction; restored below
        try:
            conn.execute("BEGIN EXCLUSIVE")
            try:
                if old():
                    columns = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(admin_users)"))
                    table = _SCHEMA.split("CREATE TABLE IF NOT EXISTS admin_users (", 1)[1].split(");", 1)[0]
                    conn.execute(f"CREATE TABLE admin_users_new ({table})")
                    conn.execute(f"INSERT INTO admin_users_new ({columns}) SELECT {columns} FROM admin_users")
                    conn.execute("DROP TABLE admin_users")
                    conn.execute("ALTER TABLE admin_users_new RENAME TO admin_users")
                    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise sqlite3.IntegrityError("foreign key check failed after the roles migration")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    # ---- accounts -------------------------------------------------------------------------

    def create_admin(self, email: str, role: Role, password: str | None, now: int) -> tuple[Admin, str | None]:
        """CLI only (the first owner is never created through the web, §4.1). Returns the TOTP secret."""
        secret = totp.new_secret() if password else None
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO admin_users (email, role, state, password_hash, totp_secret, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (email.strip(), role, "active" if password else "invited",
                 hash_password(password) if password else None, secret, now))
            self._event(conn, now, None, "admin.create_cli", "admin", str(cur.lastrowid),
                        None, {"email": email.strip(), "role": role}, None, None, "done")
            admin = Admin(int(cur.lastrowid), email.strip(), role, "active" if password else "invited")
        return admin, secret

    def totp_secret_for_cli(self, email: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT totp_secret FROM admin_users WHERE email = ?", (email,)).fetchone()
        return row[0] if row else None

    def csrf_for(self, token: str) -> str:
        with self._connect() as conn:
            key = conn.execute("SELECT value FROM meta WHERE key = 'csrf_key'").fetchone()[0]
        return hmac.new(bytes.fromhex(key), token.encode(), hashlib.sha256).hexdigest()

    def login_password(self, email: str, password: str, now: int) -> str:
        """First factor. Returns a pre-auth token valid only for the TOTP step."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, state, password_hash, locked_until FROM admin_users WHERE email = ?",
                (email.strip(),)).fetchone()
        ok = verify_password(password, row[2] if row else None)
        if row is None or row[1] != "active":
            raise AuthError("invalid_credentials")
        admin_id, _, _, locked_until = row
        if locked_until > now:
            # Same answer as a wrong e-mail/password: a lock must not reveal that the account exists.
            raise AuthError("invalid_credentials")
        if not ok:
            self._fail(admin_id, now)
            raise AuthError("invalid_credentials")
        token = secrets.token_urlsafe(32)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO admin_sessions (token_hash, admin_id, stage, created_at, last_seen_at, expires_at) "
                "VALUES (?, ?, 'password', ?, ?, ?)", (_digest(token), admin_id, now, now, now + PRE_AUTH_S))
        return token

    def login_totp(self, pre_token: str, code: str, now: int) -> str:
        """Second factor: replaces the pre-auth token by a full session token."""
        with self._write() as conn:
            row = conn.execute(
                "SELECT s.admin_id, u.totp_secret, u.last_totp_step, u.locked_until, u.state "
                "FROM admin_sessions s JOIN admin_users u ON u.id = s.admin_id "
                "WHERE s.token_hash = ? AND s.stage = 'password' AND s.revoked_at IS NULL AND s.expires_at > ?",
                (_digest(pre_token), now)).fetchone()
            if row is None:
                raise AuthError("expired")
            admin_id, secret, last_step, locked_until, state = row
            if state != "active" or locked_until > now:
                raise AuthError("locked")
            step = totp.matching_step(secret, code, now) if secret else None
            if step is None or step <= last_step:
                # The failure is COMMITTED (raising here would roll the counter back with the transaction).
                if self._fail_in(conn, admin_id, now):
                    conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE token_hash = ?",
                                 (now, _digest(pre_token)))
                    failure = "locked"
                else:
                    failure = "invalid_code"
                token = None
            else:
                failure = None
                token = self._open_session(conn, pre_token, admin_id, step, now)
        if failure is not None:
            raise AuthError(failure)
        assert token is not None
        return token

    def _open_session(self, conn: sqlite3.Connection, pre_token: str, admin_id: int, step: int, now: int) -> str:
        conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE token_hash = ?", (now, _digest(pre_token)))
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO admin_sessions (token_hash, admin_id, stage, created_at, last_seen_at, expires_at) "
            "VALUES (?, ?, 'full', ?, ?, ?)", (_digest(token), admin_id, now, now, now + ABSOLUTE_S))
        conn.execute("UPDATE admin_users SET last_totp_step = ?, failed_logins = 0, last_login_at = ? "
                     "WHERE id = ?", (step, now, admin_id))
        self._event(conn, now, admin_id, "auth.login", "admin", str(admin_id), None, None, None, None, "done")
        return token

    def _fail(self, admin_id: int, now: int) -> None:
        with self._write() as conn:
            self._fail_in(conn, admin_id, now)

    def _fail_in(self, conn: sqlite3.Connection, admin_id: int, now: int) -> bool:
        """Counts a failure; True when the account got locked (the counter survives restarts)."""
        failures = conn.execute("UPDATE admin_users SET failed_logins = failed_logins + 1 WHERE id = ? "
                                "RETURNING failed_logins", (admin_id,)).fetchone()[0]
        if failures >= MAX_FAILURES:
            conn.execute("UPDATE admin_users SET locked_until = ?, failed_logins = 0 WHERE id = ?",
                         (now + LOCK_S, admin_id))
            self._event(conn, now, admin_id, "auth.locked", "admin", str(admin_id), None, None, None, None, "done")
            return True
        return False

    def session(self, token: str, now: int) -> AdminSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.role, u.state, s.last_seen_at, s.expires_at FROM admin_sessions s "
                "JOIN admin_users u ON u.id = s.admin_id WHERE s.token_hash = ? AND s.stage = 'full' "
                "AND s.revoked_at IS NULL", (_digest(token),)).fetchone()
            if row is None:
                return None
            admin_id, email, role, state, last_seen, expires = row
            if state != "active" or now >= expires or now - last_seen >= IDLE_S:
                return None
            if now - last_seen >= _TOUCH_EVERY_S:
                conn.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, _digest(token)))
        return AdminSession(Admin(admin_id, email, role, state), _digest(token), min(expires, now + IDLE_S))

    def logout(self, token: str, now: int) -> None:
        with self._write() as conn:
            conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                         (now, _digest(token)))

    def step_up(self, conn: sqlite3.Connection, admin_id: int, code: str, now: int, permission: str) -> None:
        """Repeated TOTP for a dangerous action, inside the action's transaction (a code works once).

        The actor's state and role are re-read HERE, in the write transaction: a request that passed the
        route check and was then disabled or demoted (e.g. while its body was arriving) cannot finish."""
        self._require_actor(conn, admin_id, permission)
        secret, last_step, locked_until = conn.execute(
            "SELECT totp_secret, last_totp_step, locked_until FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
        if locked_until > now:
            raise ActionError("locked", 423)
        step = totp.matching_step(secret, code, now) if secret else None
        if step is None or step <= last_step:
            raise ActionError("invalid_code", 403)
        conn.execute("UPDATE admin_users SET last_totp_step = ? WHERE id = ?", (step, admin_id))

    @staticmethod
    def _require_actor(conn: sqlite3.Connection, admin_id: int, permission: str) -> None:
        state, role = conn.execute("SELECT state, role FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
        if state != "active" or not can(role, permission):
            raise ActionError("forbidden", 403)

    def step_up_failed(self, admin_id: int, now: int) -> None:
        """A wrong step-up code counts toward the lockout (outside the rolled-back action)."""
        with self._write() as conn:
            if self._fail_in(conn, admin_id, now):
                conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE admin_id = ? AND revoked_at IS NULL",
                             (now, admin_id))

    # ---- journal --------------------------------------------------------------------------

    @staticmethod
    def _event(conn: sqlite3.Connection, now: int, admin_id: int | None, action: str, target_type: str | None,
               target_id: str | None, before: dict | None, after: dict | None, reason: str | None,
               idempotency_key: str | None, kind: str, request_id: str | None = None) -> int:
        cur = conn.execute(
            "INSERT INTO admin_events (at, admin_id, action, target_type, target_id, before_json, after_json, "
            "reason, idempotency_key, request_id, reason_digest, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, admin_id, action, target_type, target_id,
             json.dumps(before, sort_keys=True) if before is not None else None,
             json.dumps(after, sort_keys=True) if after is not None else None,
             None, idempotency_key, request_id, reason_digest(reason), kind))
        if reason:
            conn.execute("INSERT INTO admin_event_reasons (event_id, reason) VALUES (?, ?)", (cur.lastrowid, reason))
        return int(cur.lastrowid)

    def events(self, *, since: int | None = None, admin_id: int | None = None, action: str | None = None,
               kind: str | None = None, target: tuple[str, str] | None = None, limit: int = 200) -> list[dict]:
        where, args = ["1 = 1"], []
        if since is not None:
            where.append("e.at >= ?")
            args.append(since)
        if admin_id is not None:
            where.append("e.admin_id = ?")
            args.append(admin_id)
        if action:
            where.append("e.action LIKE ?")
            args.append(f"{action}%")
        if kind:
            where.append("e.kind = ?")
            args.append(kind)
        if target:
            where.append("e.target_type = ? AND e.target_id = ?")
            args.extend(target)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.id, e.at, e.admin_id, u.email, e.action, e.target_type, e.target_id, e.before_json, "
                "e.after_json, " + REASON_OF_E + ", e.kind FROM admin_events e LEFT JOIN admin_users u ON u.id = e.admin_id "
                f"WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r[0], "at": r[1], "adminId": r[2], "adminEmail": r[3], "action": r[4], "targetType": r[5],
                 "targetId": r[6], "before": json.loads(r[7]) if r[7] else None,
                 "after": json.loads(r[8]) if r[8] else None, "reason": r[9], "result": r[10]} for r in rows]

    def _replayed(self, conn: sqlite3.Connection, key: str, admin_id: int, action: str, target_id: str | None,
                  after: dict | None = None, reason: str | None = None) -> dict | None:
        """The earlier result of the same request, or None. A key is bound to its admin, action, target and
        (when given) result: reusing it for anything else is a conflict, never a silent success."""
        row = conn.execute("SELECT id, admin_id, action, target_id, after_json, reason_digest FROM admin_events "
                           "WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            return None
        event_id, prior_admin, prior_action, prior_target, prior_after, prior_reason = row
        stored = json.loads(prior_after) if prior_after else None
        if (prior_admin, prior_action, prior_target) != (admin_id, action, target_id) or (
                after is not None and stored != after) or (reason is not None and prior_reason != reason_digest(reason)):
            raise ActionError("idempotency_key_reused", 409)
        return {"eventId": event_id, "after": stored, "replayed": True}

    # ---- grants (explicit permissions, default deny) ------------------------------------------

    def has_grant(self, admin_id: int, permission: str) -> bool:
        """True only with an explicit grant AND a role that may hold it, both read now (a demotion ends it)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT u.role, u.state FROM admin_grants g JOIN admin_users u ON u.id = g.admin_id "
                "WHERE g.admin_id = ? AND g.permission = ?", (admin_id, permission)).fetchone()
        return row is not None and row[1] == "active" and row[0] in GRANTS.get(permission, ())

    def grants_of(self, admin_id: int) -> list[str]:
        return [p for p in GRANTS if self.has_grant(admin_id, p)]

    def set_grant(self, actor: Admin, target_id: int, permission: str, granted: bool, reason: str, key: str,
                  code: str, now: int) -> dict:
        """Owner only, step-up + reason + idempotency; journalled with the permission and the new state."""
        if permission not in GRANTS or not isinstance(granted, bool):
            raise ActionError("invalid_grant")
        after = {"permission": permission, "granted": granted}
        with self._write() as conn:
            if (prior := self._replayed(conn, key, actor.id, "admin.grant", str(target_id), after, reason)) is not None:
                return prior
            self.step_up(conn, actor.id, code, now, "manage_admins")
            row = conn.execute("SELECT role FROM admin_users WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise ActionError("not_found", 404)
            if granted and row[0] not in GRANTS[permission]:
                raise ActionError("role_cannot_hold_grant", 409)
            had = conn.execute("SELECT 1 FROM admin_grants WHERE admin_id = ? AND permission = ?",
                               (target_id, permission)).fetchone() is not None
            if granted:
                conn.execute("INSERT OR IGNORE INTO admin_grants (admin_id, permission, granted_at, granted_by) "
                             "VALUES (?, ?, ?, ?)", (target_id, permission, now, actor.id))
            else:
                conn.execute("DELETE FROM admin_grants WHERE admin_id = ? AND permission = ?", (target_id, permission))
            event = self._event(conn, now, actor.id, "admin.grant", "admin", str(target_id),
                                {"permission": permission, "granted": had}, after, reason, key, "done")
        return {"eventId": event, "after": after, "replayed": False}

    # ---- admins ---------------------------------------------------------------------------

    def admins(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, email, role, state, totp_secret IS NOT NULL, last_login_at, created_at "
                                "FROM admin_users ORDER BY id").fetchall()
        return [{"id": r[0], "email": r[1], "role": r[2], "state": r[3], "twoFactor": bool(r[4]),
                 "lastLoginAt": r[5], "createdAt": r[6], "grants": self.grants_of(r[0])} for r in rows]

    def invite_admin(self, actor: Admin, email: str, role: str, reason: str, key: str, code: str, now: int) -> dict:
        if role not in ROLES:
            raise ActionError("invalid_role")
        email = email.strip()
        if "@" not in email or len(email) > 200:
            raise ActionError("invalid_email")
        with self._write() as conn:
            prior_row = conn.execute("SELECT target_id FROM admin_events WHERE idempotency_key = ?", (key,)).fetchone()
            if prior_row is not None and (prior := self._replayed(
                    conn, key, actor.id, "admin.invite", prior_row[0],
                    {"email": email, "role": role, "state": "invited"}, reason)) is not None:
                return prior
            self.step_up(conn, actor.id, code, now, "manage_admins")
            if conn.execute("SELECT 1 FROM admin_users WHERE email = ?", (email,)).fetchone():
                raise ActionError("email_taken", 409)
            cur = conn.execute("INSERT INTO admin_users (email, role, state, created_at) VALUES (?, ?, 'invited', ?)",
                               (email, role, now))
            after = {"email": email, "role": role, "state": "invited"}
            event = self._event(conn, now, actor.id, "admin.invite", "admin", str(cur.lastrowid), None, after,
                                reason, key, "done")
            token = self._new_invite(conn, int(cur.lastrowid), now)
        # The raw token is returned once, never journalled; a replay gets no token (issue a new link instead).
        return {"eventId": event, "after": after, "replayed": False, "inviteToken": token}

    # ---- invitation links -------------------------------------------------------------------

    @staticmethod
    def _new_invite(conn: sqlite3.Connection, admin_id: int, now: int) -> str:
        """One live link per account: issuing a new one revokes the earlier ones."""
        conn.execute("UPDATE admin_invites SET revoked_at = ? WHERE admin_id = ? AND used_at IS NULL "
                     "AND revoked_at IS NULL", (now, admin_id))
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO admin_invites (token_hash, admin_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                     (_digest(token), admin_id, now, now + INVITE_TTL_S))
        return token

    def reissue_invite(self, actor: Admin, target_id: int, reason: str, key: str, code: str, now: int) -> dict:
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM admin_events WHERE idempotency_key = ?", (key,)).fetchone():
                raise ActionError("idempotency_key_reused", 409)  # a token cannot be replayed: ask for a new link
            self.step_up(conn, actor.id, code, now, "manage_admins")
            row = conn.execute("SELECT state FROM admin_users WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise ActionError("not_found", 404)
            if row[0] != "invited":
                raise ActionError("not_invited", 409)
            token = self._new_invite(conn, target_id, now)
            event = self._event(conn, now, actor.id, "admin.invite_link", "admin", str(target_id), None, None,
                                reason, key, "done")
        return {"eventId": event, "inviteToken": token}

    @staticmethod
    def _live_invite(conn: sqlite3.Connection, token: str, now: int) -> tuple[str, int, str, str, str | None, int]:
        row = conn.execute(
            "SELECT i.token_hash, u.id, u.email, u.role, i.totp_secret, i.failures FROM admin_invites i "
            "JOIN admin_users u ON u.id = i.admin_id WHERE i.token_hash = ? AND i.used_at IS NULL "
            "AND i.revoked_at IS NULL AND i.expires_at > ? AND u.state = 'invited'",
            (_digest(token), now)).fetchone()
        if row is None or row[5] >= INVITE_MAX_FAILURES:
            raise AuthError("invite_invalid")  # unknown, used, expired, revoked or too many wrong codes: one answer
        return row

    def invite_info(self, token: str, now: int) -> dict:
        """What the invitee sees: their e-mail and role, and the 2FA secret to add to the authenticator app (created
        on the first visit, the same on a reload)."""
        with self._write() as conn:
            token_hash, _, email, role, secret, _ = self._live_invite(conn, token, now)
            if secret is None:
                secret = totp.new_secret()
                conn.execute("UPDATE admin_invites SET totp_secret = ? WHERE token_hash = ?", (secret, token_hash))
        return {"email": email, "role": role, "secret": secret, "otpauth": totp.provisioning_uri(secret, email)}

    def accept_invite(self, token: str, password: str, code: str, now: int) -> None:
        """Sets the password and 2FA of an invited account and activates it; the link is spent."""
        if len(password) < MIN_PASSWORD or len(password) > 200:
            raise AuthError("weak_password")
        with self._write() as conn:
            token_hash, admin_id, email, role, secret, _ = self._live_invite(conn, token, now)
            step = totp.matching_step(secret, code, now) if secret else None
            if step is None:
                conn.execute("UPDATE admin_invites SET failures = failures + 1 WHERE token_hash = ?", (token_hash,))
                failure = "invalid_code"
            else:
                failure = None
                conn.execute("UPDATE admin_users SET password_hash = ?, totp_secret = ?, last_totp_step = ?, "
                             "state = 'active', failed_logins = 0 WHERE id = ? AND state = 'invited'",
                             (hash_password(password), secret, step, admin_id))
                conn.execute("UPDATE admin_invites SET used_at = ? WHERE token_hash = ?", (now, token_hash))
                self._event(conn, now, admin_id, "admin.activate", "admin", str(admin_id), None,
                            {"email": email, "role": role, "state": "active"}, None, None, "done")
        if failure is not None:  # raised after the commit: the failure counter must persist
            raise AuthError(failure)

    def update_admin(self, actor: Admin, target_id: int, role: str | None, state: str | None, reason: str, key: str,
                     code: str, now: int) -> dict:
        if role is not None and role not in ROLES:
            raise ActionError("invalid_role")
        if state is not None and state not in ("active", "disabled"):
            raise ActionError("invalid_state")
        with self._write() as conn:
            if (prior := self._replayed(conn, key, actor.id, "admin.update", str(target_id), None, reason)) is not None:
                if (prior["after"] or {}).get("requested") != {"role": role, "state": state}:
                    raise ActionError("idempotency_key_reused", 409)
                return prior
            self.step_up(conn, actor.id, code, now, "manage_admins")
            row = conn.execute("SELECT role, state, password_hash IS NOT NULL FROM admin_users WHERE id = ?",
                               (target_id,)).fetchone()
            if row is None:
                raise ActionError("not_found", 404)
            before = {"role": row[0], "state": row[1]}
            after = {"role": role or row[0], "state": state or row[1]}
            if state == "active" and not row[2]:
                raise ActionError("invitation_pending", 409)
            # The last active owner can be neither demoted nor disabled.
            losing_owner = before["role"] == "owner" and before["state"] == "active" and (
                after["role"] != "owner" or after["state"] != "active")
            if losing_owner and conn.execute(
                    "SELECT COUNT(*) FROM admin_users WHERE role = 'owner' AND state = 'active'").fetchone()[0] <= 1:
                raise ActionError("last_owner", 409)
            conn.execute("UPDATE admin_users SET role = ?, state = ? WHERE id = ?",
                         (after["role"], after["state"], target_id))
            if after["state"] != "active" or after["role"] != before["role"]:
                conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE admin_id = ? AND revoked_at IS NULL",
                             (now, target_id))
            after = {**after, "requested": {"role": role, "state": state}}  # binds the idempotency key to the input
            event = self._event(conn, now, actor.id, "admin.update", "admin", str(target_id), before, after, reason,
                                key, "done")
        return {"eventId": event, "after": after, "replayed": False}

    # ---- settings -------------------------------------------------------------------------

    def settings(self) -> dict[str, dict]:
        with self._connect() as conn:
            stored = {r[0]: (json.loads(r[1]), r[2], r[3]) for r in conn.execute(
                "SELECT key, value_json, version, updated_at FROM runtime_settings")}
        out = {}
        for key, spec in SETTINGS.items():
            value, version, updated = stored.get(key, (spec["default"], None, None))
            out[key] = {"value": value, "version": version, "updatedAt": updated, **{
                k: v for k, v in spec.items() if k != "default"}}
        return out

    def set_setting(self, actor: Admin, key: str, value: Any, reason: str, idem: str, code: str, now: int) -> dict:
        spec = SETTINGS.get(key)
        if spec is None:
            raise ActionError("unknown_setting", 404)
        if spec["type"] == "bool" and not isinstance(value, bool):
            raise ActionError("invalid_value")
        if spec["type"] == "int" and (isinstance(value, bool) or not isinstance(value, int)
                                      or not spec["min"] <= value <= spec["max"]):
            raise ActionError("invalid_value")
        if spec["type"] == "rates":
            value = valid_rates(value)
            if value is None:
                raise ActionError("invalid_rates")
        with self._write() as conn:
            if (prior := self._replayed(conn, idem, actor.id, "settings.update", key,
                                        {"key": key, "value": value}, reason)) is not None:
                return prior
            self.step_up(conn, actor.id, code, now, "manage_settings")
            row = conn.execute("SELECT value_json, version FROM runtime_settings WHERE key = ?", (key,)).fetchone()
            before_value = json.loads(row[0]) if row else spec["default"]
            version = max(now * 1000, (row[1] + 1) if row else 0)
            conn.execute(
                "INSERT INTO runtime_settings (key, value_json, version, updated_at, admin_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, version = excluded.version, "
                "updated_at = excluded.updated_at, admin_id = excluded.admin_id",
                (key, json.dumps(value), version, now, actor.id))
            if (hook := self.setting_hooks.get(key)) is not None:
                hook(value, now, version)  # e.g. the credits switch -> Mini App command; a failure rolls back
            after = {"key": key, "value": value}
            event = self._event(conn, now, actor.id, "settings.update", "setting", key,
                                {"key": key, "value": before_value}, after, reason, idem, "done")
        return {"eventId": event, "after": after, "replayed": False}

    # ---- user sessions (the only write into miniapp.db, §4.6) ---------------------------------

    def revoke_requested(self, actor: Admin, user_id: int, reason: str, key: str, code: str, now: int,
                         request_id: str) -> dict:
        """Step 1: step-up + `requested` event. Returns {"requestId"} for the revocation to run now.

        A retry with the same idempotency key returns the earlier result only once that request is
        `completed`; a request left unfinished (crash between the steps) is handed back to be completed.
        """
        with self._write() as conn:
            row = conn.execute("SELECT kind, request_id, target_id, admin_id, reason_digest FROM admin_events "
                               "WHERE idempotency_key = ?", (key,)).fetchone()
            if row is not None:
                kind, prior_request, target, prior_admin, prior_reason = row
                if (kind, target, prior_admin, prior_reason) != ("requested", str(user_id), actor.id, reason_digest(reason)):
                    raise ActionError("idempotency_key_reused", 409)
                done = conn.execute("SELECT id, after_json FROM admin_events WHERE kind = 'completed' "
                                    "AND request_id = ?", (prior_request,)).fetchone()
                if done is not None:
                    return {"eventId": done[0], "after": json.loads(done[1]), "replayed": True}
                self._require_actor(conn, actor.id, "manage_users")  # resuming needs the permission NOW
                return {"requestId": prior_request, "resumed": True}
            self.step_up(conn, actor.id, code, now, "manage_users")
            self._event(conn, now, actor.id, "user.sessions_revoke", "user", str(user_id), None, None, reason, key,
                        "requested", request_id)
        return {"requestId": request_id, "resumed": False}

    def revoke_completed(self, actor: Admin, user_id: int, revoked: int, now: int, request_id: str,
                         action: str = "user.sessions_revoke") -> dict:
        """Records the outcome once; a concurrent retry that finished first wins and its result is returned."""
        return self.complete_request(action, actor.id, str(user_id), revoked, now, request_id)

    def complete_request(self, action: str, admin_id: int | None, target_id: str, revoked: int | None, now: int,
                         request_id: str) -> dict:
        with self._write() as conn:
            done = conn.execute("SELECT id, after_json FROM admin_events WHERE kind = 'completed' AND request_id = ?",
                                (request_id,)).fetchone()
            if done is not None:
                return {"eventId": done[0], "after": json.loads(done[1]), "replayed": True}
            event = self._event(conn, now, admin_id, action, "user", target_id, None,
                                {"revoked": revoked}, None, None, "completed", request_id)
        return {"eventId": event, "after": {"revoked": revoked}, "replayed": False}

    def pending_requests(self) -> list[dict]:
        """`requested` events whose Mini App write never completed (crash between the steps)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.action, e.target_id, e.request_id, e.admin_id FROM admin_events e WHERE e.kind = 'requested' "
                "AND NOT EXISTS (SELECT 1 FROM admin_events c WHERE c.kind = 'completed' AND c.request_id = e.request_id)"
            ).fetchall()
        return [{"action": r[0], "targetId": r[1], "requestId": r[2], "adminId": r[3]} for r in rows]
