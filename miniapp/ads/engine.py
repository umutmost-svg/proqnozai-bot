"""Choosing the offer for one viewer (ads-architecture.md §4-5): deterministic, server side only.

Eligible: inside the date window, the placement and language match, the country is allowed by the
partner narrowed by the campaign (unknown country only where both allow it), the age confirmation is
present when the partner requires one, and the audience condition holds. Order: tier (individual >
segment > general), priority descending, the later start first, the smaller id first. The first
eligible campaign wins; its A/B assignment may put the viewer into the control group — then nothing
is shown (no fall-through, so the control group stays clean).

Also eligible only: within the campaign's frequency caps (impressions of the viewer today on other matches, and
in all — today's impression of the match on screen excepted), and — for a campaign whose offer requires a price — when the verified odds store has a price of
the recommended market right now. A capped or price-less campaign lets the next one through (it is not an
experiment outcome).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from miniapp.ads import targeting
from miniapp.ads.policy import Campaign, Policy, Variant

TIER_RANK = {"individual": 0, "segment": 1, "general": 2}
BUCKETS = 10_000


@dataclass(frozen=True)
class Viewer:
    user_id: int
    lang: str
    country: str | None                 # ISO 3166-1 alpha-2 from the trusted edge, None = unknown
    confirmed_age: int = 0              # the highest age the user confirmed for a partner's gate; 0 = none
    # Activity segments over the Mini App's own data; evaluated lazily, only for campaigns that need it.
    # None (or a failing callback) = no data: a campaign whose condition needs a segment is not shown,
    # whatever the tree says (a negated segment must not turn "unknown" into "everyone").
    segment: Callable[[str, dict[str, int]], bool] | None = field(default=None, compare=False)
    # (campaign id, event key) -> (impressions today on other matches, impressions in all but today's one of this match).
    # None (or a failing callback) = unknown: a capped campaign is not shown.
    exposures: Callable[[int, str], tuple[int, int]] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Choice:
    campaign: Campaign
    variant: Variant | None             # None: the viewer is in the control group of this campaign


def order_key(c: Campaign) -> tuple[int, int, int, int]:
    return TIER_RANK[c.tier], -c.priority, -c.starts_at, c.id


def bucket(salt: str, user_id: int) -> int:
    """Stable bucket 0..9999 of a user within one experiment (the salt is fixed for the campaign's life)."""
    digest = hashlib.sha256(f"{salt}:{user_id}".encode()).digest()
    return int.from_bytes(digest, "big") % BUCKETS


def assign(c: Campaign, user_id: int) -> Variant | None:
    """Control group first (controlPct of the buckets), the rest split by the variants' weights in key order."""
    b = bucket(c.salt, user_id)
    control = c.control_pct * BUCKETS // 100
    if b < control:
        return None
    span = BUCKETS - control
    cumulative = 0
    for v in c.variants:
        cumulative += v.weight
        if b < control + span * cumulative // 100:
            return v
    return c.variants[-1]  # unreachable: weights sum to 100


def within_caps(c: Campaign, viewer: Viewer, event_key: str) -> bool:
    if not c.cap_per_day and not c.cap_total:
        return True
    if viewer.exposures is None:
        return False
    try:
        today, total = viewer.exposures(c.id, event_key)
    except Exception:  # noqa: BLE001 — unknown counts never lift a cap
        return False
    return (not c.cap_per_day or today < c.cap_per_day) and (not c.cap_total or total < c.cap_total)


def eligible(c: Campaign, viewer: Viewer, placement: str, now: int, audiences: dict[str, frozenset[int]],
             event_key: str = "", has_odds: bool = False) -> bool:
    if not c.starts_at <= now < c.ends_at or placement not in c.placements:
        return False
    if c.odds_mode == "required" and not has_odds:
        return False
    if c.lang_in and viewer.lang not in c.lang_in:
        return False
    country = None if viewer.country in targeting.UNKNOWN_COUNTRIES else viewer.country
    if country is None:
        if not c.allow_unknown_country:
            return False
    elif country not in c.countries:
        return False
    if c.age_min > viewer.confirmed_age:
        return False
    try:
        if not targeting.matches(c.condition, viewer, audiences):
            return False
    except targeting.SegmentUnavailable:
        return False
    return within_caps(c, viewer, event_key)  # last: the only check that reads the viewer's ad events


def choose(policy: Policy | None, viewer: Viewer, placement: str, now: int, event_key: str = "",
           has_odds: bool = False) -> Choice | None:
    if policy is None:
        return None
    for c in sorted(policy.campaigns, key=order_key):
        if eligible(c, viewer, placement, now, policy.audiences, event_key, has_odds):
            return Choice(c, assign(c, viewer.user_id))
    return None
