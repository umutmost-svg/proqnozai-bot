"""One match's odds from the feed, in our market keys, checked group by group.

The feed carries the bot's parsed odds (mostbet.mostbet_get_odds keys). Only markets
the goal model prices the same way are taken: full-time 1X2, double chance, totals
1.5 / 2.5 / 3.5 and both teams to score. Handicaps (the bot's line is heuristic),
draw-no-bet (a push on a draw) and half-time markets are not used.

A group is used whole or not at all: every outcome present, each price within
[MIN_ODDS, MAX_ODDS], and the group's overround inside its plausible range. A group
that fails is dropped (the others of the same match may still be used); nothing is
ever repaired or partially shown.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

MIN_ODDS = 1.01
MAX_ODDS = 100.0

# feed key -> our market key (miniapp/forecast/markets.py)
FEED_KEYS: dict[str, str] = {
    "w1": "1", "x": "X", "w2": "2",
    "dc_1x": "1X", "dc_x2": "X2", "dc_12": "12",
    "over15": "O1.5", "under15": "U1.5",
    "over25": "O2.5", "under25": "U2.5",
    "over35": "O3.5", "under35": "U3.5",
    "btts_yes": "BTTS_Y", "btts_no": "BTTS_N",
}


@dataclass(frozen=True)
class Group:
    name: str
    keys: tuple[str, ...]
    # Plausible Σ 1/odds: below the lower bound the prices are not one consistent book
    # (an arbitrage or a mixed-up line); above the upper bound the margin is implausible.
    min_overround: float
    max_overround: float


GROUPS: tuple[Group, ...] = (
    Group("1x2", ("1", "X", "2"), 1.0, 1.25),
    # Each outcome covers two of the three 1X2 results: implied probabilities sum to ~2.
    Group("double", ("1X", "X2", "12"), 2.0, 2.3),
    Group("total1.5", ("O1.5", "U1.5"), 1.0, 1.2),
    Group("total2.5", ("O2.5", "U2.5"), 1.0, 1.2),
    Group("total3.5", ("O3.5", "U3.5"), 1.0, 1.2),
    Group("btts", ("BTTS_Y", "BTTS_N"), 1.0, 1.2),
)


def _price(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    # The source price itself must be in range; it is then used and shown in cents, so every
    # figure derived from it (implied probability, expected value) uses the price the user sees.
    if not math.isfinite(f) or not MIN_ODDS <= f <= MAX_ODDS:
        return None
    f = round(f, 2)
    return f if MIN_ODDS <= f <= MAX_ODDS else None


def read_book(raw: Mapping[str, object]) -> dict[str, float]:
    """Our key -> decimal odds, for the groups that pass every check (may be empty)."""
    prices: dict[str, float] = {}
    for feed_key, key in FEED_KEYS.items():
        price = _price(raw.get(feed_key))
        if price is not None:
            prices[key] = price
    book: dict[str, float] = {}
    for group in GROUPS:
        if not all(k in prices for k in group.keys):
            continue
        total = sum(1.0 / prices[k] for k in group.keys)
        if group.min_overround - 1e-9 <= total <= group.max_overround:
            book.update({k: prices[k] for k in group.keys})
    if all(k in book for k in ("1", "X", "2", "1X", "X2", "12")) and not _double_agrees(book):
        for k in ("1X", "X2", "12"):
            del book[k]
    return book


# How far a double-chance outcome may be from the sum of its two 1X2 outcomes (both books
# normalised proportionally) before the double-chance book is taken as a different line.
DOUBLE_TOLERANCE = 0.05


def _double_agrees(book: Mapping[str, float]) -> bool:
    """Is the double-chance book the same match as the 1X2 book? Each is valid on its own, but
    a 1X at 1.80 next to 1 at 1.50 would be a contradiction (and a false value claim)."""
    q = {k: 1.0 / book[k] for k in ("1", "X", "2")}
    s = sum(q.values())
    fair = {k: v / s for k, v in q.items()}
    d = {k: 1.0 / book[k] for k in ("1X", "X2", "12")}
    sd = sum(d.values())
    implied = {k: 2.0 * v / sd for k, v in d.items()}
    expected = {"1X": fair["1"] + fair["X"], "X2": fair["X"] + fair["2"], "12": fair["1"] + fair["2"]}
    return all(abs(implied[k] - expected[k]) <= DOUBLE_TOLERANCE for k in expected)
