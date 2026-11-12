"""Choosing the offer for one viewer (ads-architecture.md §4-5): deterministic, server side only.

Eligible: inside the date window, the placement and language match, the country is allowed by the
partner narrowed by the campaign (unknown country only where both allow it), the age confirmation is
present when the partner requires one, and the audience condition holds. Order: tier (individual >
segment > general), priority descending, the later start first, the smaller id first. The first
eligible campaign wins; its A/B assignment may put the viewer into the control group — then nothing
is shown (no fall-through, so the control group stays clean).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from miniapp.ads import targeting
from miniapp.ads.policy import Campaign, Policy, Variant

TIER_RANK = {"individual": 0, "segment": 1, "general": 2}
BUCKETS = 10_000


def _no_segment(name: str, params: dict[str, int]) -> bool:
    return False


@dataclass(frozen=True)
class Viewer:
    user_id: int
    lang: str
    country: str | None                 # ISO 3166-1 alpha-2 from the trusted edge, None = unknown
    age_confirmed: bool = False
    # Activity segments over the Mini App's own data; evaluated lazily, only for campaigns that need it.
    segment: Callable[[str, dict[str, int]], bool] = field(default=_no_segment, compare=False)


@dataclass(frozen=True)
class Choice:
    campaign: Campaign
    variant: Variant | None             # None: the viewer is in the control group of this campaign


def order_key(c: Campaign) -> tuple[int, int, int, int]:
    return TIER_RANK[c.tier], -c.priority, -c.starts_at, c.id


def bucket(salt: str, user_id: int) -> int:
    """Stable bucket 0..9999 of a user within one experiment (the salt is fixed for the campaign's life)."""
    digest = hashlib.sha256(f"{salt}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


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


def eligible(c: Campaign, viewer: Viewer, placement: str, now: int, audiences: dict[str, frozenset[int]]) -> bool:
    if not c.starts_at <= now < c.ends_at or placement not in c.placements:
        return False
    if c.lang_in and viewer.lang not in c.lang_in:
        return False
    if viewer.country is None:
        if not c.allow_unknown_country:
            return False
    elif viewer.country not in c.countries:
        return False
    if c.age_min > 0 and not viewer.age_confirmed:
        return False
    return targeting.matches(c.condition, viewer, audiences)


def choose(policy: Policy | None, viewer: Viewer, placement: str, now: int) -> Choice | None:
    if policy is None:
        return None
    for c in sorted(policy.campaigns, key=order_key):
        if eligible(c, viewer, placement, now, policy.audiences):
            return Choice(c, assign(c, viewer.user_id))
    return None
