"""Scoring rules, calibration and a time-aware bootstrap for paired comparisons."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

EPS = 1e-12
OUTCOMES_1X2 = ("1", "X", "2")


def outcome_1x2(home_goals: int, away_goals: int) -> str:
    return "1" if home_goals > away_goals else "2" if home_goals < away_goals else "X"


def log_loss_1x2(p: dict[str, float], outcome: str) -> float:
    return -math.log(max(p[outcome], EPS))


def brier_1x2(p: dict[str, float], outcome: str) -> float:
    return sum((p[k] - (1.0 if k == outcome else 0.0)) ** 2 for k in OUTCOMES_1X2)


def rps_1x2(p: dict[str, float], outcome: str) -> float:
    """Ranked probability score over the ordered outcomes home win < draw < away win."""
    cum_p = [p["1"], p["1"] + p["X"]]
    cum_o = [1.0 if outcome == "1" else 0.0, 1.0 if outcome in ("1", "X") else 0.0]
    return sum((a - b) ** 2 for a, b in zip(cum_p, cum_o)) / 2


def log_loss_binary(p: float, won: bool) -> float:
    return -math.log(max(p if won else 1.0 - p, EPS))


def brier_binary(p: float, won: bool) -> float:
    return (p - (1.0 if won else 0.0)) ** 2


@dataclass(frozen=True)
class CalibrationBin:
    low: float
    high: float
    count: int
    mean_predicted: float
    observed: float

    def to_json(self) -> dict:
        return {"low": self.low, "high": self.high, "count": self.count,
                "meanPredicted": round(self.mean_predicted, 4), "observed": round(self.observed, 4)}


def calibration(pairs: Sequence[tuple[float, bool]], width: float = 0.1) -> list[CalibrationBin]:
    bins: list[CalibrationBin] = []
    edges = int(round(1 / width))
    for i in range(edges):
        low, high = i * width, (i + 1) * width
        inside = [(p, w) for p, w in pairs if low <= p < high or (i == edges - 1 and p == 1.0)]
        if inside:
            bins.append(CalibrationBin(round(low, 4), round(high, 4), len(inside),
                                       sum(p for p, _ in inside) / len(inside),
                                       sum(1 for _, w in inside if w) / len(inside)))
    return bins


def expected_calibration_error(bins: Sequence[CalibrationBin]) -> float:
    total = sum(b.count for b in bins)
    return sum(b.count * abs(b.mean_predicted - b.observed) for b in bins) / total if total else math.nan


def week_of(epoch: float) -> str:
    year, week, _ = datetime.fromtimestamp(epoch, UTC).isocalendar()
    return f"{year}-W{week:02d}"


def resample_weeks(rng: random.Random, n_weeks: int, block: int) -> list[int]:
    """Week indices of one circular moving-block resample: whole blocks of consecutive
    weeks, cut to exactly `n_weeks` (a resample is never longer than the observed series)."""
    drawn: list[int] = []
    while len(drawn) < n_weeks:
        start = rng.randrange(n_weeks)
        drawn.extend((start + j) % n_weeks for j in range(block))
    return drawn[:n_weeks]


@dataclass(frozen=True)
class PairedResult:
    mean_difference: float  # candidate − reference, per match (negative = candidate better)
    low: float
    high: float
    matches: int
    weeks: int
    block_weeks: int

    @property
    def candidate_better(self) -> bool:
        return self.high < 0

    def to_json(self) -> dict:
        return {"meanDifference": round(self.mean_difference, 5), "ci95": [round(self.low, 5), round(self.high, 5)],
                "matches": self.matches, "weeks": self.weeks, "blockWeeks": self.block_weeks,
                "candidateBetter": self.candidate_better}


def paired_block_bootstrap(differences: Sequence[tuple[float, float]], *, block_weeks: int = 4,
                           resamples: int = 5000, seed: int = 20261108) -> PairedResult:
    """95% interval of the mean per-match loss difference.

    Matches of the same week share one fitted model and the same round of results, so
    they are not independent; whole calendar weeks are resampled in circular blocks of
    `block_weeks` consecutive weeks (moving-block bootstrap), which keeps the dependence
    inside a block. `differences` holds (kickoff epoch, candidate loss − reference loss).
    """
    if not differences:
        raise ValueError("no paired observations")
    by_week: dict[str, list[float]] = {}
    for moment, diff in differences:
        by_week.setdefault(week_of(moment), []).append(diff)
    weeks = sorted(by_week)
    sums = [sum(by_week[w]) for w in weeks]
    counts = [len(by_week[w]) for w in weeks]
    n_weeks = len(weeks)
    block = max(1, min(block_weeks, n_weeks))
    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(resamples):
        drawn = resample_weeks(rng, n_weeks, block)
        stats.append(sum(sums[k] for k in drawn) / sum(counts[k] for k in drawn))
    stats.sort()
    mean = sum(sums) / sum(counts)
    return PairedResult(mean, stats[int(0.025 * resamples)], stats[int(0.975 * resamples) - 1],
                        sum(counts), n_weeks, block)
