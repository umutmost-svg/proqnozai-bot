"""POST /api/internal/odds — the bot's signed odds push (see feed.py).

Not a user route: no session, no cookie, no CORS. Off (404) without a configured secret.
Answers only counts. Every request is checked (the body is capped before it is read and an
HMAC of it is cheap); only SIGNED pushes count toward MAX_PUSHES_PER_MINUTE, so unsigned
traffic can never use up the allowance of the real sender. Refusals are logged at most once
a minute. The edge in front should still limit this path (docs/miniapp/odds.md).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .feed import MAX_BODY_BYTES, SIGNATURE_HEADER, TIMESTAMP_HEADER, FeedError, FeedReceiver
from .store import OddsStore

logger = logging.getLogger("miniapp.odds")

MAX_PUSHES_PER_MINUTE = 30


def register_odds_routes(app: FastAPI, receiver: FeedReceiver | None, store: OddsStore,
                         clock: Callable[[], float]) -> None:
    accepted: deque[float] = deque()
    refusals = {"logged_at": -1e18, "count": 0}

    def refused(code: str, status: int) -> JSONResponse:
        now = clock()
        refusals["count"] += 1
        if now - refusals["logged_at"] >= 60:
            logger.warning("odds.push_rejected code=%s refusals_since_last_log=%d", code, refusals["count"])
            refusals["logged_at"], refusals["count"] = now, 0
        return JSONResponse({"error": code}, status_code=status)

    @app.post("/api/internal/odds")
    async def push(request: Request) -> JSONResponse:
        if receiver is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            return refused("too_large", 413)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return refused("too_large", 413)
        try:
            lines = receiver.verify(request.headers.get(TIMESTAMP_HEADER), request.headers.get(SIGNATURE_HEADER),
                                    bytes(body))
        except FeedError as exc:
            return refused(exc.code, exc.status)
        if not store.enabled:
            # Switched off here: the (authenticated) sender stops asking its source for prices.
            return JSONResponse({"error": "disabled"}, status_code=410)
        now = clock()
        while accepted and now - accepted[0] >= 60:
            accepted.popleft()
        if len(accepted) >= MAX_PUSHES_PER_MINUTE:
            # Signed but too often: the sender pushes once per cycle (minutes apart).
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"})
        accepted.append(now)
        report = store.ingest(lines)
        logger.info("odds.push lines=%d accepted=%d unknown_competition=%d without_odds=%d stored=%d",
                    len(lines), report.accepted, report.unknown_competition, report.without_odds, len(store))
        return JSONResponse({"received": len(lines), **report.to_json()})
