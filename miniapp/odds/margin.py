"""From bookmaker odds to market probabilities: removing the margin.

The implied probabilities 1/odds of one market sum to more than 1 (the overround);
the excess is the bookmaker's margin. Three standard ways to remove it:

- proportional: every implied probability is scaled by the same factor;
- power: p_i = q_i ** k with k chosen so the p_i sum to 1 (takes relatively more
  margin from long shots — the favourite–longshot bias);
- Shin (1993): models a share z of insider money; the solution of
  p_i = (sqrt(z² + 4(1 − z) q_i² / S) − z) / (2(1 − z)),  S = Σ q_i,
  with Σ p_i = 1. Standard for three-way football 1X2.

All three keep the order of the outcomes, return probabilities summing to 1 and leave
odds without a margin unchanged. Inputs must already be validated (odds > 1, overround
at least 1): anything else raises ValueError, never a silently repaired number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

_ITERATIONS = 200
_TOLERANCE = 1e-12


def implied(odds: Sequence[float]) -> list[float]:
    if len(odds) < 2 or any(not math.isfinite(o) or o <= 1.0 for o in odds):
        raise ValueError("odds must be finite numbers above 1 (at least two outcomes)")
    return [1.0 / o for o in odds]


def overround(odds: Sequence[float]) -> float:
    """Σ 1/odds; the margin is overround − 1."""
    return sum(implied(odds))


def _checked(odds: Sequence[float]) -> list[float]:
    q = implied(odds)
    if sum(q) < 1.0 - 1e-9:
        raise ValueError("odds below a fair book (overround < 1)")
    return q


def proportional(odds: Sequence[float]) -> list[float]:
    q = _checked(odds)
    total = sum(q)
    return [x / total for x in q]


def power(odds: Sequence[float]) -> list[float]:
    q = _checked(odds)
    if sum(q) - 1.0 <= _TOLERANCE:
        return [x / sum(q) for x in q]
    # Σ q_i^k is decreasing in k (every q_i < 1): k = 1 gives the overround (> 1); find
    # an upper bound with a sum below 1, then bisect.
    low, high = 1.0, 2.0
    while sum(x ** high for x in q) > 1.0:
        high *= 2.0
        if high > 1e6:
            raise ValueError("no power solution")
    for _ in range(_ITERATIONS):
        mid = (low + high) / 2.0
        if sum(x ** mid for x in q) > 1.0:
            low = mid
        else:
            high = mid
        if high - low < _TOLERANCE:
            break
    k = (low + high) / 2.0
    p = [x ** k for x in q]
    total = sum(p)
    return [x / total for x in p]  # removes the last bisection residue only


def _shin_probabilities(q: Sequence[float], z: float) -> list[float]:
    s = sum(q)
    return [(math.sqrt(z * z + 4.0 * (1.0 - z) * x * x / s) - z) / (2.0 * (1.0 - z)) for x in q]


def shin(odds: Sequence[float]) -> list[float]:
    q = _checked(odds)
    if sum(q) - 1.0 <= _TOLERANCE:
        return [x / sum(q) for x in q]
    # Σ p_i(z) falls as z grows (from Σ q_i/√S·√S ≥ 1 at z = 0): bisect z on [0, 1).
    low, high = 0.0, 0.999
    if sum(_shin_probabilities(q, high)) > 1.0:
        raise ValueError("no Shin solution")
    for _ in range(_ITERATIONS):
        mid = (low + high) / 2.0
        if sum(_shin_probabilities(q, mid)) > 1.0:
            low = mid
        else:
            high = mid
        if high - low < _TOLERANCE:
            break
    p = _shin_probabilities(q, (low + high) / 2.0)
    total = sum(p)
    return [x / total for x in p]
