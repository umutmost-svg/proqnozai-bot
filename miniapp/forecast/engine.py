"""Production forecast of one match from the frozen model (see frozen_<version>.json).

The parameters, calibration table and selectable market families were fixed by the
validation run (second half of 2025, docs/miniapp/m35-goal-model.md); the 2026 run is a
diagnostic only, so recommendations stay experimental until forecasts stored before
kickoff confirm them. Nothing here is tuned at runtime. Fits are cached per competition, data and UTC day,
so a forecast normally costs a few score-matrix evaluations.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .baselines import frequencies, league_poisson
from .data import MatchResult, known_before
from .markets import display_percentages, probabilities
from .model import ModelParams, Ratings, fit
from .scores import MAX_RATE, score_matrix
from .selection import (LIMITED_COMPETITIONS, MIN_SAMPLE, NO_BET_EARLY, NO_BET_LIMITED, NO_BET_PARTIAL,
                        NO_BET_UNVALIDATED, Selection, select)
from .version import MIN_TEAM_SHOW, MODEL_VERSION, UNPROVEN_FAMILIES, stability_half_lives

FIT_CACHE_SIZE = 96
TOP_SCORES = 3


@dataclass(frozen=True)
class Frozen:
    params: ModelParams
    calibration: dict[str, list[tuple[float, float, int, float]]]
    pick_families: frozenset[str]
    digest: str
    # Competitions the selection rules were validated on (CL excluded: limited data).
    validated: frozenset[str] = frozenset()
    # Recommendations are experimental until forecasts stored before kickoff confirm them.
    experimental: bool = True


def load_frozen(path: Path) -> Frozen:
    raw = json.loads(path.read_text())
    if raw.get("modelVersion") != MODEL_VERSION:
        raise ValueError("frozen parameters belong to another model version")
    cand = raw["candidate"]
    params = ModelParams(cand["halfLifeDays"], float(cand["priorMatches"]), bool(cand["dixonColes"]))
    calibration = {f: [tuple(b) for b in bins] for f, bins in raw["calibration"].items()}
    status = raw.get("recommendationStatus")
    if status not in ("experimental", "confirmed"):
        raise ValueError("frozen parameters lack a recommendation status")
    validated = frozenset(c for c, _ in raw["validation"]["pairs"]) - LIMITED_COMPETITIONS
    return Frozen(params, calibration, frozenset(raw["pickFamilies"]),
                  hashlib.sha256(path.read_bytes()).hexdigest()[:12], validated, experimental=status == "experimental")


FROZEN = load_frozen(Path(__file__).with_name(f"frozen_{MODEL_VERSION}.json"))


@dataclass(frozen=True)
class MatchForecast:
    competition: str
    limited: bool  # CL, or a competition outside the validation (no recommendations)
    complete_history: bool
    match_day: bool  # made on the match's UTC day: the validated lead time
    computed_at: str
    data_until: str  # kickoff of the latest result used
    sample: int
    home_matches: int
    away_matches: int
    expected_goals: tuple[float, float]
    exact: dict[str, float]
    display: dict[str, int]
    top_scores: tuple[tuple[int, int, float], ...]
    selection: Selection
    league_rates: dict[str, float]
    home_rating: tuple[float, float]  # (attack, defence) relative to the competition average
    away_rating: tuple[float, float]
    home_advantage: float
    league_goals: tuple[float, float]
    # B1 (league-average Poisson on the same window): stored with each forecast so the
    # prospective gate can compare the model with it on the very same future matches.
    baseline_1x2: dict[str, float] = field(default_factory=dict)

    @property
    def gate_eligible(self) -> bool:
        """Counts toward the prospective gate: the population the rules were validated on."""
        return not self.limited and self.complete_history and self.match_day

    def to_json(self) -> dict:
        return {
            "version": MODEL_VERSION,
            "kind": "dixon_coles" if FROZEN.params.dixon_coles else "poisson",
            "parameters": FROZEN.digest,
            "competition": self.competition,
            "limited": self.limited,
            "computedAt": self.computed_at,
            "dataUntil": self.data_until,
            "sample": self.sample,
            "teamMatches": {"home": self.home_matches, "away": self.away_matches},
            "expectedGoals": {"home": round(self.expected_goals[0], 2), "away": round(self.expected_goals[1], 2)},
            "markets": self.display,
            "unprovenFamilies": sorted(UNPROVEN_FAMILIES),
            "topScores": [{"home": x, "away": y, "probability": max(1, round(p * 100))} for x, y, p in self.top_scores],
            "ratings": {
                "home": {"attack": round(self.home_rating[0], 2), "defence": round(self.home_rating[1], 2)},
                "away": {"attack": round(self.away_rating[0], 2), "defence": round(self.away_rating[1], 2)},
            },
            "homeAdvantage": round(self.home_advantage, 2),
            "leagueGoals": {"home": round(self.league_goals[0], 2), "away": round(self.league_goals[1], 2)},
            "noBetReason": self.selection.no_bet_reason,
            "experimental": FROZEN.experimental,
        }


@dataclass(frozen=True)
class Unavailable:
    reason: str  # "small_sample" | "team_history" | "extreme_rates"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ForecastEngine:
    def __init__(self, frozen: Frozen = FROZEN) -> None:
        self.frozen = frozen
        self._fits: OrderedDict[tuple, Ratings] = OrderedDict()

    def _fit(self, competition: str, results: Sequence[MatchResult], now: float, season_start: float | None,
             half_life: float | None) -> Ratings:
        day = math.floor(now / 86400)
        # Keyed by the content of every result: a corrected old score is a new fit.
        content = hash(tuple((r.match_id, r.kickoff, r.home, r.away, r.home_goals, r.away_goals) for r in results))
        key = (competition, len(results), content, day, season_start, half_life)
        cached = self._fits.get(key)
        if cached is not None:
            self._fits.move_to_end(key)
            return cached
        params = ModelParams(half_life, self.frozen.params.prior_matches, self.frozen.params.dixon_coles)
        # Fitted at 00:00 UTC of the day, as in the validation protocol: the cached fit is
        # exactly the one any forecast of that day would compute.
        ratings = fit(results, day * 86400.0, params, season_start)
        assert ratings is not None
        self._fits[key] = ratings
        while len(self._fits) > FIT_CACHE_SIZE:
            self._fits.popitem(last=False)
        return ratings

    def forecast(self, competition: str, results: Sequence[MatchResult], season_start: float | None,
                 home: int, away: int, now: float, complete_history: bool = True,
                 kickoff: float | None = None) -> MatchForecast | Unavailable:
        """`complete_history`: the window holds the previous season as validated; without it
        the probabilities are still shown, but nothing is recommended (NO_BET).
        `kickoff`: before the match's UTC day the forecast lead time is longer than the
        validated one (00:00 of match day), so nothing is recommended yet either."""
        # Exactly the validated window: fitted at 00:00 UTC of the day from matches that
        # kicked off at least BACKTEST_LAG_S before it. Fresher results wait for the next
        # day, so the frozen calibration and selection rules apply to the same kind of input.
        day_start = math.floor(now / 86400) * 86400.0
        known = known_before(results, day_start)
        if len(known) < MIN_SAMPLE:
            return Unavailable("small_sample")
        main_hl = self.frozen.params.half_life_days
        ratings = self._fit(competition, known, now, season_start, main_hl)
        hm, am = ratings.matches.get(home, 0), ratings.matches.get(away, 0)
        if min(hm, am) < MIN_TEAM_SHOW:
            return Unavailable("team_history")
        refits = [self._fit(competition, known, now, season_start, hl) for hl in stability_half_lives(main_hl)]
        # Every fit that feeds a shown number or a rule must stay in the supported range.
        if any(max(r.expected_goals(home, away)) > MAX_RATE for r in (ratings, *refits)):
            return Unavailable("extreme_rates")  # never a capped, inconsistent forecast
        lam, mu = ratings.expected_goals(home, away)
        exact = self._markets(ratings, home, away)
        variants = [self._markets(r, home, away) for r in refits]
        rates = frequencies(known)
        b1 = league_poisson(known)
        assert rates is not None
        selection = select(exact, rates, competition=competition, home_matches=hm, away_matches=am,
                           sample=len(known), calibration=self.frozen.calibration, alternatives=variants,
                           families=self.frozen.pick_families)
        match_day = kickoff is None or math.floor(now / 86400) >= math.floor(kickoff / 86400)
        unvalidated = competition not in self.frozen.validated and competition not in LIMITED_COMPETITIONS
        if unvalidated:
            # e.g. EC / WC when configured: never validated, so limited data — no recommendation.
            selection = Selection(None, (), NO_BET_UNVALIDATED)
        elif not complete_history and selection.no_bet_reason != NO_BET_LIMITED:
            selection = Selection(None, (), NO_BET_PARTIAL)
        elif not match_day and selection.no_bet_reason != NO_BET_LIMITED:
            selection = Selection(None, (), NO_BET_EARLY)
        matrix = score_matrix(lam, mu, ratings.rho)
        return MatchForecast(
            competition=competition, limited=competition in LIMITED_COMPETITIONS or unvalidated,
            complete_history=complete_history, match_day=match_day, computed_at=_iso(now),
            data_until=_iso(known[-1].kickoff), sample=len(known), home_matches=hm, away_matches=am,
            expected_goals=(lam, mu), exact=exact, display=display_percentages(exact),
            top_scores=tuple(matrix.top_scores(TOP_SCORES)), selection=selection, league_rates=rates,
            baseline_1x2={k: b1[k] for k in ("1", "X", "2")} if b1 else {},
            home_rating=_relative(ratings, home), away_rating=_relative(ratings, away),
            home_advantage=ratings.home_advantage,
            league_goals=(ratings.league_home_goals, ratings.league_away_goals),
        )

    @staticmethod
    def _markets(ratings: Ratings, home: int, away: int) -> dict[str, float]:
        lam, mu = ratings.expected_goals(home, away)
        return probabilities(score_matrix(lam, mu, ratings.rho))


def _relative(ratings: Ratings, team: int) -> tuple[float, float]:
    """(attack, defence) divided by the geometric mean over the window's teams: 1.0 = average,
    attack above 1 scores more, defence above 1 concedes more."""
    def geo(values) -> float:
        values = list(values)
        return math.exp(sum(math.log(v) for v in values) / len(values))

    return (ratings.attack.get(team, ratings.newcomer[0]) / geo(ratings.attack.values()),
            ratings.defence.get(team, ratings.newcomer[1]) / geo(ratings.defence.values()))
