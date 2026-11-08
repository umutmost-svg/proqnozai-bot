"""Sports data service: per-sport providers behind one cache.

Only sports with a connected provider return events. The others answer with an
explicit "not_connected" coverage state — never with demo data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from . import football_data
from .cache import Cached, SportsCache
from .client import UpstreamError
from .crests import CrestStore
from .football_data import FootballDataAdapter, Season
from .models import SPORTS, Provenance, Sport, SportEvent, Tournament

logger = logging.getLogger("miniapp.sports")

MAX_RANGE = timedelta(hours=48)
# How long a page response may wait for the (optional) competition emblem list.
EMBLEMS_WAIT_S = 2.0
PAST_WINDOW = timedelta(days=30)
FUTURE_WINDOW = timedelta(days=60)


class RequestError(Exception):
    """Invalid client request; ``code`` goes to the API response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CalendarResult:
    tournament: Tournament
    season: Season
    events: list[SportEvent]
    fetched_at: str
    stale: bool


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
                 football_live_plan: bool = False, crests: CrestStore | None = None,
                 warm_emblems: bool = True) -> None:
        self.football = football
        self.crests = crests or CrestStore(clock=clock)
        self.football_live_plan = football_live_plan
        self.cache = cache or SportsCache(clock=clock)
        self._clock = clock
        self._known_ids: set[str] = set()
        self._calendar_index: dict[str, str] = {}  # event id -> tournament id, from season calendars
        self._warm_emblems = warm_emblems
        self._emblems_retry_at = 0.0
        self._emblems_task: asyncio.Task[None] | None = None
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
                    "tournaments": [self.tournament_json(t) for t in self.football.tournaments()],
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

    async def ensure_emblems(self, wait_s: float = EMBLEMS_WAIT_S) -> None:
        """Learn every served competition's emblem once (one shared request a day), so even a
        cold tournament list has them. Optional: the request runs in the background and a
        response waits for it at most `wait_s` — a slow or failing provider only means no
        emblem for now, never a slow or failed page."""
        adapter = self.football
        # Refreshed once a day even when every emblem is known: the provider may change a URL.
        if adapter is None or not self._warm_emblems:
            return
        if self._emblems_task is None:
            if self._clock() < self._emblems_retry_at:
                return
            self._emblems_task = asyncio.create_task(self._load_emblems(adapter))
        try:
            await asyncio.wait_for(asyncio.shield(self._emblems_task), wait_s)
        except TimeoutError:
            return

    async def _load_emblems(self, adapter: FootballDataAdapter) -> None:
        try:
            await self.cache.get("football:competitions", adapter.competition_emblems)
            # Codes the list gave no emblem for are not asked again before the next day.
            self._emblems_retry_at = self._clock() + football_data.EMBLEMS_TTL_S
        except Exception as exc:  # optional data: any failure only gates the next attempt
            logger.warning("sports.emblems_unavailable type=%s code=%s", type(exc).__name__,
                           getattr(exc, "code", "-"))
            self._emblems_retry_at = self._clock() + football_data.EMBLEMS_RETRY_S
        finally:
            self._emblems_task = None

    def tournament_json(self, tournament: Tournament) -> dict:
        code = tournament.id.removeprefix(f"{football_data.ID_PREFIX}:")
        if self.football is not None:
            if code in self.football.emblems:
                self.crests.learn("competition", code, self.football.emblems[code])
            else:
                self.crests.forget("competition", code)  # withdrawn by the provider
        return tournament.to_json(emblem=self.crests.path("competition", code))

    def _learn_crests(self, events) -> None:
        """Pictures are served only for URLs the provider itself gave in a payload."""
        for event in events:
            for side in (event.home, event.away):
                number = football_data.team_number(side.id)
                if side.crest_source and number is not None:
                    self.crests.learn("team", str(number), side.crest_source)

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
        self._learn_crests(cached.value)
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
        # A match seen only in a season calendar is answered from that calendar (one shared
        # request per competition), never by per-match requests outside the unknown-id budget.
        calendar_of = self._calendar_index.get(event_id)
        if calendar_of is not None and (event_id not in self._known_ids or self._calendar_fresh(calendar_of)):
            season = await self.calendar("football", calendar_of)
            found = next((e for e in season.events if e.id == event_id), None) if season else None
            if found is not None and season is not None:
                return found, season.tournament
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
        self._learn_crests([event])
        tournament = next((t for t in adapter.tournaments() if t.id == event.tournament_id), None)
        return _with_cache_state(event, cached.stale), tournament

    def _calendar_fresh(self, tournament_id: str) -> bool:
        """A cached, unexpired season calendar: answering from it costs no request at all."""
        code = tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        try:
            return isinstance(self.cache.existing(f"football:calendar:{code}"), Cached)
        except UpstreamError:
            return False

    async def calendar(self, sport: Sport, tournament_id: str) -> CalendarResult | None:
        """The current season of one tournament (None: not a tournament we serve)."""
        if not self.connected(sport):
            return None
        tournament = next((t for t in self.tournaments(sport) if t.id == tournament_id), None)
        if tournament is None:
            return None
        assert self.football is not None
        adapter = self.football
        code = tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        cached = await self.cache.get(f"football:calendar:{code}", lambda: adapter.calendar(code))
        events, season = cached.value
        # Not "known" ids: their details come from this calendar, not from per-match requests.
        if len(self._calendar_index) > 50_000:
            self._calendar_index.clear()
        self._calendar_index.update((e.id, tournament_id) for e in events)
        self._learn_crests(events)
        return CalendarResult(tournament, season, [_with_cache_state(e, cached.stale) for e in events],
                              _iso(cached.fetched_at), cached.stale)

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


def build_service(settings, *, clock: Callable[[], float] = time.time, transport=None,
                  crest_fetch=None, warm_emblems: bool = True) -> SportsService:
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
    crests = CrestStore(crest_fetch, clock=clock) if crest_fetch is not None else CrestStore(clock=clock)
    return SportsService(football, clock=clock, unknown_lookups_per_hour=settings.sports_unknown_lookups_per_hour,
                         football_live_plan=settings.football_data_live_plan, crests=crests,
                         warm_emblems=warm_emblems)
