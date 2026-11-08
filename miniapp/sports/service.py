"""Sports data service: per-sport providers behind one cache.

Only sports with a connected provider return events. The others answer with an
explicit "not_connected" coverage state — never with demo data.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from . import football_data
from .cache import SportsCache
from .client import UpstreamError
from .football_data import FootballDataAdapter
from .models import SPORTS, Provenance, Sport, SportEvent, Tournament

MAX_RANGE = timedelta(hours=48)
PAST_WINDOW = timedelta(days=30)
FUTURE_WINDOW = timedelta(days=60)


class RequestError(Exception):
    """Invalid client request; ``code`` goes to the API response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EventsResult:
    sport: Sport
    events: list[SportEvent]
    tournaments: list[Tournament]
    fetched_at: str | None
    stale: bool


def parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise RequestError("bad_range") from None
    if parsed.tzinfo is None:
        raise RequestError("bad_range")
    return parsed.astimezone(UTC)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SportsService:
    def __init__(self, football: FootballDataAdapter | None, *, cache: SportsCache | None = None,
                 clock: Callable[[], float] = time.time, unknown_lookups_per_hour: int = 20,
                 football_live_plan: bool = False) -> None:
        self.football = football
        self.football_live_plan = football_live_plan
        self.cache = cache or SportsCache(clock=clock)
        self._clock = clock
        self._known_ids: set[str] = set()
        self._unknown_lookups: deque[float] = deque()
        self._unknown_per_hour = unknown_lookups_per_hour

    # --- coverage ------------------------------------------------------------

    def coverage(self) -> dict:
        sports: dict[str, dict] = {}
        for sport in SPORTS:
            if sport == "football" and self.football is not None:
                client = self.football.client
                sports[sport] = {
                    "status": "unavailable" if client.last_error in ("quota_exhausted", "upstream_error") else "connected",
                    "provider": football_data.PROVIDER,
                    "tournaments": [t.to_json() for t in self.football.tournaments()],
                    # Free plan: scores and schedules are delayed by the provider.
                    "features": {"fixtures": True,
                                 "liveScores": "realtime" if self.football_live_plan else "delayed",
                                 "minute": "when_provided", "statistics": False, "lineups": False,
                                 "odds": False},
                    "attribution": football_data.ATTRIBUTION,
                    "lastError": client.last_error,
                    "lastSuccessAt": _iso(client.last_success_at) if client.last_success_at else None,
                    "refresh": {"liveSeconds": int(football_data.LIVE_TTL_S),
                                "upcomingSeconds": int(football_data.UPCOMING_TTL_S)},
                    "quota": client.usage(),
                }
            else:
                sports[sport] = {"status": "not_connected", "provider": None, "tournaments": []}
        return {"sports": sports}

    def tournaments(self, sport: Sport) -> list[Tournament]:
        if sport == "football" and self.football is not None:
            return self.football.tournaments()
        return []

    def connected(self, sport: Sport) -> bool:
        return sport == "football" and self.football is not None

    # --- events --------------------------------------------------------------

    def _check_range(self, start: datetime, end: datetime) -> None:
        now = datetime.fromtimestamp(self._clock(), UTC)
        if end <= start or end - start > MAX_RANGE:
            raise RequestError("bad_range")
        if start < now - PAST_WINDOW or end > now + FUTURE_WINDOW:
            raise RequestError("range_out_of_window")

    async def _football_day(self, day: date):
        assert self.football is not None
        adapter = self.football
        cached = await self.cache.get(f"football:day:{day.isoformat()}", lambda: adapter.day(day))
        self._known_ids.update(e.id for e in cached.value)
        return cached

    async def events(self, sport: Sport, start: datetime, end: datetime, tournament: str | None) -> EventsResult:
        self._check_range(start, end)
        if not self.connected(sport):
            return EventsResult(sport, [], [], None, False)
        catalog = self.tournaments(sport)
        if tournament is not None and tournament not in {t.id for t in catalog}:
            raise RequestError("unknown_tournament")
        days: list[date] = []
        day = start.date()
        while day <= (end - timedelta(microseconds=1)).date():
            days.append(day)
            day += timedelta(days=1)
        events: dict[str, SportEvent] = {}
        oldest: float | None = None
        stale = False
        for d in days:
            cached = await self._football_day(d)
            stale = stale or cached.stale
            oldest = cached.fetched_at if oldest is None else min(oldest, cached.fetched_at)
            for event in cached.value:
                kickoff = parse_instant(event.starts_at)
                if not (start <= kickoff < end):
                    continue
                if tournament is not None and event.tournament_id != tournament:
                    continue
                events[event.id] = _with_cache_state(event, cached.stale)
        ordered = sorted(events.values(), key=lambda e: (e.starts_at, e.id))
        return EventsResult(sport, ordered, catalog, _iso(oldest) if oldest else None, stale)

    async def event(self, event_id: str) -> tuple[SportEvent | None, Tournament | None]:
        match_id = football_data.parse_event_id(event_id)
        if match_id is None or self.football is None:
            return None, None
        key = f"football:match:{match_id}"
        adapter = self.football

        async def load():
            # Charged when an upstream request really happens — including refreshes of
            # an expired "not found" — for ids never seen in a listing.
            if event_id not in self._known_ids:
                self._spend_unknown_lookup()
            return await adapter.match(match_id)

        cached = await self.cache.get(key, load)
        event = cached.value
        if event is None:
            return None, None
        self._known_ids.add(event.id)
        tournament = next((t for t in adapter.tournaments() if t.id == event.tournament_id), None)
        return _with_cache_state(event, cached.stale), tournament

    # --- verified context for the AI analysis (shared cache, no per-user requests) ---

    async def team_form(self, team_id: str) -> list:
        number = football_data.team_number(team_id)
        if number is None or self.football is None:
            return []
        adapter = self.football
        return (await self.cache.get(f"football:form:{number}", lambda: adapter.team_form(number))).value

    async def standings(self, tournament_id: str) -> dict:
        code = tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        if self.football is None or code not in self.football.codes:
            return {}
        adapter = self.football
        return (await self.cache.get(f"football:standings:{code}", lambda: adapter.standings(code))).value

    def _spend_unknown_lookup(self) -> None:
        """Ids never seen in a listing may cost an upstream call: cap them per hour."""
        now = self._clock()
        while self._unknown_lookups and now - self._unknown_lookups[0] >= 3600:
            self._unknown_lookups.popleft()
        if len(self._unknown_lookups) >= self._unknown_per_hour:
            raise UpstreamError("rate_limited")
        self._unknown_lookups.append(now)


def _with_cache_state(event: SportEvent, stale: bool) -> SportEvent:
    if not stale or event.provenance.stale:
        return event
    p = event.provenance
    return replace(event, provenance=Provenance(p.provider, p.fetched_at, p.source_updated_at, stale=True))


def build_service(settings, *, clock: Callable[[], float] = time.time, transport=None) -> SportsService:
    """Service from Mini App settings. No key → football is reported "not_connected"."""
    from .client import ClientLimits, ProviderClient, urllib_transport

    football = None
    if settings.football_data_key:
        client = ProviderClient(
            football_data.PROVIDER,
            football_data.BASE_URL,
            {"X-Auth-Token": settings.football_data_key},
            ClientLimits(per_minute=settings.football_data_per_minute, per_day=settings.football_data_per_day),
            transport=transport or urllib_transport,
            clock=clock,
        )
        football = FootballDataAdapter(client, settings.football_data_competitions, clock)
    return SportsService(football, clock=clock, unknown_lookups_per_hour=settings.sports_unknown_lookups_per_hour,
                         football_live_plan=settings.football_data_live_plan)
