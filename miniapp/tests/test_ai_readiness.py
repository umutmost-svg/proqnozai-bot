from __future__ import annotations

import pytest

from miniapp.ai.readiness import evidence_for, forecast_readiness, readiness_of
from miniapp.sports.models import Competitor, FootballEvent, Provenance


def event(status: str = "upcoming", stats: dict | None = None) -> FootballEvent:
    return FootballEvent(
        id="fd:1", tournament_id="fd:PL", starts_at="2027-01-15T15:00:00Z", status=status,  # type: ignore[arg-type]
        home=Competitor("fd:team:1", "A"), away=Competitor("fd:team:2", "B"),
        provenance=Provenance("football-data.org", "2027-01-15T08:00:00Z", None), source_status="TIMED",
        stats=stats, stats_availability="available" if stats else "unavailable",
    )


@pytest.mark.parametrize(("odds", "data", "expected"), [
    (True, True, "ready"), (True, False, "partial"), (False, True, "partial"), (False, False, "insufficient"),
])
def test_same_rule_as_the_bot(odds: bool, data: bool, expected: str) -> None:
    assert forecast_readiness(odds, data) == expected


def test_a_bare_fixture_is_insufficient() -> None:
    ev = event()
    evidence = evidence_for(ev)
    assert readiness_of(evidence, ev) == "insufficient"
    assert set(evidence.reasons) == {"no_verified_form", "no_odds_source"}


def test_verified_form_without_odds_is_at_most_partial() -> None:
    ev = event()
    assert readiness_of(evidence_for(ev, recent_results_both_sides=True), ev) == "partial"


@pytest.mark.parametrize("status", ["postponed", "suspended", "cancelled", "unknown"])
def test_matches_that_will_not_be_played_are_never_analysed(status: str) -> None:
    ev = event(status)
    assert readiness_of(evidence_for(ev, recent_results_both_sides=True, odds=True), ev) == "insufficient"
