"""Whether a real event has enough verified material for an AI analysis.

Same rule as the bot (match_validation.forecast_readiness), reimplemented here
because the Mini App must not import bot modules:

    READY        — real odds AND verified match data
    PARTIAL      — exactly one of them; probabilities must be marked as estimated
    INSUFFICIENT — neither; no model call is made at all

The model is never asked whether it has data; this is decided in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from miniapp.sports.models import SportEvent

Readiness = Literal["ready", "partial", "insufficient"]
NOT_ANALYSED = frozenset({"postponed", "suspended", "cancelled", "unknown"})


@dataclass(frozen=True)
class Evidence:
    """What verified material exists for one event. Nothing here is inferred."""

    has_odds: bool  # real bookmaker odds passed in as data (no source in the Mini App yet)
    has_verified_data: bool  # provider-verified form / results / statistics for both sides
    reasons: tuple[str, ...]


def forecast_readiness(has_odds: bool, has_verified_data: bool) -> Readiness:
    if has_odds and has_verified_data:
        return "ready"
    if has_odds or has_verified_data:
        return "partial"
    return "insufficient"


def evidence_for(event: SportEvent, *, recent_results_both_sides: bool = False, odds: bool = False) -> Evidence:
    """Collect evidence from a normalized event plus facts fetched elsewhere.

    A fixture (teams, kickoff, status) alone is not material for a forecast; it
    takes verified recent results for both sides or real match statistics.
    """
    reasons: list[str] = []
    if event.status in NOT_ANALYSED:
        reasons.append(f"status_{event.status}")
    verified = recent_results_both_sides or (event.stats is not None and event.stats_availability == "available")
    if not verified:
        reasons.append("no_verified_form")
    if not odds:
        reasons.append("no_odds_source")
    return Evidence(has_odds=odds, has_verified_data=verified, reasons=tuple(reasons))


def readiness_of(evidence: Evidence, event: SportEvent) -> Readiness:
    # A match that will not be played as scheduled (or is stopped mid-game) is never analysed.
    if event.status in NOT_ANALYSED:
        return "insufficient"
    return forecast_readiness(evidence.has_odds, evidence.has_verified_data)
