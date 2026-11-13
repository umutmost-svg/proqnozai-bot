"""CRM layer of the admin panel (local pilot): tags, notes, segments, broadcasts, messages to a
user, credits and access blocks. Everything lives in admin.db, written only by the admin process;
every change and its journal event share ONE transaction (the AdminStore journal).

Local pilot limitations (docs/miniapp/admin-pilot.md):
- broadcasts and personal messages are NOT sent to Telegram: the admin process has no bot token
  by design; sending goes through a bot-side internal API in a later stage. Here they are stored
  and marked "not sent (pilot)";
- credits are bookkeeping only: the Mini App never debits and access does not depend on them;
- a block is a flag plus a session revocation; the Mini App does not enforce it yet.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from typing import Any

from miniapp.admin_web.accounts import REASON_OF_E, ActionError, Admin, AdminStore, reason_digest
from miniapp.admin_web.ads import bump_revision as bump_ads_revision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_tags (
    user_id    INTEGER NOT NULL,
    tag        TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL,
    PRIMARY KEY (user_id, tag)
);
-- Tag history with the labels (editable data); the immutable journal only references these rows by id.
CREATE TABLE IF NOT EXISTS crm_tag_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    tag        TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    created_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_tag_events_user ON crm_tag_events(user_id);
CREATE TABLE IF NOT EXISTS crm_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_notes_user ON crm_notes(user_id);
CREATE TABLE IF NOT EXISTS crm_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('not_sent_pilot', 'queued', 'sent', 'failed')),
    created_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_messages_user ON crm_messages(user_id);
CREATE TABLE IF NOT EXISTS crm_segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    rules_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_broadcasts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    rules_json   TEXT NOT NULL,
    body         TEXT NOT NULL,
    buttons_json TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('draft', 'scheduled', 'cancelled', 'sent')),
    scheduled_at INTEGER,
    recipients   INTEGER,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    admin_id     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_accounts (
    user_id         INTEGER PRIMARY KEY,
    balance         INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    unlimited_until INTEGER,
    updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('grant', 'debit', 'unlimited', 'unlimited_off')),
    delta           INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    unlimited_until INTEGER,
    reason          TEXT NOT NULL,  -- '' for operations: their reason is the event's (erasable) reason
    admin_id        INTEGER NOT NULL,
    batch_key       TEXT,
    event_id        INTEGER,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS credit_ledger_user ON credit_ledger(user_id);
CREATE TRIGGER IF NOT EXISTS credit_ledger_no_update BEFORE UPDATE ON credit_ledger
BEGIN SELECT RAISE(ABORT, 'the credit ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS credit_ledger_no_delete BEFORE DELETE ON credit_ledger
BEGIN SELECT RAISE(ABORT, 'the credit ledger is append-only'); END;
-- The user's account id at the partner bookmaker (MB), entered by an admin; one id belongs to one user.
CREATE TABLE IF NOT EXISTS crm_external_ids (
    user_id    INTEGER PRIMARY KEY,
    mb_id      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    updated_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
-- Recipients frozen when a broadcast is scheduled: the queued jobs (and any retry) use exactly this list.
CREATE TABLE IF NOT EXISTS crm_broadcast_recipients (
    broadcast_id INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    PRIMARY KEY (broadcast_id, user_id)
);
-- MB id changes with the values (editable CRM data, read_personal only); the immutable journal keeps only the fact.
CREATE TABLE IF NOT EXISTS crm_mb_id_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    old_mb_id  TEXT,
    new_mb_id  TEXT,
    created_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_mb_id_history_user ON crm_mb_id_history(user_id);
CREATE TABLE IF NOT EXISTS crm_blocks (
    user_id    INTEGER PRIMARY KEY,
    reason     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
"""

LEDGER_REASON = ("COALESCE(NULLIF(l.reason, ''), (SELECT r.reason FROM admin_event_reasons r "
                 "WHERE r.event_id = l.event_id), '')")
MAX_TAG = 32
_MB_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_TEXT = 4000
MAX_GROUP = 5000

# Segment rule fields (evaluated over Store users + admin.db tags). Anything else is rejected.
RULE_FIELDS = {
    "registered_within_days": int, "active_within_days": int, "inactive_for_days": int,
    "lang_in": list, "tag_has": str, "tag_not": str, "has_active_session": bool, "blocked": bool,
}


def validate_rules(rules: Any) -> dict:
    if not isinstance(rules, dict) or len(rules) > len(RULE_FIELDS):
        raise ActionError("invalid_rules")
    out: dict[str, Any] = {}
    for key, value in rules.items():
        kind = RULE_FIELDS.get(key)
        if kind is None:
            raise ActionError("invalid_rules")
        if kind is int and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650):
            raise ActionError("invalid_rules")
        if kind is bool and not isinstance(value, bool):
            raise ActionError("invalid_rules")
        if kind is str and (not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_TAG):
            raise ActionError("invalid_rules")
        if kind is list and (not isinstance(value, list) or not 1 <= len(value) <= 10
                             or not all(isinstance(v, str) and 1 <= len(v) <= 8 for v in value)):
            raise ActionError("invalid_rules")
        out[key] = value.strip() if isinstance(value, str) else value
    return out


def matches(user: dict, rules: dict, tags: set[str], blocked: bool, now: int) -> bool:
    day = 86400
    if "registered_within_days" in rules and user["createdAt"] < now - rules["registered_within_days"] * day:
        return False
    if "active_within_days" in rules and user["lastLoginAt"] < now - rules["active_within_days"] * day:
        return False
    if "inactive_for_days" in rules and user["lastLoginAt"] >= now - rules["inactive_for_days"] * day:
        return False
    if "lang_in" in rules and user["lang"] not in rules["lang_in"]:
        return False
    if "tag_has" in rules and rules["tag_has"] not in tags:
        return False
    if "tag_not" in rules and rules["tag_not"] in tags:
        return False
    if "has_active_session" in rules and (user["activeSessions"] > 0) != rules["has_active_session"]:
        return False
    if "blocked" in rules and blocked != rules["blocked"]:
        return False
    return True


def _text(value: Any, field: str, limit: int = MAX_TEXT, minimum: int = 1) -> str:
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= limit:
        raise ActionError(f"invalid_{field}")
    return value.strip()


class CrmStore:
    def __init__(self, accounts: AdminStore) -> None:
        self.accounts = accounts

    def init_schema(self) -> None:
        with self.accounts._connect() as conn:
            conn.executescript(_SCHEMA)
            try:  # pilot databases created before the column existed (additive)
                conn.execute("ALTER TABLE crm_messages ADD COLUMN idempotency_key TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS crm_messages_key ON crm_messages(idempotency_key) "
                         "WHERE idempotency_key IS NOT NULL")

    # ---- reads -------------------------------------------------------------------------------

    def tags_by_user(self, user_ids: Iterable[int] | None = None) -> dict[int, list[str]]:
        with self.accounts._connect() as conn:
            rows = conn.execute("SELECT user_id, tag FROM crm_tags ORDER BY created_at").fetchall()
        wanted = set(user_ids) if user_ids is not None else None
        out: dict[int, list[str]] = {}
        for uid, tag in rows:
            if wanted is None or uid in wanted:
                out.setdefault(uid, []).append(tag)
        return out

    def mb_ids(self, user_ids: Iterable[int] | None = None) -> dict[int, str]:
        with self.accounts._connect() as conn:
            rows = conn.execute("SELECT user_id, mb_id FROM crm_external_ids").fetchall()
        wanted = set(user_ids) if user_ids is not None else None
        return {uid: mb for uid, mb in rows if wanted is None or uid in wanted}

    def users_by_mb(self, fragment: str) -> set[int]:
        """Users whose MB id contains the fragment (case-insensitive), like the other search fields."""
        fragment = fragment.strip()
        if not fragment or not _MB_ID.match(fragment):
            return set()
        with self.accounts._connect() as conn:
            return {int(r[0]) for r in conn.execute(
                "SELECT user_id FROM crm_external_ids WHERE instr(lower(mb_id), lower(?)) > 0", (fragment,))}

    def mb_id_history(self, user_id: int) -> list[dict]:
        with self.accounts._connect() as conn:
            return [{"old": r[0], "new": r[1], "at": r[2], "by": r[3]} for r in conn.execute(
                "SELECT h.old_mb_id, h.new_mb_id, h.created_at, u.email FROM crm_mb_id_history h LEFT JOIN admin_users u "
                "ON u.id = h.admin_id WHERE h.user_id = ? ORDER BY h.id DESC LIMIT 50", (user_id,))]

    def block_history(self, user_id: int) -> list[dict]:
        """Blocks and unblocks of one user from the journal (reasons are erasable, read through REASON_OF_E)."""
        with self.accounts._connect() as conn:
            return [{"blocked": bool(json.loads(r[0] or "{}").get("blocked")), "at": r[1], "by": r[2], "reason": r[3]}
                    for r in conn.execute(
                        "SELECT e.after_json, e.at, u.email, " + REASON_OF_E + " FROM admin_events e LEFT JOIN admin_users u "
                        "ON u.id = e.admin_id WHERE e.action = 'user.block' AND e.kind IN ('requested', 'done') "
                        "AND e.target_type = 'user' AND e.target_id = ? ORDER BY e.id DESC LIMIT 50", (str(user_id),))]

    def tag_events(self, user_id: int) -> list[dict]:
        with self.accounts._connect() as conn:
            return [{"tag": r[0], "action": r[1], "at": r[2], "by": r[3]} for r in conn.execute(
                "SELECT e.tag, e.action, e.created_at, u.email FROM crm_tag_events e LEFT JOIN admin_users u "
                "ON u.id = e.admin_id WHERE e.user_id = ? ORDER BY e.id DESC LIMIT 100", (user_id,))]

    def all_tags(self) -> list[dict]:
        with self.accounts._connect() as conn:
            return [{"tag": t, "users": n} for t, n in conn.execute(
                "SELECT tag, COUNT(*) FROM crm_tags GROUP BY tag ORDER BY COUNT(*) DESC, tag")]

    def users_with_tag(self, tag: str) -> set[int]:
        with self.accounts._connect() as conn:
            return {r[0] for r in conn.execute("SELECT user_id FROM crm_tags WHERE tag = ?", (tag,))}

    def blocked(self) -> dict[int, dict]:
        with self.accounts._connect() as conn:
            return {r[0]: {"reason": r[1], "at": r[2]} for r in conn.execute(
                "SELECT user_id, reason, created_at FROM crm_blocks")}

    def credits_of(self, user_ids: Iterable[int] | None = None, now: int = 0) -> dict[int, dict]:
        with self.accounts._connect() as conn:
            rows = conn.execute("SELECT user_id, balance, unlimited_until FROM credit_accounts").fetchall()
        wanted = set(user_ids) if user_ids is not None else None
        return {uid: {"balance": bal, "unlimitedUntil": until if until and until > now else None}
                for uid, bal, until in rows if wanted is None or uid in wanted}

    def user_crm(self, user_id: int, now: int) -> dict:
        with self.accounts._connect() as conn:
            notes = conn.execute(
                "SELECT n.id, n.body, n.created_at, u.email FROM crm_notes n LEFT JOIN admin_users u "
                "ON u.id = n.admin_id WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 50", (user_id,)).fetchall()
            msgs = conn.execute(
                "SELECT m.id, m.body, m.status, m.created_at, u.email FROM crm_messages m LEFT JOIN admin_users u "
                "ON u.id = m.admin_id WHERE m.user_id = ? ORDER BY m.id DESC LIMIT 50", (user_id,)).fetchall()
            ledger = conn.execute(
                "SELECT l.id, l.kind, l.delta, l.balance_after, l.unlimited_until, " + LEDGER_REASON + ", l.created_at, u.email "
                "FROM credit_ledger l LEFT JOIN admin_users u ON u.id = l.admin_id WHERE l.user_id = ? "
                "ORDER BY l.id DESC LIMIT 50", (user_id,)).fetchall()
            block = conn.execute("SELECT reason, created_at FROM crm_blocks WHERE user_id = ?", (user_id,)).fetchone()
        account = self.credits_of([user_id], now).get(user_id, {"balance": 0, "unlimitedUntil": None})
        return {
            "tags": self.tags_by_user([user_id]).get(user_id, []),
            "notes": [{"id": r[0], "body": r[1], "at": r[2], "by": r[3]} for r in notes],
            "messages": [{"id": r[0], "body": r[1], "status": r[2], "at": r[3], "by": r[4]} for r in msgs],
            "ledger": [{"id": r[0], "kind": r[1], "delta": r[2], "balanceAfter": r[3], "unlimitedUntil": r[4],
                        "reason": r[5], "at": r[6], "by": r[7]} for r in ledger],
            "credits": {**account, "enabled": False},
            "blocked": {"reason": block[0], "at": block[1]} if block else None,
            "mbId": self.mb_ids([user_id]).get(user_id),
            "mbIdHistory": self.mb_id_history(user_id),
            "blockHistory": self.block_history(user_id),
        }

    def segments(self) -> list[dict]:
        with self.accounts._connect() as conn:
            return [{"id": r[0], "name": r[1], "rules": json.loads(r[2]), "updatedAt": r[3]} for r in conn.execute(
                "SELECT id, name, rules_json, updated_at FROM crm_segments ORDER BY name")]

    def broadcasts(self) -> list[dict]:
        with self.accounts._connect() as conn:
            rows = conn.execute(
                "SELECT b.id, b.name, b.rules_json, b.body, b.buttons_json, b.status, b.scheduled_at, b.recipients, "
                "b.created_at, b.updated_at, u.email FROM crm_broadcasts b LEFT JOIN admin_users u ON u.id = b.admin_id "
                "ORDER BY b.id DESC").fetchall()
        return [{"id": r[0], "name": r[1], "rules": json.loads(r[2]), "body": r[3], "buttons": json.loads(r[4]),
                 "status": r[5], "scheduledAt": r[6], "recipients": r[7], "createdAt": r[8], "updatedAt": r[9],
                 "by": r[10]} for r in rows]

    def ledger(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        where, args = "", []
        if kind in ("grant", "debit", "unlimited", "unlimited_off"):
            where, args = "WHERE l.kind = ?", [kind]
        with self.accounts._connect() as conn:
            rows = conn.execute(
                "SELECT l.id, l.user_id, l.kind, l.delta, l.balance_after, l.unlimited_until, " + LEDGER_REASON + ", l.created_at, "
                f"u.email, l.batch_key FROM credit_ledger l LEFT JOIN admin_users u ON u.id = l.admin_id {where} "
                "ORDER BY l.id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r[0], "userId": r[1], "kind": r[2], "delta": r[3], "balanceAfter": r[4], "unlimitedUntil": r[5],
                 "reason": r[6], "at": r[7], "by": r[8], "batch": r[9]} for r in rows]

    def credit_totals(self, now: int, since: int) -> dict:
        with self.accounts._connect() as conn:
            total, accounts = conn.execute("SELECT COALESCE(SUM(balance), 0), COUNT(*) FROM credit_accounts").fetchone()
            unlimited = conn.execute("SELECT COUNT(*) FROM credit_accounts WHERE unlimited_until > ?",
                                     (now,)).fetchone()[0]
            granted, debited = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0), "
                "COALESCE(-SUM(CASE WHEN delta < 0 THEN delta END), 0) FROM credit_ledger WHERE created_at >= ?",
                (since,)).fetchone()
        return {"inCirculation": total, "accounts": accounts, "unlimited": unlimited, "granted": granted,
                "debited": debited}

    # ---- writes without step-up (audited) ---------------------------------------------------

    def add_tag(self, actor: Admin, user_id: int, tag: str, now: int) -> dict:
        tag = _text(tag, "tag", MAX_TAG)
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            created = conn.execute("INSERT OR IGNORE INTO crm_tags (user_id, tag, created_at, admin_id) "
                                   "VALUES (?, ?, ?, ?)", (user_id, tag, now, actor.id)).rowcount
            if created:
                ev = conn.execute("INSERT INTO crm_tag_events (user_id, tag, action, created_at, admin_id) "
                                  "VALUES (?, ?, 'add', ?, ?)", (user_id, tag, now, actor.id)).lastrowid
                # Free-text labels never enter the immutable journal: it references the tag event row.
                self.accounts._event(conn, now, actor.id, "user.tag_add", "user", str(user_id), None,
                                     {"tagEventId": ev}, None, None, "done")
        return {"tag": tag, "created": bool(created)}

    def remove_tag(self, actor: Admin, user_id: int, tag: str, now: int) -> dict:
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            removed = conn.execute("DELETE FROM crm_tags WHERE user_id = ? AND tag = ?", (user_id, tag)).rowcount
            if removed:
                ev = conn.execute("INSERT INTO crm_tag_events (user_id, tag, action, created_at, admin_id) "
                                  "VALUES (?, ?, 'remove', ?, ?)", (user_id, tag, now, actor.id)).lastrowid
                self.accounts._event(conn, now, actor.id, "user.tag_remove", "user", str(user_id), None,
                                     {"tagEventId": ev}, None, None, "done")
        return {"removed": bool(removed)}

    def set_mb_id(self, actor: Admin, user_id: int, value: object, now: int) -> dict:
        """Sets (or clears, with an empty value) the user's MB account id. The journal records only that it
        changed, never the id itself (it identifies a person at a third party)."""
        if not isinstance(value, str):
            raise ActionError("invalid_mb_id")
        mb_id = value.strip()
        if mb_id and not _MB_ID.match(mb_id):
            raise ActionError("invalid_mb_id")
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            prev = conn.execute("SELECT mb_id FROM crm_external_ids WHERE user_id = ?", (user_id,)).fetchone()
            had = prev is not None
            if mb_id:
                owner = conn.execute("SELECT user_id FROM crm_external_ids WHERE mb_id = ? COLLATE NOCASE",
                                     (mb_id,)).fetchone()
                if owner is not None and owner[0] != user_id:
                    raise ActionError("mb_id_taken", 409)
                conn.execute("INSERT INTO crm_external_ids (user_id, mb_id, updated_at, admin_id) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(user_id) DO UPDATE SET mb_id = excluded.mb_id, updated_at = excluded.updated_at, "
                             "admin_id = excluded.admin_id", (user_id, mb_id, now, actor.id))
            else:
                conn.execute("DELETE FROM crm_external_ids WHERE user_id = ?", (user_id,))
            if (prev[0] if prev else None) != (mb_id or None):
                conn.execute("INSERT INTO crm_mb_id_history (user_id, old_mb_id, new_mb_id, created_at, admin_id) "
                             "VALUES (?, ?, ?, ?, ?)", (user_id, prev[0] if prev else None, mb_id or None, now, actor.id))
            bump_ads_revision(conn)  # MB holders feed the ads audiences: the snapshot is out of date
            self.accounts._event(conn, now, actor.id, "user.mb_id", "user", str(user_id),
                                 {"set": had}, {"set": bool(mb_id)}, None, None, "done")
        return {"mbId": mb_id or None}

    def add_note(self, actor: Admin, user_id: int, body: str, now: int) -> dict:
        body = _text(body, "note", 2000)
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            note_id = conn.execute("INSERT INTO crm_notes (user_id, body, created_at, admin_id) VALUES (?, ?, ?, ?)",
                                   (user_id, body, now, actor.id)).lastrowid
            # The journal records THAT a note was added, never its text (notes may hold personal data).
            self.accounts._event(conn, now, actor.id, "user.note_add", "user", str(user_id), None,
                                 {"noteId": note_id}, None, None, "done")
        return {"id": note_id}

    def send_message(self, actor: Admin, user_id: int, body: str, key: str, now: int, request_id: str) -> dict:
        """Step 1 of 2: the message row and a `requested` event in one transaction. The caller then queues the job
        for the bot (outbox, idempotent by `msg:<id>`) and records `completed` (unfinished ones: at startup).

        `key` (the client's idempotency key) binds a retry to the same admin, user and text: a retry after a lost
        response returns the same message (and its request) instead of a second Telegram message."""
        body = _text(body, "message", MAX_TEXT)
        if not isinstance(key, str) or not 8 <= len(key) <= 80:
            raise ActionError("idempotency_key_required")
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            prior = conn.execute("SELECT id, admin_id, user_id, body FROM crm_messages WHERE idempotency_key = ?",
                                 (key,)).fetchone()
            if prior is not None:
                if (prior[1], prior[2], prior[3]) != (actor.id, user_id, body):
                    raise ActionError("idempotency_key_reused", 409)
                req = conn.execute(
                    "SELECT request_id FROM admin_events WHERE action = 'user.message' AND kind = 'requested' "
                    "AND CAST(json_extract(after_json, '$.messageId') AS INTEGER) = ?", (prior[0],)).fetchone()
                return {"id": prior[0], "status": "queued", "requestId": req[0], "replayed": True}
            msg_id = conn.execute("INSERT INTO crm_messages (user_id, body, status, created_at, admin_id, "
                                  "idempotency_key) VALUES (?, ?, 'queued', ?, ?, ?)",
                                  (user_id, body, now, actor.id, key)).lastrowid
            # The journal keeps the message id, never its text.
            self.accounts._event(conn, now, actor.id, "user.message", "user", str(user_id), None,
                                 {"messageId": msg_id}, None, None, "requested", request_id)
        return {"id": msg_id, "status": "queued", "requestId": request_id}

    def message_queued(self, admin_id: int | None, user_id: int, msg_id: int, request_id: str, now: int) -> None:
        """Step 2: the job is in the outbox (exactly one `completed` per request, a retry is a no-op)."""
        with self.accounts._write() as conn:
            if conn.execute("SELECT 1 FROM admin_events WHERE kind = 'completed' AND request_id = ?",
                            (request_id,)).fetchone() is None:
                self.accounts._event(conn, now, admin_id, "user.message", "user", str(user_id), None,
                                     {"messageId": msg_id, "queued": True}, None, None, "completed", request_id)

    def frozen_recipients(self, broadcast_id: int) -> list[int]:
        with self.accounts._connect() as conn:
            return [r[0] for r in conn.execute("SELECT user_id FROM crm_broadcast_recipients WHERE broadcast_id = ? "
                                               "ORDER BY user_id", (broadcast_id,))]

    def unqueued_messages(self) -> list[dict]:
        """Messages whose `requested` event has no `completed` one (a crash between the two steps)."""
        with self.accounts._connect() as conn:
            rows = conn.execute(
                "SELECT e.request_id, e.admin_id, e.after_json, m.user_id, m.body FROM admin_events e "
                "JOIN crm_messages m ON m.id = CAST(json_extract(e.after_json, '$.messageId') AS INTEGER) "
                "WHERE e.action = 'user.message' AND e.kind = 'requested' AND NOT EXISTS (SELECT 1 FROM admin_events c "
                "WHERE c.kind = 'completed' AND c.request_id = e.request_id)").fetchall()
        return [{"requestId": r[0], "adminId": r[1], "messageId": json.loads(r[2])["messageId"], "userId": r[3],
                 "body": r[4]} for r in rows]

    def save_segment(self, actor: Admin, name: str, rules: Any, now: int, segment_id: int | None = None) -> dict:
        name = _text(name, "name", 80)
        rules = validate_rules(rules)
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            try:
                if segment_id is None:
                    segment_id = conn.execute(
                        "INSERT INTO crm_segments (name, rules_json, created_at, updated_at, admin_id) "
                        "VALUES (?, ?, ?, ?, ?)", (name, json.dumps(rules, sort_keys=True), now, now, actor.id)).lastrowid
                elif not conn.execute("UPDATE crm_segments SET name = ?, rules_json = ?, updated_at = ?, admin_id = ? "
                                      "WHERE id = ?", (name, json.dumps(rules, sort_keys=True), now, actor.id,
                                                       segment_id)).rowcount:
                    raise ActionError("not_found", 404)
            except sqlite3.IntegrityError:
                raise ActionError("name_taken", 409) from None
            self.accounts._event(conn, now, actor.id, "segment.save", "segment", str(segment_id), None,
                                 {"fields": sorted(rules)}, None, None, "done")
        return {"id": segment_id}

    def save_broadcast(self, actor: Admin, data: dict, now: int, broadcast_id: int | None = None) -> dict:
        name = _text(data.get("name"), "name", 120)
        body = _text(data.get("body"), "body", MAX_TEXT)
        rules = validate_rules(data.get("rules") or {})
        buttons = data.get("buttons") or []
        if not isinstance(buttons, list) or len(buttons) > 3 or not all(
                isinstance(b, dict) and isinstance(b.get("text"), str) and 1 <= len(b["text"]) <= 40
                and isinstance(b.get("url"), str) and (b["url"].startswith("https://") or b["url"] == "miniapp")
                and len(b["url"]) <= 300 for b in buttons):
            raise ActionError("invalid_buttons")
        buttons = [{"text": b["text"], "url": b["url"]} for b in buttons]
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            if broadcast_id is None:
                broadcast_id = conn.execute(
                    "INSERT INTO crm_broadcasts (name, rules_json, body, buttons_json, status, created_at, updated_at, "
                    "admin_id) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)",
                    (name, json.dumps(rules), body, json.dumps(buttons), now, now, actor.id)).lastrowid
            else:
                status = conn.execute("SELECT status FROM crm_broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
                if status is None:
                    raise ActionError("not_found", 404)
                if status[0] != "draft":
                    raise ActionError("not_a_draft", 409)
                conn.execute("UPDATE crm_broadcasts SET name = ?, rules_json = ?, body = ?, buttons_json = ?, "
                             "updated_at = ?, admin_id = ? WHERE id = ?",
                             (name, json.dumps(rules), body, json.dumps(buttons), now, actor.id, broadcast_id))
            self.accounts._event(conn, now, actor.id, "broadcast.save", "broadcast", str(broadcast_id), None,
                                 {"status": "draft"}, None, None, "done")
        return {"id": broadcast_id}

    # ---- dangerous writes: step-up + reason + idempotency ------------------------------------

    def schedule_broadcast(self, actor: Admin, broadcast_id: int, at: int, recipients: list[int], reason: str,
                           key: str, code: str, now: int) -> dict:
        """The recipient list is frozen here (crm_broadcast_recipients), in the schedule's transaction."""
        with self.accounts._write() as conn:
            # A retry returns the recorded result even if the audience size changed since (only the time binds).
            if (prior := self.accounts._replayed(conn, key, actor.id, "broadcast.schedule", str(broadcast_id),
                                                 None, reason)) is not None:
                if (prior["after"] or {}).get("scheduledAt") != at:
                    raise ActionError("idempotency_key_reused", 409)
                return prior
            if not now - 60 <= at <= now + 90 * 86400:  # only for a new schedule: a replay may come later
                raise ActionError("invalid_time")
            self.accounts.step_up(conn, actor.id, code, now, "manage_users")
            status = conn.execute("SELECT status FROM crm_broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
            if status is None:
                raise ActionError("not_found", 404)
            if status[0] != "draft":
                raise ActionError("not_a_draft", 409)
            conn.execute("UPDATE crm_broadcasts SET status = 'scheduled', scheduled_at = ?, recipients = ?, "
                         "updated_at = ? WHERE id = ?", (at, len(recipients), now, broadcast_id))
            conn.executemany("INSERT OR IGNORE INTO crm_broadcast_recipients (broadcast_id, user_id) VALUES (?, ?)",
                             [(broadcast_id, uid) for uid in sorted(set(recipients))])
            after = {"scheduledAt": at, "recipients": len(recipients)}
            event = self.accounts._event(conn, now, actor.id, "broadcast.schedule", "broadcast", str(broadcast_id),
                                         {"status": "draft"}, after, reason, key, "done")
        return {"eventId": event, "after": after, "replayed": False}

    def cancel_broadcast(self, actor: Admin, broadcast_id: int, reason: str, key: str, code: str, now: int) -> dict:
        """Irreversible: step-up + reason + idempotency like scheduling."""
        with self.accounts._write() as conn:
            if (prior := self.accounts._replayed(conn, key, actor.id, "broadcast.cancel", str(broadcast_id),
                                                 {"status": "cancelled"}, reason)) is not None:
                return prior
            self.accounts.step_up(conn, actor.id, code, now, "manage_users")
            if not conn.execute("UPDATE crm_broadcasts SET status = 'cancelled', updated_at = ? WHERE id = ? "
                                "AND status IN ('draft', 'scheduled')", (now, broadcast_id)).rowcount:
                raise ActionError("not_cancellable", 409)
            event = self.accounts._event(conn, now, actor.id, "broadcast.cancel", "broadcast", str(broadcast_id),
                                         None, {"status": "cancelled"}, reason, key, "done")
        return {"eventId": event, "after": {"status": "cancelled"}, "replayed": False}

    def credit(self, actor: Admin, user_ids: list[int], kind: str, amount: int, days: int, reason: str, key: str,
               code: str, now: int) -> dict:
        """grant / debit `amount`, or unlimited for `days` (0 = switch off). Bookkeeping only (credits are off)."""
        if kind not in ("grant", "debit", "unlimited") or not user_ids or len(user_ids) > MAX_GROUP:
            raise ActionError("invalid_credit")
        if kind in ("grant", "debit") and (isinstance(amount, bool) or not isinstance(amount, int)
                                           or not 1 <= amount <= 100_000):
            raise ActionError("invalid_amount")
        if kind == "unlimited" and (isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 366):
            raise ActionError("invalid_days")
        users = sorted(set(user_ids))
        target = str(users[0]) if len(users) == 1 else f"group:{len(users)}"
        request = {"kind": kind, "amount": amount if kind != "unlimited" else 0,
                   "days": days if kind == "unlimited" else 0, "users": len(users),
                   # the whole recipient list binds the idempotency key (not only its size and first id)
                   "usersDigest": hashlib.sha256(",".join(map(str, users)).encode()).hexdigest()}
        with self.accounts._write() as conn:
            if (prior := self.accounts._replayed(conn, key, actor.id, "credits.change", target, None, reason)) is not None:
                if (prior["after"] or {}).get("request") != request:
                    raise ActionError("idempotency_key_reused", 409)
                return prior
            self.accounts.step_up(conn, actor.id, code, now, "manage_users")
            results, rows = [], []
            for uid in users:
                row = conn.execute("SELECT balance, unlimited_until FROM credit_accounts WHERE user_id = ?",
                                   (uid,)).fetchone()
                balance, until = (row[0], row[1]) if row else (0, None)
                if kind == "grant":
                    delta, balance, ledger_kind = amount, balance + amount, "grant"
                elif kind == "debit":
                    if amount > balance:
                        raise ActionError("insufficient_balance", 409)
                    delta, balance, ledger_kind = -amount, balance - amount, "debit"
                else:
                    delta = 0
                    until = now + days * 86400 if days else None
                    ledger_kind = "unlimited" if days else "unlimited_off"
                rows.append((uid, ledger_kind, delta, balance, until))
                results.append({"userId": uid, "balance": balance, "unlimitedUntil": until})
            # The journal keeps the request shape only; per-user ids and balances stay in the CRM tables.
            after = {"request": request}
            event = self.accounts._event(conn, now, actor.id, "credits.change", "user" if len(users) == 1 else "group",
                                         target, None, after, reason, key, "done")
            for uid, ledger_kind, delta, balance, until in rows:
                conn.execute("INSERT INTO credit_accounts (user_id, balance, unlimited_until, updated_at) "
                             "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance, "
                             "unlimited_until = excluded.unlimited_until, updated_at = excluded.updated_at",
                             (uid, balance, until, now))
                conn.execute("INSERT INTO credit_ledger (user_id, kind, delta, balance_after, unlimited_until, reason, "
                             "admin_id, batch_key, event_id, created_at) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?)",
                             (uid, ledger_kind, delta, balance, until, actor.id, key if len(users) > 1 else None,
                              event, now))
        return {"eventId": event, "after": {**after, "results": results[:20]}, "replayed": False}

    def set_block(self, actor: Admin, user_id: int, blocked: bool, reason: str, key: str, code: str,
                  now: int, request_id: str) -> dict:
        """Block = flag + `requested` event; the caller then revokes the sessions and records `completed`
        (same two-step flow as a session revocation, finished at startup if a crash hit in between).
        Returns {"requestId", "resumed"} when sessions must be revoked now, else the recorded result."""
        with self.accounts._write() as conn:
            row = conn.execute("SELECT id, kind, request_id, target_id, admin_id, reason_digest, after_json FROM admin_events "
                               "WHERE idempotency_key = ?", (key,)).fetchone()
            if row is not None:
                event_id, kind, prior_request, target, prior_admin, prior_reason, after_json = row
                after = json.loads(after_json) if after_json else None
                if (target, prior_admin, prior_reason, (after or {}).get("blocked")) != (str(user_id), actor.id, reason_digest(reason),
                                                                                        blocked):
                    raise ActionError("idempotency_key_reused", 409)
                if kind != "requested":
                    return {"eventId": event_id, "after": after, "replayed": True}
                done = conn.execute("SELECT id, after_json FROM admin_events WHERE kind = 'completed' "
                                    "AND request_id = ?", (prior_request,)).fetchone()
                if done is not None:  # finished: a replay never revokes sessions created since
                    revoked = json.loads(done[1]).get("revoked")
                    return {"eventId": done[0], "after": {"blocked": True, "revoked": revoked},
                            "revokedSessions": revoked, "replayed": True}
                self.accounts._require_actor(conn, actor.id, "manage_users")  # resuming needs the permission now
                return {"requestId": prior_request, "resumed": True}
            self.accounts.step_up(conn, actor.id, code, now, "manage_users")
            if blocked:
                conn.execute("INSERT OR REPLACE INTO crm_blocks (user_id, reason, created_at, admin_id) "
                             "VALUES (?, ?, ?, ?)", (user_id, reason, now, actor.id))
                self.accounts._event(conn, now, actor.id, "user.block", "user", str(user_id), None, {"blocked": True},
                                     reason, key, "requested", request_id)
                return {"requestId": request_id, "resumed": False}
            conn.execute("DELETE FROM crm_blocks WHERE user_id = ?", (user_id,))
            event = self.accounts._event(conn, now, actor.id, "user.block", "user", str(user_id), None,
                                         {"blocked": False}, reason, key, "done")
        return {"eventId": event, "after": {"blocked": False}, "replayed": False}

    def add_tags(self, actor: Admin, user_ids: list[int], tag: str, now: int) -> int:
        """One transaction for the whole batch: all users get the tag (and journal rows) or none."""
        tag = _text(tag, "tag", MAX_TAG)
        created = 0
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_users")
            for uid in sorted(set(user_ids)):
                if conn.execute("INSERT OR IGNORE INTO crm_tags (user_id, tag, created_at, admin_id) VALUES (?, ?, ?, ?)",
                                (uid, tag, now, actor.id)).rowcount:
                    ev = conn.execute("INSERT INTO crm_tag_events (user_id, tag, action, created_at, admin_id) "
                                      "VALUES (?, ?, 'add', ?, ?)", (uid, tag, now, actor.id)).lastrowid
                    self.accounts._event(conn, now, actor.id, "user.tag_add", "user", str(uid), None,
                                         {"tagEventId": ev}, None, None, "done")
                    created += 1
        return created
