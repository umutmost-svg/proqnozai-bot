"""Sports data service: per-sport providers behind one cache.

Only sports with a connected provider return events. The others answer with an
explicit "not_connected" coverage state — never with demo data.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from . import football_data
from . import top as top_matches
from .cache import Cached, SportsCache
from .client import LOW_PRIORITY_DAY_SHARE, LOW_PRIORITY_RESERVE, UpstreamError
from .crests import CrestStore
from .football_data import FootballDataAdapter, Season, Standings
from .search import REFRESH_AFTER_S, WARM_SPACING_S, SearchIndex, search_pacing
from .models import SPORTS, FootballEvent, Provenance, Sport, SportEvent, Tournament

logger = logging.getLogger("miniapp.sports")

MAX_RANGE = timedelta(hours=48)
# How long a page response may wait for the (optional) competition emblem list.
EMBLEMS_WAIT_S = 2.0
PAST_WINDOW = timedelta(days=30)
PAST_SEASON_RETRY_S = 6 * 3600.0
FUTURE_WINDOW = timedelta(days=60)
# Day-table snapshots of the top-match ranking kept at most (one per day start and competition).
TOP_TABLES_MAX = 2000
# Standings warm-up: every served table is refreshed in the background before its cache entry
# (football_data.STANDINGS_TTL_S, 30 min) expires, so a user never waits on, or fails at, the
# provider's per-minute limit for a table. It runs only while the app is used (each signed-in
# sports request may start a pass), one table at a time, and leaves the minute and the day to
# users: it needs more than STANDINGS_WARM_RESERVE requests of headroom and stops once half
# of the daily budget is spent. At most ~2 refreshes per table per hour.
STANDINGS_REFRESH_AFTER_S = 25 * 60.0
STANDINGS_WARM_SPACING_S = 15.0
STANDINGS_WARM_RESERVE = 3
STANDINGS_WARM_DAY_SHARE = 0.5
# After a warm-up attempt fails (or leaves the old table in place), the warm-up leaves that table
# alone this long; users' own requests keep the cache's usual rules.
STANDINGS_WARM_RETRY_S = 10 * 60.0
STANDINGS_WARM_NOT_FOUND_S = 6 * 3600.0


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
class StandingsResult:
    tournament: Tournament
    standings: Standings
    fetched_at: str
    stale: bool


@dataclass(frozen=True)
class ModelHistory:
    """What the goal model learns from: the previous season (if the provider has it) and the
    current one, as normalized events."""

    events: list[SportEvent]
    season_start: str | None  # current season's first day (ISO date)
    previous_season: bool
    # Why the previous season is missing: "absent" (the source has none) or "transient"
    # (timeout, rate limit, outage: may load on a later try); None when it is there.
    previous_missing: str | None = None


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
                 warm_emblems: bool = True, warm_standings: bool = False,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
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
        spacing, refresh = WARM_SPACING_S, REFRESH_AFTER_S
        if football is not None:
            usage = football.client.usage()
            spacing, refresh = search_pacing(len(football.codes), int(usage["perMinuteLimit"]),
                                             int(usage["dailyLimit"]))
        self.search_index = SearchIndex(self._search_warm_load,
                                        lambda: [t.id for t in self.tournaments("football")], clock,
                                        spacing_s=spacing, refresh_after_s=refresh)
        self._unknown_lookups: deque[float] = deque()
        # Called with every season calendar that loads (e.g. forecast settlement); never fails a request.
        self.calendar_listeners: list[Callable[[str, list[SportEvent]], None]] = []
        self._notified: dict[str, float] = {}
        self._past_retry_at: dict[tuple[str, int], float] = {}
        self._past_failure: dict[tuple[str, int], str] = {}
        self._top_tables: OrderedDict[tuple[str, str], top_matches.Table] = OrderedDict()
        self._unknown_per_hour = unknown_lookups_per_hour
        self._warm_standings = warm_standings
        self._background_loads = 0  # settlement calendar loads in flight (see background_busy)
        self._standings_task: asyncio.Task[None] | None = None
        # ONE background request at a time (standings / search warm-up / settlement): the client has two
        # request slots, and the other one always stays free for users.
        self._background_slot = asyncio.Lock()
        self._standings_warm_retry_at: dict[str, float] = {}
        self._sleep = sleep

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
            self._learn_crests_of((event.home, event.away))

    def _learn_crests_of(self, teams) -> None:
        for side in teams:
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

    async def top(self, start: datetime, end: datetime,
                  tournament: str | None = None) -> tuple[list[top_matches.Ranked], EventsResult]:
        """The day's top matches from the same day listings as the catalogue (no extra request);
        all competitions unless the user narrowed the day to one."""
        result = await self.events("football", start, end, tournament)
        events = [e for e in result.events if isinstance(e, FootballEvent)]
        tables = {t: self._day_table(start, t) for t in {e.tournament_id for e in events}}
        return top_matches.rank(events, lambda t: t.removeprefix(f"{football_data.ID_PREFIX}:"), tables,
                                now=self._clock()), result

    def _day_table(self, start: datetime, tournament_id: str) -> top_matches.Table | None:
        """The table the day's ranking uses, fixed for that day once known: a cache entry aging
        out (or refreshing) never reorders the day's cards; only a first table can add weight.
        Keyed by the day start on a 15-minute grid (every time zone's midnight lies on it) and
        bounded: finished days go first, then the least recently used."""
        utc = start.astimezone(UTC)
        key = (utc.replace(minute=utc.minute - utc.minute % 15, second=0, microsecond=0).isoformat(), tournament_id)
        if key in self._top_tables:
            self._top_tables.move_to_end(key)
            return self._top_tables[key]
        table = self._cached_table(tournament_id)
        if table is not None:
            self._top_tables[key] = table
            if len(self._top_tables) > TOP_TABLES_MAX:
                horizon = datetime.fromtimestamp(self._clock() - 2 * 86400, UTC).isoformat()
                for old in [k for k in self._top_tables if k[0] < horizon]:
                    del self._top_tables[old]
                while len(self._top_tables) > TOP_TABLES_MAX:
                    self._top_tables.popitem(last=False)
        return table

    def _cached_table(self, tournament_id: str) -> top_matches.Table | None:
        """A single overall table already in the cache (never loaded for the ranking)."""
        code = tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        entry = self.cache.peek(f"football:standings:{code}")
        if entry is None or self._clock() - entry.fetched_at > top_matches.WEIGHTS.standings_max_age_s:
            return None
        table = entry.value
        return top_matches.table_of(table.rows_by_team()) if len(table.blocks) == 1 else None

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
        marked = [_with_cache_state(e, cached.stale) for e in events]
        # The index keeps the same staleness marks as the calendar answer (served-from-cache scores).
        self.search_index.store(tournament_id, marked, cached.fetched_at)
        # Listeners see each loaded calendar once (not every cache hit of it).
        if self.calendar_listeners and self._notified.get(tournament_id) != cached.fetched_at:
            self._notified[tournament_id] = cached.fetched_at
            for listener in self.calendar_listeners:
                try:
                    listener(tournament_id, events)
                except Exception as exc:  # a listener's failure must never fail a calendar
                    logger.warning("sports.calendar_listener_failed type=%s", type(exc).__name__)
        return CalendarResult(tournament, season, marked, _iso(cached.fetched_at), cached.stale)

    async def standings_table(self, sport: Sport, tournament_id: str) -> StandingsResult | None:
        """The provider's current tables of one tournament (None: not a tournament we serve)."""
        if not self.connected(sport):
            return None
        tournament = next((t for t in self.tournaments(sport) if t.id == tournament_id), None)
        if tournament is None:
            return None
        cached = await self._standings(tournament_id)
        for block in cached.value.blocks:
            self._learn_crests_of([row.team for row in block.rows])
        return StandingsResult(tournament, cached.value, _iso(cached.fetched_at), cached.stale)

    def _costs_request(self, key: str) -> bool:
        """Would reading this cache key now make a provider request? (A fresh value, a load
        already running and a remembered failure cost nothing.)"""
        try:
            return self.cache.existing(key) is None
        except UpstreamError:
            return False

    def upstream_headroom(self) -> int:
        """Provider requests left in this minute for everyone."""
        return self.football.client.minute_headroom() if self.football is not None else 0

    def day_cost(self, day: date) -> int:
        return int(self.football is not None and self._costs_request(f"football:day:{day.isoformat()}"))

    def history_cost(self, tournament_id: str) -> int:
        """Provider requests model_history() would make now (an unknown season counts as one)."""
        code = tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        key = f"football:calendar:{code}"
        cost = int(self._costs_request(key))
        entry = self.cache.peek(key)
        if entry is None:
            return cost + 1
        start = entry.value[1].start
        if start is None:
            return cost
        year = int(start[:4]) - 1
        if self._clock() < self._past_retry_at.get((code, year), 0.0):
            return cost
        return cost + int(self._costs_request(f"football:season:{code}:{year}"))

    async def model_history(self, tournament_id: str) -> ModelHistory | None:
        """Current-season calendar plus the previous season (one request a day, shared)."""
        current = await self.calendar("football", tournament_id)
        if current is None:
            return None
        assert self.football is not None
        adapter = self.football
        code = tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        start = current.season.start
        events: list[SportEvent] = []
        previous = False
        missing: str | None = None
        if start is not None:
            year = int(start[:4]) - 1
            if self._clock() < self._past_retry_at.get((code, year), 0.0):
                missing = self._past_failure.get((code, year), "transient")
            else:
                try:
                    past = await self.cache.get(f"football:season:{code}:{year}",
                                                lambda: adapter.past_season(code, year))
                except UpstreamError as exc:
                    # Optional depth: without it the model shows probabilities but recommends
                    # nothing (partial_history). Not asked again for PAST_SEASON_RETRY_S, so a
                    # season the provider cannot supply never costs a request per analysis.
                    self._past_retry_at[(code, year)] = self._clock() + PAST_SEASON_RETRY_S
                    missing = "absent" if exc.code == "not_found" else "transient"
                    self._past_failure[(code, year)] = missing
                    logger.info("sports.past_season_unavailable tournament=%s code=%s", tournament_id, exc.code)
                else:
                    events.extend(past.value[0])
                    previous = True
        events.extend(current.events)
        return ModelHistory(events, start, previous, missing)

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
        table = (await self._standings(tournament_id)).value
        # Positions compare only inside one table: with groups or several stages there is no
        # single ranking, so the AI gets no table evidence at all rather than a mixed one.
        return table.rows_by_team() if len(table.blocks) == 1 else {}

    # --- standings warm-up ---------------------------------------------------

    def ensure_standings_warm(self) -> asyncio.Task[None] | None:
        """Starts a background pass over the tables that are missing or due (never waits for it)."""
        if not self._warm_standings or self.football is None:
            return self._standings_task
        if self._background_loads:
            return self._standings_task  # a settlement load holds the background slot: next request
        if self._standings_task is None and self._standings_due():
            self._standings_task = asyncio.create_task(self._warm_standings_pass())
        return self._standings_task

    def _standings_due(self) -> list[str]:
        now = self._clock()
        due = []
        codes = [t.id.removeprefix(f"{football_data.ID_PREFIX}:") for t in self.tournaments("football")]
        if len(codes) > len(football_data.STANDINGS_WARM):
            # Large catalogue: only the main competitions are warmed; the rest load on demand.
            codes = [c for c in codes if c in football_data.STANDINGS_WARM]
        for code in codes:
            if now < self._standings_warm_retry_at.get(code, 0.0):
                continue  # the warm-up's own pause after a failed attempt
            key = f"football:standings:{code}"
            entry = self.cache.peek(key)
            if entry is not None and now < entry.expires_at and (
                    entry.stale or now - entry.fetched_at < STANDINGS_REFRESH_AFTER_S):
                continue  # in date (or a stale copy inside its retry backoff)
            try:
                if isinstance(self.cache.existing(key), asyncio.Future):
                    continue  # someone is loading it right now
            except UpstreamError:
                continue  # a remembered failure: its backoff holds for the warm-up too
            due.append(code)
        return due

    async def background_calendar(self, tournament_id: str) -> CalendarResult | None:
        """A season calendar for background work (forecast settlement): None when the
        warm-up's budget is used up or another background load is running. Same low priority
        as the standings warm-up: it keeps the minute's reserve for users and stops at its
        share of the day."""
        if self.football is None or not self._warm_budget_left() or self.background_busy():
            return None
        reserve = LOW_PRIORITY_RESERVE.set(STANDINGS_WARM_RESERVE)
        share = LOW_PRIORITY_DAY_SHARE.set(STANDINGS_WARM_DAY_SHARE)
        self._background_loads += 1
        try:
            # A load started here copies this context (its retries keep the same limits).
            async with self._background_slot:
                return await self.calendar("football", tournament_id)
        finally:
            self._background_loads -= 1
            LOW_PRIORITY_DAY_SHARE.reset(share)
            LOW_PRIORITY_RESERVE.reset(reserve)

    async def _search_warm_load(self, tournament_id: str) -> CalendarResult | None:
        """A calendar for the team-search warm-up: same low priority and day share as the other
        background work (users' requests keep the minute's reserve); refused when the budget is spent."""
        if self.football is None or not self._warm_budget_left():
            raise UpstreamError("rate_limited", low_priority_only=True)
        async with self._background_slot:
            reserve = LOW_PRIORITY_RESERVE.set(STANDINGS_WARM_RESERVE)
            share = LOW_PRIORITY_DAY_SHARE.set(STANDINGS_WARM_DAY_SHARE)
            try:
                return await self.calendar("football", tournament_id)
            finally:
                LOW_PRIORITY_DAY_SHARE.reset(share)
                LOW_PRIORITY_RESERVE.reset(reserve)

    def background_busy(self) -> bool:
        """A background load (standings warm-up, search warm-up, settlement) is running: the
        provider client has two request slots, and background work may hold at most one."""
        standings = self._standings_task is not None and not self._standings_task.done()
        return standings or self.search_index.warming or self._background_loads > 0

    def _warm_budget_left(self) -> bool:
        client = self.football.client  # type: ignore[union-attr]
        usage = client.usage()
        if usage["usedToday"] + 1 > math.floor(usage["dailyLimit"] * STANDINGS_WARM_DAY_SHARE):
            return False
        return client.minute_headroom() > STANDINGS_WARM_RESERVE

    async def _warm_standings_pass(self) -> None:
        adapter = self.football
        assert adapter is not None
        try:
            first = True
            for code in self._standings_due():
                if not first:
                    await self._sleep(STANDINGS_WARM_SPACING_S)
                first = False
                if code not in self._standings_due():
                    continue  # loaded meanwhile (by a user)
                if not self._warm_budget_left():
                    logger.info("sports.standings_warm_paused code=%s", code)
                    return
                key = f"football:standings:{code}"
                before = self.cache.peek(key)
                try:
                    async with self._background_slot:  # one background request at a time
                        # The load task copies this context: every attempt it makes (retries included)
                        # keeps the minute's reserve free and stops at the warm-up's share of the day.
                        reserve = LOW_PRIORITY_RESERVE.set(STANDINGS_WARM_RESERVE)
                        share = LOW_PRIORITY_DAY_SHARE.set(STANDINGS_WARM_DAY_SHARE)
                        try:
                            future = self.cache.start(key, lambda c=code: adapter.standings(c))
                        finally:
                            LOW_PRIORITY_DAY_SHARE.reset(share)
                            LOW_PRIORITY_RESERVE.reset(reserve)
                        cached = await asyncio.shield(future)
                except UpstreamError as exc:
                    pause = STANDINGS_WARM_NOT_FOUND_S if exc.code == "not_found" else STANDINGS_WARM_RETRY_S
                    self._standings_warm_retry_at[code] = self._clock() + pause
                    logger.info("sports.standings_warm_skipped code=%s reason=%s", code, exc.code)
                    continue
                if cached is before or cached.stale:
                    # The refresh failed and the cache kept what it had: not asked again soon.
                    self._standings_warm_retry_at[code] = self._clock() + STANDINGS_WARM_RETRY_S
                    logger.info("sports.standings_warm_skipped code=%s reason=kept", code)
                    continue
                if not cached.value.blocks:
                    # A competition without a table (a cup): not worth asking again soon.
                    self._standings_warm_retry_at[code] = self._clock() + STANDINGS_WARM_NOT_FOUND_S
                    continue
                self._standings_warm_retry_at.pop(code, None)
                for block in cached.value.blocks:
                    self._learn_crests_of([row.team for row in block.rows])
        except Exception as exc:  # optional background work: log, the next request starts a new pass
            logger.warning("sports.standings_warm_failed type=%s", type(exc).__name__)
        finally:
            self._standings_task = None

    async def _standings(self, tournament_id: str) -> Cached[Standings]:
        assert self.football is not None
        adapter = self.football
        code = tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        return await self.cache.get(f"football:standings:{code}", lambda: adapter.standings(code))

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
                  crest_fetch=None, warm_emblems: bool = True, warm_standings: bool = False) -> SportsService:
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
                         warm_emblems=warm_emblems, warm_standings=warm_standings)
