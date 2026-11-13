"""HTTP API of partner offers (ads-architecture.md §6). Registered only with MINIAPP_ADS_ENABLED=true; with the
flag off these paths do not exist (404) and nothing about ads is read or written.

- GET  /api/ads/offer?placement=…&event_id=…  -> {"offer": null} | {"offer": {...texts, odds, impressionToken}} | ageGate
- POST /api/ads/impression {impressionToken}   -> one per (user, campaign, variant, event, day)
- POST /api/ads/click {impressionToken}         -> {"url"} from the CURRENT snapshot; repeated taps within 10 s count once
- POST /api/ads/age {"min": N}                  -> the user states "I am N or older" (the gate's own age)
- GET  /api/ads/postback?partner=&click_id=&txn=[&event=] + X-Postback-Token header, or POST the same fields with
  `token` as a form/JSON body                   -> a partner's conversion (server to server)
Every answer is personal: `Cache-Control: no-store` (the app guard) and no shared cache key.

The price in an offer is never the campaign's: it is the verified odds store's current price of the market the
server recommended for that match (`odds_for`), or nothing. A campaign with `oddsMode: required` is not offered
(and its click is refused) while there is no such price.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from miniapp.ads import engine, policy
from miniapp.ads.service import (EVENT_KEY, PolicyReader, RecentPicks, Segments, Token, TokenError, Tokens,
                                 country_from)
from miniapp.ads.telemetry import Telemetry, day_of

logger = logging.getLogger("miniapp.ads")

COUNTRY_HEADER = "cf-ipcountry"
POSTBACK_TXN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
POSTBACK_EVENT = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
POSTBACK_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
CLICK_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_POSTBACKS_PER_MINUTE = 600  # authenticated postbacks per process; forged ones never count
MAX_POSTBACK_ATTEMPTS_PER_MINUTE = 3000  # every postback request, before any work (bounds forged traffic)
POSTBACK_TOKEN_HEADER = "x-postback-token"
MAX_POSTBACK_BODY = 2048

# (event id, market) -> {"market", "odds", "fetchedAt", "validFor"} from the verified odds store, or None.
OddsLookup = Callable[[str, str], Awaitable[dict | None]]


def postback_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
    picks: RecentPicks,
    odds_for: OddsLookup | None = None,
) -> None:
    """`write_check` does Origin + session + CSRF and returns (token, session); `read_json` reads a strict JSON
    object with exactly the given keys; `odds_for` reads the verified odds store (None: offers carry no price)."""

    async def read_token(request: Request) -> object:
        return (await read_json(request, {"impressionToken"}))["impressionToken"]

    def safe(fn: Callable[[], Any], fallback: Any, what: str) -> Any:
        """Telemetry is not authoritative: a failed read or write never breaks an answer (logged by code only)."""
        try:
            return fn()
        except sqlite3.Error as exc:
            logger.warning("event=ads_telemetry_failed op=%s type=%s", what, type(exc).__name__)
            return fallback

    def viewer(user_id: int, lang: str, country: str | None, ts: int) -> tuple[engine.Viewer, bool]:
        """The viewer and whether the age statement could be read (unreadable = no age: gated offers denied)."""
        age = safe(lambda: telemetry.confirmed_age(user_id), None, "age_read")
        return engine.Viewer(user_id=user_id, lang=lang, country=country, confirmed_age=age or 0,
                             segment=Segments(telemetry, user_id, store.user_created_at(user_id), ts),
                             exposures=lambda cid, ev: telemetry.impression_counts(user_id, cid, ev, ts)), age is not None

    def viewer_class(user_id: int, ts: int) -> str | None:
        return Segments(telemetry, user_id, store.user_created_at(user_id), ts).viewer_class()

    async def price(user_id: int, placement: str, event_key: str) -> tuple[bool, str | None, dict | None]:
        """(recommended to this user here, market, the verified price of that market now or None)."""
        picked, market = await run_in_threadpool(picks.get, user_id, placement, event_key, now())
        if not picked or market is None or odds_for is None:
            return picked, market, None
        try:
            found = await odds_for(event_key, market)
        except Exception as exc:  # noqa: BLE001 — no price is a normal answer, never a failed offer
            logger.warning("event=ads_odds_failed type=%s", type(exc).__name__)
            return picked, market, None
        return picked, market, found if isinstance(found, dict) and found.get("market") == market else None

    def offer_sync(user_id: int, country: str | None, placement: str, event_key: str, odds: dict | None) -> dict:
        ts = now()
        safe(lambda: telemetry.maybe_purge(ts), None, "purge")
        user = store.get_user(user_id)
        if user is None:
            return {"offer": None}
        current = reader.current()
        v, age_known = viewer(user_id, user.lang, country, ts)
        has_odds = odds is not None
        choice = engine.choose(current, v, placement, ts, event_key, has_odds)
        if choice is None:
            if not age_known:
                return {"offer": None}
            # Would a partner with a higher age requirement have an offer for this user? Then ask for that age.
            maybe = engine.choose(current, engine.Viewer(user_id, user.lang, country, policy.MAX_AGE, v.segment,
                                                         v.exposures), placement, ts, event_key, has_odds)
            if maybe is not None and maybe.campaign.age_min > v.confirmed_age:
                return {"offer": None, "ageGate": {"min": maybe.campaign.age_min}}
            return {"offer": None}
        c = choice.campaign
        if choice.variant is None:  # control group: nothing shown, the exposure is counted for the comparison
            safe(lambda: telemetry.exposure("control", user_id, c.id, "control", placement, event_key, country, ts,
                                            viewer_class(user_id, ts)), False, "control")
            return {"offer": None}
        variant = choice.variant
        token = tokens.issue(Token(user_id, c.id, variant.key, event_key, placement, day_of(ts)))
        return {"offer": {
            "id": c.id, "variant": variant.key, "headline": variant.headline, "body": variant.body,
            "extra": variant.extra, "cta": variant.cta,
            "disclosure": {k: v for k, v in c.disclosure.items() if v},
            # Only from the odds store, for the recommended market; null = shown without a price.
            "odds": odds, "oddsRequired": c.odds_mode == "required",
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
        user_id = active[1].user_id
        # Only for a match the server itself recommended to this user in this placement.
        picked, _, odds = await price(user_id, placement, event_key)
        if not picked:
            return JSONResponse({"offer": None})
        return JSONResponse(await run_in_threadpool(offer_sync, user_id, country, placement, event_key, odds))

    def current_variant(t: Token, country: str | None, has_odds: bool,
                        fresh: bool = False) -> tuple[policy.Campaign, policy.Variant] | None:
        """The offer as it stands NOW for this user: the campaign is still in the snapshot, and the user — with the
        current country, age statement, audience, caps and price — is still eligible and still assigned this
        variant. A paused campaign, a narrowed audience or a raised age minimum takes effect on a token issued
        before; so does a required price that is gone."""
        current = reader.current(fresh=fresh)
        if current is None:
            return None
        ts = now()
        user = store.get_user(t.user_id)
        if user is None:
            return None
        v, _ = viewer(t.user_id, user.lang, country, ts)
        for c in current.campaigns:
            if c.id != t.campaign_id:
                continue
            if not engine.eligible(c, v, t.placement, ts, current.audiences, t.event_key, has_odds):
                return None
            assigned = engine.assign(c, t.user_id)
            if assigned is None or assigned.key != t.variant:
                return None
            return c, assigned
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
        _, _, odds = await price(t.user_id, t.placement, t.event_key)

        def record() -> bool:
            found = current_variant(t, country, odds is not None)
            if found is None:
                return False
            c = found[0]
            ts = now()
            return safe(lambda: telemetry.exposure("impression", t.user_id, t.campaign_id, t.variant, t.placement,
                                                   t.event_key, country, ts, viewer_class(t.user_id, ts),
                                                   (c.cap_per_day, c.cap_total)), False, "impression")
        counted = await run_in_threadpool(record)
        return JSONResponse({"ok": True, "counted": counted})

    @app.post("/api/ads/click")
    async def click(request: Request) -> JSONResponse:
        _, session = await write_check(request)
        t = verified(await read_token(request), session.user_id)
        if isinstance(t, JSONResponse):
            return t
        country = country_from(request.headers.get(COUNTRY_HEADER), trust_country)
        _, _, odds = await price(t.user_id, t.placement, t.event_key)

        def record() -> str | None:
            # The newest snapshot on disk, not a cached one.
            found = current_variant(t, country, odds is not None, fresh=True)
            if found is None:
                return None
            c, v = found
            click_id = secrets.token_urlsafe(18)
            # Already checked on read; once more, with this click's id in place, right before it leaves.
            url = policy.render_url(v.url, click_id, c.hosts)
            ts = now()
            # A lost click record must not cost the user the link (its conversion is then unattributed).
            safe(lambda: telemetry.click(t.user_id, t.campaign_id, t.variant, t.placement, t.event_key, country, ts,
                                         viewer_class(t.user_id, ts), click_id, c.partner_id), False, "click")
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
        saved = await run_in_threadpool(
            safe, lambda: telemetry.confirm_age(session.user_id, age, now()) or True, False, "age_write")
        if not saved:
            return _error(503, "unavailable")
        return JSONResponse({"ok": True})

    accepted: deque[float] = deque()
    attempts: deque[float] = deque()

    def admit(window: deque[float], limit: int, ts: float) -> bool:
        while window and ts - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(ts)
        return True

    async def postback_fields(request: Request) -> dict[str, str] | JSONResponse:
        """GET: fields in the query string, the token ONLY in the X-Postback-Token header. POST (for platforms that
        cannot set headers): fields and the token in a form or JSON body. A token in a URL is refused: URLs end up in
        proxy and access logs."""
        if "token" in request.query_params:
            return _error(400, "token_in_url")
        if request.method == "GET":
            params = request.query_params
            if any(len(params.getlist(k)) > 1 for k in params):
                return _error(400, "bad_request")
            fields = dict(params)
            header = request.headers.get(POSTBACK_TOKEN_HEADER)
            if header is not None:
                fields["token"] = header
            return fields
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_POSTBACK_BODY):
            return _error(413, "too_large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_POSTBACK_BODY:
                return _error(413, "too_large")
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        try:
            if kind == "application/json":
                data = json.loads(body)
                if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
                    return _error(400, "bad_request")
                fields = data
            elif kind == "application/x-www-form-urlencoded":
                pairs = parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=bool(body))
                if len({k for k, _ in pairs}) != len(pairs):
                    return _error(400, "bad_request")
                fields = dict(pairs)
            else:
                return _error(415, "unsupported_media_type")
        except (ValueError, UnicodeDecodeError):
            return _error(400, "bad_request")
        header = request.headers.get(POSTBACK_TOKEN_HEADER)
        if header is not None:
            if "token" in fields:
                return _error(400, "bad_request")
            fields["token"] = header
        return fields

    @app.api_route("/api/ads/postback", methods=["GET", "POST"])
    async def postback(request: Request) -> JSONResponse:
        """A partner's server reports a conversion of one of our clicks. Authenticated by the partner's own token
        (only its SHA-256 is in the signed snapshot); idempotent by (partner, txn). No session, no cookie."""
        if not admit(attempts, MAX_POSTBACK_ATTEMPTS_PER_MINUTE, now()):
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"})
        fields = await postback_fields(request)
        if isinstance(fields, JSONResponse):
            return fields
        if not {"partner", "click_id", "txn", "token"} <= set(fields) <= {"partner", "click_id", "txn", "event", "token"}:
            return _error(400, "bad_request")
        partner, click_id, txn, token = fields["partner"], fields["click_id"], fields["txn"], fields["token"]
        event = fields.get("event", "conversion")
        if (not partner.isdigit() or len(partner) > 15 or not POSTBACK_TOKEN.match(token)
                or not CLICK_ID.match(click_id) or not POSTBACK_TXN.match(txn) or not POSTBACK_EVENT.match(event)):
            return _error(400, "bad_request")
        current = await run_in_threadpool(reader.current, True)
        expected = (current.postback_hashes or {}).get(int(partner)) if current is not None else None
        if expected is None or not hmac.compare_digest(postback_token_hash(token), expected):
            return _error(403, "forbidden")
        ts = now()
        if not admit(accepted, MAX_POSTBACKS_PER_MINUTE, ts):
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"})
        try:
            status = await run_in_threadpool(telemetry.record_conversion, int(partner), txn, click_id, event, ts)
        except sqlite3.Error as exc:
            logger.warning("event=ads_postback_failed type=%s", type(exc).__name__)
            return _error(503, "unavailable")  # the partner retries; the txn id keeps the retry idempotent
        logger.info("event=ads_postback partner=%s status=%s", partner, status)
        return JSONResponse({"status": status})
