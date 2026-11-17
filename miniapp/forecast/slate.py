"""The daily slate: the goal model's forecast of every match of the validated competitions, written into the
forecast history on the match's UTC day before kickoff — whatever users open (the prospective quality sample of
`ForecastHistory.evaluate`, docs/miniapp/m35-goal-model.md).

A pass runs at most every PASS_EVERY_S, started by app activity (like settlement). Per validated competition the
model history comes from the cache; a missing or old calendar is loaded with the background priority (minute
reserve for users, the warm-up's day share). When that is refused (budget spent, another background load running)
the pass goes on with the cached competitions and the next one is tried after RETRY_S. What may still be missed depends only on traffic and the
shared budget, never on which leagues or periods users chose.

Rows carry no user data; keys `slate:<model version>:<event>:<digest>` — the same numbers are written once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Protocol

from miniapp.sports import football_data
from miniapp.sports.models import FootballEvent
from miniapp.sports.service import ModelHistory, SportsService, parse_instant

from .engine import MatchForecast
from .history import SLATE_PREFIX, ForecastHistory, ForecastRecord, model_record

logger = logging.getLogger("miniapp.forecast")

PASS_EVERY_S = 30 * 60.0
RETRY_S = 2 * 60.0
RECORDED_KEYS_MAX = 5000


class Forecaster(Protocol):
    def compute(self, event: FootballEvent, history: ModelHistory | None) -> Any: ...


def forecast_digest(forecast: MatchForecast) -> str:
    """Identity of what the model said: the same numbers on the same data are one history row."""
    body = {"exact": {k: round(v, 9) for k, v in forecast.exact.items()}, "selection": forecast.selection.to_json(),
            "dataUntil": forecast.data_until, "parameters": forecast.parameters}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


class SlateRecorder:
    def __init__(self, history: ForecastHistory, sports: SportsService, forecaster: Forecaster,
                 competitions: Iterable[str], clock: Callable[[], float]) -> None:
        self.history = history
        self.sports = sports
        self.forecaster = forecaster  # its own engine: the CPU part runs in this recorder's worker only
        self.competitions = sorted(competitions)
        self._clock = clock
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slate-model")
        self._task: asyncio.Task | None = None
        self._next_at = 0.0
        self._recorded: OrderedDict[str, None] = OrderedDict()

    def ensure(self) -> asyncio.Task | None:
        """Starts a pass when one is due and none is running (never waits for it)."""
        if self._task is not None and not self._task.done():
            return self._task
        if self._clock() < self._next_at:
            return None
        self._next_at = self._clock() + PASS_EVERY_S
        self._task = asyncio.create_task(self._pass())
        return self._task

    async def _pass(self) -> None:
        try:
            served = {t.id for t in self.sports.tournaments("football")}
            refused = False
            for code in self.competitions:
                tournament = f"{football_data.ID_PREFIX}:{code}"
                if tournament in served and not await self._competition(tournament):
                    refused = True  # the cached competitions still go on; this one on the next try
            if refused:
                self._next_at = self._clock() + RETRY_S
        except Exception:  # noqa: BLE001 — background work never reaches a request
            logger.exception("slate.pass_failed")
            self._next_at = self._clock() + RETRY_S

    async def _competition(self, tournament: str) -> bool:
        """Records today's remaining matches of one competition; False when its calendar could not be had now."""
        history = self.sports.cached_model_history(tournament)
        if history is None:
            if await self.sports.background_calendar(tournament) is None:
                return False
            history = self.sports.cached_model_history(tournament)
            if history is None:
                return True  # loaded but unusable (e.g. failed): nothing to forecast from, go on
        now = self._clock()
        today = datetime.fromtimestamp(now, UTC).date()
        upcoming = [e for e in history.events
                    if isinstance(e, FootballEvent) and e.tournament_id == tournament and e.status == "upcoming"
                    and parse_instant(e.starts_at).date() == today and parse_instant(e.starts_at).timestamp() > now]
        if not upcoming:
            return True
        outcomes = await asyncio.get_running_loop().run_in_executor(
            self._worker, lambda: [self.forecaster.compute(e, history) for e in upcoming])
        records = self._records(upcoming, outcomes, now)
        if records:
            try:
                await asyncio.to_thread(self.history.record_many, records)
            except Exception as exc:  # noqa: BLE001 — log the type only; the next pass writes them
                logger.warning("slate.history_write_failed type=%s", type(exc).__name__)
                return True
            for r in records:
                self._recorded[r.forecast_key] = None
            while len(self._recorded) > RECORDED_KEYS_MAX:
                self._recorded.popitem(last=False)
            logger.info("slate.recorded tournament=%s forecasts=%d", tournament, len(records))
        return True

    def _records(self, events: list[FootballEvent], outcomes: list[Any], now: float) -> list[ForecastRecord]:
        created = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = []
        for event, forecast in zip(events, outcomes, strict=True):
            if not isinstance(forecast, MatchForecast):
                continue
            digest = forecast_digest(forecast)
            key = f"{SLATE_PREFIX}{forecast.model_version}:{event.id}:{digest}"
            if key in self._recorded:
                continue
            records.append(model_record(event.id, event.tournament_id, event.home.id, event.away.id, event.starts_at,
                                        forecast, key=key, created_at=created, digest=digest,
                                        explanation={"factors": [], "reasons": []}, llm_model=None,
                                        source=football_data.PROVIDER))
        return records
