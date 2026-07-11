"""Offline tests for deterministic match validation (no network)."""
from datetime import datetime, timedelta, timezone

from match_validation import Confidence, MatchRef, validate_match

KO = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def _ref(home="Arsenal", away="Chelsea", **kw):
    return MatchRef(home=home, away=away, **kw)


def test_exact_match_is_high():
    r = validate_match(
        _ref(kickoff=KO, league="Premier League"),
        _ref(home="FC Arsenal", away="Chelsea FC", kickoff=KO, league="Premier League"),
    )
    assert r.level is Confidence.HIGH
    assert r.team_order == "same"


def test_exact_match_league_unknown_still_high():
    # League absent on one side must not block a clear same-team, same-time match.
    r = validate_match(_ref(kickoff=KO), _ref(kickoff=KO))
    assert r.level is Confidence.HIGH
    assert "league_unknown" in r.reasons


def test_swapped_home_away_is_handled_not_high():
    r = validate_match(
        _ref(home="Arsenal", away="Chelsea", kickoff=KO),
        _ref(home="Chelsea", away="Arsenal", kickoff=KO),
    )
    assert r.team_order == "swapped"
    assert r.level is Confidence.MEDIUM  # usable but flagged, never silently HIGH
    assert r.usable


def test_kickoff_within_tolerance_is_high():
    r = validate_match(
        _ref(kickoff=KO),
        _ref(kickoff=KO + timedelta(minutes=45)),
        tolerance_minutes=90,
    )
    assert r.level is Confidence.HIGH


def test_kickoff_outside_tolerance_is_rejected():
    r = validate_match(
        _ref(kickoff=KO, league="Premier League"),
        _ref(kickoff=KO + timedelta(hours=6), league="Premier League"),
    )
    assert r.level is Confidence.LOW
    assert "kickoff_mismatch" in r.reasons
    assert not r.usable


def test_wrong_league_is_rejected():
    r = validate_match(
        _ref(kickoff=KO, league="Premier League"),
        _ref(kickoff=KO, league="Serie A"),
    )
    assert r.level is Confidence.LOW
    assert "league_mismatch" in r.reasons


def test_live_prematch_mismatch_is_rejected():
    r = validate_match(
        _ref(kickoff=KO, is_live=True),
        _ref(kickoff=KO, is_live=False),
    )
    assert r.level is Confidence.LOW
    assert "live_prematch_mismatch" in r.reasons


def test_different_teams_rejected():
    r = validate_match(
        _ref(home="Arsenal", away="Chelsea", kickoff=KO),
        _ref(home="Liverpool", away="Everton", kickoff=KO),
    )
    assert r.level is Confidence.LOW
    assert r.team_order == "none"
    assert "teams_mismatch" in r.reasons


def test_missing_time_strong_teams_is_medium():
    # No kickoff on either side, but the teams match strongly → cautious MEDIUM.
    r = validate_match(_ref(), _ref(home="Arsenal", away="Chelsea"))
    assert r.level is Confidence.MEDIUM
    assert "kickoff_unknown" in r.reasons


def test_both_live_no_time_is_usable_but_not_high():
    r = validate_match(
        _ref(is_live=True),
        _ref(home="Arsenal", away="Chelsea", is_live=True),
    )
    assert r.usable
    assert r.level is Confidence.MEDIUM  # no real kickoff → never HIGH
    assert "both_live" in r.reasons


def test_youth_team_not_matched_to_senior():
    r = validate_match(
        _ref(home="Barcelona", away="Sevilla", kickoff=KO),
        _ref(home="Barcelona U19", away="Sevilla", kickoff=KO),
    )
    assert r.level is Confidence.LOW
    assert "team_tier_mismatch" in r.reasons


def test_reserve_b_team_not_matched_to_senior():
    r = validate_match(
        _ref(home="Porto", away="Benfica", kickoff=KO),
        _ref(home="Porto B", away="Benfica", kickoff=KO),
    )
    assert r.level is Confidence.LOW
    assert "team_tier_mismatch" in r.reasons


def test_women_team_not_matched_to_senior():
    r = validate_match(
        _ref(home="Arsenal", away="Chelsea", kickoff=KO),
        _ref(home="Arsenal Women", away="Chelsea", kickoff=KO),
    )
    assert r.level is Confidence.LOW
    assert "team_tier_mismatch" in r.reasons


def test_same_tier_reserve_matches():
    # Both sides are the reserve team → same tier → allowed.
    r = validate_match(
        _ref(home="Porto B", away="Benfica B", kickoff=KO),
        _ref(home="FC Porto B", away="Benfica B", kickoff=KO),
    )
    assert r.level is Confidence.HIGH


def test_senior_names_with_numbers_not_flagged_as_reserve():
    # "Schalke 04" / "Bayer 04" must not be read as reserve teams.
    r = validate_match(
        _ref(home="Schalke 04", away="Bayer 04", kickoff=KO),
        _ref(home="Schalke 04", away="Bayer 04", kickoff=KO),
    )
    assert r.level is Confidence.HIGH
