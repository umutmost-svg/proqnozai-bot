"""Score distribution P(home = x, away = y) for one match.

The grid grows until each team's Poisson tail beyond it is below TAIL (10⁻¹⁰), so the
mass left out is negligible; the matrix is then renormalised to sum to exactly 1 so
every market derived from it is internally consistent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .model import dc_tau

TAIL = 1e-10
MIN_GOALS = 10
MAX_GOALS = 30
# Supported scoring rates: far beyond any real football match. A rate above it is refused
# (never silently capped: the shown expected goals and the markets must use one number).
MAX_RATE = 8.0


def _poisson(rate: float, size: int) -> list[float]:
    probs = [math.exp(-rate)]
    for k in range(1, size + 1):
        probs.append(probs[-1] * rate / k)
    return probs


def _size(rate: float) -> int:
    n, term, cdf = 0, math.exp(-rate), math.exp(-rate)
    while 1.0 - cdf > TAIL and n < MAX_GOALS:
        n += 1
        term *= rate / n
        cdf += term
    return max(MIN_GOALS, n)


@dataclass(frozen=True)
class ScoreMatrix:
    probs: tuple[tuple[float, ...], ...]  # probs[x][y]
    home_rate: float
    away_rate: float
    tail: float  # mass outside the grid before renormalisation (diagnostics)

    @property
    def size(self) -> int:
        return len(self.probs)

    def cells(self):
        for x, row in enumerate(self.probs):
            for y, p in enumerate(row):
                yield x, y, p

    def probability(self, predicate) -> float:
        return sum(p for x, y, p in self.cells() if predicate(x, y))

    def top_scores(self, count: int = 3) -> list[tuple[int, int, float]]:
        ranked = sorted(self.cells(), key=lambda c: (-c[2], c[0] + c[1], c[0]))
        return ranked[:count]


def score_matrix(home_rate: float, away_rate: float, rho: float = 0.0) -> ScoreMatrix:
    if not (math.isfinite(home_rate) and math.isfinite(away_rate)) or home_rate <= 0 or away_rate <= 0:
        raise ValueError("scoring rates must be positive and finite")
    if home_rate > MAX_RATE or away_rate > MAX_RATE:
        raise ValueError("scoring rate outside the supported range")
    lam, mu = home_rate, away_rate
    # The Dixon–Coles correction is valid only while every factor stays non-negative; for
    # rates where ρ would make one negative, the match falls back to independent Poisson
    # (never a clipped, renormalised distribution).
    if any(dc_tau(x, y, lam, mu, rho) < 0 for x in (0, 1) for y in (0, 1)):
        rho = 0.0
    size = max(_size(lam), _size(mu))
    ph, pa = _poisson(lam, size), _poisson(mu, size)
    raw = [[ph[x] * pa[y] * dc_tau(x, y, lam, mu, rho) for y in range(size + 1)] for x in range(size + 1)]
    total = sum(map(sum, raw))
    if total <= 0:
        raise ValueError("empty score distribution")
    probs = tuple(tuple(p / total for p in row) for row in raw)
    return ScoreMatrix(probs, lam, mu, tail=max(0.0, 1.0 - total))
