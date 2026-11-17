"""Quick prediction: a single or a coupon chosen from the goal model's own selections.

Nothing here invents a new rule of what may be recommended. A match can contribute
only a market that already passed the frozen selection rules (selection.py: family
proven on validation, calibrated bin, stable under refits, model ≠ league average) —
the pick or one of its alternatives. The user's criteria only narrow that set:

- market family: one family or any;
- risk level: a minimum model probability (never below the rules' own P_MIN);
- for a coupon: the number of different matches (2–5).

Ranking is the selection's own (distance from the league frequency, then probability),
then kickoff and id, so the same data always gives the same answer. Fewer suitable
matches than asked → no result and the honest counts; criteria are never loosened.

A coupon uses different matches with no team in common. Whether their outcomes are
independent was never checked (matches of one competition share league conditions and
the fitted parameters), so the probabilities are NOT multiplied. The chance that every
leg wins is given only by its assumption-free (Fréchet) bounds: at most the weakest leg,
at least max(0, sum − (n − 1)). No odds, no payout, no value claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .engine import MatchForecast
from .markets import FAMILY, KEYS
from .selection import P_MIN, Candidate

MODES = ("single", "coupon")
MARKET_FILTERS = ("any", "1x2", "double", "total", "btts", "handicap", "team_total")
RISK_MIN_PROBABILITY = {"conservative": 0.70, "balanced": 0.60, "elevated": P_MIN}
COUPON_SIZES = range(2, 6)

STATUS_OK = "ok"
STATUS_NO_BET = "no_bet"  # a single: nothing suitable
STATUS_NOT_ENOUGH = "not_enough"  # a coupon: fewer suitable matches than legs asked
STATUS_UNAVAILABLE = "unavailable"  # today's data could not be loaded now: retry later
# Coupon search steps before settling for the greedy set (never reached with real fixtures:
# a team plays at most once a day, and only today's matches can qualify).
SEARCH_LIMIT = 20_000


@dataclass(frozen=True)
class Criteria:
    mode: str
    market: str
    risk: str
    size: int  # 1 for a single

    def __post_init__(self) -> None:
        if self.mode not in MODES or self.market not in MARKET_FILTERS or self.risk not in RISK_MIN_PROBABILITY:
            raise ValueError("unknown criteria")
        if (self.mode == "single" and self.size != 1) or (self.mode == "coupon" and self.size not in COUPON_SIZES):
            raise ValueError("bad size")


@dataclass(frozen=True)
class Option:
    """One match's forecast, as the pipeline gives it to the chooser."""

    event_id: str
    kickoff: str  # ISO-8601 UTC
    teams: tuple[str, str]
    forecast: MatchForecast


@dataclass(frozen=True)
class Leg:
    option: Option
    candidate: Candidate


@dataclass(frozen=True)
class QuickResult:
    status: str
    legs: tuple[Leg, ...]
    suitable: int  # matches with at least one market meeting the criteria
    combined: tuple[float, float] | None  # coupon: Fréchet bounds of "every leg wins" (no independence assumed)


def suitable_markets(forecast: MatchForecast, criteria: Criteria) -> list[Candidate]:
    """The selection's own candidates of this match that meet the user's criteria."""
    selection = forecast.selection
    if selection.pick is None:
        return []
    floor = RISK_MIN_PROBABILITY[criteria.risk]
    found = [c for c in (selection.pick, *selection.alternatives)
             if c.probability >= floor and (criteria.market == "any" or FAMILY[c.market] == criteria.market)]
    return sorted(found, key=_rank)


def _rank(c: Candidate) -> tuple:
    return (-c.edge_over_league, -c.probability, KEYS.index(c.market))


def choose(options: Iterable[Option], criteria: Criteria) -> QuickResult:
    best: list[Leg] = []
    for option in options:
        markets = suitable_markets(option.forecast, criteria)
        if markets:
            best.append(Leg(option, markets[0]))
    best.sort(key=lambda leg: (*_rank(leg.candidate), leg.option.kickoff, leg.option.event_id))
    if criteria.mode == "single":
        return QuickResult(STATUS_OK if best else STATUS_NO_BET, tuple(best[:1]), len(best), None)
    legs = coupon(best, criteria.size)
    if len(legs) < criteria.size:
        # How many legs these matches allow at most (fewer than asked).
        return QuickResult(STATUS_NOT_ENOUGH, (), _capacity(best, criteria.size), None)
    probs = [leg.candidate.probability for leg in legs]
    return QuickResult(STATUS_OK, tuple(legs), len(best), frechet(probs))


def frechet(probs: Sequence[float]) -> tuple[float, float]:
    """Bounds of P(all events) valid for any dependence between them."""
    return max(0.0, sum(probs) - (len(probs) - 1)), min(probs)


def coupon(ranked: Sequence[Leg], size: int) -> list[Leg]:
    """The best `size` legs with no team in common, preferring better-ranked legs first
    (lexicographic over the ranking), or the largest such set when `size` is out of reach."""
    steps = 0
    found: list[Leg] = []

    def search(start: int, taken: list[Leg], teams: frozenset[str]) -> bool:
        nonlocal steps, found
        if len(taken) > len(found):
            found = list(taken)
        if len(taken) == size:
            return True
        for i in range(start, len(ranked)):
            steps += 1
            if steps > SEARCH_LIMIT:
                return False
            leg = ranked[i]
            if teams.intersection(leg.option.teams):
                continue
            if search(i + 1, [*taken, leg], teams | set(leg.option.teams)):
                return True
        return False

    search(0, [], frozenset())
    return found


def _capacity(ranked: Sequence[Leg], limit: int) -> int:
    """How many team-disjoint legs these matches allow, up to `limit`."""
    return len(coupon(ranked, limit))
