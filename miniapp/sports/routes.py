"""HTTP API of the sports data layer.

Every route requires an active Mini App session (M2): the provider quota is
spent only for signed-in Telegram users, and only through the shared cache.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .client import UpstreamError
from .crests import KINDS, media_type
from .models import SPORTS
from .search import MAX_QUERY, MIN_QUERY, normalize
from .service import RequestError, SportsService, parse_instant

logger = logging.getLogger("miniapp.sports")

SessionCheck = Callable[[Request], Awaitable[Any]]
MAX_TOP_DAY = timedelta(hours=26)


def _error(status: int, code: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": code, **extra}, status_code=status)


MAX_RETRY_AFTER_S = 3600


def _upstream(exc: UpstreamError) -> JSONResponse:
    body: dict[str, Any] = {"error": "source_unavailable", "reason": exc.code}
    headers = None
    if exc.code in ("rate_limited", "quota_exhausted"):
        # Whole seconds, rounded up: a client that waits this long is admitted (60: unknown wait).
        wait = 60 if exc.retry_after is None else min(max(math.ceil(exc.retry_after), 1), MAX_RETRY_AFTER_S)
        headers = {"Retry-After": str(wait)}
        body["retryAfter"] = wait
    return JSONResponse(body, status_code=503, headers=headers)


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
        if await current_session(request) is None:
            return False
        # The app is in use: keep the tables warm (a background pass, only when one is due).
        service.ensure_standings_warm()
        return True

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

    @app.get("/api/sports/football/top")
    async def top(request: Request) -> JSONResponse:
        """The day's top matches. `from`/`to`: the user's local day in UTC (as for /events)."""
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        params = request.query_params
        if set(params) - {"from", "to", "tournament"} or "from" not in params or "to" not in params:
            return _error(400, "bad_request")
        try:
            start, end = parse_instant(params["from"]), parse_instant(params["to"])
            # One local day (23–25 h with a clock change), never a longer ranking.
            if not timedelta(0) < end - start <= MAX_TOP_DAY:
                return _error(400, "bad_range")
            ranked, result = await service.top(start, end, params.get("tournament") or None)
        except RequestError as exc:
            return _error(400, exc.code)
        except UpstreamError as exc:
            return _upstream(exc)
        await service.ensure_emblems()
        return JSONResponse({
            "sport": "football",
            "coverage": _short(service.coverage()["sports"]["football"]),
            "top": [{"event": r.event.to_json(), "leaders": r.leaders} for r in ranked],
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

    @app.get("/api/sports/{sport}/tournaments/{tournament_id}/standings")
    async def standings(sport: str, tournament_id: str, request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if sport not in SPORTS or len(tournament_id) > 40:
            return _error(404, "not_found")
        if request.query_params:
            return _error(400, "bad_request")
        try:
            result = await service.standings_table(sport, tournament_id)  # type: ignore[arg-type]
        except UpstreamError as exc:
            return _upstream(exc)
        if result is None:
            return _error(404, "not_found")
        return JSONResponse({
            "sport": sport,
            "coverage": _short(service.coverage()["sports"][sport]),
            "tournament": service.tournament_json(result.tournament),
            "season": result.standings.season.to_json(),
            # Provider order inside every block; an empty list means the competition has no table.
            "blocks": [b.to_json() for b in result.standings.blocks],
            "fetchedAt": result.fetched_at,
            "stale": result.stale,
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
        # The bytes were checked: a PNG, or an SVG of plain vector graphics (crests.is_safe_svg). An SVG gets a
        # no-script CSP from the app middleware; caching and nosniff come from there too.
        return Response(body, media_type=media_type(body))

    @app.api_route("/api/crests/{rest:path}", methods=["GET", "HEAD"])
    async def crest_other(rest: str) -> Response:
        # Any other shape under the crest prefix (also outside the edge's per-IP limit):
        # the same capped cheap 404 as an unknown crest.
        if not service.crests.admit_rejected():
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "1"})
        return _error(404, "not_found")

    def _coverage_json(indexed: list[str], total: int) -> dict:
        return {"indexed": indexed, "total": total, "complete": len(indexed) == total}

    @app.get("/api/sports/{sport}/search")
    async def search(sport: str, request: Request) -> JSONResponse:
        # Global: independent of any date or tournament the catalogue shows. Served from the
        # season index (no upstream request per keystroke; the warm-up runs in the background).
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if sport not in SPORTS:
            return _error(404, "not_found")
        params = request.query_params
        raw = params.get("q", "")
        if set(params) != {"q"} or len(raw) > MAX_QUERY or len(normalize(raw)) < MIN_QUERY:
            return _error(400, "bad_query")
        if not service.connected(sport):  # type: ignore[arg-type]
            return JSONResponse({"sport": sport, "query": raw, "teams": [], "events": [],
                                 "coverage": _coverage_json([], 0), "tournaments": []})
        index = service.search_index
        await index.wait_for_some()
        result = index.search(raw)
        return JSONResponse({
            "sport": sport,
            "query": raw,
            "teams": [h.to_json() for h in result.teams],
            "events": [e.to_json() for e in result.events],
            "coverage": _coverage_json(result.indexed, result.total),
            "tournaments": [service.tournament_json(t) for t in service.tournaments(sport)],  # type: ignore[arg-type]
            "source": _short(service.coverage()["sports"][sport]),
        })

    @app.get("/api/sports/{sport}/teams/{team_id}/matches")
    async def team_matches(sport: str, team_id: str, request: Request) -> JSONResponse:
        if not await signed_in(request):
            return _error(401, "unauthenticated")
        if sport not in SPORTS or len(team_id) > 40 or not service.connected(sport):  # type: ignore[arg-type]
            return _error(404, "not_found")
        index = service.search_index
        await index.wait_for_some()
        hit, events = index.team(team_id)
        indexed = index.indexed()
        if hit is None:
            total = len(service.tournaments(sport))  # type: ignore[arg-type]
            if len(indexed) < total:
                # The team may be in a competition not indexed yet: "try again", never "not found".
                return JSONResponse({"error": "source_unavailable", "reason": "indexing"}, status_code=503,
                                    headers={"Retry-After": "15"})
            return _error(404, "not_found")
        return JSONResponse({
            "sport": sport,
            "team": hit.to_json(),
            "events": [e.to_json() for e in events],
            "coverage": _coverage_json(indexed, len(service.tournaments(sport))),  # type: ignore[arg-type]
            "tournaments": [service.tournament_json(t) for t in service.tournaments(sport)],  # type: ignore[arg-type]
            "source": _short(service.coverage()["sports"][sport]),
        })
