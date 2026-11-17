"""FastAPI application of the Mini App API (M2: Telegram auth + session).

`create_app()` builds everything from explicit arguments; importing this
module creates no database, reads no env and starts nothing.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from miniapp.credits import COST, Busy, Credits, MbError, NoCredits
from miniapp.security import ACCESS_POLICY_FILE, AccessPolicy, CsrfTokens, LoginRateLimiter, origin_allowed
from miniapp.settings import Settings
from miniapp.ai.analysis import AI_ERROR_CODES, AiError, AiService, build_ai_service
from miniapp.forecast.history import ForecastHistory, history_path
from miniapp.forecast.settlement import Settlement
from miniapp.odds.feed import FeedReceiver
from miniapp.odds.routes import register_odds_routes
from miniapp.odds.source import OddsSource
from miniapp.odds.store import OddsStore
from miniapp.quick.routes import register_quick_routes
from miniapp.quick.service import QuickService
from miniapp.sports.client import UpstreamError
from miniapp.forecast.coverage import ModelCoverage
from miniapp.sports.routes import refused, register_sports_routes, upstream_response, user_share
from miniapp.sports.user_limits import UserUpstreamLimiter
from miniapp.sports.service import SportsService, build_service
from miniapp.store import SUPPORTED_LANGS, BurnWindow, InitDataConsumedError, Session, Store, User, referral_code
from miniapp.telegram_auth import TELEGRAM_PUBLIC_KEYS, InitDataError, ed25519_shadow_status, validate_init_data

logger = logging.getLogger("miniapp")

CSRF_HEADER = "X-CSRF-Token"
REQUEST_ID_HEADER = "X-Request-ID"
EDGE_SECRET_HEADER = "X-Edge-Auth"
HEALTH_CACHE_S = 5
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
# Auth and personal responses must never be cached by the WebView or a proxy.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# Vite puts content-hashed files under assets/: a new build gets new names.
_IMMUTABLE = "public, max-age=31536000, immutable"
# Public PNGs (no user data): the edge may cache them, so repeated requests never reach us.
_CREST_CACHE = "public, max-age=86400"
# Telegram Web (K and A) embeds the Mini App in an iframe; mobile and desktop
# clients use a native WebView, where frame-ancestors does not apply.
TELEGRAM_FRAME_ANCESTORS = "https://web.telegram.org"
TELEGRAM_SDK_ORIGIN = "https://telegram.org"


def content_security_policy(production: bool) -> str:
    """Policy for the built frontend: own files only, plus the Telegram SDK script.

    No 'unsafe-inline' anywhere: the build has no inline scripts or <style>
    blocks; React and the SDK change styles through CSSOM, which CSP allows.
    """
    directives = [
        "default-src 'none'",
        f"script-src 'self' {TELEGRAM_SDK_ORIGIN}",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "object-src 'none'",
        f"frame-ancestors {TELEGRAM_FRAME_ANCESTORS}",
    ]
    if production:
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


class ApiError(Exception):
    def __init__(self, status: int, code: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.headers = headers or {}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _public_user(user: User) -> dict[str, Any]:
    return {
        "id": user.telegram_user_id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username,
        "lang": user.lang,
        "lang_source": user.lang_source,
    }


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApiError(400, "bad_request")
        result[key] = value
    return result


def _require_json(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise ApiError(415, "unsupported_media_type")


async def _read_body(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
        raise ApiError(413, "payload_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ApiError(413, "payload_too_large")
    return bytes(body)


def _json_object(body: bytes, allowed: set[str]) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError(400, "bad_request") from None
    if not isinstance(data, dict) or set(data) != allowed:
        raise ApiError(400, "bad_request")
    return data


async def _read_json_object(request: Request, max_bytes: int, allowed: set[str],
                            optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Strict JSON contract: application/json, bounded size, exact key set (`optional` keys may be absent)."""
    _require_json(request)
    body = await _read_body(request, max_bytes)
    try:
        keys = set(json.loads(body.decode("utf-8")))  # only to pick the expected set; parsed strictly below
    except (UnicodeDecodeError, ValueError, RecursionError, TypeError):
        keys = set()
    return _json_object(body, allowed | (keys & optional))


# Telegram WebApp.platform values (the client reports it; anything else is stored as "other").
TELEGRAM_PLATFORMS = frozenset({"android", "android_x", "ios", "macos", "tdesktop", "web", "weba", "webk",
                                "unigram", "unknown"})


def _edge_country(request: Request, trusted: bool) -> str | None:
    """CF-IPCountry, only when every request must come through the edge (else a client could set it)."""
    if not trusted:
        return None
    raw = request.headers.get("cf-ipcountry", "").strip()
    if not (len(raw) == 2 and raw.isascii() and raw.isalpha()):  # checked before uppercasing ("ß" -> "SS")
        return None
    value = raw.upper()
    return value if value not in ("XX", "T1") else None


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """SQLITE_BUSY by result code: another connection held the write lock past busy_timeout (overload).
    SQLITE_LOCKED (a conflict inside one connection) and everything else remain internal errors."""
    return getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY


def _error_site(exc: BaseException) -> str:
    """`file:line` of the innermost frame: code location only, never data."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is None:
        return "unknown"
    return f"{tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno}"


SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


def create_app(
    settings: Settings,
    *,
    clock: Callable[[], float] = time.time,
    store: Store | None = None,
    sports: SportsService | None = None,
    ai: AiService | None = None,
    odds_source_enabled: bool = True,  # tests switch the Mostbet source off (no network)
) -> FastAPI:
    store = store or Store(settings.db_path)
    store.init_schema()
    store.purge_expired(int(clock()))
    logger.info("startup access_mode=%s blocked=%s", "public" if settings.allowed_user_ids is None
                else f"allowlist({len(settings.allowed_user_ids)})", len(settings.blocked_user_ids))
    csrf = CsrfTokens(settings.csrf_key_material)
    credits = Credits(store, clock)
    # One replica, stop-first deploys: a hold left by the previous process produced nothing for its user.
    if released := credits.release_all_holds():
        logger.info("credits.holds_released count=%d", released)

    def no_credits(balance: int) -> JSONResponse:
        return JSONResponse({"error": "no_credits", "balance": balance, "cost": COST}, status_code=402)

    def verify_launch(init_data: str):
        """initData by the deployment's algorithm: HMAC with the bot token, or Ed25519 without it."""
        if settings.initdata_mode == "ed25519":
            assert settings.bot_id is not None  # guaranteed by Settings
            return validate_init_data(
                init_data, now=clock(), max_age_s=settings.init_data_max_age_s,
                future_skew_s=settings.init_data_future_skew_s, max_chars=settings.max_init_data_chars,
                ed25519=(settings.bot_id, TELEGRAM_PUBLIC_KEYS[settings.telegram_env]))
        return validate_init_data(
            init_data, settings.bot_token, now=clock(), max_age_s=settings.init_data_max_age_s,
            future_skew_s=settings.init_data_future_skew_s, max_chars=settings.max_init_data_chars)
    limiter = LoginRateLimiter(settings.login_rate_limit, settings.login_rate_window_s, clock)

    app = FastAPI(title="Spor AI Mini App API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.limiter = limiter

    def now() -> int:
        return int(clock())

    production = settings.environment == "production"
    csp_header = "Content-Security-Policy" + ("-Report-Only" if settings.csp_mode == "report-only" else "")
    csp_value = content_security_policy(production)
    edge_secret = settings.edge_secret.encode() if settings.edge_secret else None

    def edge_allowed(request: Request) -> bool:
        if edge_secret is None:
            return True
        # The platform healthcheck reaches the container directly, without the edge.
        if request.method == "GET" and request.url.path == "/api/health":
            return True
        presented = request.headers.get(EDGE_SECRET_HEADER, "").encode()
        return hmac.compare_digest(presented, edge_secret)

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Any]) -> Response:
        # Server-generated id only: a client-supplied one could forge log lines.
        request_id = secrets.token_hex(8)
        try:
            if not edge_allowed(request):
                # Reached past the edge (platform domain, direct address): no service.
                logger.warning("event=edge_bypass_blocked request_id=%s", request_id)
                response: Response = JSONResponse({"error": "forbidden"}, status_code=403)
            else:
                response = await call_next(request)
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                logger.error("event=unhandled_error request_id=%s type=%s site=%s",
                             request_id, type(exc).__name__, _error_site(exc))
                response = JSONResponse({"error": "internal", "request_id": request_id}, status_code=500)
            else:
                # The database stayed locked past busy_timeout (overload): a retryable answer, not a bug.
                logger.warning("event=db_busy request_id=%s site=%s", request_id, _error_site(exc))
                response = JSONResponse({"error": "busy"}, status_code=503, headers={"Retry-After": "2"})
        except Exception as exc:
            # Handled here, not re-raised: Starlette's ServerErrorMiddleware would
            # re-raise to uvicorn, which logs the full traceback with the exception
            # message (library messages can echo request data). Type and code
            # location are enough to find the bug; the id links it to a user report.
            logger.error("event=unhandled_error request_id=%s type=%s site=%s",
                         request_id, type(exc).__name__, _error_site(exc))
            response = JSONResponse({"error": "internal", "request_id": request_id}, status_code=500)
        response.headers.update(_SECURITY_HEADERS)
        if request.url.path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = _IMMUTABLE
        elif request.url.path.startswith("/api/crests/") and response.status_code == 200:
            # A crest is a verified public PNG with no user data: cacheable for a day, also at the edge.
            response.headers.setdefault("Cache-Control", _CREST_CACHE)  # a stand-in flag sets a shorter one
        else:
            response.headers.update(_NO_STORE)
        # A crest SVG opened directly is a document: nothing in it may run or load (style attributes only).
        svg = response.headers.get("content-type", "").startswith("image/svg+xml")
        if svg:
            # Always ENFORCED for an SVG, whatever the site's CSP mode (report-only would not block anything).
            response.headers["Content-Security-Policy"] = SVG_CSP
        else:
            response.headers[csp_header] = csp_value
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status, headers=exc.headers)

    def require_origin(request: Request) -> None:
        if not origin_allowed(request.headers.get("origin"), settings.trusted_origins):
            raise ApiError(403, "origin_not_allowed")

    def mark_partitioned(response: Response) -> None:
        # SameSite=None cookies live in the Telegram Web iframe, where browsers need
        # CHIPS (Partitioned). Starlette's `partitioned=` raises before Python 3.14
        # (http.cookies lacks the attribute), so the flag is appended to the header
        # that set_cookie/delete_cookie just added.
        if settings.cookie_samesite != "none":
            return
        name, value = response.raw_headers[-1]
        if name != b"set-cookie":
            raise RuntimeError("expected the session Set-Cookie header last")
        response.raw_headers[-1] = (name, value + b"; Partitioned")

    def set_session_cookie(response: Response, token: str, expires_at: int) -> None:
        response.set_cookie(
            settings.cookie_name,
            token,
            max_age=max(0, expires_at - now()),
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite=settings.cookie_samesite,
        )
        mark_partitioned(response)

    def clear_session_cookie(response: Response) -> None:
        response.delete_cookie(
            settings.cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite=settings.cookie_samesite,
        )
        mark_partitioned(response)

    # Users blocked in the admin panel (signed access-policy.json): no sign-in, sessions stop (≤ 30 s); data stays.
    access = (AccessPolicy(settings.db_path.parent / ACCESS_POLICY_FILE, settings.access_policy_public_key, clock)
              if settings.access_policy_public_key else None)

    def is_blocked(user_id: int) -> bool:
        return user_id in settings.blocked_user_ids or (access is not None and user_id in access.blocked())

    async def unrevoked_session(request: Request) -> tuple[str, Session] | None:
        """Session from the cookie that was not revoked, expired or not."""
        token = request.cookies.get(settings.cookie_name)
        if not token or len(token) > 128:
            return None
        session = await run_in_threadpool(store.get_session, token)
        if session is None or session.revoked_at is not None:
            return None
        return token, session

    async def current_session(request: Request) -> tuple[str, Session] | None:
        """Active (not revoked, not expired) session from the cookie, else None."""
        found = await unrevoked_session(request)
        if found is None or found[1].expires_at <= now():
            return None
        if settings.allowed_user_ids is not None and found[1].user_id not in settings.allowed_user_ids:
            return None  # closed launch: a session of someone taken off the list stops working at once
        if is_blocked(found[1].user_id):
            return None  # blocked: an existing session stops working at once
        return found

    # Ads telemetry (activity segments, exposures): exists only with MINIAPP_ADS_ENABLED=true. The ads package
    # is imported only then: an image built without it keeps working with the flag off.
    telemetry: Any = None
    picks: Any = None
    if settings.ads_enabled:
        from miniapp.ads.service import RecentPicks
        from miniapp.ads.telemetry import Telemetry, telemetry_path

        try:
            telemetry = Telemetry(telemetry_path(settings.db_path))
            telemetry.init_schema()
            telemetry.maybe_purge(now())
            picks = RecentPicks(telemetry)
            app.state.ads_picks = picks
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            # Non-authoritative data must not stop the Mini App: without telemetry there are no ads (routes 404).
            logger.error("event=ads_telemetry_unavailable type=%s", type(exc).__name__)
            telemetry = None

    def note_activity(user_id: int, field: str = "requests") -> None:
        """Neutral app activity for ads segments; never breaks the request it is attached to."""
        if telemetry is None:
            return
        try:
            telemetry.record_activity(user_id, now(), field)
            telemetry.maybe_purge(now())
        except sqlite3.Error as exc:
            logger.warning("event=telemetry_write_failed type=%s", type(exc).__name__)

    def note_edge(user_id: int, country: str | None, platform: str | None) -> None:
        """Sign-in country (edge) and Telegram platform for the admin's cards; never breaks the sign-in."""
        if telemetry is None:
            return
        try:
            telemetry.record_edge(user_id, country, platform, now())
        except sqlite3.Error as exc:
            logger.warning("event=telemetry_write_failed type=%s", type(exc).__name__)

    def session_payload(user: User, token: str, session: Session) -> dict[str, Any]:
        return {
            "user": _public_user(user),
            "session": {"expires_at": _iso(session.expires_at), "csrf_token": csrf.for_session(token)},
        }

    # The probe is exempt from the edge secret, so it is reachable around the edge:
    # a success is reused for HEALTH_CACHE_S and a flood of probes stays off SQLite.
    # Failures are never cached, so recovery shows up on the next probe.
    health_ok_at: list[float] = []

    @app.get("/api/health")
    async def health() -> JSONResponse:
        # Gates deploys: a release that cannot read its database never goes live.
        if health_ok_at and 0 <= clock() - health_ok_at[0] < HEALTH_CACHE_S:
            return JSONResponse({"status": "ok"})
        try:
            await run_in_threadpool(store.ping)
        except sqlite3.Error as exc:
            health_ok_at.clear()
            logger.error("event=health_db_error type=%s", type(exc).__name__)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        health_ok_at[:] = [clock()]
        return JSONResponse({"status": "ok"})

    def burn_window(ts: int) -> BurnWindow:
        return BurnWindow.for_policy(ts, settings.init_data_max_age_s, settings.init_data_future_skew_s)

    @app.post("/api/auth/session")
    async def create_session(request: Request) -> JSONResponse:
        # Login CSRF: only our own page may post, and only with a JSON body,
        # which a cross-site form cannot produce without a CORS preflight we never answer.
        require_origin(request)
        if not settings.logins_enabled:
            # Emergency switch (MINIAPP_LOGINS_ENABLED=false): existing sessions keep working.
            raise ApiError(503, "logins_disabled")
        body = await _read_json_object(request, settings.max_body_bytes, {"init_data"}, frozenset({"platform"}))
        init_data = body["init_data"]
        platform = body.get("platform")
        if not isinstance(init_data, str) or not (platform is None or (isinstance(platform, str) and len(platform) <= 32)):
            raise ApiError(400, "bad_request")
        try:
            verified = verify_launch(init_data)
        except InitDataError as exc:
            logger.info("auth.rejected reason=%s", exc.code)
            raise ApiError(401, "init_data_expired" if exc.expired else "init_data_invalid") from None
        if settings.initdata_mode == "hmac" and settings.bot_id is not None:
            # Readiness for the token-free Ed25519 mode, measured on real clients: a result code only.
            logger.info("auth.ed25519_shadow result=%s", ed25519_shadow_status(
                init_data, settings.bot_id, TELEGRAM_PUBLIC_KEYS[settings.telegram_env]))

        # Limited per VERIFIED Telegram user, after the signature check. The peer
        # address is not used: behind a reverse proxy it is the proxy's, shared by
        # everyone. Unsigned traffic cannot reach this budget; flooding with it is
        # cut at the edge proxy, which sees the real client IP (docs/miniapp/auth.md).
        ts = now()
        # Burned data is rejected before the limit, so replaying it cannot spend the
        # user's budget. Not the final word: create_session re-checks atomically.
        burned = [i for i in (verified.launch_id, verified.legacy_id) if i is not None]
        if any([await run_in_threadpool(store.is_init_data_consumed, i, ts) for i in burned]):
            logger.info("auth.rejected reason=reused_after_logout")
            raise ApiError(401, "init_data_reused")
        if settings.allowed_user_ids is not None and verified.user.id not in settings.allowed_user_ids:
            # Closed launch: nothing about this user is stored (no user row, no session).
            logger.info("auth.rejected reason=not_allowed")
            raise ApiError(403, "not_allowed")
        if is_blocked(verified.user.id):
            # Same answer as the closed list; nothing about this user is stored.
            logger.info("auth.rejected reason=blocked")
            raise ApiError(403, "not_allowed")
        retry_after = limiter.hit(f"user:{verified.user.id}")
        if retry_after is not None:
            logger.warning("auth.rate_limited user=%s", verified.user.id)
            raise ApiError(429, "rate_limited", {"Retry-After": str(int(retry_after + 0.999))})

        referral = referral_code(verified.start_param) if settings.referrals_enabled else None
        user = await run_in_threadpool(store.upsert_user, verified.user, ts, referral)

        existing = await current_session(request)
        if existing is not None and existing[1].user_id == user.telegram_user_id:
            # Idempotent bootstrap: a live session for the same user is reused.
            token, session = existing
            created = False
        else:
            if existing is not None:
                # The device switched Telegram accounts: the old session ends, and its
                # launch stays burned for the whole initData validity window (as on logout),
                # so the old account's still-fresh initData cannot sign in again.
                await run_in_threadpool(store.revoke_session, existing[0], ts, burn_window(ts).until)
            token = secrets.token_urlsafe(32)
            try:
                session, evicted = await run_in_threadpool(
                    store.create_session, token, user.telegram_user_id, verified.launch_id, ts,
                    settings.session_ttl_s, settings.max_sessions_per_user, burn_window(ts),
                )
            except InitDataConsumedError:
                logger.info("auth.rejected reason=reused_after_logout")
                raise ApiError(401, "init_data_reused") from None
            if evicted:
                logger.info("auth.session_evicted user=%s count=%s", user.telegram_user_id, evicted)
            created = True
        logger.info("auth.session user=%s created=%s", user.telegram_user_id, created)
        await run_in_threadpool(note_activity, user.telegram_user_id)
        await run_in_threadpool(note_edge, user.telegram_user_id, _edge_country(request, settings.edge_secret is not None),
                                (platform if platform in TELEGRAM_PLATFORMS else "other") if platform else None)
        response = JSONResponse({**session_payload(user, token, session), "created": created})
        set_session_cookie(response, token, session.expires_at)
        return response

    @app.get("/api/me")
    async def me(request: Request) -> JSONResponse:
        token = request.cookies.get(settings.cookie_name)
        active = await current_session(request)
        if active is None:
            code = "unauthenticated"
            if token and len(token) <= 128:
                stale = await run_in_threadpool(store.get_session, token)
                if stale is not None and stale.revoked_at is None and stale.expires_at <= now():
                    code = "session_expired"
            response = JSONResponse({"error": code}, status_code=401)
            if token:
                clear_session_cookie(response)
            return response
        token, session = active
        user = await run_in_threadpool(store.get_user, session.user_id)
        if user is None:
            raise ApiError(401, "unauthenticated")
        await run_in_threadpool(note_activity, session.user_id)
        return JSONResponse(session_payload(user, token, session))

    @app.post("/api/me/language")
    async def set_language(request: Request) -> JSONResponse:
        require_origin(request)
        active = await current_session(request)
        if active is None:
            raise ApiError(401, "unauthenticated")
        token, session = active
        if not csrf.verify(token, request.headers.get(CSRF_HEADER)):
            raise ApiError(403, "csrf_failed")
        body = await _read_json_object(request, 1024, {"lang"})
        if body["lang"] not in SUPPORTED_LANGS:
            raise ApiError(400, "bad_request")
        await run_in_threadpool(store.set_language, session.user_id, body["lang"])
        return JSONResponse({"lang": body["lang"]})

    def launch_ids_to_burn(raw: str, user_id: int | None) -> list[str]:
        """Ids of the current launch if it could still be exchanged (the launch id, plus the legacy
        `hash` id in HMAC mode so releases from before the common id keep it burned too).

        `user_id`: owner required by the session; None when there is no session.
        """
        try:
            launch = verify_launch(raw)
        except InitDataError as exc:
            # Invalid or expired data cannot be exchanged anyway: nothing to burn.
            logger.info("auth.logout launch_not_burned reason=%s", exc.code)
            return []
        if user_id is not None and launch.user.id != user_id:
            logger.info("auth.logout launch_not_burned reason=other_user")
            return []
        return [i for i in (launch.launch_id, launch.legacy_id) if i is not None]

    async def read_launch(request: Request) -> str | None:
        """Optional logout body {"init_data": ...}: the launch the page runs in now."""
        raw = await _read_body(request, settings.max_body_bytes)
        if not raw:
            return None
        _require_json(request)
        launch = _json_object(raw, {"init_data"})["init_data"]
        if not isinstance(launch, str):
            raise ApiError(400, "bad_request")
        return launch

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        require_origin(request)
        ts = now()
        # Burned initData cannot be exchanged again while it is still fresh:
        # auth_date <= ts + skew, so it expires by ts + max_age + skew.
        burn_until = burn_window(ts).until
        # An expired (not revoked) session still identifies the user and its CSRF
        # token still verifies: the current launch must be burned in that case too.
        active = await unrevoked_session(request)
        if active is not None:
            token, session = active
            if not csrf.verify(token, request.headers.get(CSRF_HEADER)):
                raise ApiError(403, "csrf_failed")
            # The current launch may differ from the one the session was created
            # with (reopened Mini App reusing the cookie); it must not sign the
            # user back in after a reload.
            launch = await read_launch(request)
            also_consume = launch_ids_to_burn(launch, session.user_id) if launch is not None else []
            await run_in_threadpool(store.revoke_session, token, ts, burn_until, also_consume)
            logger.info("auth.logout user=%s launch_burned=%s", session.user_id, bool(also_consume))
        else:
            # No cookie: the browser drops it at Max-Age, so a page that outlived its
            # session logs out without one. A validly signed, fresh launch is burned
            # anyway. No CSRF token exists here; burning needs the signed initData
            # itself (whoever holds it could sign in with it), and only ends that
            # one launch. Origin is still required.
            launch = await read_launch(request)
            ids = launch_ids_to_burn(launch, None) if launch is not None else []
            if ids:
                for launch_id in ids:
                    await run_in_threadpool(store.consume_init_data, launch_id, ts, burn_until)
                logger.info("auth.logout without_session launch_burned=True")
        response = JSONResponse({"ok": True})
        clear_session_cookie(response)
        return response

    @app.post("/api/auth/logout-all")
    async def logout_all(request: Request) -> JSONResponse:
        """Ends every session of the user (all devices). Needs a live session + CSRF."""
        require_origin(request)
        active = await current_session(request)
        if active is None:
            raise ApiError(401, "unauthenticated")
        token, session = active
        if not csrf.verify(token, request.headers.get(CSRF_HEADER)):
            raise ApiError(403, "csrf_failed")
        launch = await read_launch(request)
        launch_ids = launch_ids_to_burn(launch, session.user_id) if launch is not None else []
        ts = now()
        revoked = await run_in_threadpool(
            store.revoke_user_sessions, session.user_id, ts, burn_window(ts),
            launch_ids,
        )
        logger.info("auth.logout_all user=%s revoked=%s", session.user_id, revoked)
        response = JSONResponse({"ok": True, "revoked": revoked})
        clear_session_cookie(response)
        return response

    sports = sports or build_service(settings, clock=clock, warm_standings=True, warm_calendars=True)
    # Forecast history in its own file next to the database (rollback-safe, see forecast/history.py).
    history = ForecastHistory(history_path(settings.db_path), clock)
    history.init_schema()
    # Results of stored forecasts: from every loaded calendar plus a low-priority pass (internal only).
    settlement = Settlement(history, sports, clock)
    sports.calendar_listeners.append(settlement.on_calendar)
    # Odds pushed by the bot: in memory only, served after strict matching (miniapp/odds).
    odds = OddsStore(clock, enabled=settings.odds_feed_enabled, catalog=settings.odds_catalog)
    receiver = FeedReceiver(settings.odds_feed_secret.encode(), clock) if settings.odds_feed_secret else None
    register_odds_routes(app, receiver, odds, clock)
    # The Mini App's own Mostbet source (the bot is going to be retired): catalogue mode only, started by use.
    odds_source = (OddsSource(odds, lambda: sports.search_index.upcoming(),
                              max_per_cycle=settings.odds_source_max_per_cycle,
                              snapshot=settings.db_path.with_name("odds-snapshot.json"),
                              warm=sports.ensure_calendars_warm)
                   if settings.odds_catalog and settings.odds_feed_enabled and odds_source_enabled else None)
    ai = ai or build_ai_service(settings, sports, clock=clock, ledger=store, history=history, odds=odds)
    if odds_source is not None:
        # The source runs from the start, not from the first signed-in request: after a deploy (or with nobody in the
        # app) prices would otherwise go stale and every match would show none until someone opened it.
        async def start_odds_source() -> None:
            odds_source.ensure_running()
        app.router.on_startup.append(start_odds_source)

    async def active_session(request: Request) -> tuple[str, Session] | None:
        """current_session for the data APIs: a signed-in user may also start the settlement pass."""
        found = await current_session(request)
        if found is not None:
            settlement.ensure()
            if odds_source is not None:
                odds_source.ensure_running()
        return found

    # One share per user of the shared football-data budget, for every route that may read it.
    user_limiter = UserUpstreamLimiter(settings.user_share_per_minute, settings.sports_user_unknown_per_hour, clock,
                                       per_day=settings.user_share_per_day)
    # Lists and top matches: only matches the goal model can forecast (owner 16.11), judged from cached calendars.
    coverage = ModelCoverage(lambda tid: sports.cached_model_history(tid, any_age=True), clock)
    register_sports_routes(app, sports, active_session,
                           ai_status=lambda: "connected" if ai.connected else "not_connected",
                           user_limiter=user_limiter, listed=coverage.keep)
    def on_pick(user_id: int, field: str, legs: list[tuple[str, str | None]]) -> None:
        note_activity(user_id, field)
        try:
            picks.add(user_id, legs, now())
        except sqlite3.Error as exc:  # no record = no offer for that match, never a failed pick
            logger.warning("event=telemetry_write_failed type=%s", type(exc).__name__)

    async def on_ai_pick(user_id: int, result: Any) -> None:
        """The AI screen's recommended bet was shown to this user: an offer may sit inside it (ads only)."""
        pick = getattr(result, "pick", None)
        if picks is None or pick is None or pick.market == "NO_BET":
            return
        try:
            await run_in_threadpool(picks.add, user_id, [(result.event_id, pick.market)], now(), "ai_pick")
        except sqlite3.Error as exc:
            logger.warning("event=telemetry_write_failed type=%s", type(exc).__name__)

    register_quick_routes(app, QuickService(sports, clock=clock, odds=odds), active_session, user_limiter=user_limiter,
                          on_pick=on_pick if telemetry else None, credits=credits,
                          csrf_ok=lambda token, presented: csrf.verify(token, presented))

    if telemetry is not None:
        from miniapp.ads.odds_source import make_odds_lookup
        from miniapp.ads.policy import load_public_key
        from miniapp.ads.routes import register_ads_routes
        from miniapp.ads.service import PolicyReader, Tokens, policy_paths

        async def ads_write_check(request: Request) -> tuple[str, Session]:
            require_origin(request)
            active = await active_session(request)
            if active is None:
                raise ApiError(401, "unauthenticated")
            if not csrf.verify(active[0], request.headers.get(CSRF_HEADER)):
                raise ApiError(403, "csrf_failed")
            return active

        async def ads_read_json(request: Request, keys: set[str]) -> dict:
            return await _read_json_object(request, 1024, keys)

        policy_file, applied_file = policy_paths(settings.db_path)
        register_ads_routes(
            app, reader=PolicyReader(policy_file, applied_file,
                                     load_public_key(settings.ads_policy_public_key or ""), clock),
            telemetry=telemetry,
            tokens=Tokens(settings.csrf_key_material), store=store, session_check=active_session,
            write_check=ads_write_check, read_json=ads_read_json,
            # CF-IPCountry is trusted only when every request must come through the edge (secret checked above).
            trust_country=settings.edge_secret is not None, now=now, picks=picks,
            odds_for=make_odds_lookup(sports.event, odds))

    @app.get("/api/ai/analysis")
    async def ai_analysis_cached(request: Request) -> JSONResponse:
        # Read-only: an analysis already made (or one that costs nothing). Never reserves a
        # limit and never calls the model, so showing the AI screen can never pay.
        active = await active_session(request)
        if active is None:
            raise ApiError(401, "unauthenticated")
        params = request.query_params
        event_id = params.get("event_id")
        if set(params) != {"event_id"} or not event_id or len(event_id) > 40 or len(params.getlist("event_id")) > 1:
            raise ApiError(400, "bad_request")
        # Evidence (event, forms, seasons, tables) may read the data source: gated by the user's share
        # with what is still cold (a never-listed id also by its hourly count), charged by what it loaded.
        cost, unknown = sports.analysis_cost(event_id)
        share = user_share(user_limiter, active[1].user_id)
        if no := refused(share, cost, unknown=unknown):
            return no
        try:
            with share.tally():
                result = await ai.analyse(event_id, active[1].user_id, create=False)
        except UpstreamError as exc:
            # The football data source, not the AI: its own answer (with its wait), never "AI busy".
            logger.info("ai.source_unavailable path=read reason=%s", exc.code)
            return upstream_response(exc)
        except AiError as exc:
            if exc.code == "not_created":
                return JSONResponse({"error": "ai_not_created"}, status_code=404)
            logger.info("ai.unavailable path=read reason=%s", exc.code)  # code only: monitoring of timeouts etc.
            if exc.code in AI_ERROR_CODES:
                return JSONResponse({"error": "ai_unavailable", "reason": exc.code}, status_code=503)
            return JSONResponse({"error": "source_unavailable", "reason": exc.code}, status_code=503)
        if result is None:
            raise ApiError(404, "not_found")
        await run_in_threadpool(note_activity, active[1].user_id, "ai_opened")  # telemetry only (off: no-op)
        await on_ai_pick(active[1].user_id, result)
        return JSONResponse(result.to_json())

    @app.post("/api/ai/analysis")
    async def ai_analysis(request: Request) -> JSONResponse:
        # Costs money: same gate as other state-changing calls (Origin, session, CSRF).
        require_origin(request)
        active = await active_session(request)
        if active is None:
            raise ApiError(401, "unauthenticated")
        token, session = active
        if not csrf.verify(token, request.headers.get(CSRF_HEADER)):
            raise ApiError(403, "csrf_failed")
        body = await _read_json_object(request, 1024, {"event_id"})
        event_id = body["event_id"]
        if not isinstance(event_id, str) or not 0 < len(event_id) <= 40:
            raise ApiError(400, "bad_request")
        item = f"ai:{event_id}"
        cost, unknown = sports.analysis_cost(event_id)
        share = user_share(user_limiter, session.user_id)
        if no := refused(share, cost, unknown=unknown):
            return no
        # Credits are taken BEFORE generating (no two analyses can share one balance) and given back unless the
        # language model actually produced one (docs/miniapp/credits.md).
        try:
            held = await run_in_threadpool(credits.hold, session.user_id, item)
        except NoCredits as exc:
            return no_credits(exc.balance)
        except Busy:  # this user's same analysis is being generated by another request: "busy, retry shortly"
            return JSONResponse({"error": "ai_unavailable", "reason": "rate_limited"}, status_code=503)
        # The work runs as its own task and the hold is settled when IT finishes: a client that goes away neither
        # gets a free analysis (refund before the shielded model call completes) nor pays for one that then fails
        # (Codex credits r2-r3). A process that dies meanwhile: refunded at the next startup.
        work = asyncio.ensure_future(_analysis_response(event_id, session.user_id, share))
        if held is None:
            return (await asyncio.shield(work))[0]
        user_id, settled = session.user_id, {"done": False}

        def settle_when_done(task: asyncio.Future) -> None:
            if settled["done"]:
                return  # settled inline below, before the answer went out
            generated = not task.cancelled() and task.exception() is None and task.result()[1]
            settling = asyncio.ensure_future(run_in_threadpool(credits.settle, user_id, item, held, generated))
            settling.add_done_callback(lambda t: t.cancelled() or t.exception())  # logged by asyncio if any

        work.add_done_callback(settle_when_done)  # only for a request cancelled while the work runs on
        try:
            response, generated = await asyncio.shield(work)
        except asyncio.CancelledError:
            raise
        except Exception:
            settled["done"] = True
            await run_in_threadpool(credits.settle, user_id, item, held, False)
            raise
        # Settled before answering: a retry right after never sees the hold, and a stop now cannot refund a
        # delivered analysis (Codex credits r7 P2).
        settled["done"] = True
        await run_in_threadpool(credits.settle, user_id, item, held, generated)
        return response

    async def _analysis_response(event_id: str, user_id: int, share: Any) -> tuple[JSONResponse, bool]:
        """The analysis answer, and whether a language-model analysis was produced (what credits pay for: an
        insufficient-data answer or a market-only estimate made without the model costs nothing)."""
        try:
            with share.tally():
                result = await ai.analyse(event_id, user_id)
        except UpstreamError as exc:
            logger.info("ai.source_unavailable path=create reason=%s", exc.code)
            return upstream_response(exc), False
        except AiError as exc:
            logger.info("ai.unavailable path=create reason=%s", exc.code)  # code only: monitoring of timeouts etc.
            if exc.code in AI_ERROR_CODES:
                status = 429 if exc.code == "limit_reached" else 503
                return JSONResponse({"error": "ai_unavailable", "reason": exc.code}, status_code=status), False
            return JSONResponse({"error": "source_unavailable", "reason": exc.code}, status_code=503), False
        if result is None:
            raise ApiError(404, "not_found")
        await run_in_threadpool(note_activity, user_id, "ai_created")  # delivered analysis (off: no-op)
        await on_ai_pick(user_id, result)
        return JSONResponse(result.to_json()), result.kind != "insufficient" and result.model is not None

    # ---- credits and MB ID (docs/miniapp/credits.md) ------------------------------------------

    @app.get("/api/credits")
    async def credits_summary(request: Request) -> JSONResponse:
        active = await active_session(request)
        if active is None:
            raise ApiError(401, "unauthenticated")
        # Switched off (owner 16.11): credits are hidden from users entirely; nothing is created or shown.
        if not await run_in_threadpool(credits.enabled):
            return JSONResponse({"enabled": False})
        # The calculator's rate follows the viewer's country (CF-IPCountry behind the verified edge only).
        try:  # the ads package is optional (MINIAPP_ADS_ENABLED off may ship without it): then no country
            from miniapp.ads.service import country_from
        except ImportError:
            country = None
        else:
            country = country_from(request.headers.get("cf-ipcountry"), settings.edge_secret is not None)
        return JSONResponse(await run_in_threadpool(credits.summary, active[1].user_id, country))

    @app.post("/api/credits/mb")
    async def credits_mb(request: Request) -> JSONResponse:
        require_origin(request)
        active = await active_session(request)
        if active is None:
            raise ApiError(401, "unauthenticated")
        token, session = active
        if not csrf.verify(token, request.headers.get(CSRF_HEADER)):
            raise ApiError(403, "csrf_failed")
        body = await _read_json_object(request, 1024, {"mbId"})
        if not await run_in_threadpool(credits.enabled):
            raise ApiError(409, "credits_off")
        try:
            mb = await run_in_threadpool(credits.submit_mb, session.user_id, body["mbId"])
        except MbError as exc:
            raise ApiError(409 if str(exc) in ("mb_taken", "mb_locked") else 400, str(exc)) from None
        return JSONResponse({"mb": mb})


    static = StaticFiles(directory=settings.static_dir, html=True) if settings.static_dir is not None else None

    async def fallback(scope: Any, receive: Any, send: Any) -> None:
        # Router default: runs only when no route matched, not even by path alone,
        # so a wrong method on an API path still gets 405. /api never reaches the
        # static files. StaticFiles follows no symlinks and rejects path traversal.
        path = scope.get("path", "")
        if static is not None and scope["type"] == "http" and path != "/api" and not path.startswith("/api/"):
            await static(scope, receive, send)
            return
        await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)

    app.router.default = fallback

    return app
