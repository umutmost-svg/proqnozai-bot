"""The signed odds push from the bot (POST /api/internal/odds).

Wire format (the bot's mostbet_feed.py builds it):

    headers  X-Odds-Timestamp: <unix seconds>
             X-Odds-Signature: hex(HMAC-SHA256(secret, "<timestamp>." + body))
    body     {"v": 1, "nonce": "<16-64 hex>", "lines": [
                {"id": 123, "country": "England", "league": "Premier League",
                 "home": "Arsenal", "away": "Chelsea", "kickoff": "2026-11-09T15:00:00Z",
                 "live": false, "fetchedAt": "2026-11-09T14:58:30Z",
                 "odds": {"w1": 2.1, "x": 3.4, "w2": 3.5, ...}}, ...]}

Checks, in order: size, timestamp within MAX_SKEW_S of our clock, signature (constant
time), JSON shape, nonce never seen within the replay window. Only then are lines read;
a malformed line is skipped on its own (the push is authentic, one line can still be
odd). The channel is write-only: it answers with counts, never with data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .book import read_book

MAX_BODY_BYTES = 512 * 1024
MAX_LINES = 600
MAX_SKEW_S = 120.0
REPLAY_WINDOW_S = 2 * MAX_SKEW_S
MAX_NONCES = 5000
TIMESTAMP_HEADER = "X-Odds-Timestamp"
SIGNATURE_HEADER = "X-Odds-Signature"
_NONCE = re.compile(r"^[0-9a-f]{16,64}$")
_TEXT_MAX = 120
# A fetch time later than the push itself is impossible; a little clock noise is allowed.
_FUTURE_SLACK_S = 5.0
# Delivery takes time that cannot be measured (it is mixed with clock skew): a price's servable
# life is shortened by this much (store.py), more than the sender's own push timeout
# (mostbet_feed.PUSH_TIMEOUT_S), so a slow delivery never lets it outlive its 15 minutes.
# The fetch time itself (shown to users) is not shifted.
TRANSIT_ALLOWANCE_S = 15.0


class FeedError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code  # too_large | stale | bad_signature | bad_payload | replayed
        self.status = status


@dataclass(frozen=True)
class FeedLine:
    line_id: int
    country: str
    league: str
    home: str
    away: str
    kickoff: float
    live: bool
    fetched_at: float
    book: dict[str, float]  # our market key -> decimal odds (checked groups only)


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _instant(v: object) -> float | None:
    if not isinstance(v, str) or not v.endswith("Z") or len(v) > 30:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s if 0 < len(s) <= _TEXT_MAX and s.isprintable() else None


def _line(raw: object, sent: float, offset: float) -> FeedLine | None:
    """`sent`: the push's own timestamp (the bot's clock); `offset`: our clock minus the bot's.
    Fetch times are the bot's: checked against its own clock, then converted to ours, so a few
    seconds or minutes of skew between the two machines never shifts how long prices live."""
    if not isinstance(raw, dict):
        return None
    line_id = raw.get("id")
    if isinstance(line_id, bool) or not isinstance(line_id, int) or not 0 < line_id < 2 ** 53:
        return None
    country, league, home, away = (_text(raw.get(k)) for k in ("country", "league", "home", "away"))
    kickoff, fetched = _instant(raw.get("kickoff")), _instant(raw.get("fetchedAt"))
    live, odds = raw.get("live"), raw.get("odds")
    if None in (country, home, away, kickoff, fetched) or not isinstance(live, bool) or not isinstance(odds, dict):
        return None
    # International competitions may give the stage as an empty league.
    league = league if league is not None else ("" if raw.get("league") == "" else None)
    if league is None or not math.isfinite(fetched) or fetched > sent + _FUTURE_SLACK_S:
        return None
    # In our clock, and never later than the moment the push was sent (by our clock).
    fetched = min(fetched, sent) + offset
    return FeedLine(line_id, country, league, home, away, kickoff, live, fetched, read_book(odds))  # type: ignore[arg-type]


class FeedReceiver:
    def __init__(self, secret: bytes, clock: Callable[[], float]) -> None:
        if len(secret) < 32:
            raise ValueError("the odds feed secret must be at least 32 bytes")
        self._secret = secret
        self._clock = clock
        self._nonces: OrderedDict[str, float] = OrderedDict()

    def verify(self, timestamp: str | None, signature: str | None, body: bytes) -> list[FeedLine]:
        if len(body) > MAX_BODY_BYTES:
            raise FeedError("too_large", 413)
        now = self._clock()
        if not timestamp or not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
            raise FeedError("bad_signature", 401)
        if abs(now - int(timestamp)) > MAX_SKEW_S:
            raise FeedError("stale", 401)
        expected = sign(self._secret, timestamp, body)
        if not signature or not hmac.compare_digest(signature.encode(), expected.encode()):
            raise FeedError("bad_signature", 401)
        try:
            payload = json.loads(body)
        except ValueError:
            raise FeedError("bad_payload", 400) from None
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise FeedError("bad_payload", 400)
        nonce, lines = payload.get("nonce"), payload.get("lines")
        if not isinstance(nonce, str) or not _NONCE.match(nonce) or not isinstance(lines, list) or len(lines) > MAX_LINES:
            raise FeedError("bad_payload", 400)
        self._forget(now)
        if nonce in self._nonces:
            raise FeedError("replayed", 409)
        self._nonces[nonce] = now
        while len(self._nonces) > MAX_NONCES:
            self._nonces.popitem(last=False)
        sent = float(int(timestamp))
        # now - sent is clock skew plus transit time; the two cannot be told apart, so a later
        # arrival never lengthens a price's life: only a bot clock AHEAD of ours shifts it back.
        offset = min(now - sent, 0.0)
        return [line for line in (_line(raw, sent, offset) for raw in lines) if line is not None]

    def _forget(self, now: float) -> None:
        while self._nonces:
            nonce, seen = next(iter(self._nonces.items()))
            if now - seen <= REPLAY_WINDOW_S:
                break
            self._nonces.popitem(last=False)
