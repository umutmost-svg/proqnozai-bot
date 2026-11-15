"""Admin web app (local pilot): own login (password + TOTP), own sessions, read access to the
Mini App data through the SAME classes the Mini App uses (Store, ForecastHistory).

Data sources (docs/miniapp/admin-dashboard.md §3):
- users and sessions: miniapp.db via Store (reads; the only write is the session revocation);
- forecasts and quality: miniapp.forecasts.db via ForecastHistory(read_only=True) and its evaluate();
- telemetry (activity, AI calls, API usage, errors) is NOT collected before D1: endpoints answer
  source="not_collected"; pilot-only demo telemetry is served on explicit request, source="demo".
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from miniapp.admin_web import ads_routes, crm_routes, demo, ops_routes, referral_routes, value_routes
from miniapp.admin_web.accounts import ActionError, Admin, AdminStore, AuthError, can
from miniapp.admin_web.config import EDGE_SECRET_HEADER, AdminWebConfig
from miniapp.forecast.history import GATE_MIN_FORECASTS, ForecastHistory
from miniapp.forecast.version import MODEL_VERSION
from miniapp.sports.football_data import KNOWN_COMPETITIONS
from miniapp.store import BurnWindow, Store

logger = logging.getLogger("miniapp.admin_web")

SESSION_COOKIE = "sporbot_admin_session"
PRE_COOKIE = "sporbot_admin_pre"
CSRF_HEADER = "x-csrf-token"
MAX_BODY = 16 * 1024
PAGE_MAX = 100
PERIODS = {1, 7, 30, 90}

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _err(code: str, status: int, request: Request) -> JSONResponse:
    return JSONResponse({"error": code, "requestId": request.state.request_id}, status_code=status)


def _finite(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def create_admin_app(config: AdminWebConfig, clock: Callable[[], float] = time.time) -> FastAPI:
    config.require_data()  # the pilot: its demo directory only; the server: the Mini App's data directory only
    SESSION_COOKIE, PRE_COOKIE = config.session_cookie, config.pre_cookie  # noqa: N806 — per mode
    secure = config.server  # Secure cookies over HTTPS (server); the pilot is plain http on loopback
    edge_secret = config.edge_secret.encode() if config.server and config.edge_secret else None
    accounts = AdminStore(config.admin_db)
    accounts.init_schema()
    store = Store(config.miniapp_db)
    app = FastAPI(title="SporBot Admin", docs_url=None, redoc_url=None, openapi_url=None)

    def now() -> int:
        return int(clock())

    def history() -> ForecastHistory | None:
        if not config.history_db.is_file():
            return None
        h = ForecastHistory(config.history_db, read_only=True)
        return h if h.is_forecast_history() else None

    def labels() -> dict[str, str]:
        try:
            return json.loads(config.labels_file.read_text())
        except (OSError, ValueError):
            return {}

    def demo_allowed(request: Request) -> bool:
        # Demo telemetry is a pilot feature only: the server never serves made-up figures, whatever is asked.
        return not config.server and request.query_params.get("demo") == "1"

    # ---- middleware: host, body size, origin + CSRF, headers -------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request.state.request_id = secrets.token_hex(8)
        request.state.admin = None
        if edge_secret is not None:
            # Only the edge (Cloudflare, behind Access) injects it: any other route to the process is refused
            # before anything else is looked at. Compared in constant time; its value is never logged.
            presented = request.headers.get(EDGE_SECRET_HEADER, "").encode()
            if not hmac.compare_digest(presented, edge_secret):
                return _err("forbidden", 403, request)
        if request.headers.get("host") not in config.hosts:
            return _err("bad_host", 421, request)  # DNS rebinding: only the loopback names are served
        path = request.url.path
        if path.startswith("/api/"):
            if int(request.headers.get("content-length") or 0) > MAX_BODY:
                return _err("too_large", 413, request)
            token = request.cookies.get(SESSION_COOKIE)
            session = accounts.session(token, now()) if token else None
            if session is not None:
                request.state.admin = session.admin
            if request.method not in ("GET", "HEAD"):
                if request.headers.get("origin") not in config.origins:
                    return _err("bad_origin", 403, request)
                if session is not None and path != "/api/admin/auth/login":
                    presented = request.headers.get(CSRF_HEADER) or ""
                    if not secrets.compare_digest(accounts.csrf_for(token), presented):
                        return _err("csrf", 403, request)
            public = path in ("/api/admin/health", "/api/admin/auth/login", "/api/admin/auth/totp",
                              "/api/admin/auth/logout")
            if not public and session is None:
                return _err("unauthenticated", 401, request)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 — never leak internals; the type is enough for the log
            logger.error("event=unhandled_error request_id=%s type=%s", request.state.request_id, type(exc).__name__)
            response = _err("internal", 500, request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if config.server:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def admin_of(request: Request, permission: str = "read") -> Admin:
        admin: Admin = request.state.admin
        if not can(admin.role, permission):
            raise ActionError("forbidden", 403)
        return admin

    @app.exception_handler(ActionError)
    async def action_error(request: Request, exc: ActionError) -> JSONResponse:
        return _err(exc.code, exc.status, request)

    async def body(request: Request) -> dict:
        raw = bytearray()
        async for chunk in request.stream():  # the limit holds without Content-Length (chunked) too
            raw += chunk
            if len(raw) > MAX_BODY:
                raise ActionError("too_large", 413)
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            raise ActionError("bad_json") from None
        if not isinstance(data, dict):
            raise ActionError("bad_json")
        return data

    def dangerous(data: dict) -> tuple[str, str, str]:
        reason = str(data.get("reason") or "").strip()
        code = str(data.get("code") or "").strip()
        key = str(data.get("idempotencyKey") or "").strip()
        if not 3 <= len(reason) <= 300:
            raise ActionError("reason_required")
        if not 8 <= len(key) <= 80:
            raise ActionError("idempotency_key_required")
        return reason, code, key

    def step_up_guard(admin: Admin, fn: Callable[[], dict]) -> dict:
        try:
            return fn()
        except ActionError as exc:
            if exc.code == "invalid_code":
                accounts.step_up_failed(admin.id, now())
            raise

    # ---- auth --------------------------------------------------------------------------------

    @app.get("/api/admin/health", response_model=None)
    async def health(request: Request) -> dict | JSONResponse:
        # Healthy only if both databases it needs can be read (the deploy rolls back on an unhealthy task).
        try:
            store.ping()
            accounts.admins()
        except sqlite3.Error:
            return _err("unavailable", 503, request)
        return {"status": "ok"}

    @app.post("/api/admin/auth/login")
    async def login(request: Request) -> Response:
        data = await body(request)
        try:
            pre = accounts.login_password(str(data.get("email") or ""), str(data.get("password") or ""), now())
        except AuthError as exc:
            return _err(str(exc), 401, request)
        response = JSONResponse({"next": "totp"})
        response.set_cookie(PRE_COOKIE, pre, max_age=300, httponly=True, samesite="strict", path="/api/admin/auth",
                            secure=secure)
        return response

    @app.post("/api/admin/auth/totp")
    async def login_totp(request: Request) -> Response:
        data = await body(request)
        pre = request.cookies.get(PRE_COOKIE)
        if not pre:
            return _err("expired", 401, request)
        try:
            token = accounts.login_totp(pre, str(data.get("code") or ""), now())
        except AuthError as exc:
            status = {"locked": 423, "expired": 401}.get(str(exc), 401)
            response = _err(str(exc), status, request)
            if str(exc) != "invalid_code":
                response.delete_cookie(PRE_COOKIE, path="/api/admin/auth", secure=secure, httponly=True,
                                       samesite="strict")
            return response
        response = JSONResponse({"ok": True})
        response.delete_cookie(PRE_COOKIE, path="/api/admin/auth", secure=secure, httponly=True, samesite="strict")
        # The pilot is plain http on loopback (no Secure); the server: `__Host-` + Secure, Path=/, no Domain.
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/", secure=secure)
        return response

    @app.post("/api/admin/auth/logout")
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            accounts.logout(token, now())
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/admin/auth/me")
    async def me(request: Request) -> dict:
        admin = admin_of(request)
        return {"admin": {"id": admin.id, "email": admin.email, "role": admin.role,
                          "grants": accounts.grants_of(admin.id)},
                "csrf": accounts.csrf_for(request.cookies[SESSION_COOKIE]),
                "pilot": {"dataset": config.dataset(), "demoTelemetry": not config.server}}

    # ---- helpers -----------------------------------------------------------------------------

    def period(request: Request) -> int:
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            raise ActionError("bad_period") from None
        if days not in PERIODS:
            raise ActionError("bad_period")
        return days

    def page(request: Request) -> tuple[int, int]:
        try:
            size = min(PAGE_MAX, max(1, int(request.query_params.get("size", "25"))))
            number = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            raise ActionError("bad_page") from None
        return size, (number - 1) * size

    def tournament_name(tid: str) -> str:
        code = tid.split(":", 1)[-1]
        return KNOWN_COMPETITIONS.get(code, (tid,))[0]

    def label_rows(rows: list[dict]) -> list[dict]:
        names = labels()
        for row in rows:
            row["match"] = names.get(row["eventId"])
            row["tournament"] = tournament_name(row["tournamentId"])
        return rows

    def today() -> datetime:
        return datetime.fromtimestamp(now(), UTC).replace(microsecond=0)

    def quality(h: ForecastHistory | None) -> dict:
        """Quality per model version; the 300-match gate is per version, never a sum across versions."""
        if h is None:
            return {"settled": 0, "minimum": GATE_MIN_FORECASTS, "currentVersion": MODEL_VERSION,
                    "currentGateForecasts": 0, "versions": {}}
        evaluated = _finite(h.evaluate())
        current = evaluated.get(MODEL_VERSION, {}).get("prospectiveGate", {}).get("forecasts", 0)
        return {"settled": h.status_counts()["settled"], "minimum": GATE_MIN_FORECASTS,
                "currentVersion": MODEL_VERSION, "currentGateForecasts": current, "versions": evaluated}

    # ---- overview ----------------------------------------------------------------------------

    @app.get("/api/admin/overview")
    async def overview(request: Request) -> dict:
        admin_of(request)
        days = period(request)
        since = now() - days * 86400
        h = history()
        counts = h.status_counts() if h else {"pending": 0, "settled": 0, "void": 0}
        upcoming: list[dict] = []
        in_period = 0
        if h:
            _, rows = h.list_forecasts(status="pending", kickoff_from=today().strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       sort="kickoff", descending=False, limit=5)
            upcoming = label_rows(rows)
            in_period, _ = h.list_forecasts(
                created_since=datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), limit=1)
        return {
            "period": days,
            "users": store.user_counts(now(), since),
            "forecasts": {**counts, "total": sum(counts.values()), "createdInPeriod": in_period},
            "quality": quality(h),
            "upcoming": upcoming,
            "telemetry": demo.telemetry(days, today()) if demo_allowed(request) else {"source": "not_collected"},
        }

    # ---- users -------------------------------------------------------------------------------

    @app.post("/api/admin/users/{user_id}/sessions/revoke")
    async def revoke(user_id: int, request: Request) -> Response:
        admin = admin_of(request, "manage_users")
        reason, code, key = dangerous(await body(request))
        if store.user_profile(user_id, now()) is None:
            return _err("not_found", 404, request)
        rid = request.state.request_id
        step = step_up_guard(admin, lambda: accounts.revoke_requested(admin, user_id, reason, key, code, now(), rid))
        if step.get("replayed"):
            return JSONResponse(step)
        t = now()
        # No await between the permission check above and this write: the pilot's single process cannot
        # interleave another request (e.g. a demotion) here. Revocation is idempotent on resume.
        revoked = store.revoke_user_sessions(user_id, t, BurnWindow.for_policy(t, 300, 30))
        result = accounts.revoke_completed(admin, user_id, revoked, t, step["requestId"])
        return JSONResponse({**result, "resumed": step["resumed"]})

    # ---- forecasts ---------------------------------------------------------------------------

    @app.get("/api/admin/forecasts")
    async def forecasts(request: Request) -> dict:
        admin_of(request)
        q = request.query_params
        size, offset = page(request)
        h = history()
        if h is None:
            return {"total": 0, "page": 1, "size": size, "items": [], "tournaments": [], "available": False}
        search = q.get("q", "").strip().casefold()[:64]
        event_ids = None
        if search:
            event_ids = [e for e, name in labels().items() if search in name.casefold()] + (
                [search] if search.startswith("fd:") else [])
        days = q.get("days")
        created_since = None
        if days and days != "all":
            created_since = (today() - timedelta(days=period(request))).strftime("%Y-%m-%dT%H:%M:%SZ")
        total, rows = h.list_forecasts(status=q.get("status") or None, tournament=q.get("tournament") or None,
                                       created_since=created_since, event_ids=event_ids,
                                       sort=q.get("sort", "kickoff"), descending=q.get("order", "desc") != "asc",
                                       limit=size, offset=offset)
        return {"total": total, "page": offset // size + 1, "size": size, "items": label_rows(rows),
                "tournaments": [{"id": t, "name": tournament_name(t)} for t in h.tournaments()], "available": True}

    @app.get("/api/admin/forecasts/summary")
    async def forecasts_summary(request: Request) -> dict:
        admin_of(request)
        h = history()
        counts = h.status_counts() if h else {"pending": 0, "settled": 0, "void": 0}
        q = quality(h)
        for version in q["versions"].values():
            version["byTournament"] = {tournament_name(t): v for t, v in version["byTournament"].items()}
        return {"counts": {**counts, "total": sum(counts.values())}, "quality": q, "available": h is not None}

    @app.get("/api/admin/forecasts/{key:path}")
    async def forecast(key: str, request: Request) -> Response:
        admin_of(request)
        h = history()
        detail = h.forecast_detail(key) if h else None
        if detail is None:
            return _err("not_found", 404, request)
        return JSONResponse(label_rows([detail])[0])

    # ---- telemetry-backed sections -----------------------------------------------------------

    @app.get("/api/admin/ai")
    async def ai(request: Request) -> dict:
        admin_of(request)
        days = period(request)
        if not demo_allowed(request):
            return {"source": "not_collected"}
        calls = demo.ai_calls(days, today())
        if not can(request.state.admin.role, "read_personal"):
            for c in calls:
                c["userId"] = None  # analysts see aggregates without identifiers (§4.8)
        return {"source": "demo", "calls": calls}

    @app.get("/api/admin/sports-api")
    async def sports_api(request: Request) -> dict:
        admin_of(request)
        days = period(request)
        if not demo_allowed(request):
            return {"source": "not_collected"}
        return {"source": "demo", "providers": demo.api_providers(today()),
                "series": demo.telemetry(days, today())["series"]}

    @app.get("/api/admin/system")
    async def system(request: Request) -> dict:
        admin_of(request)
        checks = {}
        for name, path in (("miniapp.db", config.miniapp_db), ("forecasts.db", config.history_db),
                           ("admin.db", config.admin_db)):
            try:
                size = path.stat().st_size
                wal = Path(f"{path}-wal")
                checks[name] = {"ok": True, "bytes": size, "walBytes": wal.stat().st_size if wal.exists() else 0}
            except OSError:
                checks[name] = {"ok": False, "bytes": None, "walBytes": None}
        try:
            store.ping()
            health_state = "ok"
        except sqlite3.Error:
            health_state = "unavailable"
        out: dict[str, Any] = {"health": health_state, "databases": checks, "build": None}
        if demo_allowed(request):
            out.update({"source": "demo", "events": demo.system_events(today()), "tasks": demo.tasks(today())})
        else:
            out["source"] = "not_collected"
        return out

    # ---- settings, admins, journal -----------------------------------------------------------

    @app.get("/api/admin/settings")
    async def settings_view(request: Request) -> dict:
        admin_of(request)
        return {"settings": accounts.settings(), "publishing": "pilot_not_published",
                "credits": {"enabled": False, "forecastPrice": None}}

    @app.post("/api/admin/settings/{key}")
    async def settings_update(key: str, request: Request) -> Response:
        admin = admin_of(request, "manage_settings")
        data = await body(request)
        reason, code, idem = dangerous(data)
        result = step_up_guard(admin, lambda: accounts.set_setting(admin, key, data.get("value"), reason, idem,
                                                                   code, now()))
        return JSONResponse(result)

    @app.get("/api/admin/admins")
    async def admins(request: Request) -> dict:
        admin_of(request, "read_personal")  # e-mails of colleagues: not for analysts
        return {"items": accounts.admins()}

    @app.post("/api/admin/admins")
    async def admins_invite(request: Request) -> Response:
        admin = admin_of(request, "manage_admins")
        data = await body(request)
        reason, code, key = dangerous(data)
        return JSONResponse(step_up_guard(admin, lambda: accounts.invite_admin(
            admin, str(data.get("email") or ""), str(data.get("role") or ""), reason, key, code, now())))

    @app.post("/api/admin/admins/{target_id}")
    async def admins_update(target_id: int, request: Request) -> Response:
        admin = admin_of(request, "manage_admins")
        data = await body(request)
        reason, code, key = dangerous(data)
        role = data.get("role")
        state = data.get("state")
        return JSONResponse(step_up_guard(admin, lambda: accounts.update_admin(
            admin, target_id, str(role) if role else None, str(state) if state else None, reason, key, code, now())))

    @app.post("/api/admin/admins/{target_id}/grants")
    async def admins_grant(target_id: int, request: Request) -> Response:
        admin = admin_of(request, "manage_admins")
        data = await body(request)
        reason, code, key = dangerous(data)
        granted = data.get("granted")
        return JSONResponse(step_up_guard(admin, lambda: accounts.set_grant(
            admin, target_id, str(data.get("permission") or ""), granted, reason, key, code, now())))

    @app.get("/api/admin/events")
    async def events(request: Request) -> dict:
        admin_of(request)
        q = request.query_params
        since = now() - period(request) * 86400
        personal = can(request.state.admin.role, "read_personal")
        try:
            # Analysts may not filter by actor: probing ids would reveal who did what.
            admin_id = int(q["admin"]) if q.get("admin") and personal else None
        except ValueError:
            raise ActionError("bad_filter") from None
        items = accounts.events(since=since, admin_id=admin_id, action=q.get("action") or None,
                                kind=q.get("result") or None, limit=500)
        if not personal:
            # Analysts: an explicit allowlist of fields (§4.8) — no actors, targets, values or reasons, whatever
            # the event type (group credit events, CRM events, future actions).
            items = [{"id": i["id"], "at": i["at"], "action": i["action"], "targetType": i["targetType"],
                      "result": i["result"], "adminId": None, "adminEmail": None, "targetId": None, "before": None,
                      "after": None, "reason": None} for i in items]
        return {"items": items}

    ctx = SimpleNamespace(
        accounts=accounts, store=store, now=now, today=today, page=page, period=period, admin_of=admin_of,
        body=body, dangerous=dangerous, step_up_guard=step_up_guard, demo_allowed=demo_allowed, history=history,
        tournament_name=tournament_name, dataset=config.dataset, access_file=config.access_policy_file,
        outbox_file=config.outbox_db, telemetry_file=config.telemetry_db, bot_crm_file=config.bot_crm_db,
        bot_username=config.bot_username, miniapp_short_name=config.miniapp_short_name,
        can_personal=lambda request: can(request.state.admin.role, "read_personal"))
    referrals = referral_routes.register(app, ctx)
    ctx.sources, ctx.referral_funnel = referrals.sources, referrals.funnel  # users list / card, product analytics
    crm_routes.register(app, ctx)
    ops_routes.register(app, ctx)
    value_routes.register(app, ctx, config.admin_dir / "bot-snapshot.json")
    ads_routes.register(app, ctx, config.ads_policy_file, config.ads_applied_file,  # after CRM: MB ids live there
                        config.telemetry_db)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def api_not_found(rest: str, request: Request) -> Response:
        return _err("not_found", 404, request)

    # ---- static frontend (same origin; no CDN) -----------------------------------------------

    if config.static_dir is not None:
        root = config.static_dir.resolve()

        @app.get("/{path:path}")
        async def spa(path: str) -> Response:
            candidate = (root / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-store"})

    return app
