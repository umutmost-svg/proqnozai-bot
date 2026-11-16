"""HTTP API of the quick prediction. No paid call; with credits on (docs/miniapp/credits.md) a generated pick costs
credits, so the request then needs the session's anti-CSRF header."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from miniapp.forecast.quick import Criteria
from miniapp.sports.client import UpstreamError
from miniapp.sports.routes import SessionCheck, _error, _upstream, refused, user_share
from miniapp.sports.user_limits import UserUpstreamLimiter
from miniapp.sports.service import RequestError, parse_instant

from miniapp.credits import COST, Credits, NoCredits

from .service import QuickBusy, QuickService

PARAMS = frozenset({"mode", "from", "to", "tournaments", "market", "risk", "size"})
MAX_TOURNAMENTS = 20


def register_quick_routes(app: FastAPI, service: QuickService, current_session: SessionCheck,
                          user_limiter: UserUpstreamLimiter | None = None,
                          on_pick: Callable[[int, str, list[str]], None] | None = None,
                          credits: Credits | None = None,
                          csrf_ok: Callable[[str, str | None], bool] | None = None) -> None:
    """`on_pick(user_id, field, event_ids)`: optional hook for a successful pick (ads: activity counter and the
    matches an offer may be shown for)."""
    @app.get("/api/quick/pick")
    async def pick(request: Request) -> JSONResponse:
        active = await current_session(request)
        if active is None:
            return _error(401, "unauthenticated")
        token, session = active
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
        # One paid item per period (its UTC dates) and criteria: the same request again costs nothing, whatever exact
        # instants the client sent for that period (Codex credits r1 P2).
        item = ":".join(("quick", start.date().isoformat(), end.date().isoformat(), mode, criteria.market,
                         criteria.risk, str(size), ",".join(sorted(tournaments or ()))))
        if credits is not None and await run_in_threadpool(credits.enabled):
            # Spends credits: a cross-site page must not be able to trigger it (custom header + session token).
            if csrf_ok is None or not csrf_ok(token, request.headers.get("X-CSRF-Token")):
                return _error(403, "csrf_failed")
            try:
                await run_in_threadpool(credits.precheck, session.user_id, item)
            except NoCredits as exc:
                return _no_credits(exc.balance)
        share = user_share(user_limiter, session.user_id)
        # Admitted while the share has room for its first cold step; every further step (a day, a season
        # history) is reserved as it goes and skipped ("unavailable") when the share has no room left.
        if no := refused(share, service.first_cost(start, end)):
            return no
        try:
            with share.tally():
                answer = await service.pick(session.user_id, start, end, tournaments, criteria, budget=share.take)
        except QuickBusy:
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "10"})
        except RequestError as exc:
            return _error(400, exc.code)
        except UpstreamError as exc:
            return _upstream(exc)
        body = answer.to_json()
        if credits is not None and body.get("status") == "ok":
            try:
                await run_in_threadpool(credits.charge, session.user_id, item)
            except NoCredits as exc:
                return _no_credits(exc.balance)  # spent concurrently: the pick is not given
        if on_pick is not None and body.get("status") == "ok":
            events = [leg["event"]["id"] for leg in body.get("legs", []) if isinstance(leg.get("event"), dict)]
            await run_in_threadpool(on_pick, session.user_id,
                                    "quick_coupon_ok" if body.get("mode") == "coupon" else "quick_single_ok", events)
        return JSONResponse(body)


def _no_credits(balance: int) -> JSONResponse:
    body: dict[str, Any] = {"error": "no_credits", "balance": balance, "cost": COST}
    return JSONResponse(body, status_code=402)
