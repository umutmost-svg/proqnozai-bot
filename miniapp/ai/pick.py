"""The model's preferred outcome ("pick"), checked by explicit Python rules.

There is no bookmaker odds source, so no pick can be called a *value* bet: it is
only the outcome the model prefers, shown with its probability computed here from
the 1X2 estimate. The model chooses one market and up to three reason codes; this
module keeps a pick only when the probabilities and the verified numbers support
it, otherwise the answer is NO_BET. A downgrade only ever removes a claim — it
never invents one.

Rules (shared with the frontend's favourite/outsider colours, see CLOSE_MARGIN):
- a team is the favourite when its win probability exceeds the other team's by at
  least CLOSE_MARGIN points; closer than that there is no favourite;
- "1" / "2": that team is the favourite AND has at least SINGLE_MIN percent;
- "1X" / "X2": that team is the favourite (a double chance never picks a side the
  estimate does not favour) AND the pair reaches DOUBLE_MIN percent;
- "X": the draw is strictly the most likely of the three outcomes;
- "12": the draw is at most TWELVE_DRAW_MAX percent;
- every reason must be true in the verified numbers and point the same way as the
  pick; a pick other than NO_BET needs at least one such reason.
"""

from __future__ import annotations

from dataclasses import dataclass

MARKETS = ("1", "X", "2", "1X", "X2", "12", "NO_BET")
REASONS = (
    "home_better_form", "away_better_form",
    "home_higher_table", "away_higher_table",
    "home_more_goals", "away_more_goals",
    "home_fewer_conceded", "away_fewer_conceded",
    "evenly_matched",
)
MAX_REASONS = 3
CLOSE_MARGIN = 8
SINGLE_MIN = 45
DOUBLE_MIN = 60
TWELVE_DRAW_MAX = 22
# Which reasons can support which pick (side of the claim).
_HOME = {"home_better_form", "home_higher_table", "home_more_goals", "home_fewer_conceded"}
_AWAY = {"away_better_form", "away_higher_table", "away_more_goals", "away_fewer_conceded"}
_SUPPORTS = {
    "1": _HOME, "1X": _HOME,
    "2": _AWAY, "X2": _AWAY,
    "X": {"evenly_matched"},
    "12": _HOME | _AWAY,
}


@dataclass(frozen=True)
class Pick:
    market: str
    reasons: tuple[str, ...]
    probability: int | None  # percent from the 1X2 estimate; None for NO_BET

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


def market_probability(market: str, p: dict) -> int | None:
    return {
        "1": p["home"], "X": p["draw"], "2": p["away"],
        "1X": p["home"] + p["draw"], "X2": p["draw"] + p["away"], "12": p["home"] + p["away"],
    }.get(market)


def market_allowed(market: str, p: dict) -> bool:
    fav = favourite(p)
    if market == "1":
        return fav == "home" and p["home"] >= SINGLE_MIN
    if market == "2":
        return fav == "away" and p["away"] >= SINGLE_MIN
    if market == "1X":
        return fav == "home" and p["home"] + p["draw"] >= DOUBLE_MIN
    if market == "X2":
        return fav == "away" and p["away"] + p["draw"] >= DOUBLE_MIN
    if market == "X":
        return p["draw"] > p["home"] and p["draw"] > p["away"]
    if market == "12":
        return p["draw"] <= TWELVE_DRAW_MAX
    return market == "NO_BET"


def reason_holds(reason: str, home: dict, away: dict) -> bool:
    """True only when the verified basis numbers (the ones shown to the user) say so."""
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
    return False


def checked_pick(market: str, reasons: tuple[str, ...], p: dict, basis: dict) -> Pick:
    """The model's pick if the rules support it, else NO_BET (never a stronger claim)."""
    if market == "NO_BET" or not market_allowed(market, p):
        return NO_BET
    kept = tuple(r for r in reasons if r in _SUPPORTS[market] and reason_holds(r, basis["home"], basis["away"]))
    if not kept:
        return NO_BET
    return Pick(market, kept, market_probability(market, p))
