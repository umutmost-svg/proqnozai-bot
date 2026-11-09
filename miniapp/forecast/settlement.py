"""Automatic settlement of the forecast history (internal quality tracking, not shown to users).

Two paths, both through `ForecastHistory.settle_events` (results only; forecasts never change):

1. Every season calendar the app loads anyway (a user's tournament, AI analysis, search
   warm-up) is handed to the history: no request of its own.
2. A low-priority pass for what path 1 missed: competitions with forecasts still pending
   whose kickoff was SETTLE_AFTER_S..LOOKBACK_S ago get their calendar through
   `SportsService.background_calendar` (minute reserve and day share of the standings
   warm-up), one competition at a time, each at most once per RETRY_S. Started by app
   activity, checked at most every CHECK_EVERY_S; postponed matches simply stay pending and
   stop costing requests after LOOKBACK_S.

SQLite work runs in a worker thread; a failure is logged and never reaches a request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from miniapp.sports.client import UpstreamError
from miniapp.sports.models import SportEvent
from miniapp.sports.service import SportsService

from .history import ForecastHistory

logger = logging.getLogger("miniapp.forecast")

# A match is normally over (extra time included) and its result published by then.
SETTLE_AFTER_S = 3 * 3600.0
# Older pending forecasts (postponed, abandoned) are left to path 1: no more requests for them.
LOOKBACK_S = 14 * 86400.0
RETRY_S = 3600.0
CHECK_EVERY_S = 300.0
SPACING_S = 15.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Settlement:
    def __init__(self, history: ForecastHistory, sports: SportsService, clock: Callable[[], float],
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.history = history
        self.sports = sports
        self._clock = clock
        self._sleep = sleep
        self._attempted: dict[str, float] = {}  # tournament id -> last calendar handed to the history
        self._checked_at = float("-inf")
        self._task: asyncio.Task[None] | None = None
        self._jobs: set[asyncio.Task[int]] = set()

    # --- path 1: calendars loaded anyway -------------------------------------------------

    def on_calendar(self, tournament_id: str, events: list[SportEvent]) -> None:
        """SportsService calendar listener (sync, inside the event loop): settles in a thread."""
        self._attempted[tournament_id] = self._clock()
        job = asyncio.get_running_loop().create_task(self._settle(tournament_id, events))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def _settle(self, tournament_id: str, events: list[SportEvent]) -> int:
        try:
            changed = await asyncio.to_thread(self.history.settle_events, events)
        except Exception as exc:  # optional bookkeeping: the next calendar retries it
            logger.warning("forecast.settle_failed tournament=%s type=%s", tournament_id, type(exc).__name__)
            return 0
        if changed:
            logger.info("forecast.settled tournament=%s changed=%d", tournament_id, changed)
        return changed

    # --- path 2: the low-priority pass ---------------------------------------------------

    def ensure(self) -> asyncio.Task[None] | None:
        """Starts a pass when one is due (never waits for it); cheap to call on every request."""
        if self._task is not None:
            return self._task
        now = self._clock()
        if now - self._checked_at < CHECK_EVERY_S:
            return None
        self._checked_at = now
        self._task = asyncio.get_running_loop().create_task(self._pass())
        return self._task

    def _due(self, pending: list[str]) -> list[str]:
        now = self._clock()
        return [t for t in pending if now - self._attempted.get(t, float("-inf")) >= RETRY_S]

    async def _pass(self) -> None:
        try:
            now = self._clock()
            pending = await asyncio.to_thread(self.history.pending_tournaments,
                                              _iso(now - SETTLE_AFTER_S), _iso(now - LOOKBACK_S))
            served = {t.id for t in self.sports.tournaments("football")}
            first = True
            for tournament_id in self._due(pending):
                if tournament_id not in served:
                    self._attempted[tournament_id] = self._clock()  # no calendar exists for it
                    continue
                if not first:
                    await self._sleep(SPACING_S)
                first = False
                if not self._due([tournament_id]):
                    continue  # a user's calendar settled it meanwhile
                try:
                    calendar = await self.sports.background_calendar(tournament_id)
                except UpstreamError as exc:
                    self._attempted[tournament_id] = self._clock()
                    logger.info("forecast.settle_skipped tournament=%s reason=%s", tournament_id, exc.code)
                    continue
                if calendar is None:
                    # The background budget is used up: the next check tries again.
                    logger.info("forecast.settle_paused tournament=%s", tournament_id)
                    return
                self._attempted[tournament_id] = self._clock()
                await self._settle(tournament_id, calendar.events)
        except Exception as exc:  # optional background work: the next check starts a new pass
            logger.warning("forecast.settle_pass_failed type=%s", type(exc).__name__)
        finally:
            self._task = None
