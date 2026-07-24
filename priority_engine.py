"""Match Priority Engine (MVP) — deterministic, offline event-interest scoring.

``priority_score`` answers exactly one question: how interesting/important is
this event to show and how high to rank it. It must NEVER be confused with:

  * ``data_quality_score``  — how complete the analytical data is (not built here)
  * ``confidence_score``    — how reliable a generated forecast is (not built here)
  * ``value_score``         — market mispricing potential (not built here)

which are separate, future concerns (see the Match Priority Engine design
notes). This module computes ONLY priority, from signals that are the same
for every viewer (no personalization, no language/region boost in MVP).

Explicitly excluded from the MVP total (fields kept for future extension,
never contribute to ``total`` right now):
  * ``team_strength``          — no reliable source (no independent ratings/
                                  standings) without a new external API.
  * ``match_competitiveness``  — odds are only present for a random subset of
                                  matches (whatever a user happened to open
                                  before), so using them would make ranking
                                  depend on other users' prior activity.

Nothing here touches the network or the database: all inputs are plain
values passed in by the caller (event_list.py / handlers), matching the
"pure, offline-testable" style of match_validation.py / metrics.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from priority_config import (
    DEFAULT_STAGE_POINTS,
    DEFAULT_TOURNAMENT_TIER,
    DEMAND_CAP_REFERENCE,
    DEMAND_MAX_POINTS,
    DERBY_PAIR_KEYS,
    DERBY_POINTS,
    STAGE_POINTS,
    TEAM_POPULARITY_MAX_TOTAL,
    TEAM_POPULARITY_POINTS,
    TEAM_POPULARITY_TIER1_KEYS,
    TEAM_POPULARITY_TIER2_KEYS,
    TEAM_POPULARITY_TIER3_KEYS,
    TIME_PROXIMITY_BANDS,
    TIME_PROXIMITY_DEFAULT,
    TIME_PROXIMITY_LIVE,
    TOURNAMENT_TIER_POINTS,
    detect_stage,
    normalize_participant_tokens,
    tournament_tier,
)


@dataclass(frozen=True)
class PriorityInput:
    """Everything compute_priority needs, already resolved by the caller.
    No network/DB access happens from here on."""

    league_name: str
    country: Optional[str]
    home: str
    away: str
    is_live: bool
    kickoff_utc: Optional[datetime]
    now_utc: datetime
    # Stage/round text ONLY (e.g. "Play-off", "Semi-final") — already
    # separated from the competition name by event_list._resolve_competition.
    # tournament_stage is computed from THIS field alone, never from
    # league_name/country, so a competition's own name can never be misread
    # as a round (and vice versa).
    stage_hint: str = ""
    # Pre-aggregated unique-user demand count for this event (see
    # db.db_match_demand); 0 when unknown/unavailable.
    demand_count: int = 0


@dataclass(frozen=True)
class PriorityBreakdown:
    """Component-level score trace, 0-100 total. ``team_strength`` and
    ``match_competitiveness`` are always None in the MVP — present only so the
    shape is stable once they are implemented, and to make it explicit in code
    that they are NOT silently folded into ``total``."""

    tournament_prestige: float
    tournament_stage: float
    team_popularity: float
    team_strength: Optional[float]
    derby_or_rivalry: float
    match_competitiveness: Optional[float]
    time_proximity: float
    internal_user_demand: float
    total: int
    reasons: tuple[str, ...]


# ─── Component scorers ─────────────────────────────────────────────────────────
def _tournament_prestige_points(league_name: str, country: Optional[str]) -> tuple[float, Optional[str]]:
    tier = tournament_tier(league_name, country)
    points = TOURNAMENT_TIER_POINTS.get(tier, TOURNAMENT_TIER_POINTS[DEFAULT_TOURNAMENT_TIER])
    reason = f"tournament_tier_{tier}" if tier < DEFAULT_TOURNAMENT_TIER else None
    return points, reason


def _tournament_stage_points(stage_raw: str) -> tuple[float, Optional[str]]:
    """Stage/round score computed ONLY from stage_raw — never from the
    competition name/country, so a competition's prestige text can never be
    misread as a stage (and vice versa)."""
    stage = detect_stage(stage_raw)
    if stage is None:
        return DEFAULT_STAGE_POINTS, None
    return STAGE_POINTS.get(stage, DEFAULT_STAGE_POINTS), f"stage_{stage}"


def _team_tier(name: str) -> int:
    key = normalize_participant_tokens(name)
    if not key:
        return 0
    if key in TEAM_POPULARITY_TIER1_KEYS:
        return 1
    if key in TEAM_POPULARITY_TIER2_KEYS:
        return 2
    if key in TEAM_POPULARITY_TIER3_KEYS:
        return 3
    return 0


def _team_popularity_points(home: str, away: str) -> tuple[float, list[str]]:
    total = 0.0
    reasons: list[str] = []
    for name in (home, away):
        tier = _team_tier(name)
        if tier:
            total += TEAM_POPULARITY_POINTS[tier]
            reasons.append(f"popular_team_tier{tier}")
    return min(total, TEAM_POPULARITY_MAX_TOTAL), reasons


def _is_derby(home: str, away: str) -> bool:
    pair_key = frozenset({normalize_participant_tokens(home), normalize_participant_tokens(away)})
    return pair_key in DERBY_PAIR_KEYS


def _derby_points(home: str, away: str) -> tuple[float, Optional[str]]:
    if _is_derby(home, away):
        return DERBY_POINTS, "derby"
    return 0.0, None


def _time_proximity_points(kickoff_utc: Optional[datetime], now_utc: datetime,
                           is_live: bool) -> tuple[float, Optional[str]]:
    if is_live:
        return TIME_PROXIMITY_LIVE, "live"
    if kickoff_utc is None:
        return TIME_PROXIMITY_DEFAULT, None
    hours_until = (kickoff_utc - now_utc).total_seconds() / 3600.0
    if hours_until < 0:
        hours_until = 0.0  # already underway without a live flag: treat as most imminent
    for max_hours, points in TIME_PROXIMITY_BANDS:
        if hours_until <= max_hours:
            return points, f"kickoff_within_{max_hours:g}h"
    return TIME_PROXIMITY_DEFAULT, None


def _demand_points(demand_count: int) -> float:
    """Logarithmic, capped bonus: a handful of extra requests from the SAME
    users barely moves the needle, and no volume of demand can exceed
    DEMAND_MAX_POINTS — spam from one/few users cannot rebuild the whole
    ranking (see db.db_match_demand for the unique-user counting)."""
    if demand_count <= 0:
        return 0.0
    points = DEMAND_MAX_POINTS * math.log1p(demand_count) / math.log1p(DEMAND_CAP_REFERENCE)
    return min(DEMAND_MAX_POINTS, points)


# ─── Public API ─────────────────────────────────────────────────────────────────
def compute_priority(inp: PriorityInput) -> PriorityBreakdown:
    """Deterministic priority score in [0, 100]. Pure function: same input
    always yields the same output, no I/O."""
    reasons: list[str] = []

    prestige, r = _tournament_prestige_points(inp.league_name, inp.country)
    if r:
        reasons.append(r)

    stage_points, r = _tournament_stage_points(inp.stage_hint)
    if r:
        reasons.append(r)

    popularity, pop_reasons = _team_popularity_points(inp.home, inp.away)
    reasons.extend(pop_reasons)

    derby_points, r = _derby_points(inp.home, inp.away)
    if r:
        reasons.append(r)

    time_points, r = _time_proximity_points(inp.kickoff_utc, inp.now_utc, inp.is_live)
    if r:
        reasons.append(r)

    demand_points = _demand_points(inp.demand_count)
    if demand_points > 0:
        reasons.append("has_demand")

    total = prestige + stage_points + popularity + derby_points + time_points + demand_points
    total_clamped = max(0, min(100, round(total)))

    return PriorityBreakdown(
        tournament_prestige=prestige,
        tournament_stage=stage_points,
        team_popularity=popularity,
        team_strength=None,
        derby_or_rivalry=derby_points,
        match_competitiveness=None,
        time_proximity=time_points,
        internal_user_demand=demand_points,
        total=total_clamped,
        reasons=tuple(reasons),
    )
