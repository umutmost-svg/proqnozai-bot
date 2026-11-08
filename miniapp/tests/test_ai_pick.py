"""The preferred outcome: structure, explicit rules, NO_BET fallbacks, cache version (M3.4)."""

from __future__ import annotations

import json

import pytest

from miniapp.ai import pick as rules
from miniapp.ai.analysis import SCHEMA, SCHEMA_VERSION, AiError, validate_output
from miniapp.ai.pick import CLOSE_MARGIN, NO_BET, checked_pick, favourite, market_allowed, market_probability

ALL = frozenset({"home_form", "away_form", "home_goals", "away_goals", "home_table", "away_table"})


def side(points: int, gf: int, ga: int, pos: int | None = None) -> dict:
    return {"points": points, "goalsFor": gf, "goalsAgainst": ga, "tablePosition": pos}


STRONG_HOME = {"home": side(13, 11, 3, 2), "away": side(4, 3, 10, 18)}
EVEN = {"home": side(7, 6, 6, 9), "away": side(7, 6, 7, 10)}


def answer(home: int, draw: int, away: int, market: str, reasons: list) -> str:
    return json.dumps({"home_win": home, "draw": draw, "away_win": away, "factors": ["home_form", "away_form"],
                       "pick": {"market": market, "reasons": reasons}})


def p(home: int, draw: int, away: int) -> dict:
    return {"home": home, "draw": draw, "away": away}


# --- explicit rules -------------------------------------------------------------------

@pytest.mark.parametrize(("probs", "expected"), [
    (p(52, 24, 24), "home"), (p(24, 24, 52), "away"),
    (p(40, 25, 35), None),                       # 5 points apart: close, no favourite
    (p(40, 21, 39), None),
    (p(42, 24, 34), "home"),                     # exactly CLOSE_MARGIN apart
])
def test_favourite_needs_a_clear_margin(probs: dict, expected) -> None:
    assert CLOSE_MARGIN == 8
    assert favourite(probs) == expected


@pytest.mark.parametrize(("market", "probs", "ok"), [
    ("1", p(52, 24, 24), True), ("1", p(44, 30, 26), False),        # favourite but below 45 %
    ("1", p(40, 25, 35), False),                                      # close teams
    ("2", p(20, 25, 55), True), ("2", p(36, 30, 34), False),
    ("1X", p(44, 30, 26), True),                                       # favourite, 74 % together
    ("1X", p(38, 30, 32), False),                                      # no favourite: never a free side
    ("1X", p(26, 30, 44), False),                                      # the other team is favoured
    ("X2", p(26, 30, 44), True), ("X2", p(36, 30, 34), False),
    ("X", p(30, 40, 30), True), ("X", p(35, 35, 30), False),           # the draw must be strictly first
    ("12", p(45, 20, 35), True), ("12", p(40, 24, 36), False),
    ("NO_BET", p(34, 33, 33), True),
])
def test_market_rules(market: str, probs: dict, ok: bool) -> None:
    assert market_allowed(market, probs) is ok


def test_double_chance_probability_is_computed_from_the_1x2_estimate() -> None:
    probs = p(52, 24, 24)
    assert [market_probability(m, probs) for m in ("1", "X", "2", "1X", "X2", "12")] == [52, 24, 24, 76, 48, 76]
    assert market_probability("NO_BET", probs) is None


def test_a_supported_pick_keeps_only_true_reasons_of_its_own_side() -> None:
    got = checked_pick("1", ("home_better_form", "away_more_goals", "home_higher_table"), p(52, 24, 24), STRONG_HOME)
    assert got.market == "1" and got.reasons == ("home_better_form", "home_higher_table") and got.probability == 52


@pytest.mark.parametrize(("market", "reasons", "probs", "basis"), [
    ("1", ("home_better_form",), p(40, 25, 35), STRONG_HOME),        # rule fails: close teams
    ("1", ("away_better_form",), p(52, 24, 24), STRONG_HOME),        # reason of the other side
    ("1", ("home_better_form",), p(52, 24, 24), EVEN),               # reason not true in the numbers
    ("1", (), p(52, 24, 24), STRONG_HOME),                            # no reason at all
    ("1", ("home_higher_table",), p(52, 24, 24), {"home": side(9, 5, 5), "away": side(4, 3, 9)}),  # no table
    ("X", ("evenly_matched",), p(30, 40, 30), STRONG_HOME),          # not evenly matched
])
def test_an_unsupported_pick_becomes_no_bet(market, reasons, probs, basis) -> None:
    assert checked_pick(market, reasons, probs, basis) == NO_BET


def test_a_draw_pick_needs_evenly_matched_teams() -> None:
    got = checked_pick("X", ("evenly_matched",), p(30, 40, 30), EVEN)
    assert got.market == "X" and got.probability == 40


# --- structured answer ----------------------------------------------------------------

def test_the_schema_allows_only_the_market_enum_and_reason_codes() -> None:
    pick_schema = SCHEMA["properties"]["pick"]
    assert pick_schema["properties"]["market"]["enum"] == ["1", "X", "2", "1X", "X2", "12", "NO_BET"]
    assert set(pick_schema["properties"]["reasons"]["items"]["enum"]) == set(rules.REASONS)
    assert pick_schema["additionalProperties"] is False and "pick" in SCHEMA["required"]


@pytest.mark.parametrize("bad", [
    {"market": "over_2_5", "reasons": []},                          # markets we cannot justify
    {"market": "1", "reasons": ["injuries"]},                       # unknown reason
    {"market": "1", "reasons": ["home_better_form"] * 2},          # duplicate
    {"market": "1", "reasons": list(rules.REASONS[:4])},            # too many
    {"market": "1", "reasons": ["home_better_form"], "text": "sure win"},  # free text
    {"market": "1"},
    "1", None,
])
def test_a_malformed_pick_is_a_bad_output(bad) -> None:
    text = json.dumps({"home_win": 52, "draw": 24, "away_win": 24, "factors": ["home_form", "away_form"], "pick": bad})
    with pytest.raises(AiError) as exc:
        validate_output(text, ALL, STRONG_HOME)
    assert exc.value.code == "bad_output"


def test_a_missing_pick_is_a_bad_output() -> None:
    text = json.dumps({"home_win": 52, "draw": 24, "away_win": 24, "factors": ["home_form", "away_form"]})
    with pytest.raises(AiError):
        validate_output(text, ALL, STRONG_HOME)


def test_validated_pick_and_downgrade() -> None:
    _, _, good = validate_output(answer(52, 24, 24, "1X", ["home_better_form"]), ALL, STRONG_HOME)
    assert good.to_json() == {"market": "1X", "reasons": ["home_better_form"], "probability": 76}
    _, _, weak = validate_output(answer(38, 30, 32, "1X", ["home_better_form"]), ALL, STRONG_HOME)
    assert weak.to_json() == {"market": "NO_BET", "reasons": [], "probability": None}


def test_probabilities_are_never_changed_by_the_pick() -> None:
    probs, _, _ = validate_output(answer(38, 30, 32, "1", ["home_better_form"]), ALL, STRONG_HOME)
    assert probs == {"home": 38, "draw": 30, "away": 32}


def test_cache_keys_carry_the_schema_version() -> None:
    assert SCHEMA_VERSION == "v2"
