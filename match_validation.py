"""Deterministic match validation.

Before attaching football data or odds fetched from one source to a match
identified by another, we must be sure it is the *same* fixture. This module
compares a requested match against a candidate match on team names, kickoff
time, league and live/prematch state, and returns a confidence level:

    high   — safe to use the candidate's data.
    medium — use only when both teams and kickoff clearly match (e.g. league
             unknown, or home/away reported in swapped order).
    low    — reject; do not attach the candidate's data.

Everything here is pure and offline: no network, no clock dependence except an
explicit ``kickoff`` you pass in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

# Club/name noise tokens dropped before comparison (mirrors mostbet._NOISE so
# "FC Barcelona" and "Barcelona" match).
_NOISE = {
    "fc", "cf", "ac", "sc", "afc", "fk", "sk", "bk", "rsc", "rc", "ud", "cd",
    "sd", "club", "sporting", "atletico", "united", "city", "the",
}

# Team-name overlap needed to consider two names the same team.
_TEAM_THRESHOLD = 0.5
# Stronger overlap that, on its own, can carry a match when time is unknown.
_TEAM_STRONG = 0.75
# League-name overlap needed to consider two leagues the same.
_LEAGUE_THRESHOLD = 0.34
# Default kickoff tolerance: kickoff times within this many minutes are "same".
DEFAULT_TOLERANCE_MINUTES = 90


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def _tokens(name: Optional[str]) -> set[str]:
    name = (name or "").lower()
    name = re.sub(r"[^\w\s]", " ", name)
    return {t for t in name.split() if len(t) > 1 and t not in _NOISE}


def _overlap(a: Optional[str], b: Optional[str]) -> float:
    """Symmetric token-overlap score in [0, 1]."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


# A senior first team must never be silently matched to its women's, youth or
# reserve/B side (their short single-letter markers otherwise get dropped by
# token normalization, leaving e.g. "Barcelona" == "Barcelona B"). We detect
# the tier from the RAW name and refuse to attach data across a tier gap.
_WOMEN = re.compile(
    r"(?:\bw\b|\(w\)|\bwomen\b|\bwomens\b|\bladies\b|\bfemin\w*|\bfemenino\b|"
    r"\bfemminile\b|\bfrauen\b|\bdamen\b|\bkadin\w*)")
_YOUTH = re.compile(
    r"(?:\bu\s?\d{2}\b|\bunder\s?\d{2}\b|\byouth\b|\bjunior\w*|\bjnr\b|\bjr\b|"
    r"\bacademy\b|\bsub\s?\d{2}\b|\bprimavera\b|\bjong\b)")
_RESERVE = re.compile(
    r"(?:\bii\b|\bb\b|\bc\b|\breserves?\b|\bamateure?\b|\bcastilla\b)")


def _team_tier(name: Optional[str]) -> frozenset:
    """Tags for a team's tier: women / youth / reserve. Senior teams yield the
    empty set. Two names with different tag sets are NOT the same team."""
    n = (name or "").lower()
    tags = set()
    if _WOMEN.search(n):
        tags.add("women")
    if _YOUTH.search(n):
        tags.add("youth")
    if _RESERVE.search(n):
        tags.add("reserve")
    return frozenset(tags)


@dataclass
class MatchRef:
    """A match as seen by one source. All fields optional except the teams;
    absent fields are treated as 'unknown', never as a mismatch."""

    home: str
    away: str
    kickoff: Optional[datetime] = None
    league: Optional[str] = None
    is_live: Optional[bool] = None


@dataclass
class MatchConfidence:
    level: Confidence
    score: float
    team_order: str  # "same" | "swapped" | "none"
    reasons: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when the candidate may be attached (high or medium)."""
        return self.level in (Confidence.HIGH, Confidence.MEDIUM)


def _minutes_apart(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 60.0


def validate_match(
    requested: MatchRef,
    candidate: MatchRef,
    *,
    tolerance_minutes: float = DEFAULT_TOLERANCE_MINUTES,
) -> MatchConfidence:
    """Compare a requested match against a candidate and grade the confidence
    that they are the same fixture. Deterministic; see module docstring."""
    reasons: list[str] = []

    # ── Team names, both orderings ────────────────────────────────────────────
    same = min(_overlap(requested.home, candidate.home),
               _overlap(requested.away, candidate.away))
    swapped = min(_overlap(requested.home, candidate.away),
                  _overlap(requested.away, candidate.home))
    teams_same = same >= _TEAM_THRESHOLD
    teams_swapped = swapped >= _TEAM_THRESHOLD

    if not teams_same and not teams_swapped:
        return MatchConfidence(Confidence.LOW, round(max(same, swapped), 2),
                               "none", ["teams_mismatch"])

    if teams_same and same >= swapped:
        team_order, team_score = "same", same
    else:
        team_order, team_score = "swapped", swapped
        reasons.append("teams_swapped")

    # ── Tier: never bridge senior ↔ women / youth / reserve ───────────────────
    if team_order == "same":
        home_pair = ((requested.home, candidate.home), (requested.away, candidate.away))
    else:
        home_pair = ((requested.home, candidate.away), (requested.away, candidate.home))
    if any(_team_tier(a) != _team_tier(b) for a, b in home_pair):
        return MatchConfidence(Confidence.LOW, round(team_score, 2),
                               team_order, reasons + ["team_tier_mismatch"])

    # ── Live / prematch state: a hard contradiction cannot be the same match ──
    if (requested.is_live is not None and candidate.is_live is not None
            and requested.is_live != candidate.is_live):
        return MatchConfidence(Confidence.LOW, round(team_score, 2),
                               team_order, reasons + ["live_prematch_mismatch"])
    both_live = bool(requested.is_live) and bool(candidate.is_live)

    # ── League when available ─────────────────────────────────────────────────
    league_ok = league_unknown = False
    if requested.league and candidate.league:
        if _overlap(requested.league, candidate.league) >= _LEAGUE_THRESHOLD:
            league_ok = True
        else:
            return MatchConfidence(Confidence.LOW, round(team_score, 2),
                                   team_order, reasons + ["league_mismatch"])
    else:
        league_unknown = True
        reasons.append("league_unknown")

    # ── Kickoff time & tolerance ──────────────────────────────────────────────
    # `time_corroborated` = an ACTUAL kickoff match within tolerance. Only that
    # can carry a match to HIGH; two live feeds ("both_live") are supportive but
    # never enough for HIGH on their own (no real kickoff to compare).
    gap = _minutes_apart(requested.kickoff, candidate.kickoff)
    time_corroborated = False
    if gap is not None:
        if gap <= tolerance_minutes:
            time_corroborated = True
        else:
            return MatchConfidence(Confidence.LOW, round(team_score, 2),
                                   team_order, reasons + ["kickoff_mismatch"])
    elif both_live:
        reasons.append("both_live")
    else:
        reasons.append("kickoff_unknown")

    time_ok = time_corroborated or both_live

    # ── Grade ────────────────────────────────────────────────────────────────
    if team_order == "same" and time_corroborated and (league_ok or league_unknown):
        level = Confidence.HIGH
    elif time_ok:
        # Teams match and time is at least supported (real match within
        # tolerance, or both live), but order swapped / league unknown / no real
        # kickoff → usable with caution.
        level = Confidence.MEDIUM
    elif team_order == "same" and team_score >= _TEAM_STRONG:
        # No time to corroborate, but the team match is strong.
        level = Confidence.MEDIUM
    else:
        level = Confidence.LOW

    return MatchConfidence(level, round(team_score, 2), team_order, reasons)
