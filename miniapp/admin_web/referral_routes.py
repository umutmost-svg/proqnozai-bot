"""HTTP routes of referral links: list with per-link stats, create, archive; plus the acquisition-source lookup the
users list and card use. Stats are aggregates (counts only), so the analyst role sees them; links are created and
archived by roles with `manage_referrals`.

Funnel per link: starts (first starts attributed to the code, bot and Mini App) -> in Mini App (has a Mini App
account) -> active in 7 days (last seen) -> converted (at least one forecast in the telemetry).
"""
from __future__ import annotations

import sqlite3

from types import SimpleNamespace

from fastapi import FastAPI, Request

from miniapp.admin_web.accounts import ActionError
from miniapp.admin_web.bot_crm import BotCrmFile
from miniapp.admin_web.insights import UserInsights
from miniapp.admin_web.referrals import PREFIX, Referrals, link_url, source_of

DAY = 86400


def register(app: FastAPI, ctx: SimpleNamespace) -> SimpleNamespace:
    """Returns `sources(user_ids)` (users list and card) and `funnel()` (product analytics)."""
    referrals = Referrals(ctx.accounts)
    referrals.init_schema()
    bot_file = BotCrmFile(ctx.bot_crm_file)
    insights = UserInsights(ctx.telemetry_file)
    store = ctx.store

    def sources(user_ids: list[int], strict: bool = False) -> dict[int, dict | None]:
        if not user_ids:
            return {}
        wanted = set(user_ids)
        if strict and not bot_file.has_table("referral_starts"):  # not recorded is not "no referral"
            raise sqlite3.OperationalError("bot referral records are not available")
        bot = bot_file.starts(user_ids, strict=strict)
        app_starts = {uid: a for uid, a in store.attributions(strict=strict).items() if uid in wanted}
        links = referrals.by_code()
        return {uid: source_of(uid, bot, app_starts, links) for uid in user_ids}

    def stats() -> tuple[dict[str, dict], dict]:
        t = ctx.now()
        bot = bot_file.starts()
        app_starts = store.attributions()
        links = referrals.by_code()
        users = set(bot) | set(app_starts)
        by_code: dict[str, set[int]] = {}
        for uid in users:
            src = source_of(uid, bot, app_starts, links)
            if src is not None:
                by_code.setdefault(src["code"], set()).add(uid)
        known = {u["id"]: u for u in store.users_by_ids(sorted(users), t)} if users else {}
        # A telemetry file that exists but cannot be read: conversion unknown (None), never a measured zero.
        try:
            collected = insights.available(strict=True)
            activity = insights.activity(sorted(users), t, strict=True) if users and collected else {}
        except sqlite3.Error:
            collected, activity = False, {}
        out: dict[str, dict] = {}
        for code, ids in by_code.items():
            seen = {uid: max((known.get(uid) or {}).get("lastLoginAt") or 0,
                             (activity.get(uid) or {}).get("lastSeenAt") or 0) for uid in ids}
            # Nested: active and converted are counted among the users that have a Mini App account.
            in_app = [uid for uid in ids if uid in known]
            out[code] = {
                "starts": len(ids),
                "botStarts": sum(1 for uid in ids if uid in bot and bot[uid]["code"] == code),
                "miniappStarts": sum(1 for uid in ids if uid in app_starts and app_starts[uid]["code"] == code),
                "inMiniApp": len(in_app),
                "active7d": sum(1 for uid in in_app if seen[uid] >= t - 7 * DAY),
                # Without telemetry the conversion is unknown, never a measured zero.
                "converted": sum(1 for uid in in_app if (activity.get(uid) or {}).get("forecasts", 0) > 0)
                if collected else None,
            }
        funnel = {k: sum(s[k] for s in out.values()) for k in ("starts", "inMiniApp", "active7d")}
        funnel["converted"] = sum(s["converted"] for s in out.values()) if collected else None
        return out, funnel

    @app.get("/api/admin/referrals")
    async def list_links(request: Request) -> dict:
        ctx.admin_of(request)
        personal = ctx.can_personal(request)  # who created a link is an e-mail: not for analysts
        per_code, funnel = stats()
        empty = {"starts": 0, "botStarts": 0, "miniappStarts": 0, "inMiniApp": 0, "active7d": 0,
                 "converted": 0 if insights.available() else None}
        items = []
        for link in referrals.links():
            items.append({**link, "by": link["by"] if personal else None, "param": PREFIX + link["code"],
                          "url": link_url(link["channel"], link["code"], ctx.bot_username, ctx.miniapp_short_name),
                          "stats": per_code.get(link["code"], empty)})
        unknown = {code: s for code, s in per_code.items() if code not in {i["code"] for i in items}}
        return {"items": items, "funnel": funnel,
                "unknownCodes": [{"code": c, "stats": s} for c, s in sorted(unknown.items())],
                "telemetry": insights.available(),
                "names": {"bot": ctx.bot_username, "miniapp": ctx.miniapp_short_name}}

    @app.post("/api/admin/referrals")
    async def create(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_referrals")
        data = await ctx.body(request)
        return referrals.create(admin, data.get("name"), data.get("channel"), data.get("code"), ctx.now())

    @app.post("/api/admin/referrals/{link_id}/archive")
    async def archive(link_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_referrals")
        data = await ctx.body(request)
        if "archived" not in data:
            raise ActionError("invalid_value")
        return referrals.set_archived(admin, link_id, data["archived"], ctx.now())

    return SimpleNamespace(sources=sources, funnel=lambda: stats()[1])
