"""FastAPI application of the Mini App API (M2: Telegram auth + session).

`create_app()` builds everything from explicit arguments; importing this
module creates no database, reads no env and starts nothing.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from miniapp.security import CsrfTokens, LoginRateLimiter, origin_allowed
from miniapp.settings import Settings
from miniapp.store import SUPPORTED_LANGS, Session, Store, User
from miniapp.telegram_auth import InitDataError, validate_init_data

logger = logging.getLogger("miniapp")

CSRF_HEADER = "X-CSRF-Token"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


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


async def _read_json_object(request: Request, max_bytes: int, allowed: set[str]) -> dict[str, Any]:
    """Strict JSON contract: application/json, bounded size, exact key set."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise ApiError(415, "unsupported_media_type")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
        raise ApiError(413, "payload_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ApiError(413, "payload_too_large")
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError(400, "bad_request") from None
    if not isinstance(data, dict) or set(data) != allowed:
        raise ApiError(400, "bad_request")
    return data


def create_app(
    settings: Settings,
    *,
    clock: Callable[[], float] = time.time,
    store: Store | None = None,
) -> FastAPI:
    store = store or Store(settings.db_path)
    store.init_schema()
    csrf = CsrfTokens(settings.bot_token)
    limiter = LoginRateLimiter(settings.login_rate_limit, settings.login_rate_window_s, clock)

    app = FastAPI(title="Spor AI Mini App API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.limiter = limiter

    def now() -> int:
        return int(clock())

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        # Auth and personal responses must never be cached by the WebView or a proxy.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Type only: messages of library exceptions could echo request data.
        logger.error("unhandled error: %s", type(exc).__name__)
        return JSONResponse({"error": "internal"}, status_code=500)

    def require_origin(request: Request) -> None:
        if not origin_allowed(request.headers.get("origin"), settings.trusted_origins):
            raise ApiError(403, "origin_not_allowed")

    def set_session_cookie(response: Response, token: str, expires_at: int) -> None:
        response.set_cookie(
            settings.cookie_name,
            token,
            max_age=max(0, expires_at - now()),
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite=settings.cookie_samesite,
            partitioned=settings.cookie_samesite == "none",
        )

    def clear_session_cookie(response: Response) -> None:
        response.delete_cookie(
            settings.cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite=settings.cookie_samesite,
            partitioned=settings.cookie_samesite == "none",
        )

    async def current_session(request: Request) -> tuple[str, Session] | None:
        """Active (not revoked, not expired) session from the cookie, else None."""
        token = request.cookies.get(settings.cookie_name)
        if not token or len(token) > 128:
            return None
        session = await run_in_threadpool(store.get_session, token)
        if session is None or session.revoked_at is not None or session.expires_at <= now():
            return None
        return token, session

    def session_payload(user: User, token: str, session: Session) -> dict[str, Any]:
        return {
            "user": _public_user(user),
            "session": {"expires_at": _iso(session.expires_at), "csrf_token": csrf.for_session(token)},
        }

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/auth/session")
    async def create_session(request: Request) -> JSONResponse:
        # Login CSRF: only our own page may post, and only with a JSON body,
        # which a cross-site form cannot produce without a CORS preflight we never answer.
        require_origin(request)
        peer = request.client.host if request.client else "unknown"
        retry_after = limiter.hit(peer)
        if retry_after is not None:
            logger.warning("auth.rate_limited")
            raise ApiError(429, "rate_limited", {"Retry-After": str(int(retry_after + 0.999))})
        body = await _read_json_object(request, settings.max_body_bytes, {"init_data"})
        init_data = body["init_data"]
        if not isinstance(init_data, str):
            raise ApiError(400, "bad_request")
        try:
            verified = validate_init_data(
                init_data,
                settings.bot_token,
                now=clock(),
                max_age_s=settings.init_data_max_age_s,
                future_skew_s=settings.init_data_future_skew_s,
                max_chars=settings.max_init_data_chars,
            )
        except InitDataError as exc:
            logger.info("auth.rejected reason=%s", exc.code)
            raise ApiError(401, "init_data_expired" if exc.expired else "init_data_invalid") from None

        ts = now()
        if await run_in_threadpool(store.is_init_data_consumed, verified.signature_hash, ts):
            logger.info("auth.rejected reason=reused_after_logout")
            raise ApiError(401, "init_data_reused")

        user = await run_in_threadpool(store.upsert_user, verified.user, ts)

        existing = await current_session(request)
        if existing is not None and existing[1].user_id == user.telegram_user_id:
            # Idempotent bootstrap: a live session for the same user is reused.
            token, session = existing
            created = False
        else:
            if existing is not None:
                # The device switched Telegram accounts: the old session ends.
                await run_in_threadpool(store.revoke_session, existing[0], ts, ts)
            token = secrets.token_urlsafe(32)
            session = await run_in_threadpool(
                store.create_session, token, user.telegram_user_id, verified.signature_hash, ts, settings.session_ttl_s
            )
            created = True
        logger.info("auth.session user=%s created=%s", user.telegram_user_id, created)
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

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        require_origin(request)
        active = await current_session(request)
        if active is not None:
            token, session = active
            if not csrf.verify(token, request.headers.get(CSRF_HEADER)):
                raise ApiError(403, "csrf_failed")
            ts = now()
            # The initData of this session cannot be exchanged again while it is still fresh.
            burn_until = ts + settings.init_data_max_age_s + settings.init_data_future_skew_s
            await run_in_threadpool(store.revoke_session, token, ts, burn_until)
            logger.info("auth.logout user=%s", session.user_id)
        response = JSONResponse({"ok": True})
        clear_session_cookie(response)
        return response

    return app
