"""HTTP API of the quick prediction (read-only: no state change, no paid call)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from miniapp.forecast.quick import Criteria
from miniapp.sports.client import UpstreamError
from miniapp.sports.routes import SessionCheck, _error, _upstream
from miniapp.sports.service import RequestError, parse_instant

from .service import QuickBusy, QuickService

PARAMS = frozenset({"mode", "from", "to", "tournaments", "market", "risk", "size"})
MAX_TOURNAMENTS = 20


def register_quick_routes(app: FastAPI, service: QuickService, current_session: SessionCheck) -> None:
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
        return JSONResponse(answer.to_json())
