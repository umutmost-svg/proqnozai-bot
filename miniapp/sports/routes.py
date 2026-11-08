"""HTTP API of the sports data layer.

Every route requires an active Mini App session (M2): the provider quota is
spent only for signed-in Telegram users, and only through the shared cache.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .client import UpstreamError
from .models import SPORTS
from .service import RequestError, SportsService, parse_instant

logger = logging.getLogger("miniapp.sports")

SessionCheck = Callable[[Request], Awaitable[Any]]


def _error(status: int, code: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": code, **extra}, status_code=status)


def _upstream(exc: UpstreamError) -> JSONResponse:
    headers = {"Retry-After": "60"} if exc.code in ("rate_limited", "quota_exhausted") else None
    return JSONResponse({"error": "source_unavailable", "reason": exc.code}, status_code=503, headers=headers)


def _short(coverage: dict) -> dict:
    return {
        "status": coverage["status"],
        "provider": coverage["provider"],
        "attribution": coverage.get("attribution"),
        "liveScores": coverage.get("features", {}).get("liveScores"),
    }


def register_sports_routes(app: FastAPI, service: SportsService, current_session: SessionCheck,
                           ai_status: Callable[[], str] = lambda: "not_connected") -> None:
    async def signed_in(request: Request) -> bool:
        return await current_session(request) is not None

    @app.get("/api/sports/coverage")
    async def coverage(request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        return JSONResponse({**service.coverage(), "ai": {"status": ai_status()}})

    @app.get("/api/sports/{sport}/tournaments")
    async def tournaments(sport: str, request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if sport not in SPORTS:
            return _error(404, "not_found")
        coverage = service.coverage()["sports"][sport]
        return JSONResponse({
            "sport": sport,
            "coverage": _short(coverage),
            "tournaments": [t.to_json() for t in service.tournaments(sport)],  # type: ignore[arg-type]
        })

    @app.get("/api/sports/{sport}/events")
    async def events(sport: str, request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if sport not in SPORTS:
            return _error(404, "not_found")
        params = request.query_params
        unexpected = set(params) - {"from", "to", "tournament"}
        if unexpected or "from" not in params or "to" not in params:
            return _error(400, "bad_request")
        try:
            start, end = parse_instant(params["from"]), parse_instant(params["to"])
            result = await service.events(sport, start, end, params.get("tournament") or None)  # type: ignore[arg-type]
        except RequestError as exc:
            return _error(400, exc.code)
        except UpstreamError as exc:
            return _upstream(exc)
        coverage = service.coverage()["sports"][sport]
        return JSONResponse({
            "sport": sport,
            "coverage": _short(coverage),
            "events": [e.to_json() for e in result.events],
            "tournaments": [t.to_json() for t in result.tournaments],
            "fetchedAt": result.fetched_at,
            "stale": result.stale,
            "ai": {"status": ai_status()},
        })

    @app.get("/api/sports/event/{event_id}")
    async def event(event_id: str, request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if len(event_id) > 40:
            return _error(404, "not_found")
        try:
            found, tournament = await service.event(event_id)
        except UpstreamError as exc:
            return _upstream(exc)
        if found is None:
            return _error(404, "not_found")
        return JSONResponse({"event": found.to_json(), "tournament": tournament.to_json() if tournament else None,
                             "coverage": _short(service.coverage()["sports"][found.sport]),
                             "ai": {"status": ai_status()}})
