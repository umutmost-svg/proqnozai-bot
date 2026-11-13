"""HTTP routes of the CRM layer (users with tags/segments, user 360, messages, broadcasts,
credits, analytics). Registered by app.create_admin_app; shares its guards and helpers."""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from miniapp.admin_web import demo
from miniapp.ads.policy import public_key_hex as policy_public_key_hex
from miniapp.admin_web.access import AccessPublisher
from miniapp.admin_web.outbox import Outbox
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

    # Finish revocations left half-done by a crash (block / session revoke: the request is journalled, the Mini
    # App write may be missing). Revocation is idempotent; each request completes exactly once.
    for pending in ctx.accounts.pending_requests():
        if pending["action"] in ("user.block", "user.sessions_revoke") and pending["targetId"]:
            t = ctx.now()
            store.revoke_user_sessions(int(pending["targetId"]), t, BurnWindow.for_policy(t, 300, 30))
            # The original attempt may have revoked sessions before crashing: the true count is unknown.
            ctx.accounts.complete_request(pending["action"], pending["adminId"], pending["targetId"], None, t,
                                          pending["requestId"])

    outbox = Outbox(ctx.outbox_file)
    outbox.init_schema()

    def queue_message(admin_id: int | None, user_id: int, msg_id: int, body: str, request_id: str) -> None:
        t = ctx.now()
        outbox.enqueue([(f"msg:{msg_id}", user_id, body, [])], batch_id=None, not_before=t, admin_event_id=None, now=t)
        crm.message_queued(admin_id, user_id, msg_id, request_id, t)

    for pending in crm.unqueued_messages():  # finish messages left between the two steps by a crash
        queue_message(pending["adminId"], pending["userId"], pending["messageId"], pending["body"], pending["requestId"])

    def queue_broadcast(item: dict, recipients: list[int]) -> int:
        """Jobs of a scheduled broadcast (one per recipient, sent by the bot from `scheduledAt`). Idempotent."""
        jobs = [(f"bc:{item['id']}:{uid}", uid, item["body"], item["buttons"]) for uid in recipients]
        return outbox.enqueue(jobs, batch_id=f"bc:{item['id']}", not_before=item["scheduledAt"], admin_event_id=None,
                              now=ctx.now())

    access = AccessPublisher(ctx.accounts, ctx.access_file)
    access.init_schema()

    def publish_access() -> dict | None:
        """After any block change (and at startup): the Mini App's list follows crm_blocks; a failed file write is
        repaired by the next call (status read, restart)."""
        try:
            return access.sync(ctx.now())
        except OSError:
            return None

    publish_access()

    def audience(rules: dict, query: str = "") -> list[dict]:
        t = ctx.now()
        _, users = store.list_users(t, query=query, sort="created", descending=True, limit=MAX_SCAN)
        tags = crm.tags_by_user()
        blocked = crm.blocked()
        return [u for u in users if matches(u, rules, set(tags.get(u["id"], [])), u["id"] in blocked, t)]

    def broadcast_audience(rules: dict) -> list[dict]:
        """Recipients of a broadcast: blocked users are always excluded."""
        return audience({**rules, "blocked": False})  # always: a rule cannot opt blocked users into a broadcast

    for item in crm.broadcasts():  # a schedule committed just before a crash: queue it now
        if item["status"] == "scheduled" and not outbox.batch_counts(f"bc:{item['id']}"):
            queue_broadcast(item, [u["id"] for u in broadcast_audience(item["rules"])])

    def decorate(rows: list[dict]) -> list[dict]:
        ids = [r["id"] for r in rows]
        tags = crm.tags_by_user(ids)
        credits = crm.credits_of(ids, ctx.now())
        blocked = crm.blocked()
        mb = crm.mb_ids(ids)
        for r in rows:
            r["mbId"] = mb.get(r["id"])
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
        query = q.get("q", "")[:64]
        rules = rules_from_query(request)
        rows = audience(rules, query)
        by_mb = crm.users_by_mb(query) - {r["id"] for r in rows}
        if by_mb:  # the search also finds an MB id
            rows += [r for r in audience(rules) if r["id"] in by_mb]
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
        for te in crm.tag_events(user_id):
            timeline.append({"at": te["at"], "kind": "admin", "by": te["by"], "detail": None,
                             "title": f"{'Добавлен' if te['action'] == 'add' else 'Снят'} тег {te['tag']}"})
        for e in events:
            if e["action"] == "user.mb_id":
                timeline.append({"at": e["at"], "kind": "admin", "by": e["adminEmail"], "detail": None,
                                 "title": "Указан ID MB" if (e["after"] or {}).get("set") else "ID MB удалён"})
                continue
            if e["action"] in ("user.block", "user.sessions_revoke"):
                if e["action"] == "user.sessions_revoke" and e["result"] != "completed":
                    continue
                if e["action"] == "user.block" and e["result"] == "completed":
                    continue
                title = {"user.block": "Доступ заблокирован" if (e["after"] or {}).get("blocked") else "Блокировка снята",
                         "user.sessions_revoke": "Отозваны сессии: " + (
                             "неизвестно (восстановлено после сбоя)" if (e["after"] or {}).get("revoked") is None
                             else str((e["after"] or {}).get("revoked")))}[e["action"]]
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
        _, everyone = store.list_users(ctx.now(), limit=MAX_SCAN)
        known = {u["id"] for u in everyone}
        if not set(ids) <= known:
            raise ActionError("not_found", 404)
        return {"tagged": crm.add_tags(admin, ids, data.get("tag"), ctx.now())}

    @app.post("/api/admin/users/{user_id}/tags")
    async def add_tag(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        return crm.add_tag(admin, user_id, (await ctx.body(request)).get("tag"), ctx.now())

    @app.post("/api/admin/users/{user_id}/tags/remove")
    async def remove_tag(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        return crm.remove_tag(admin, user_id, str((await ctx.body(request)).get("tag") or ""), ctx.now())

    @app.post("/api/admin/users/{user_id}/mb-id")
    async def set_mb_id(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        return crm.set_mb_id(admin, user_id, (await ctx.body(request)).get("mbId"), ctx.now())

    @app.post("/api/admin/users/{user_id}/notes")
    async def add_note(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        return crm.add_note(admin, user_id, (await ctx.body(request)).get("body"), ctx.now())

    @app.post("/api/admin/users/{user_id}/messages")
    async def message(user_id: int, request: Request) -> dict:
        """Queued for the bot (admin -> bot outbox); the admin process never calls Telegram itself."""
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        body = (await ctx.body(request)).get("body")
        result = crm.send_message(admin, user_id, body, ctx.now(), request.state.request_id)
        queue_message(admin.id, user_id, result["id"], body.strip(), result["requestId"])
        return {"id": result["id"], "status": "queued"}

    @app.post("/api/admin/users/{user_id}/block")
    async def block(user_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        exists(user_id)
        data = await ctx.body(request)
        reason, code, key = ctx.dangerous(data)
        flag = data.get("blocked")
        if not isinstance(flag, bool):
            raise ActionError("invalid_value")
        rid = request.state.request_id
        step = ctx.step_up_guard(admin, lambda: crm.set_block(admin, user_id, flag, reason, key, code, ctx.now(), rid))
        if "requestId" not in step:
            return {**step, "access": publish_access()}
        # No await between the checks above and this write (single pilot process). Sessions are revoked once per
        # request; a crash here is finished by the next retry or at startup (pending_requests).
        t = ctx.now()
        revoked = store.revoke_user_sessions(user_id, t, BurnWindow.for_policy(t, 300, 30))
        result = ctx.accounts.revoke_completed(admin, user_id, revoked, t, step["requestId"], action="user.block")
        return {**result, "after": {"blocked": True, "revoked": result["after"]["revoked"]},
                "revokedSessions": result["after"]["revoked"], "resumed": step["resumed"], "access": publish_access()}

    @app.get("/api/admin/access/publication")
    async def access_status(request: Request) -> dict:
        ctx.admin_of(request)
        status = publish_access() or {"fileWritten": False}
        return {**status, "publicKey": policy_public_key_hex(access.signing_key)}

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
        data = await ctx.body(request)
        rules = validate_rules(data.get("rules") or {})
        rows = broadcast_audience(rules) if data.get("forBroadcast") is True else audience(rules)
        return {"size": len(rows), "sample": [{"id": r["id"], "username": r["username"],
                                               "firstName": r["firstName"]} for r in rows[:5]],
                "langs": dict(Counter(r["lang"] for r in rows))}

    # ---- broadcasts --------------------------------------------------------------------------

    @app.get("/api/admin/broadcasts")
    async def broadcasts(request: Request) -> dict:
        ctx.admin_of(request, "read_personal")  # texts and audience rules are free text: not for analysts
        items = crm.broadcasts()
        for b in items:
            if b["status"] == "draft":
                b["audience"] = len(broadcast_audience(b["rules"]))
        for b in items:
            if b["status"] in ("scheduled", "cancelled"):
                b["delivery"] = outbox.batch_counts(f"bc:{b['id']}")  # queued / sent / failed … from the bot
        return {"items": items, "sending": "bot_outbox", "segments": crm.segments()}

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
        audience_ids = [u["id"] for u in broadcast_audience(item["rules"])]
        result = ctx.step_up_guard(admin, lambda: crm.schedule_broadcast(admin, broadcast_id, at, len(audience_ids),
                                                                         reason, key, code, ctx.now()))
        # Also on a replay: a crash between the schedule and the queue is finished by the retry (keys are idempotent).
        queue_broadcast({**item, "scheduledAt": at}, audience_ids)
        return result

    @app.post("/api/admin/broadcasts/{broadcast_id}/cancel")
    async def cancel(broadcast_id: int, request: Request) -> dict:
        admin = ctx.admin_of(request, "manage_users")
        reason, code, key = ctx.dangerous(await ctx.body(request))
        result = ctx.step_up_guard(admin, lambda: crm.cancel_broadcast(admin, broadcast_id, reason, key, code,
                                                                       ctx.now()))
        # Jobs the bot has not claimed yet are cancelled (also on a replay: idempotent).
        return {**result, "cancelledJobs": outbox.cancel_batch(f"bc:{broadcast_id}")}

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
        admin = ctx.admin_of(request, "read_personal")
        if not ctx.accounts.has_grant(admin.id, "view_dialogs"):
            raise ActionError("dialogs_not_allowed", 403)  # an explicit grant per admin, never implied by a role
        if not ctx.demo_allowed(request):
            return {"source": "not_collected"}
        threads = demo.dialogs(ctx.today())
        for th in threads:  # demo texts, but names from the (demo) users they link to
            profile = store.user_profile(th["userId"], ctx.now())
            if profile:
                th["name"] = " ".join(p for p in (profile["firstName"], profile["lastName"]) if p) or th["name"]
                th["username"] = profile["username"] or ""
        return {"source": "demo", "threads": threads}

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
        first = datetime.fromtimestamp(since, UTC).date()
        last = datetime.fromtimestamp(t, UTC).date()
        for offset in range((last - first).days + 1):  # every UTC day the rolling window touches
            day = (first + timedelta(days=offset)).strftime("%Y-%m-%d")
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
            _, rows = h.list_forecasts(created_since=datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       limit=MAX_SCAN)
            counts = Counter(ctx.tournament_name(r["tournamentId"]) for r in rows)
            leagues = [{"name": n, "count": c} for n, c in counts.most_common(8)]
        personal = ctx.can_personal(request)
        out: dict[str, Any] = {
            "period": days,
            # "measured" = computed from the Mini App files the panel reads; in the pilot that is the demo set.
            "dataset": ctx.dataset(),
            "real": {"totalUsers": len(everyone), "newUsers": len(new), "newByDay": series,
                     "registrationHeat": heat, "languages": [{"lang": k, "count": v} for k, v in langs.most_common()],
                     "leagues": leagues, "tags": crm.all_tags() if personal else []},
        }
        out["demo"] = demo.audience(days, ctx.today()) if ctx.demo_allowed(request) else None
        return out

    return crm
