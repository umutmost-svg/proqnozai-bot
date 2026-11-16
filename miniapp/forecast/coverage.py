"""Which football matches the lists show (owner 16.11): only those the goal model can forecast.

A match whose competition has too few results, or whose team has too few matches, would open as an analysis
"without the statistical model" — such matches are left out of the day's list and the top matches. The check
uses only the season calendars already in the shared cache (never a provider request). A competition whose
calendar is not cached yet keeps its matches only if it is one of the competitions the model was validated on
(they always have the data); the others appear once the background warm-up has loaded their calendar.

A previous season that is not loaded yet (not one the source lacks) may still lift a validated competition over
the sample size, so only that shortfall keeps its match listed meanwhile; a team with too few matches is hidden
(Codex coverage r2-r3). A calendar of any age counts (finished results only grow). Accepted limits (Codex coverage
r1): a validated competition with a cold cache (no calendar at all, right after a deploy until the warm-up started
by the first request loads it) is listed as is, and the
rare "extreme_rates" outcome needs the fit itself, so such a match stays listed.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from miniapp.sports.football_data import ID_PREFIX, team_number
from miniapp.sports.models import SportEvent

from .data import results_from_events
from .engine import FROZEN, model_gap
from .selection import LIMITED_COMPETITIONS


class ModelCoverage:
    def __init__(self, cached_history: Callable[[str], object | None], clock: Callable[[], float] = time.time,
                 trusted: frozenset[str] = FROZEN.validated | LIMITED_COMPETITIONS) -> None:
        self.cached_history = cached_history  # SportsService.cached_model_history
        self.clock = clock
        self.trusted = trusted

    def keep(self, events: Sequence[SportEvent]) -> list[SportEvent]:
        now = self.clock()
        # per competition, once per call: (results, final, pooled)
        results: dict[str, tuple[list, bool, bool] | None] = {}
        out: list[SportEvent] = []
        for event in events:
            if event.sport != "football":
                out.append(event)
                continue
            tid = event.tournament_id
            if tid not in results:
                history = self.cached_history(tid)
                pool = getattr(history, "pool", None)
                results[tid] = None if history is None else (
                    results_from_events(pool if pool is not None else history.events),  # type: ignore[attr-defined]
                    history.previous_missing != "transient", pool is not None)  # type: ignore[attr-defined]
            cached = results[tid]
            home, away = team_number(event.home.id), team_number(event.away.id)
            if home is None or away is None:
                continue  # the model never runs without both provider team ids
            trusted = tid.removeprefix(f"{ID_PREFIX}:") in self.trusted
            if cached is None:
                if trusted:
                    out.append(event)  # cold cache: a validated competition stays listed
                continue
            gap = model_gap(cached[0], home, away, now, pooled=cached[2])
            if gap is None or (gap == "small_sample" and not cached[1] and trusted):
                out.append(event)  # forecastable, or only short of the previous season that is still loading
        return out
