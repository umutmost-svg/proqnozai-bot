"""HTTP API of partner offers (ads-architecture.md §6). Registered only with MINIAPP_ADS_ENABLED=true; with the
flag off these paths do not exist (404) and nothing about ads is read or written.

- GET  /api/ads/offer?placement=…&event_id=…  -> {"offer": null} | {"offer": {...texts, impressionToken}} | ageGate
- POST /api/ads/impression {impressionToken}   -> one per (user, campaign, variant, event, day)
- POST /api/ads/click {impressionToken}         -> {"url"} from the CURRENT snapshot; repeated taps within 10 s count once
- POST /api/ads/age {"min": N}                  -> the user states "I am N or older" (the gate's own age)
Every answer is personal: `Cache-Control: no-store` (the app guard) and no shared cache key.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from miniapp.ads import engine, policy
from miniapp.ads.service import EVENT_KEY, PolicyReader, Segments, Token, TokenError, Tokens, country_from
from miniapp.ads.telemetry import Telemetry, day_of

logger = logging.getLogger("miniapp.ads")

COUNTRY_HEADER = "cf-ipcountry"


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def register_ads_routes(
    app: FastAPI,
    *,
    reader: PolicyReader,
    telemetry: Telemetry,
    tokens: Tokens,
    store: Any,
    session_check: Callable[[Request], Awaitable[Any]],
    write_check: Callable[[Request], Awaitable[Any]],
    read_json: Callable[[Request, set[str]], Awaitable[dict]],
    trust_country: bool,
    now: Callable[[], int],
) -> None:
    """`write_check` does Origin + session + CSRF and returns (token, session); `read_json` reads a strict JSON
    object with exactly the given keys."""

    async def read_token(request: Request) -> object:
        return (await read_json(request, {"impressionToken"}))["impressionToken"]

    def viewer(user_id: int, lang: str, country: str | None, ts: int) -> engine.Viewer:
        return engine.Viewer(user_id=user_id, lang=lang, country=country,
                             confirmed_age=telemetry.confirmed_age(user_id),
                             segment=Segments(telemetry, user_id, store.user_created_at(user_id), ts))

    def offer_sync(user_id: int, country: str | None, placement: str, event_key: str) -> dict:
        ts = now()
        user = store.get_user(user_id)
        if user is None:
            return {"offer": None}
        current = reader.current()
        v = viewer(user_id, user.lang, country, ts)
        choice = engine.choose(current, v, placement, ts)
        if choice is None:
            # Would a partner with a higher age requirement have an offer for this user? Then ask for that age.
            maybe = engine.choose(current, engine.Viewer(user_id, user.lang, country, policy.MAX_AGE, v.segment),
                                  placement, ts)
            if maybe is not None and maybe.campaign.age_min > v.confirmed_age:
                return {"offer": None, "ageGate": {"min": maybe.campaign.age_min}}
            return {"offer": None}
        c = choice.campaign
        if choice.variant is None:  # control group: nothing shown, the exposure is counted for the comparison
            telemetry.exposure("control", user_id, c.id, "control", placement, event_key, country, ts)
            return {"offer": None}
        variant = choice.variant
        token = tokens.issue(Token(user_id, c.id, variant.key, event_key, placement, day_of(ts)))
        return {"offer": {
            "id": c.id, "variant": variant.key, "headline": variant.headline, "body": variant.body,
            "extra": variant.extra, "cta": variant.cta,
            "disclosure": {k: v for k, v in c.disclosure.items() if v},
            "impressionToken": token,
        }}

    @app.get("/api/ads/offer")
    async def offer(request: Request) -> JSONResponse:
        active = await session_check(request)
        if active is None:
            return _error(401, "unauthenticated")
        params = request.query_params
        if set(params) != {"placement", "event_id"} or any(len(params.getlist(k)) > 1 for k in params):
            return _error(400, "bad_request")
        placement, event_key = params["placement"], params["event_id"]
        if placement not in policy.PLACEMENTS or not EVENT_KEY.match(event_key):
            return _error(400, "bad_request")
        country = country_from(request.headers.get(COUNTRY_HEADER), trust_country)
        return JSONResponse(await run_in_threadpool(offer_sync, active[1].user_id, country, placement, event_key))

    def current_variant(t: Token) -> tuple[policy.Campaign, policy.Variant] | None:
        """The offer as it stands NOW: a paused/ended campaign or a removed variant is gone."""
        current = reader.current()
        ts = now()
        for c in current.campaigns if current else ():
            if c.id == t.campaign_id and c.starts_at <= ts < c.ends_at and t.placement in c.placements:
                for v in c.variants:
                    if v.key == t.variant:
                        return c, v
        return None

    def verified(raw: object, user_id: int) -> Token | JSONResponse:
        try:
            return tokens.verify(raw, user_id, now())
        except TokenError as exc:
            return _error(403, str(exc))

    @app.post("/api/ads/impression")
    async def impression(request: Request) -> JSONResponse:
        _, session = await write_check(request)
        t = verified(await read_token(request), session.user_id)
        if isinstance(t, JSONResponse):
            return t
        country = country_from(request.headers.get(COUNTRY_HEADER), trust_country)

        def record() -> bool:
            if current_variant(t) is None:
                return False
            return telemetry.exposure("impression", t.user_id, t.campaign_id, t.variant, t.placement, t.event_key,
                                      country, now())
        counted = await run_in_threadpool(record)
        return JSONResponse({"ok": True, "counted": counted})

    @app.post("/api/ads/click")
    async def click(request: Request) -> JSONResponse:
        _, session = await write_check(request)
        t = verified(await read_token(request), session.user_id)
        if isinstance(t, JSONResponse):
            return t
        country = country_from(request.headers.get(COUNTRY_HEADER), trust_country)

        def record() -> str | None:
            found = current_variant(t)
            if found is None:
                return None
            c, v = found
            url = policy.check_url(v.url, c.hosts)  # already checked on read; once more right before it leaves
            telemetry.click(t.user_id, t.campaign_id, t.variant, t.placement, t.event_key, country, now())
            return url
        url = await run_in_threadpool(record)
        if url is None:
            return _error(410, "offer_gone")
        return JSONResponse({"url": url})

    @app.post("/api/ads/age")
    async def confirm_age(request: Request) -> JSONResponse:
        _, session = await write_check(request)
        age = (await read_json(request, {"min"}))["min"]
        if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= policy.MAX_AGE:
            return _error(400, "bad_request")
        await run_in_threadpool(telemetry.confirm_age, session.user_id, age, now())
        return JSONResponse({"ok": True})
