"""HTTP API of the quick prediction (read-only: no state change, no paid call)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from miniapp.forecast.quick import Criteria
from miniapp.sports.client import UpstreamError
from miniapp.sports.routes import SessionCheck, _error, _upstream
from miniapp.sports.service import RequestError, parse_instant

from .service import QuickBusy, QuickService

PARAMS = frozenset({"mode", "from", "to", "tournaments", "market", "risk", "size"})
MAX_TOURNAMENTS = 20


def register_quick_routes(app: FastAPI, service: QuickService, current_session: SessionCheck,
                          on_pick: Callable[[int, str, list[tuple[str, str | None]]], None] | None = None) -> None:
    """`on_pick(user_id, field, legs)`: optional hook for a successful pick with its (event id, market) legs (ads:
    activity counter, and the matches and markets an offer may be shown — and priced — for)."""
    @app.get("/api/quick/pick")
    async def pick(request: Request) -> JSONResponse:
        active = await current_session(request)
        if active is None:
            return _error(401, "unauthenticated")
        _, session = active
        params = request.query_params
        if set(params) - PARAMS or any(len(params.getlist(k)) > 1 for k in params):
            return _error(400, "bad_request")
        try:
            mode = params["mode"]
            size = 1 if mode == "single" and "size" not in params else int(params["size"])
            criteria = Criteria(mode, params.get("market", "any"), params.get("risk", "balanced"), size)
            start, end = parse_instant(params["from"]), parse_instant(params["to"])
        except (KeyError, ValueError):
            return _error(400, "bad_request")
        tournaments = None
        if params.get("tournaments"):
            ids = params["tournaments"].split(",")
            if len(ids) > MAX_TOURNAMENTS or any(not 0 < len(i) <= 40 for i in ids):
                return _error(400, "bad_request")
            tournaments = frozenset(ids)
        try:
            answer = await service.pick(session.user_id, start, end, tournaments, criteria)
        except QuickBusy:
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "10"})
        except RequestError as exc:
            return _error(400, exc.code)
        except UpstreamError as exc:
            return _upstream(exc)
        body = answer.to_json()
        if on_pick is not None and body.get("status") == "ok":
            legs = [(leg["event"]["id"], leg.get("market")) for leg in body.get("legs", [])
                    if isinstance(leg.get("event"), dict)]
            await run_in_threadpool(on_pick, session.user_id,
                                    "quick_coupon_ok" if body.get("mode") == "coupon" else "quick_single_ok", legs)
        return JSONResponse(body)
