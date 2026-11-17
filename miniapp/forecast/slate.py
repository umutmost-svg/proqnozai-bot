"""The daily slate: the goal model's forecast of every match of the validated competitions, written into the
forecast history on the match's UTC day before kickoff — whatever users open. It is the prospective gate's sample
(`ForecastHistory.evaluate`, docs/miniapp/m35-goal-model.md): AI analyses that users request never enter the gate.

Its own loop (started with the app): a pass every PASS_EVERY_S and one right after each UTC midnight, so a day's
early matches are not missed. Per validated competition the model history (calendar + previous season) comes from
the cache, or is loaded with the warm-up's low priority (minute reserve for users, its day share), LOAD_SPACING_S
apart. A refused load (budget spent, slot busy) or a failure of one competition never stops the others; that
competition is retried after RETRY_S. A miss depends only on the shared budget and the provider, never on users.

A forecast is stamped after it was computed and only kept while its match has not kicked off. The key covers the
kickoff (`slate:<model version>:<event>:<digest>`): a fixture moved to another day is a new row on its new day.
Rows carry no user data; the same numbers for the same kickoff are written once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from miniapp.sports import football_data
from miniapp.sports.models import FootballEvent
from miniapp.sports.service import ModelHistory, SportsService, parse_instant

from .engine import MatchForecast
from .history import SLATE_PREFIX, ForecastHistory, ForecastRecord, model_record

logger = logging.getLogger("miniapp.forecast")

PASS_EVERY_S = 30 * 60.0
RETRY_S = 2 * 60.0
# The slate reads a calendar at most this old (the provider cache's own TTL is 15 min): which matches it sees
# never depends on whether a user happened to refresh the cache.
CALENDAR_MAX_AGE_S = 20 * 60.0
LOAD_SPACING_S = 15.0  # between competitions whose data had to be loaded: never a burst on the shared minute
MIDNIGHT_DELAY_S = 5.0
RECORDED_KEYS_MAX = 5000


class Forecaster(Protocol):
    def compute(self, event: FootballEvent, history: ModelHistory | None) -> Any: ...


def forecast_digest(event: FootballEvent, forecast: MatchForecast) -> str:
    """Identity of what the model said for this kickoff: the same numbers on the same data are one row."""
    body = {"kickoff": event.starts_at, "exact": {k: round(v, 9) for k, v in forecast.exact.items()},
            "selection": forecast.selection.to_json(), "dataUntil": forecast.data_until,
            "parameters": forecast.parameters}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


class SlateRecorder:
    def __init__(self, history: ForecastHistory, sports: SportsService, forecaster: Forecaster,
                 competitions: Iterable[str], clock: Callable[[], float],
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.history = history
        self.sports = sports
        self.forecaster = forecaster  # its own engine: the CPU part runs in this recorder's worker only
        self.competitions = sorted(competitions)
        self._clock = clock
        self._sleep = sleep
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slate-model")
        self._loop_task: asyncio.Task | None = None
        self._retry: set[str] = set()
        self._recorded: OrderedDict[str, None] = OrderedDict()

    async def start(self) -> None:
        """App startup: runs the loop in the background."""
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._worker.shutdown(wait=False, cancel_futures=True)
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _loop(self) -> None:
        while True:
            started = self._clock()
            await self.run_pass()
            await self._sleep(self.wait_s(started))

    def wait_s(self, started: float | None = None) -> float:
        """Until the next pass, counted from when this one started: PASS_EVERY_S (RETRY_S with a competition to
        retry), or just after the first UTC midnight after that start (a pass running across midnight never
        postpones the post-midnight one)."""
        now = self._clock()
        started = now if started is None else started
        day = datetime.fromtimestamp(started, UTC).date()
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC).timestamp()
        every = RETRY_S if self._retry else PASS_EVERY_S
        return max(1.0, min(started + every, midnight + MIDNIGHT_DELAY_S) - now)

    async def run_pass(self) -> None:
        served = {t.id for t in self.sports.tournaments("football")}
        retry: set[str] = set()
        for code in self.competitions:
            tournament = f"{football_data.ID_PREFIX}:{code}"
            if tournament not in served:
                continue
            try:
                done, loaded = await self._competition(tournament)
            except Exception as exc:  # noqa: BLE001 — one competition never stops the others (type only)
                logger.warning("slate.competition_failed tournament=%s type=%s", tournament, type(exc).__name__)
                done, loaded = False, True
            if not done:
                retry.add(tournament)
            if loaded:
                await self._sleep(LOAD_SPACING_S)
        self._retry = retry

    async def _competition(self, tournament: str) -> tuple[bool, bool]:
        """Records today's remaining matches of one competition: (done, whether the provider was asked)."""
        history, loaded = await self.sports.background_model_history(tournament, CALENDAR_MAX_AGE_S)
        if history is None:
            return False, loaded
        # A previous season that may still load (transient): forecasts now are partial (outside the gate); retried
        # soon so an eligible one can follow before kickoff.
        complete = history.previous_season or history.season_start is None or history.previous_missing == "absent"
        today = datetime.fromtimestamp(self._clock(), UTC).date()
        upcoming = [e for e in history.events
                    if isinstance(e, FootballEvent) and e.tournament_id == tournament and e.status == "upcoming"
                    and parse_instant(e.starts_at).date() == today
                    and parse_instant(e.starts_at).timestamp() > self._clock()]
        if not upcoming:
            return True, loaded
        outcomes = await asyncio.get_running_loop().run_in_executor(
            self._worker, lambda: [self.forecaster.compute(e, history) for e in upcoming])
        records = self._records(upcoming, outcomes, self._clock())  # stamped once computed
        if records:
            try:
                await asyncio.to_thread(self.history.record_many, records)
            except Exception as exc:  # noqa: BLE001 — log the type only; retried soon
                logger.warning("slate.history_write_failed type=%s", type(exc).__name__)
                return False, loaded
            for r in records:
                self._recorded[r.forecast_key] = None
            while len(self._recorded) > RECORDED_KEYS_MAX:
                self._recorded.popitem(last=False)
            logger.info("slate.recorded tournament=%s forecasts=%d", tournament, len(records))
        return complete, loaded

    def _records(self, events: list[FootballEvent], outcomes: list[Any], now: float) -> list[ForecastRecord]:
        created = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = []
        for event, forecast in zip(events, outcomes, strict=True):
            if not isinstance(forecast, MatchForecast) or parse_instant(event.starts_at).timestamp() <= now:
                continue  # no forecast, or the match kicked off while it was computed
            digest = forecast_digest(event, forecast)
            key = f"{SLATE_PREFIX}{forecast.model_version}:{event.id}:{digest}"
            if key in self._recorded:
                continue
            records.append(model_record(event.id, event.tournament_id, event.home.id, event.away.id, event.starts_at,
                                        forecast, key=key, created_at=created, digest=digest,
                                        explanation={"factors": [], "reasons": []}, llm_model=None,
                                        source=football_data.PROVIDER))
        return records
