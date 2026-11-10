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
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from miniapp.admin_web import totp

Role = Literal["owner", "admin", "analyst"]
ROLES: tuple[Role, ...] = ("owner", "admin", "analyst")

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
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role           TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'analyst')),
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
    kind            TEXT NOT NULL CHECK (kind IN ('done', 'requested', 'completed', 'denied', 'failed'))
);
CREATE INDEX IF NOT EXISTS admin_events_at ON admin_events(at);
CREATE TRIGGER IF NOT EXISTS admin_events_no_update BEFORE UPDATE ON admin_events
BEGIN SELECT RAISE(ABORT, 'admin events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS admin_events_no_delete BEFORE DELETE ON admin_events
BEGIN SELECT RAISE(ABORT, 'admin events are immutable'); END;
"""


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


def can(role: str, permission: str) -> bool:
    """Server-side role check for every route (§4.2); the UI only hides what is not allowed."""
    allowed = {
        "read": ROLES,
        "read_personal": ("owner", "admin"),
        "manage_users": ("owner", "admin"),
        "manage_settings": ("owner", "admin"),
        "manage_admins": ("owner",),
    }
    return role in allowed[permission]


class AdminStore:
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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not self.path.exists()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('csrf_key', ?)", (secrets.token_hex(32),))
        if fresh:
            os.chmod(self.path, 0o600)

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
            raise AuthError("locked")
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
                failed = self._fail_in(conn, admin_id, now)
                if failed:
                    conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE token_hash = ?",
                                 (now, _digest(pre_token)))
                    raise AuthError("locked")
                raise AuthError("invalid_code")
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

    def step_up(self, conn: sqlite3.Connection, admin_id: int, code: str, now: int) -> None:
        """Repeated TOTP for a dangerous action, inside the action's transaction (a code works once)."""
        secret, last_step, locked_until = conn.execute(
            "SELECT totp_secret, last_totp_step, locked_until FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
        if locked_until > now:
            raise ActionError("locked", 423)
        step = totp.matching_step(secret, code, now) if secret else None
        if step is None or step <= last_step:
            raise ActionError("invalid_code", 403)
        conn.execute("UPDATE admin_users SET last_totp_step = ? WHERE id = ?", (step, admin_id))

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
            "reason, idempotency_key, request_id, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, admin_id, action, target_type, target_id,
             json.dumps(before, sort_keys=True) if before is not None else None,
             json.dumps(after, sort_keys=True) if after is not None else None,
             reason, idempotency_key, request_id, kind))
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
                "e.after_json, e.reason, e.kind FROM admin_events e LEFT JOIN admin_users u ON u.id = e.admin_id "
                f"WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r[0], "at": r[1], "adminId": r[2], "adminEmail": r[3], "action": r[4], "targetType": r[5],
                 "targetId": r[6], "before": json.loads(r[7]) if r[7] else None,
                 "after": json.loads(r[8]) if r[8] else None, "reason": r[9], "result": r[10]} for r in rows]

    def _replayed(self, conn: sqlite3.Connection, key: str) -> dict | None:
        row = conn.execute("SELECT id, after_json FROM admin_events WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            return None
        return {"eventId": row[0], "after": json.loads(row[1]) if row[1] else None, "replayed": True}

    # ---- admins ---------------------------------------------------------------------------

    def admins(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, email, role, state, totp_secret IS NOT NULL, last_login_at, created_at "
                                "FROM admin_users ORDER BY id").fetchall()
        return [{"id": r[0], "email": r[1], "role": r[2], "state": r[3], "twoFactor": bool(r[4]),
                 "lastLoginAt": r[5], "createdAt": r[6]} for r in rows]

    def invite_admin(self, actor: Admin, email: str, role: str, reason: str, key: str, code: str, now: int) -> dict:
        if role not in ROLES:
            raise ActionError("invalid_role")
        email = email.strip()
        if "@" not in email or len(email) > 200:
            raise ActionError("invalid_email")
        with self._write() as conn:
            if (prior := self._replayed(conn, key)) is not None:
                return prior
            self.step_up(conn, actor.id, code, now)
            if conn.execute("SELECT 1 FROM admin_users WHERE email = ?", (email,)).fetchone():
                raise ActionError("email_taken", 409)
            cur = conn.execute("INSERT INTO admin_users (email, role, state, created_at) VALUES (?, ?, 'invited', ?)",
                               (email, role, now))
            after = {"email": email, "role": role, "state": "invited"}
            event = self._event(conn, now, actor.id, "admin.invite", "admin", str(cur.lastrowid), None, after,
                                reason, key, "done")
        return {"eventId": event, "after": after, "replayed": False}

    def update_admin(self, actor: Admin, target_id: int, role: str | None, state: str | None, reason: str, key: str,
                     code: str, now: int) -> dict:
        if role is not None and role not in ROLES:
            raise ActionError("invalid_role")
        if state is not None and state not in ("active", "disabled"):
            raise ActionError("invalid_state")
        with self._write() as conn:
            if (prior := self._replayed(conn, key)) is not None:
                return prior
            self.step_up(conn, actor.id, code, now)
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
        with self._write() as conn:
            if (prior := self._replayed(conn, idem)) is not None:
                return prior
            self.step_up(conn, actor.id, code, now)
            row = conn.execute("SELECT value_json, version FROM runtime_settings WHERE key = ?", (key,)).fetchone()
            before_value = json.loads(row[0]) if row else spec["default"]
            version = max(now * 1000, (row[1] + 1) if row else 0)
            conn.execute(
                "INSERT INTO runtime_settings (key, value_json, version, updated_at, admin_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, version = excluded.version, "
                "updated_at = excluded.updated_at, admin_id = excluded.admin_id",
                (key, json.dumps(value), version, now, actor.id))
            after = {"key": key, "value": value}
            event = self._event(conn, now, actor.id, "settings.update", "setting", key,
                                {"key": key, "value": before_value}, after, reason, idem, "done")
        return {"eventId": event, "after": after, "replayed": False}

    # ---- user sessions (the only write into miniapp.db, §4.6) ---------------------------------

    def revoke_requested(self, actor: Admin, user_id: int, reason: str, key: str, code: str, now: int,
                         request_id: str) -> dict | None:
        """Step 1: step-up + `requested` event. Returns the earlier result on an idempotent replay."""
        with self._write() as conn:
            if (prior := self._replayed(conn, key)) is not None:
                return prior
            self.step_up(conn, actor.id, code, now)
            self._event(conn, now, actor.id, "user.sessions_revoke", "user", str(user_id), None, None, reason, key,
                        "requested", request_id)
        return None

    def revoke_completed(self, actor: Admin, user_id: int, revoked: int, now: int, request_id: str) -> int:
        with self._write() as conn:
            return self._event(conn, now, actor.id, "user.sessions_revoke", "user", str(user_id), None,
                               {"revoked": revoked}, None, None, "completed", request_id)
