"""HTTP routes of the ads module (ads-architecture.md §8). Registered by app.create_admin_app after the CRM
routes (MB ids live in the CRM tables); shares the app's guards: session, Origin + CSRF, role checks.

Roles: analyst reads campaigns without identifiers (audience trees redacted, no group members); admin
manages campaigns, groups and publication; owner also manages partners (link hosts, countries, age)."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request

from miniapp.ads import targeting
from miniapp.admin_web.accounts import ActionError
from miniapp.admin_web.ads import AdsStore


def register(app: FastAPI, ctx: SimpleNamespace, policy_file, applied_file) -> AdsStore:
    ads = AdsStore(ctx.accounts, policy_file, applied_file)
    ads.init_schema()
    try:  # a publication committed just before a crash reaches the file now
        ads.ensure_published()
    except OSError:
        pass

    def optional_id(data: dict, field: str = "id") -> int | None:
        value = data.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ActionError("invalid_id")
        return value

    def view(item: dict, personal: bool) -> dict:
        if not personal:
            item = {**item, "condition": targeting.redact(item["condition"])}
        return item

    @app.get("/api/admin/ads/campaigns")
    async def campaigns(request: Request) -> dict:
        ctx.admin_of(request)
        personal = ctx.can_personal(request)
        return {"items": [view(c, personal) for c in ads.campaigns(ctx.now())], "partners": ads.partners(),
                "publication": ads.publication_status()}

    @app.get("/api/admin/ads/campaigns/{campaign_id}")
    async def campaign(campaign_id: int, request: Request) -> dict:
        ctx.admin_of(request)
        item = ads.campaign(campaign_id, ctx.now())
        if item is None:
            raise ActionError("not_found", 404)
        return view(item, ctx.can_personal(request))

    @app.post("/api/admin/ads/campaigns")
    async def save_campaign(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_ads")
        data = await ctx.body(request)
        return ads.save_campaign(admin, data, ctx.now(), optional_id(data))

    @app.post("/api/admin/ads/campaigns/{campaign_id}/status")
    async def campaign_status(campaign_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_ads")
        return ads.set_status(admin, campaign_id, str((await ctx.body(request)).get("action") or ""), ctx.now())

    @app.post("/api/admin/ads/campaigns/{campaign_id}/copy")
    async def campaign_copy(campaign_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_ads")
        await ctx.body(request)
        return ads.copy_campaign(admin, campaign_id, ctx.now())

    @app.get("/api/admin/ads/partners")
    async def partners(request: Request) -> dict:
        ctx.admin_of(request)
        return {"items": ads.partners()}

    @app.post("/api/admin/ads/partners")
    async def save_partner(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_partners")
        data = await ctx.body(request)
        return ads.save_partner(admin, data, ctx.now(), optional_id(data))

    @app.get("/api/admin/ads/groups")
    async def groups(request: Request) -> dict:
        ctx.admin_of(request)
        return {"items": ads.groups()}

    @app.get("/api/admin/ads/groups/{group_id}/members")
    async def group_members(group_id: int, request: Request) -> dict:
        ctx.admin_of(request, "read_personal")
        return {"items": ads.group_members(group_id)}

    @app.post("/api/admin/ads/groups")
    async def save_group(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_ads")
        data = await ctx.body(request)
        return ads.save_group(admin, data.get("name"), ctx.now(), optional_id(data))

    @app.post("/api/admin/ads/groups/{group_id}/members")
    async def change_members(group_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_ads")
        data = await ctx.body(request)
        try:
            return ads.change_members(admin, group_id, data.get("add"), data.get("remove"), ctx.now())
        except targeting.ConditionError:
            raise ActionError("invalid_users") from None

    @app.get("/api/admin/ads/publication")
    async def publication(request: Request) -> dict:
        ctx.admin_of(request)
        return ads.publication_status()

    @app.post("/api/admin/ads/publish")
    async def publish(request: Request) -> dict:
        admin = ctx.admin_of(request, "publish_ads")
        data = await ctx.body(request)
        reason, code, key = ctx.dangerous(data)
        pause_all = data.get("pauseAll", False)
        if not isinstance(pause_all, bool):
            raise ActionError("invalid_value")
        return ctx.step_up_guard(admin, lambda: ads.publish(admin, reason, key, code, ctx.now(), pause_all))

    return ads
