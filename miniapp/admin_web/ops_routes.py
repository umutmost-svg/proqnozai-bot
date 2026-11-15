"""Operations screens of the panel: the admin -> bot message queue (status only, never texts), the role and grant
matrix, and product analytics (aggregates from the Mini App telemetry, read-only)."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request

from miniapp.admin_web.accounts import GRANTS, PERMISSIONS, ROLES
from miniapp.admin_web.insights import UserInsights, cohort_since, product
from miniapp.admin_web.outbox import BOT_LIMITS, STATUSES, Outbox

QUEUE_ACTIONS = ("user.message", "broadcast.schedule", "broadcast.cancel")


def register(app: FastAPI, ctx: SimpleNamespace) -> None:
    outbox = Outbox(ctx.outbox_file)
    insights = UserInsights(ctx.telemetry_file)

    @app.get("/api/admin/outbox")
    async def queue(request: Request) -> dict:
        # Recipients are Telegram ids (personal): the queue view is for roles that may read personal data.
        ctx.admin_of(request, "read_personal")
        status = request.query_params.get("status")
        # Only what the log shows: reasons and before/after may hold free text.
        log = [{k: e[k] for k in ("id", "at", "action", "adminEmail", "result")}
               for action in QUEUE_ACTIONS for e in ctx.accounts.events(action=action, limit=50)]
        log.sort(key=lambda e: e["id"], reverse=True)
        return {"summary": outbox.summary(ctx.now()), "limits": BOT_LIMITS, "statuses": list(STATUSES),
                "items": outbox.recent(100, status if status in STATUSES else None), "log": log[:50]}

    @app.get("/api/admin/roles")
    async def roles(request: Request) -> dict:
        ctx.admin_of(request)
        return {"roles": list(ROLES),
                "permissions": [{"permission": p, "roles": list(r)} for p, r in PERMISSIONS.items()],
                "grants": [{"permission": p, "roles": list(r)} for p, r in GRANTS.items()]}

    @app.get("/api/admin/product")
    async def product_analytics(request: Request) -> dict:
        ctx.admin_of(request)  # aggregates only: no user ids leave this route
        days = ctx.period(request)
        t = ctx.now()
        users = ctx.store.registrations_since(cohort_since(t))  # every cohort member, no row cap
        return {"period": days, "product": product(insights, t, days, users),
                "referrals": ctx.referral_funnel(),
                # The Mini App keeps odds in memory and records no coverage yet: shown as not collected.
                "oddsCoverage": {"source": "not_collected"}}
