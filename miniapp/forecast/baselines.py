"""Reference forecasts the team model must beat (same window, same matches).

B0 — frequencies: each market's share of wins in the window (Laplace-smoothed),
     1X2 as the three outcome shares. No team information at all.
B1 — league Poisson: the window's mean home and away goals as Poisson rates for
     every match; markets from that score matrix. Home advantage, no team strength.
"""

from __future__ import annotations

from collections.abc import Sequence

from .data import MatchResult
from .markets import KEYS, PAIRS, probabilities, settle
from .scores import score_matrix


def frequencies(results: Sequence[MatchResult]) -> dict[str, float] | None:
    if not results:
        return None
    n = len(results)
    counts = {key: sum(1 for r in results if settle(key, r.home_goals, r.away_goals) == "win") for key in KEYS}
    one, draw, two = counts["1"] + 1, counts["X"] + 1, counts["2"] + 1
    total = one + draw + two
    out = {"1": one / total, "X": draw / total, "2": two / total}
    out.update({"1X": out["1"] + out["X"], "X2": out["X"] + out["2"], "12": out["1"] + out["2"],
                "H1-0.5": out["1"], "H2+0.5": out["X"] + out["2"]})
    for first, second in PAIRS:
        out[first] = (counts[first] + 1) / (n + 2)
        out[second] = 1.0 - out[first]
    return out


def league_poisson(results: Sequence[MatchResult]) -> dict[str, float] | None:
    if not results:
        return None
    n = len(results)
    home = max(sum(r.home_goals for r in results) / n, 0.05)
    away = max(sum(r.away_goals for r in results) / n, 0.05)
    return probabilities(score_matrix(home, away))
