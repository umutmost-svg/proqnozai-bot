"""HTTP API of the sports data layer.

Every route requires an active Mini App session (M2): the provider quota is
spent only for signed-in Telegram users, and only through the shared cache.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .client import UpstreamError
from .crests import KINDS
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
        await service.ensure_emblems()
        return JSONResponse({**service.coverage(), "ai": {"status": ai_status()}})

    @app.get("/api/sports/{sport}/tournaments")
    async def tournaments(sport: str, request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if sport not in SPORTS:
            return _error(404, "not_found")
        await service.ensure_emblems()
        coverage = service.coverage()["sports"][sport]
        return JSONResponse({
            "sport": sport,
            "coverage": _short(coverage),
            "tournaments": [service.tournament_json(t) for t in service.tournaments(sport)],  # type: ignore[arg-type]
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
        await service.ensure_emblems()  # after the data: an emblem never delays or fails a listing
        coverage = service.coverage()["sports"][sport]
        return JSONResponse({
            "sport": sport,
            "coverage": _short(coverage),
            "events": [e.to_json() for e in result.events],
            "tournaments": [service.tournament_json(t) for t in result.tournaments],
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
        return JSONResponse({"event": found.to_json(),
                             "tournament": service.tournament_json(tournament) if tournament else None,
                             "coverage": _short(service.coverage()["sports"][found.sport]),
                             "ai": {"status": ai_status()}})

    @app.get("/api/sports/{sport}/tournaments/{tournament_id}/calendar")
    async def calendar(sport: str, tournament_id: str, request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if sport not in SPORTS or len(tournament_id) > 40:
            return _error(404, "not_found")
        if request.query_params:
            return _error(400, "bad_request")
        try:
            result = await service.calendar(sport, tournament_id)  # type: ignore[arg-type]
        except UpstreamError as exc:
            return _upstream(exc)
        if result is None:
            return _error(404, "not_found")
        return JSONResponse({
            "sport": sport,
            "coverage": _short(service.coverage()["sports"][sport]),
            # After the load: the emblem URL is learned from this very payload.
            "tournament": service.tournament_json(result.tournament),
            "season": result.season.to_json(),
            "events": [e.to_json() for e in result.events],
            "fetchedAt": result.fetched_at,
            "stale": result.stale,
            "ai": {"status": ai_status()},
        })

    @app.api_route("/api/crests/{kind}/{ident}", methods=["GET", "HEAD"])
    async def crest(kind: str, ident: str, request: Request) -> Response:
        # Public on purpose: a screen shows dozens of crests at once, so this path is left out
        # of the edge's per-IP API rate limit and cached at the edge instead (public, 1 day).
        # It never touches the session store or the provider quota: only PNGs the provider
        # listed, from memory, with capped fetches and a global per-second cap.
        valid = kind in KINDS and 0 < len(ident) <= 12 and ident.isascii() and (
            (ident.isdigit() and ident[0] != "0") if kind == "team" else ident.isalnum())
        # Junk ids and query strings (which would bypass the edge cache) get a cheap 404 under
        # their own cap, so they can neither crowd out real crests nor spin the origin.
        if not valid or request.url.query or service.crests.path(kind, ident) is None:
            if not service.crests.admit_rejected():
                return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "1"})
            return _error(404, "not_found")
        if not service.crests.admit():
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "1"})
        body = await service.crests.image(kind, ident)
        if body is None:
            return _error(404, "not_found")
        # The bytes were checked to be a PNG (never SVG); caching and nosniff come from the app middleware.
        return Response(body, media_type="image/png")

    @app.api_route("/api/crests/{rest:path}", methods=["GET", "HEAD"])
    async def crest_other(rest: str) -> Response:
        # Any other shape under the crest prefix (also outside the edge's per-IP limit):
        # the same capped cheap 404 as an unknown crest.
        if not service.crests.admit_rejected():
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "1"})
        return _error(404, "not_found")
