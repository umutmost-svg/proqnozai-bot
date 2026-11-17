"""User inquiries (обращения) and the interaction history, from bot-crm.db (written by the bot only while
BOT_INQUIRIES_ENABLED is on; nothing older is ever collected).

Access: reading needs `read_personal` AND the explicit `view_dialogs` grant (owner-issued, per admin); every opened
thread is journalled (`inquiry.view`, ids only). Changing a thread's status needs `manage_users` and the grant; the
retention period is an owner decision with reason + 2FA + idempotency and purges at once. Replies go through the
existing message route (admin -> bot outbox); the history merges the user's inquiries with our messages.
"""
from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI, Request

from miniapp.admin_web.accounts import ActionError, Admin
from miniapp.admin_web.bot_crm import MAX_RETENTION_DAYS, MIN_RETENTION_DAYS, BotCrmFile
from miniapp.admin_web.crm import CrmStore

logger = logging.getLogger("miniapp.admin_web")

HISTORY_LIMIT = 200
STATUSES = ("open", "closed")

_SCHEMA = """
-- `through_id`: the newest inquiry id when the status was set; a newer inquiry reopens a closed thread.
CREATE TABLE IF NOT EXISTS crm_inquiry_status (
    user_id    INTEGER PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    through_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
-- The retention chosen by the owner: the source of truth, changed with its journal event in one transaction and
-- then applied to bot-crm.db (again at startup and on reads until the file matches).
CREATE TABLE IF NOT EXISTS crm_inquiry_retention (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    days       INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
"""


def register(app: FastAPI, ctx: SimpleNamespace, crm: CrmStore, outbox_status) -> None:
    accounts = ctx.accounts
    with accounts._connect() as conn:
        conn.executescript(_SCHEMA)
    inbox = BotCrmFile(ctx.bot_crm_file)
    store = ctx.store
    def desired_days() -> int | None:
        with accounts._connect() as conn:
            row = conn.execute("SELECT days FROM crm_inquiry_retention WHERE id = 1").fetchone()
        return int(row[0]) if row else None

    def apply(now: int) -> int:
        """Brings bot-crm.db to the owner's setting and purges; raises when the file cannot be written."""
        days = desired_days()
        if days is not None and (not inbox.exists() or inbox.retention_days() != days):
            return inbox.apply_retention(days, now)
        return inbox.purge(now)

    def purge() -> None:
        """Before every read of inquiries: the file follows the owner's setting and holds nothing past it. When that
        cannot be ensured, nothing is served (503) rather than texts that should already be gone."""
        try:
            apply(ctx.now())
        except (sqlite3.Error, OSError) as exc:
            logger.warning("event=inquiry_purge_failed type=%s", type(exc).__name__)
            raise ActionError("retention_not_applied", 503) from None

    try:
        purge()  # at startup: retention holds even when the bot is not running
    except ActionError:
        pass  # logged; every read retries and refuses until it succeeds

    def reader(request: Request, permission: str = "read_personal") -> Admin:
        admin = ctx.admin_of(request, permission)
        if not accounts.has_grant(admin.id, "view_dialogs"):
            raise ActionError("dialogs_not_allowed", 403)
        return admin

    def read(fn):
        try:
            return fn()
        except sqlite3.Error:
            raise ActionError("inquiries_unavailable", 503) from None

    def statuses() -> dict[int, tuple[str, int]]:
        with accounts._connect() as conn:
            return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT user_id, status, through_id FROM crm_inquiry_status")}

    def status_of(stored: tuple[str, int] | None, last_id: int | None) -> str:
        """Closed only while no inquiry newer than the one seen at closing has arrived."""
        if stored is None or stored[0] != "closed":
            return "open"
        return "closed" if last_id is None or last_id <= stored[1] else "open"

    @app.get("/api/admin/inquiries")
    async def threads(request: Request) -> dict:
        reader(request)
        purge()
        items = read(inbox.threads)
        known = {u["id"]: u for u in store.users_by_ids([i["userId"] for i in items], ctx.now())} if items else {}
        state = statuses()
        for item in items:
            u = known.get(item["userId"])
            item["name"] = " ".join(p for p in ((u or {}).get("firstName"), (u or {}).get("lastName")) if p) or None
            item["username"] = (u or {}).get("username")
            item["status"] = status_of(state.get(item["userId"]), item.pop("lastId"))
        wanted = request.query_params.get("status")
        if wanted in STATUSES:
            items = [i for i in items if i["status"] == wanted]
        return {"collected": inbox.collected(), "retentionDays": desired_days() or inbox.retention_days(), "items": items}

    @app.get("/api/admin/inquiries/{user_id}")
    async def thread(user_id: int, request: Request) -> dict:
        admin = reader(request)
        purge()
        inbound = read(lambda: inbox.messages(user_id, HISTORY_LIMIT + 1))
        more_inbound = len(inbound) > HISTORY_LIMIT
        inbound = inbound[:HISTORY_LIMIT]
        info = crm.user_crm(user_id, ctx.now())  # the newest 50 of our messages
        with accounts._connect() as conn:
            outbound_total = conn.execute("SELECT COUNT(*) FROM crm_messages WHERE user_id = ?", (user_id,)).fetchone()[0]
        delivered = outbox_status([f"msg:{m['id']}" for m in info["messages"]])
        history = [{"from": "user", "at": m["at"], "text": m["text"], "id": f"in:{m['id']}"} for m in inbound]
        history += [{"from": "admin", "at": m["at"], "text": m["body"], "by": m["by"], "id": f"out:{m['id']}",
                     "status": (delivered.get(f"msg:{m['id']}") or {}).get("status", m["status"]),
                     "error": (delivered.get(f"msg:{m['id']}") or {}).get("error")}
                    for m in info["messages"]]
        history.sort(key=lambda h: (h["at"], h["id"]))
        with accounts._write() as conn:  # who read whose messages: ids only, never the text
            accounts._event(conn, ctx.now(), admin.id, "inquiry.view", "user", str(user_id), None,
                            {"messages": len(inbound)}, None, None, "done")
        profile = store.user_profile(user_id, ctx.now())
        return {"userId": user_id, "noticeAt": inbox.notice_at(user_id), "history": history,
                # Only the newest messages are shown; older retained ones exist when these are set.
                "truncated": {"inbound": more_inbound, "outbound": outbound_total > len(info["messages"])},
                "status": status_of(statuses().get(user_id), max((m["id"] for m in inbound), default=None)),
                "user": None if profile is None else {k: profile[k] for k in ("firstName", "lastName", "username", "lang")}}

    @app.post("/api/admin/inquiries/{user_id}/status")
    async def set_status(user_id: int, request: Request) -> dict:
        admin = reader(request, "manage_users")
        status = (await ctx.body(request)).get("status")
        if status not in STATUSES:
            raise ActionError("invalid_value")
        t = ctx.now()
        through = max((m["id"] for m in read(lambda: inbox.messages(user_id, 1))), default=0)
        with accounts._write() as conn:
            accounts._require_actor(conn, admin.id, "manage_users")
            conn.execute("INSERT INTO crm_inquiry_status (user_id, status, through_id, updated_at, admin_id) "
                         "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET status = excluded.status, "
                         "through_id = excluded.through_id, updated_at = excluded.updated_at, admin_id = excluded.admin_id",
                         (user_id, status, through, t, admin.id))
            accounts._event(conn, t, admin.id, "inquiry.status", "user", str(user_id), None, {"status": status},
                            None, None, "done")
        return {"status": status}

    @app.get("/api/admin/inquiries-settings")
    async def settings(request: Request) -> dict:
        reader(request)
        purge()
        desired = desired_days()
        return {"retentionDays": desired or inbox.retention_days(), "min": MIN_RETENTION_DAYS, "max": MAX_RETENTION_DAYS,
                "collected": inbox.collected(), "applied": desired is None or inbox.retention_days() == desired}

    @app.post("/api/admin/inquiries-settings")
    async def set_settings(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_inquiry_settings")
        data = await ctx.body(request)
        reason, code, key = ctx.dangerous(data)
        days = data.get("retentionDays")
        if isinstance(days, bool) or not isinstance(days, int) or not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
            raise ActionError("invalid_value")
        after = {"retentionDays": days}
        t = ctx.now()

        def change() -> dict:
            with accounts._write() as conn:
                if (prior := accounts._replayed(conn, key, admin.id, "inquiry.retention", "inquiries", after,
                                                reason)) is not None:
                    return prior
                accounts.step_up(conn, admin.id, code, t, "manage_inquiry_settings")
                row = conn.execute("SELECT days FROM crm_inquiry_retention WHERE id = 1").fetchone()
                before = {"retentionDays": int(row[0]) if row else inbox.retention_days()}
                conn.execute("INSERT INTO crm_inquiry_retention (id, days, updated_at, admin_id) VALUES (1, ?, ?, ?) "
                             "ON CONFLICT(id) DO UPDATE SET days = excluded.days, updated_at = excluded.updated_at, "
                             "admin_id = excluded.admin_id", (days, t, admin.id))
                event = accounts._event(conn, t, admin.id, "inquiry.retention", "settings", "inquiries", before,
                                        after, reason, key, "done")
            return {"eventId": event, "after": after, "replayed": False}

        result = ctx.step_up_guard(admin, change)
        # Recorded (setting + journal, one transaction); now applied to the shared file. A failure is reported and the
        # same request (same key) or the next startup/read applies it: the file never holds an unjournalled value.
        try:
            removed = apply(ctx.now())
        except (sqlite3.Error, OSError):
            raise ActionError("retention_not_applied", 503) from None
        return {**result, "removed": removed}
