"""Mini App side of the ads module: snapshot reader, activity segments, offers, impressions, clicks.

The reader re-reads `ads-policy.json` when it changed (checked at most every RELOAD_S), validates it
completely and rejects any version below the applied mark it keeps in `ads-policy.applied`; a bad file
keeps the last good policy. Offers are chosen on the server for the verified session's user only; the
client gets texts and a signed impression token, never a link (the link comes from the CURRENT snapshot
on click, so a paused campaign or a changed link takes effect at once).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from miniapp.ads import policy, targeting
from miniapp.ads.policy import Policy, PolicyError
from miniapp.ads.telemetry import DAY, Telemetry, day_of, days_back

logger = logging.getLogger("miniapp.ads")

RELOAD_S = 30
TOKEN_DAYS = 2  # a token from yesterday's offer still counts (the card may stay open over midnight)
EVENT_KEY = re.compile(r"^[A-Za-z0-9:_.-]{1,40}$")
MARKET_KEY = re.compile(r"^[A-Za-z0-9_.+-]{1,12}$")
COUNTRY = re.compile(r"^[A-Z]{2}$")


class RecentPicks:
    """Matches the server itself recommended to a user, per placement (a successful quick pick, an AI recommended
    bet), with the recommended market, for PICK_TTL_S. An offer, and so an impression token, exists only for one of
    them: a client cannot invent event ids to inflate impressions, and the offer's price is looked up for exactly
    the market the server recommended. Kept in telemetry.db, so every worker sees the same picks; an unreadable
    file = no offer."""

    PICK_TTL_S = 6 * 3600

    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    def add(self, user_id: int, picks: list[tuple[str, str | None]], now: float, placement: str = "quick_pick") -> None:
        if placement not in policy.PLACEMENTS:
            raise ValueError(placement)
        valid = [(e, m if isinstance(m, str) and MARKET_KEY.match(m) else None)
                 for e, m in picks if isinstance(e, str) and EVENT_KEY.match(e)]
        if valid:
            self.telemetry.add_picks(user_id, placement, valid[:20], int(now) + self.PICK_TTL_S)

    def get(self, user_id: int, placement: str, event_id: str, now: float) -> tuple[bool, str | None]:
        """(recommended, market) — (False, None) when not recommended or unreadable."""
        try:
            return self.telemetry.pick(user_id, placement, event_id, int(now))
        except sqlite3.Error:
            return False, None


class PolicyReader:
    def __init__(self, path: Path, applied_path: Path, public_key: Ed25519PublicKey,
                 clock: Callable[[], float]) -> None:
        self.path = path
        self.public_key = public_key
        self.applied_path = applied_path
        self.clock = clock
        self._lock = threading.Lock()
        self._policy: Policy | None = None
        self._stamp: tuple[int, int] | None = None
        self._checked_at: float | None = None

    def _applied(self) -> int:
        try:
            value = int(self.applied_path.read_text().strip())
        except (OSError, ValueError):
            return 0  # a lost mark accepts the current snapshot (the only one that exists)
        # A mark far in the future would refuse every real snapshot: treated like a lost mark.
        return value if policy.plausible_version(value, self.clock()) else 0

    def current(self, fresh: bool = False) -> Policy | None:
        """The applied policy. Offers may use a copy up to RELOAD_S old; `fresh` (a click: the link leaves the
        app) always checks the file now — one stat, a re-read only when it changed — so a paused or replaced
        campaign is never clicked through once its publication is on disk."""
        with self._lock:
            now = self.clock()
            if not fresh and self._checked_at is not None and 0 <= now - self._checked_at < RELOAD_S:
                return self._policy
            self._checked_at = now
            try:
                st = self.path.stat()
            except OSError:
                return self._policy  # no file yet (or a moment between writes): keep what we have
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp == self._stamp:
                return self._policy
            try:
                loaded = policy.parse(self.path.read_bytes(), self.public_key)
            except (OSError, PolicyError) as exc:
                logger.warning("event=ads_policy_rejected reason=%s", exc)
                self._stamp = stamp
                return self._policy
            applied = self._applied()
            if loaded.version < applied or (self._policy is not None and loaded.version < self._policy.version):
                logger.warning("event=ads_policy_rejected reason=older_version version=%s", loaded.version)
                self._stamp = stamp
                return self._policy
            self._policy, self._stamp = loaded, stamp
            if loaded.version > applied:
                try:
                    policy.write_atomic(self.applied_path, str(loaded.version).encode(), mode=0o660)
                except OSError as exc:
                    logger.warning("event=ads_applied_mark_failed type=%s", type(exc).__name__)
            logger.info("event=ads_policy_applied version=%s campaigns=%s", loaded.version, len(loaded.campaigns))
            return self._policy


@dataclass(frozen=True)
class Token:
    user_id: int
    campaign_id: int
    variant: str
    event_key: str
    placement: str
    day: str
    market: str | None = None  # the market recommended when the offer was issued: its price is the one re-checked


class TokenError(ValueError):
    pass


class Tokens:
    """Impression token = payload + HMAC(user, campaign, variant, event, placement, day, market): binds an offer to
    the user it was chosen for and the market whose price it showed; another user's token, a forged or an old one is
    refused."""

    def __init__(self, key_material: str) -> None:
        self._key = hashlib.sha256(("ads-impression-token:" + key_material).encode()).digest()

    def issue(self, t: Token) -> str:
        payload = json.dumps([t.user_id, t.campaign_id, t.variant, t.event_key, t.placement, t.day, t.market],
                             separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(payload).rstrip(b"=")
        sig = base64.urlsafe_b64encode(hmac.new(self._key, body, hashlib.sha256).digest()).rstrip(b"=")
        return (body + b"." + sig).decode()

    def verify(self, raw: object, user_id: int, now: int) -> Token:
        if not isinstance(raw, str) or len(raw) > 400 or raw.count(".") != 1:
            raise TokenError("bad_token")
        body, sig = raw.encode().split(b".")
        expected = base64.urlsafe_b64encode(hmac.new(self._key, body, hashlib.sha256).digest()).rstrip(b"=")
        if not hmac.compare_digest(sig, expected):
            raise TokenError("bad_token")
        try:
            uid, cid, variant, event_key, placement, day, market = json.loads(
                base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
        except (ValueError, TypeError):
            raise TokenError("bad_token") from None
        if uid != user_id:
            raise TokenError("bad_token")
        if day not in {day_of(now - d * DAY) for d in range(TOKEN_DAYS)}:
            raise TokenError("token_expired")
        if market is not None and not isinstance(market, str):
            raise TokenError("bad_token")
        return Token(uid, cid, variant, event_key, placement, day, market)


def country_from(header: str | None, trusted: bool) -> str | None:
    """CF-IPCountry only behind the verified edge; XX/T1/absent/garbage = unknown. Never stored in a profile."""
    if not trusted or not header:
        return None
    value = header.strip().upper()
    if not COUNTRY.match(value) or value in targeting.UNKNOWN_COUNTRIES:
        return None
    return value


class Segments:
    """Activity segments of one viewer over the Mini App's own data (users table + telemetry), evaluated on
    demand and cached for the one request."""

    def __init__(self, telemetry: Telemetry, user_id: int, created_at: int | None, now: int) -> None:
        self.telemetry = telemetry
        self.user_id = user_id
        self.created_at = created_at
        self.now = now
        self._rows: list[dict] | None = None

    def _activity(self) -> list[dict]:
        if self._rows is None:
            self._rows = self.telemetry.activity(self.user_id, days_back(self.now, 400))
        return self._rows

    def _since(self, days: int) -> list[dict]:
        start = days_back(self.now, days - 1)  # "within 7 days" = today and the 6 days before
        return [r for r in self._activity() if r["day"] >= start]

    def viewer_class(self) -> str | None:
        """The coarse class stored with each ad event for analytics by segment (SEGMENT_CLASSES): registered
        within 7 days, else active on 3+ days of the last 7, else other. None when the data is unreadable."""
        try:
            if self("new", {"days": 7}):
                return "new"
            return "active" if self("active", {"days": 7, "min_visits": 3}) else "other"
        except sqlite3.Error:
            return None

    def __call__(self, name: str, params: dict[str, int]) -> bool:
        today = day_of(self.now)
        if name == "new":
            return self.created_at is not None and self.created_at >= self.now - params["days"] * DAY
        if name == "active":
            return len([r for r in self._since(params["days"]) if r["requests"] > 0]) >= params["min_visits"]
        if name == "inactive":  # the last visit before today was N or more days ago (a first visit is not "inactive")
            earlier = [r["day"] for r in self._activity() if r["day"] < today and r["requests"] > 0]
            return bool(earlier) and _gap(earlier[-1], today) >= params["days"]
        if name == "returned":  # a visit within M days that came after a break of at least N days
            visits = [r["day"] for r in self._activity() if r["requests"] > 0]
            start = days_back(self.now, params["within_days"] - 1)
            for i, day in enumerate(visits):
                if day < start:
                    continue
                previous = visits[i - 1] if i else None
                if previous is not None and _gap(previous, day) >= params["gap_days"]:
                    return True
            return False
        if name == "forecasts":
            return sum(r["forecasts"] for r in self._since(params["days"])) >= params["min"]
        if name == "coupons":
            return sum(r["coupons"] for r in self._since(params["days"])) >= params["min"]
        if name == "offer_clicks":
            return self.telemetry.clicks_since(self.user_id, self.now - params["days"] * DAY) >= params["min"]
        return False


def _gap(a: str, b: str) -> int:
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def policy_paths(db_path: Path) -> tuple[Path, Path]:
    """The snapshot and the applied mark live next to the Mini App database (the shared data volume)."""
    return db_path.parent / "ads-policy.json", db_path.parent / "ads-policy.applied"
