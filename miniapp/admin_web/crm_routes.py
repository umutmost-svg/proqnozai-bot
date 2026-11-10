"""HTTP routes of the CRM layer (users with tags/segments, user 360, messages, broadcasts,
credits, analytics). Registered by app.create_admin_app; shares its guards and helpers."""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from miniapp.admin_web import demo
from miniapp.admin_web.accounts import ActionError
from miniapp.admin_web.crm import RULE_FIELDS, CrmStore, matches, validate_rules
from miniapp.store import BurnWindow

MAX_SCAN = 50_000  # pilot: audiences are evaluated in memory over at most this many users
CHIPS = {"new": {"registered_within_days": 7}, "active": {"active_within_days": 7},
         "inactive": {"inactive_for_days": 14}}
_USER_SORT = {"id": "id", "created": "createdAt", "login": "lastLoginAt", "username": "username",
              "sessions": "activeSessions"}


def register(app: FastAPI, ctx: SimpleNamespace) -> CrmStore:
    crm = CrmStore(ctx.accounts)
    crm.init_schema()
    store = ctx.store

    def audience(rules: dict, query: str = "") -> list[dict]:
        t = ctx.now()
        _, users = store.list_users(t, query=query, sort="created", descending=True, limit=MAX_SCAN)
        tags = crm.tags_by_user()
        blocked = crm.blocked()
        return [u for u in users if matches(u, rules, set(tags.get(u["id"], [])), u["id"] in blocked, t)]

    def decorate(rows: list[dict]) -> list[dict]:
        ids = [r["id"] for r in rows]
        tags = crm.tags_by_user(ids)
        credits = crm.credits_of(ids, ctx.now())
        blocked = crm.blocked()
        for r in rows:
            r["tags"] = tags.get(r["id"], [])
            r["credits"] = credits.get(r["id"], {"balance": 0, "unlimitedUntil": None})
            r["blocked"] = r["id"] in blocked
        return rows

    def rules_from_query(request: Request) -> dict:
        q = request.query_params
        rules: dict[str, Any] = dict(CHIPS.get(q.get("chip", ""), {}))
        if q.get("segment"):
            seg = next((s for s in crm.segments() if str(s["id"]) == q["segment"]), None)
            if seg is None:
                raise ActionError("not_found", 404)
            rules.update(seg["rules"])
        if q.get("tag"):
            rules["tag_has"] = q["tag"][:32]
        if q.get("lang"):
            rules["lang_in"] = [q["lang"][:8]]
        if q.get("rules"):
            try:
                rules.update(validate_rules(json.loads(q["rules"])))
            except ValueError:
                raise ActionError("invalid_rules") from None
        return validate_rules(rules) if rules else {}

    def user_ids(data: dict) -> list[int]:
        ids = data.get("userIds")
        if not isinstance(ids, list) or not ids or len(ids) > 5000 or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise ActionError("invalid_users")
        return ids

    def exists(user_id: int) -> None:
        if store.user_profile(user_id, ctx.now()) is None:
            raise ActionError("not_found", 404)

    # ---- users -------------------------------------------------------------------------------

    @app.get("/api/admin/users")
    async def users(request: Request) -> dict:
        ctx.admin_of(request, "read_personal")
        q = request.query_params
        size, offset = ctx.page(request)
        rows = audience(rules_from_query(request), q.get("q", "")[:64])
        key = _USER_SORT.get(q.get("sort", "created"), "createdAt")
        rows.sort(key=lambda r: (r[key] is None, r[key] or 0) if key != "username"
                  else (r[key] is None, (r[key] or "").casefold()), reverse=q.get("order", "desc") != "asc")
        t = ctx.now()
        chip_counts = {name: len(audience(rules)) for name, rules in CHIPS.items()}
        return {"total": len(rows), "page": offset // size + 1, "size": size,
                "items": decorate(rows[offset:offset + size]),
                "counts": {**store.user_counts(t, t - 7 * 86400), "chips": chip_counts},
                "tags": crm.all_tags(), "segments": crm.segments()}

    @app.get("/api/admin/users/{user_id}")
    async def user(user_id: int, request: Request) -> Response:
        ctx.admin_of(request, "read_personal")
        t = ctx.now()
        profile = store.user_profile(user_id, t)
        if profile is None:
            raise ActionError("not_found", 404)
        info = crm.user_crm(user_id, t)
        events = ctx.accounts.events(target=("user", str(user_id)), limit=100)
        ai_calls = ([c for c in demo.ai_calls(30, ctx.today()) if c["userId"] == user_id][:20]
                    if ctx.demo_allowed(request) else None)
        timeline: list[dict] = [{"at": profile["createdAt"], "kind": "registered", "title": "Регистрация в Mini App",
                                 "detail": f"язык {profile['lang']}"}]
        for s in profile["sessions"]:
            timeline.append({"at": s["createdAt"], "kind": "login", "title": "Вход в Mini App", "detail": None})
        for m in info["messages"]:
            timeline.append({"at": m["at"], "kind": "message", "title": "Сообщение от администратора",
                             "detail": m["body"], "by": m["by"], "status": m["status"]})
        for l in info["ledger"]:
            timeline.append({"at": l["at"], "kind": "credits", "title": {
                "grant": f"Кредиты: начислено {l['delta']}", "debit": f"Кредиты: списано {-l['delta']}",
                "unlimited": "Кредиты: включён безлимит", "unlimited_off": "Кредиты: безлимит выключен"}[l["kind"]],
                "detail": l["reason"], "by": l["by"]})
        for n in info["notes"]:
            timeline.append({"at": n["at"], "kind": "note", "title": "Заметка", "detail": n["body"], "by": n["by"]})
        for e in events:
            if e["action"] in ("user.tag_add", "user.tag_remove", "user.block", "user.sessions_revoke"):
                if e["action"] == "user.sessions_revoke" and e["result"] != "completed":
                    continue
                title = {"user.tag_add": f"Добавлен тег {(e['after'] or {}).get('tag')}",
                         "user.tag_remove": f"Снят тег {(e['before'] or {}).get('tag')}",
                         "user.block": "Доступ заблокирован" if (e["after"] or {}).get("blocked") else "Блокировка снята",
                         "user.sessions_revoke": f"Отозваны сессии: {(e['after'] or {}).get('revoked', 0)}"}[e["action"]]
                timeline.append({"at": e["at"], "kind": "admin", "title": title, "detail": e["reason"],
                                 "by": e["adminEmail"]})
        for c in ai_calls or []:
            timeline.append({"at": c["at"], "kind": "ai", "title": f"AI-разбор · {c['model']}", "detail": c["result"],
                             "demo": True})
        timeline.sort(key=lambda x: x["at"], reverse=True)
        profile.update(info)
        profile.update({"events": events, "aiCalls": ai_calls, "timeline": timeline[:100]})
        return JSONResponse(profile)

    @app.post("/api/admin/users/bulk/tags")
    async def bulk_tag(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        data = await ctx.body(request)
        ids = user_ids(data)
        created = sum(crm.add_tag(admin, uid, data.get("tag"), ctx.now())["created"] for uid in ids)
        return {"tagged": created}

    @app.post("/api/admin/users/{user_id}/tags")
    async def add_tag(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        return crm.add_tag(admin, user_id, (await ctx.body(request)).get("tag"), ctx.now())

    @app.post("/api/admin/users/{user_id}/tags/remove")
    async def remove_tag(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        return crm.remove_tag(admin, user_id, str((await ctx.body(request)).get("tag") or ""), ctx.now())

    @app.post("/api/admin/users/{user_id}/notes")
    async def add_note(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        return crm.add_note(admin, user_id, (await ctx.body(request)).get("body"), ctx.now())

    @app.post("/api/admin/users/{user_id}/messages")
    async def message(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        return crm.send_message(admin, user_id, (await ctx.body(request)).get("body"), ctx.now())

    @app.post("/api/admin/users/{user_id}/block")
    async def block(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        data = await ctx.body(request)
        reason, code, key = ctx.dangerous(data)
        flag = data.get("blocked")
        if not isinstance(flag, bool):
            raise ActionError("invalid_value")
        result = ctx.step_up_guard(admin, lambda: crm.set_block(admin, user_id, flag, reason, key, code, ctx.now()))
        if flag and not result.get("replayed"):
            t = ctx.now()  # a block also signs the user out everywhere (the Mini App does not enforce blocks yet)
            result["revokedSessions"] = store.revoke_user_sessions(user_id, t, BurnWindow.for_policy(t, 300, 30))
        return result

    @app.get("/api/admin/tags")
    async def tags(request: Request) -> dict:
        ctx.admin_of(request, "read_personal")
        return {"items": crm.all_tags()}

    # ---- segments ----------------------------------------------------------------------------

    @app.get("/api/admin/segments")
    async def segments(request: Request) -> dict:
        ctx.admin_of(request, "read_personal")
        items = crm.segments()
        for s in items:
            s["size"] = len(audience(s["rules"]))
        return {"items": items, "fields": sorted(RULE_FIELDS),
                "tags": crm.all_tags()}

    @app.post("/api/admin/segments")
    async def save_segment(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        data = await ctx.body(request)
        sid = data.get("id")
        if sid is not None and (isinstance(sid, bool) or not isinstance(sid, int)):
            raise ActionError("invalid_segment")
        return crm.save_segment(admin, data.get("name"), data.get("rules"), ctx.now(), sid)

    @app.post("/api/admin/audience/preview")
    async def audience_preview(request: Request) -> dict:
        ctx.admin_of(request, "read_personal")
        rules = validate_rules((await ctx.body(request)).get("rules") or {})
        rows = audience(rules)
        return {"size": len(rows), "sample": [{"id": r["id"], "username": r["username"],
                                               "firstName": r["firstName"]} for r in rows[:5]],
                "langs": dict(Counter(r["lang"] for r in rows))}

    # ---- broadcasts --------------------------------------------------------------------------

    @app.get("/api/admin/broadcasts")
    async def broadcasts(request: Request) -> dict:
        ctx.admin_of(request)
        items = crm.broadcasts()
        for b in items:
            if b["status"] == "draft":
                b["audience"] = len(audience(b["rules"]))
        return {"items": items, "sending": "pilot_not_sent", "segments": crm.segments()}

    @app.post("/api/admin/broadcasts")
    async def save_broadcast(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        data = await ctx.body(request)
        bid = data.get("id")
        if bid is not None and (isinstance(bid, bool) or not isinstance(bid, int)):
            raise ActionError("invalid_broadcast")
        return crm.save_broadcast(admin, data, ctx.now(), bid)

    @app.post("/api/admin/broadcasts/{broadcast_id}/schedule")
    async def schedule(broadcast_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        data = await ctx.body(request)
        reason, code, key = ctx.dangerous(data)
        at = data.get("at")
        if isinstance(at, bool) or not isinstance(at, int):
            raise ActionError("invalid_time")
        item = next((b for b in crm.broadcasts() if b["id"] == broadcast_id), None)
        if item is None:
            raise ActionError("not_found", 404)
        recipients = len(audience(item["rules"]))
        return ctx.step_up_guard(admin, lambda: crm.schedule_broadcast(admin, broadcast_id, at, recipients, reason,
                                                                       key, code, ctx.now()))

    @app.post("/api/admin/broadcasts/{broadcast_id}/cancel")
    async def cancel(broadcast_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        return crm.cancel_broadcast(admin, broadcast_id, ctx.now())

    # ---- credits -----------------------------------------------------------------------------

    @app.get("/api/admin/credits")
    async def credits(request: Request) -> dict:
        ctx.admin_of(request, "read_personal")
        t = ctx.now()
        return {"enabled": False, "totals": crm.credit_totals(t, t - 30 * 86400),
                "ledger": crm.ledger(200, request.query_params.get("kind") or None),
                "rules": {"aiPrice": 5, "quickPrice": 0, "freePerDay": 3, "signupBonus": 20, "inviteBonus": 10,
                          "editable": False}}

    @app.post("/api/admin/credits/change")
    async def credit_change(request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        data = await ctx.body(request)
        reason, code, key = ctx.dangerous(data)
        ids = user_ids(data)
        for uid in set(ids):
            exists(uid)
        return ctx.step_up_guard(admin, lambda: crm.credit(
            admin, ids, str(data.get("kind") or ""), data.get("amount") or 0, data.get("days") or 0, reason, key,
            code, ctx.now()))

    # ---- dialogs (demo only until the bot-side inbox exists) ---------------------------------

    @app.get("/api/admin/dialogs")
    async def dialogs(request: Request) -> dict:
        ctx.admin_of(request, "read_personal")
        if not ctx.demo_allowed(request):
            return {"source": "not_collected"}
        return {"source": "demo", "threads": demo.dialogs(ctx.today())}

    # ---- analytics ---------------------------------------------------------------------------

    @app.get("/api/admin/analytics")
    async def analytics(request: Request) -> dict:
        ctx.admin_of(request)
        days = ctx.period(request)
        t = ctx.now()
        since = t - days * 86400
        _, everyone = store.list_users(t, sort="created", descending=False, limit=MAX_SCAN)
        new = [u for u in everyone if u["createdAt"] >= since]
        by_day = Counter(datetime.fromtimestamp(u["createdAt"], UTC).strftime("%Y-%m-%d") for u in new)
        series = []
        for back in range(days - 1, -1, -1):
            day = datetime.fromtimestamp(t - back * 86400, UTC).strftime("%Y-%m-%d")
            series.append({"day": day, "new": by_day.get(day, 0)})
        # Registrations by weekday x hour, Baku time (UTC+4, no DST): real, from users.created_at.
        heat = [[0] * 24 for _ in range(7)]
        for u in new:
            local = datetime.fromtimestamp(u["createdAt"] + 4 * 3600, UTC)
            heat[local.weekday()][local.hour] += 1
        langs = Counter(u["lang"] for u in everyone)
        h = ctx.history()
        leagues: list[dict] = []
        if h:
            _, rows = h.list_forecasts(limit=MAX_SCAN)
            counts = Counter(ctx.tournament_name(r["tournamentId"]) for r in rows)
            leagues = [{"name": n, "count": c} for n, c in counts.most_common(8)]
        out: dict[str, Any] = {
            "period": days,
            "real": {"totalUsers": len(everyone), "newUsers": len(new), "newByDay": series,
                     "registrationHeat": heat, "languages": [{"lang": k, "count": v} for k, v in langs.most_common()],
                     "leagues": leagues, "tags": crm.all_tags()},
        }
        out["demo"] = demo.audience(days, ctx.today()) if ctx.demo_allowed(request) else None
        return out

    return crm
