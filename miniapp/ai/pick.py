"""Reasons that may explain the recommended market — each one checked in Python.

Since M3.5 the market itself is chosen by the goal model's frozen rules
(miniapp/forecast/selection.py); the language model only picks, from the reasons
listed here as TRUE in the verified numbers and pointing the same way as that
market, the ones that explain it best. A reason that does not hold, or that argues
for the other side, is never offered to it.

CLOSE_MARGIN is shared with the frontend's favourite/outsider colours: a team is the
favourite when its win probability exceeds the other team's by at least that many
points; closer than that there is no favourite.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

REASONS = (
    "home_better_form", "away_better_form",
    "home_higher_table", "away_higher_table",
    "home_more_goals", "away_more_goals",
    "home_fewer_conceded", "away_fewer_conceded",
    "evenly_matched",
    "home_stronger_rating", "away_stronger_rating",
    "home_advantage",
)
MAX_REASONS = 3
CLOSE_MARGIN = 8
# Ratings closer than this (difference of ln attack − ln defence) are not "stronger".
RATING_MARGIN = 0.10
HOME_ADVANTAGE_MIN = 1.10
_HOME = {"home_better_form", "home_higher_table", "home_more_goals", "home_fewer_conceded", "home_stronger_rating",
         "home_advantage"}
_AWAY = {"away_better_form", "away_higher_table", "away_more_goals", "away_fewer_conceded", "away_stronger_rating"}
# Which reasons can support which market (the side of the claim).
_HOME_MARKETS = {"1", "1X", "H1-0.5", "H1-1.5"}
_AWAY_MARKETS = {"2", "X2", "H2+0.5", "H2+1.5"}


@dataclass(frozen=True)
class Pick:
    market: str  # a market key of miniapp.forecast.markets, or NO_BET
    reasons: tuple[str, ...]
    probability: int | None  # whole percent from the model; None for NO_BET

    def to_json(self) -> dict:
        return {"market": self.market, "reasons": list(self.reasons), "probability": self.probability}


NO_BET = Pick("NO_BET", (), None)


def favourite(p: dict) -> str | None:
    """"home" / "away" when one team leads by CLOSE_MARGIN points or more, else None."""
    if p["home"] - p["away"] >= CLOSE_MARGIN:
        return "home"
    if p["away"] - p["home"] >= CLOSE_MARGIN:
        return "away"
    return None


def _strength(rating: dict) -> float:
    return math.log(rating["attack"]) - math.log(rating["defence"])


def reason_holds(reason: str, home: dict, away: dict, model: dict | None = None) -> bool:
    """True only when the verified numbers shown to the user say so."""
    hp, ap = home.get("tablePosition"), away.get("tablePosition")
    if reason == "home_better_form":
        return home["points"] > away["points"]
    if reason == "away_better_form":
        return away["points"] > home["points"]
    if reason == "home_higher_table":
        return hp is not None and ap is not None and hp < ap
    if reason == "away_higher_table":
        return hp is not None and ap is not None and ap < hp
    if reason == "home_more_goals":
        return home["goalsFor"] > away["goalsFor"]
    if reason == "away_more_goals":
        return away["goalsFor"] > home["goalsFor"]
    if reason == "home_fewer_conceded":
        return home["goalsAgainst"] < away["goalsAgainst"]
    if reason == "away_fewer_conceded":
        return away["goalsAgainst"] < home["goalsAgainst"]
    if reason == "evenly_matched":
        close_table = hp is None or ap is None or abs(hp - ap) <= 3
        return abs(home["points"] - away["points"]) <= 2 and close_table
    if model is None:
        return False
    if reason in ("home_stronger_rating", "away_stronger_rating"):
        gap = _strength(model["ratings"]["home"]) - _strength(model["ratings"]["away"])
        return gap >= RATING_MARGIN if reason == "home_stronger_rating" else -gap >= RATING_MARGIN
    if reason == "home_advantage":
        return model["homeAdvantage"] >= HOME_ADVANTAGE_MIN
    return False


def supporting(market: str) -> frozenset[str]:
    if market in _HOME_MARKETS:
        return frozenset(_HOME)
    if market in _AWAY_MARKETS:
        return frozenset(_AWAY)
    if market == "X":
        return frozenset({"evenly_matched"})
    if market == "12":
        return frozenset(_HOME | _AWAY)
    return frozenset()  # NO_BET and markets without a side: nothing to argue


def allowed_reasons(market: str, basis: dict, model: dict | None) -> tuple[str, ...]:
    """The reasons that are true AND support `market`, in the fixed order of REASONS."""
    side = supporting(market)
    return tuple(r for r in REASONS if r in side and reason_holds(r, basis["home"], basis["away"], model))
