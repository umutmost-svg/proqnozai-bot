"""Reasons offered for the model's recommended market: true, on its side, nothing else (M3.4 → M3.5)."""

from __future__ import annotations

import pytest

from miniapp.ai.analysis import SCHEMA, SCHEMA_VERSION
from miniapp.ai.pick import CLOSE_MARGIN, NO_BET, REASONS, allowed_reasons, favourite, reason_holds, supporting


def side(points: int, gf: int, ga: int, pos: int | None = None) -> dict:
    return {"points": points, "goalsFor": gf, "goalsAgainst": ga, "tablePosition": pos}


STRONG_HOME = {"home": side(13, 11, 3, 2), "away": side(4, 3, 10, 18)}
EVEN = {"home": side(7, 6, 6, 9), "away": side(7, 6, 7, 10)}
MODEL = {"ratings": {"home": {"attack": 1.4, "defence": 0.8}, "away": {"attack": 0.9, "defence": 1.2}},
         "homeAdvantage": 1.25}
FLAT = {"ratings": {"home": {"attack": 1.0, "defence": 1.0}, "away": {"attack": 1.02, "defence": 1.0}},
        "homeAdvantage": 1.05}


@pytest.mark.parametrize(("probs", "expected"), [
    ({"home": 52, "draw": 24, "away": 24}, "home"), ({"home": 24, "draw": 24, "away": 52}, "away"),
    ({"home": 40, "draw": 25, "away": 35}, None),     # 5 points apart: close, no favourite
    ({"home": 42, "draw": 24, "away": 34}, "home"),   # exactly CLOSE_MARGIN apart
])
def test_favourite_needs_a_clear_margin(probs: dict, expected) -> None:
    assert CLOSE_MARGIN == 8
    assert favourite(probs) == expected


def test_only_true_reasons_on_the_markets_side_are_offered() -> None:
    home_side = allowed_reasons("1X", STRONG_HOME, MODEL)
    assert home_side == ("home_better_form", "home_higher_table", "home_more_goals", "home_fewer_conceded",
                         "home_stronger_rating", "home_advantage")
    assert allowed_reasons("H1-1.5", STRONG_HOME, MODEL) == home_side  # a home handicap argues the same side
    assert allowed_reasons("X2", STRONG_HOME, MODEL) == ()             # nothing true argues for the away side
    assert allowed_reasons("X", EVEN, FLAT) == ("evenly_matched",)
    assert allowed_reasons("X", STRONG_HOME, MODEL) == ()


@pytest.mark.parametrize("market", ["NO_BET", "O2.5", "BTTS_Y", "HO0.5"])
def test_markets_without_a_side_get_no_reasons(market: str) -> None:
    assert supporting(market) == frozenset() and allowed_reasons(market, STRONG_HOME, MODEL) == ()


def test_model_reasons_need_a_clear_rating_gap_and_a_real_home_advantage() -> None:
    assert reason_holds("home_stronger_rating", EVEN["home"], EVEN["away"], MODEL)
    assert not reason_holds("home_stronger_rating", EVEN["home"], EVEN["away"], FLAT)
    assert not reason_holds("away_stronger_rating", EVEN["home"], EVEN["away"], FLAT)
    assert reason_holds("home_advantage", EVEN["home"], EVEN["away"], MODEL)
    assert not reason_holds("home_advantage", EVEN["home"], EVEN["away"], FLAT)
    assert not reason_holds("home_advantage", EVEN["home"], EVEN["away"], None)  # no model → never true


def test_table_reasons_need_both_rows() -> None:
    assert not reason_holds("home_higher_table", side(9, 5, 5, 1), side(1, 1, 9, None))
    assert reason_holds("home_higher_table", side(9, 5, 5, 1), side(1, 1, 9, 12))


def test_answer_contract_is_versioned_and_has_no_numbers() -> None:
    assert SCHEMA_VERSION == "v3"
    assert set(SCHEMA["required"]) == {"factors", "reasons"} and SCHEMA["additionalProperties"] is False
    assert SCHEMA["properties"]["reasons"]["items"]["enum"] == list(REASONS)
    assert NO_BET.to_json() == {"market": "NO_BET", "reasons": [], "probability": None}
