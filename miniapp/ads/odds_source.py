"""The price an offer may show: the verified odds store's current price of the recommended market.

The campaign never carries a price (the editor and the snapshot have no such field, and its texts may not
contain price-like numbers); this is the only source. Same rules as the match page and the quick pick: a
strictly matched, fresh line before kickoff (miniapp/odds/store.py) and a market group that passed its checks
(miniapp/odds/book.py). Anything else is "no price".
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from miniapp.odds.store import OddsStore
from miniapp.sports.models import FootballEvent


def make_odds_lookup(event_of: Callable[[str], Awaitable[tuple[Any, Any]]],
                     odds: OddsStore) -> Callable[[str, str], Awaitable[dict | None]]:
    """`event_of` is SportsService.cached_event: never a provider request (no user share is charged here)."""

    async def lookup(event_id: str, market: str) -> dict | None:
        event, _ = await event_of(event_id)
        if not isinstance(event, FootballEvent):
            return None
        found = odds.lookup(event)
        if found is None:
            return None
        price = found.book.get(market)
        if price is None:
            return None
        return {"market": market, "odds": price,
                "fetchedAt": datetime.fromtimestamp(found.fetched_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                # Whole seconds the client may show it (it hides the price after that on its own clock).
                "validFor": int(found.valid_for)}

    return lookup
