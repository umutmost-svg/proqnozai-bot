"""Which market, if any, becomes the "recommended bet" — explicit, frozen rules.

There are no bookmaker odds, so no selection is ever a claim of value or profit:
a high probability alone (e.g. "under 3.5") is not a reason to recommend anything.
A market qualifies only when ALL of these hold:

0. its market family beat the reference baseline on the validation period (families
   where the team model was no better than the league average — on validation that
   was totals and both-teams-to-score — are shown as estimates but never picked);
1. the competition is not in limited-data mode and both teams have at least
   MIN_TEAM_MATCHES results in the window (MIN_SAMPLE results overall);
2. its probability is at least P_MIN;
3. it differs from the same market's frequency in this competition's window by at
   least DELTA_MIN — the model sees something specific to this match, not just the
   league's usual pattern (this is a ranking rule, not evidence of value);
4. the model was calibrated there on the validation period: the validation bin of THIS
   market that holds this probability has at least CAL_MIN_COUNT matches and
   |observed − predicted| ≤ CAL_MAX_GAP;
5. the probability is stable: refits with half and double the time-decay half-life
   move it by at most STABILITY_MAX.

Qualifying markets are ranked by the difference in (3); the first is the pick, up to
three more from other market families are alternatives. Nothing qualifies → NO_BET.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .markets import FAMILY, KEYS

LIMITED_COMPETITIONS = frozenset({"CL"})
MIN_TEAM_MATCHES = 8
MIN_SAMPLE = 80
P_MIN = 0.55
DELTA_MIN = 0.08
CAL_MIN_COUNT = 100
CAL_MAX_GAP = 0.05
STABILITY_MAX = 0.05
MAX_ALTERNATIVES = 3
# The same event under another name: never offered as a separate selection.
DUPLICATES = frozenset({"H1-0.5", "H2+0.5"})

# Why a match was not given a pick (stable codes for the API and the history).
NO_BET_LIMITED = "limited_competition"
# A configurable competition the rules were never validated on (e.g. EC, WC).
NO_BET_UNVALIDATED = "unvalidated_competition"
# Requested before the match day: the rules were validated only for match-day forecasts.
NO_BET_EARLY = "early_forecast"
NO_BET_SAMPLE = "small_sample"
NO_BET_NONE = "no_qualifying_market"
# The previous season could not be loaded: the window differs from the validated one.
NO_BET_PARTIAL = "partial_history"


@dataclass(frozen=True)
class Candidate:
    market: str
    probability: float
    league_rate: float

    @property
    def edge_over_league(self) -> float:
        return self.probability - self.league_rate

    def to_json(self) -> dict:
        return {"market": self.market, "family": FAMILY[self.market], "probability": round(self.probability, 4),
                "leagueRate": round(self.league_rate, 4)}


@dataclass(frozen=True)
class Selection:
    pick: Candidate | None
    alternatives: tuple[Candidate, ...]
    no_bet_reason: str | None

    def to_json(self) -> dict:
        return {"pick": self.pick.to_json() if self.pick else None,
                "alternatives": [c.to_json() for c in self.alternatives], "noBetReason": self.no_bet_reason}


# Calibration table from the validation period: market -> [(low, high, matches, gap)].
CalibrationTable = Mapping[str, Sequence[tuple[float, float, int, float]]]


def calibrated(table: CalibrationTable, market: str, p: float) -> bool:
    for low, high, count, gap in table.get(market, ()):
        if low <= p < high or (high >= 1.0 and p == 1.0):
            return count >= CAL_MIN_COUNT and gap <= CAL_MAX_GAP
    return False


def select(probs: Mapping[str, float], league_rates: Mapping[str, float], *, competition: str,
           home_matches: int, away_matches: int, sample: int, calibration: CalibrationTable,
           alternatives: Sequence[Mapping[str, float]], families: frozenset[str]) -> Selection:
    """`alternatives`: the same markets from the stability refits."""
    if competition in LIMITED_COMPETITIONS:
        return Selection(None, (), NO_BET_LIMITED)
    if min(home_matches, away_matches) < MIN_TEAM_MATCHES or sample < MIN_SAMPLE:
        return Selection(None, (), NO_BET_SAMPLE)
    qualified: list[Candidate] = []
    for market in KEYS:
        if market in DUPLICATES or FAMILY[market] not in families:
            continue
        p, base = probs[market], league_rates[market]
        if p < P_MIN or p - base < DELTA_MIN or not calibrated(calibration, market, p):
            continue
        if any(abs(p - alt[market]) > STABILITY_MAX for alt in alternatives):
            continue
        qualified.append(Candidate(market, p, base))
    if not qualified:
        return Selection(None, (), NO_BET_NONE)
    qualified.sort(key=lambda c: (-c.edge_over_league, -c.probability, KEYS.index(c.market)))
    pick = qualified[0]
    seen = {FAMILY[pick.market]}
    extra: list[Candidate] = []
    for c in qualified[1:]:
        if FAMILY[c.market] in seen:
            continue
        seen.add(FAMILY[c.market])
        extra.append(c)
        if len(extra) == MAX_ALTERNATIVES:
            break
    return Selection(pick, tuple(extra), None)
