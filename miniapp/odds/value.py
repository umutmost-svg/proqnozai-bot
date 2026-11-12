"""The model against the market, for one match: computed here, never by the language model.

For every market that has a checked price (book.py):
- implied probability 1/odds and the group's margin (overround − 1);
- the market probability with the margin removed: Shin for the three-way 1X2, the
  power method for two-way groups; double chances are the sums of the de-margined 1X2
  (so they agree with it) — their own margin is still reported;
- the model's probability (the goal model's exact number) and its difference from the
  market probability (the edge, in probability points);
- the expected value of a unit stake at that price, p_model · odds − 1.

The expected value is a statement about the model's estimate at the quoted price, not a
promise: it is shown per single outcome only. Accumulators get no expected value here —
their legs are not independent in general and no dependence model is validated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import margin
from .book import GROUPS


@dataclass(frozen=True)
class MarketQuote:
    key: str
    odds: float
    implied: float
    market: float  # margin removed
    model: float
    margin: float  # of the market's group

    @property
    def edge(self) -> float:
        return self.model - self.market

    @property
    def expected_value(self) -> float:
        return self.model * self.odds - 1.0

    def to_json(self) -> dict:
        return {
            "market": self.key,
            "odds": round(self.odds, 2),
            "impliedProbability": round(self.implied * 100, 1),
            "marketProbability": round(self.market * 100, 1),
            "modelProbability": round(self.model * 100, 1),
            "edge": round(self.edge * 100, 1),
            "expectedValue": round(self.expected_value * 100, 1),
            "margin": round(self.margin * 100, 1),
        }


def market_book(book: Mapping[str, float], model: Mapping[str, float] | None = None) -> dict[str, tuple[float, float, float]]:
    """Our key -> (odds, market probability with the margin removed, the group's margin), for every
    complete group of the book (and, when a model is given, only groups the model also prices)."""
    out: dict[str, tuple[float, float, float]] = {}
    fair_1x2: dict[str, float] = {}
    for group in GROUPS:
        if not all(k in book for k in group.keys) or (model is not None and not all(k in model for k in group.keys)):
            continue
        prices = [book[k] for k in group.keys]
        group_margin = sum(1.0 / p for p in prices) - (2.0 if group.name == "double" else 1.0)
        if group.name == "1x2":
            fair = dict(zip(group.keys, margin.shin(prices)))
            fair_1x2 = fair
        elif group.name == "double":
            if not fair_1x2:
                continue  # its market probability comes from the 1X2 book only
            fair = {"1X": fair_1x2["1"] + fair_1x2["X"], "X2": fair_1x2["X"] + fair_1x2["2"],
                    "12": fair_1x2["1"] + fair_1x2["2"]}
        else:
            fair = dict(zip(group.keys, margin.power(prices)))
        for k, price in zip(group.keys, prices):
            out[k] = (price, fair[k], group_margin)
    return out


def compare(book: Mapping[str, float], model: Mapping[str, float]) -> dict[str, MarketQuote]:
    """Our key -> quote, for every priced market the model also prices."""
    return {k: MarketQuote(k, price, 1.0 / price, fair, model[k], group_margin)
            for k, (price, fair, group_margin) in market_book(book, model).items()}


def book_json(book: Mapping[str, float]) -> list[dict]:
    """The match's own market, for the match page: odds and the market probability without the margin.
    Numbers only: never the source's name."""
    return [{"market": k, "odds": round(price, 2), "marketProbability": round(fair * 100, 1), "margin": round(m * 100, 1)}
            for k, (price, fair, m) in market_book(book).items()]
