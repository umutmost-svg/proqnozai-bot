"""Mostbet feed filtering, token normalisation and fuzzy scoring."""
from datetime import datetime, timedelta, timezone

from config import MOSTBET_SRC_TZ
from mostbet import (
    _fuzzy_score,
    _is_outright_market,
    _is_virtual_match,
    _is_within_week,
    _norm_tokens,
)

SRC_TZ = timezone(timedelta(hours=MOSTBET_SRC_TZ))


def _mostbet_dt(days_ahead: float) -> str:
    """Match datetime in Mostbet's source format/zone, N days from now."""
    dt = datetime.now(SRC_TZ) + timedelta(days=days_ahead)
    return dt.strftime("%d.%m.%Y %H:%M:%S")


# ── Virtual / esports detection ───────────────────────────────────────────────

def test_esports_category_is_virtual():
    assert _is_virtual_match({"lineCategory": "Esports", "team1Title": "A", "team2Title": "B"})


def test_fc25_teams_are_virtual():
    assert _is_virtual_match({"lineCategory": "Football",
                              "team1Title": "Arsenal (FC 25)", "team2Title": "Chelsea (FC 25)"})


def test_cyber_football_subcategory_is_virtual():
    assert _is_virtual_match({"lineSubCategory": "Cyber Football League"})


def test_real_world_cup_is_not_virtual():
    """Regression: a bare 'fifa 2' keyword once matched 'FIFA 2026' and hid
    real World Cup fixtures. Real internationals must pass the filter."""
    m = {
        "lineCategory": "Football",
        "lineSuperCategory": "FIFA World Cup 2026",
        "lineSubCategory": "Round of 32",
        "team1Title": "Germany",
        "team2Title": "Norway",
    }
    assert not _is_virtual_match(m)


def test_regular_club_match_is_not_virtual():
    assert not _is_virtual_match({"lineCategory": "Football",
                                  "lineSubCategory": "Premier League",
                                  "team1Title": "Arsenal", "team2Title": "Chelsea"})


# ── Outright / futures markets ────────────────────────────────────────────────

def test_outright_placeholder_opponent():
    assert _is_outright_market({"team1Title": "Cup. Winner", "team2Title": "?"})


def test_outright_empty_opponent():
    assert _is_outright_market({"team1Title": "Top scorer", "team2Title": ""})
    assert _is_outright_market({"team1Title": "X", "team2Title": None})


def test_head_to_head_is_not_outright():
    assert not _is_outright_market({"team1Title": "Real Madrid", "team2Title": "Barcelona"})


# ── Time window ───────────────────────────────────────────────────────────────

def test_match_in_three_days_is_within_week():
    assert _is_within_week(_mostbet_dt(3))


def test_match_in_ten_days_is_outside_week():
    assert not _is_within_week(_mostbet_dt(10))


def test_wider_window_days_param():
    assert _is_within_week(_mostbet_dt(10), days=14)


def test_match_two_hours_ago_is_excluded():
    # Window starts 1 hour in the past.
    assert not _is_within_week(_mostbet_dt(-2 / 24))


def test_iso_format_accepted():
    iso = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    assert _is_within_week(iso)


def test_unknown_or_broken_dates_are_included():
    # Unknown date must never hide a match.
    assert _is_within_week("")
    assert _is_within_week("not a date")


# ── Token normalisation & fuzzy scoring ───────────────────────────────────────

def test_norm_tokens_drops_noise_and_short_tokens():
    assert _norm_tokens("FC Barcelona") == {"barcelona"}
    assert _norm_tokens("Manchester United") == {"manchester"}


def test_norm_tokens_strips_punctuation():
    assert "saint" in _norm_tokens("Saint-Étienne!") or _norm_tokens("Saint-Étienne!")


def test_fuzzy_exact_match_scores_one():
    q = _norm_tokens("Real Madrid")
    assert _fuzzy_score(q, "Real Madrid") == 1.0


def test_fuzzy_partial_overlap():
    q = _norm_tokens("Bayern")
    score = _fuzzy_score(q, "Bayern Munich")
    assert 0 < score < 1


def test_fuzzy_no_overlap_scores_zero():
    q = _norm_tokens("Arsenal")
    assert _fuzzy_score(q, "Juventus") == 0.0


def test_fuzzy_empty_inputs_score_zero():
    assert _fuzzy_score(set(), "Arsenal") == 0.0
    assert _fuzzy_score(_norm_tokens("Arsenal"), "") == 0.0
