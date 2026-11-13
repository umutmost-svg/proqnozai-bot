"""Betting markets derived from one score distribution, and how each one settles.

Every probability comes from the same ScoreMatrix, so the markets agree with each
other by construction (P(1X) = P(1) + P(X), P(over) + P(under) = 1, …). Nothing is
derived from the three 1X2 numbers.

Settlement is per selection: win / lose / push. All lines offered now are half
lines (x.5), which never push; whole lines would return the stake (push), and
`settle` already handles that case so they cannot be mixed up with binary markets.

Keys are stable identifiers (API, translations, forecast history):
  1 X 2 · 1X X2 12 · O1.5 U1.5 O2.5 U2.5 O3.5 U3.5 · BTTS_Y BTTS_N ·
  H1-0.5 H1-1.5 H2+0.5 H2+1.5 · HO0.5 HU0.5 HO1.5 HU1.5 AO0.5 AU0.5 AO1.5 AU1.5
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from .scores import ScoreMatrix

Outcome = Literal["win", "lose", "push"]

FAMILIES = ("1x2", "double", "total", "btts", "handicap", "team_total")


@dataclass(frozen=True)
class Market:
    key: str
    family: str


MARKETS: tuple[Market, ...] = (
    Market("1", "1x2"), Market("X", "1x2"), Market("2", "1x2"),
    Market("1X", "double"), Market("X2", "double"), Market("12", "double"),
    Market("O1.5", "total"), Market("U1.5", "total"),
    Market("O2.5", "total"), Market("U2.5", "total"),
    Market("O3.5", "total"), Market("U3.5", "total"),
    Market("BTTS_Y", "btts"), Market("BTTS_N", "btts"),
    Market("H1-0.5", "handicap"), Market("H1-1.5", "handicap"),
    Market("H2+0.5", "handicap"), Market("H2+1.5", "handicap"),
    Market("HO0.5", "team_total"), Market("HU0.5", "team_total"),
    Market("HO1.5", "team_total"), Market("HU1.5", "team_total"),
    Market("AO0.5", "team_total"), Market("AU0.5", "team_total"),
    Market("AO1.5", "team_total"), Market("AU1.5", "team_total"),
)
KEYS = tuple(m.key for m in MARKETS)
FAMILY = {m.key: m.family for m in MARKETS}
# Both sides of each two-way market: their probabilities sum to 1.
PAIRS = (("O1.5", "U1.5"), ("O2.5", "U2.5"), ("O3.5", "U3.5"), ("BTTS_Y", "BTTS_N"),
         ("H1-1.5", "H2+1.5"), ("HO0.5", "HU0.5"), ("HO1.5", "HU1.5"), ("AO0.5", "AU0.5"), ("AO1.5", "AU1.5"))

_TOTAL = re.compile(r"^([HA]?)([OU])(\d+(?:\.5)?)$")
_HANDICAP = re.compile(r"^H([12])([+-]\d+(?:\.5)?)$")


def _sign(value: float) -> Outcome:
    return "win" if value > 0 else "lose" if value < 0 else "push"


def settle(key: str, home_goals: int, away_goals: int) -> Outcome:
    """How a selection settles on a 90-minute score."""
    h, a = home_goals, away_goals
    simple = {
        "1": h > a, "X": h == a, "2": h < a,
        "1X": h >= a, "X2": h <= a, "12": h != a,
        "BTTS_Y": h > 0 and a > 0, "BTTS_N": h == 0 or a == 0,
    }
    if key in simple:
        return "win" if simple[key] else "lose"
    if m := _HANDICAP.match(key):
        side, line = m.group(1), float(m.group(2))
        return _sign((h - a if side == "1" else a - h) + line)
    if m := _TOTAL.match(key):
        who, direction, line = m.group(1), m.group(2), float(m.group(3))
        goals = h if who == "H" else a if who == "A" else h + a
        return _sign(goals - line if direction == "O" else line - goals)
    raise ValueError(f"unknown market {key!r}")


@lru_cache(maxsize=4096)
def _cells(key: str, size: int) -> tuple[tuple[tuple[int, int], ...], bool]:
    """Grid cells where `key` wins, and whether any cell pushes (settlement table)."""
    wins, pushes = [], False
    for x in range(size):
        for y in range(size):
            result = settle(key, x, y)
            if result == "win":
                wins.append((x, y))
            pushes = pushes or result == "push"
    return tuple(wins), pushes


def probabilities(matrix: ScoreMatrix) -> dict[str, float]:
    """Win probability of every market (no offered line can push)."""
    out: dict[str, float] = {}
    probs = matrix.probs
    for key in KEYS:
        wins, pushes = _cells(key, matrix.size)
        if pushes:  # whole lines are not offered: their win share alone would mislead
            raise ValueError(f"market {key} can push; not supported for display")
        out[key] = sum(probs[x][y] for x, y in wins)
    return out


def largest_remainder(values: list[float], total: int = 100) -> list[int]:
    scaled = [v * total for v in values]
    floors = [math.floor(s) for s in scaled]
    left = total - sum(floors)
    order = sorted(range(len(values)), key=lambda i: (-(scaled[i] - floors[i]), i))
    for i in order[:left]:
        floors[i] += 1
    return floors


def _round_half_up(value: float) -> int:
    return int(math.floor(value * 100 + 0.5))


def display_percentages(p: dict[str, float]) -> dict[str, int]:
    """Whole percents that keep every identity of the exact numbers:
    1+X+2 = 100; 1X = 1+X, X2 = X+2, 12 = 1+2; H1-0.5 = 1; H2+0.5 = X2; each two-way
    pair sums to 100; over/under and handicap lines stay ordered."""
    one, draw, two = largest_remainder([p["1"], p["X"], p["2"]])
    out = {"1": one, "X": draw, "2": two, "1X": one + draw, "X2": draw + two, "12": one + two,
           "H1-0.5": one, "H2+0.5": draw + two}
    for first, second in PAIRS:
        out[first] = _round_half_up(p[first])
        out[second] = 100 - out[first]
    # Rounding is monotone, but a ±1 inversion against a value fixed above must not appear.
    out["H1-1.5"] = min(out["H1-1.5"], one)
    out["H2+1.5"] = 100 - out["H1-1.5"]
    return out
