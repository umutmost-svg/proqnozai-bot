"""Global football search over the served competitions (M3.4).

The index is built from the season calendars (one shared request per competition,
the same cache as the tournament calendar), never per keystroke. A background warm-up
loads missing or old calendars one at a time with a pause between upstream requests,
so it stays far below the provider's per-minute limit. Results say how many of the
served competitions are indexed: partial coverage is never presented as complete.

Matching is case-, accent- and punctuation-insensitive. Alternative names are tied to
the provider's stable team id, so a club is never confused with a namesake.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from . import football_data
from .client import UpstreamError
from .models import Competitor, FootballEvent

logger = logging.getLogger("miniapp.sports")

MIN_QUERY = 2
MAX_QUERY = 40
MAX_TEAMS = 10
MAX_EVENTS = 30
WARM_SPACING_S = 15.0  # between upstream calendar loads of the warm-up
REFRESH_AFTER_S = 30 * 60.0  # an indexed calendar older than this is refreshed in the background
# With many competitions the warm-up adapts to the plan (search_pacing): it may use at most this share of
# the per-minute and of the daily request budget, so users' own requests always keep the rest.
WARM_MINUTE_SHARE = 0.25
WARM_DAY_SHARE = 0.25
MIN_WARM_SPACING_S = 2.0


def search_pacing(competitions: int, per_minute: int, per_day: int) -> tuple[float, float]:
    """(spacing between warm-up loads, refresh age) for this many competitions and request budget.
    Ten competitions on the free tier keep the original 15 s / 30 min; more competitions on a paid plan
    load faster and refresh less often, staying inside WARM_*_SHARE of the budget."""
    spacing = min(WARM_SPACING_S, max(MIN_WARM_SPACING_S, 60.0 / max(1.0, per_minute * WARM_MINUTE_SHARE)))
    refresh = max(REFRESH_AFTER_S, competitions * 86400.0 / max(1.0, per_day * WARM_DAY_SHARE))
    return spacing, refresh
FIRST_WAIT_S = 3.0  # a search on a cold index waits this long for the first calendars
# A calendar older than this is neither searched nor counted as covered (same bound as the
# shared sports cache serves stale data): refresh failures can never pass old fixtures off
# as complete coverage.
MAX_AGE_S = 6 * 3600.0

# Common alternative names, keyed by the provider's stable team id.
ALIASES: dict[int, tuple[str, ...]] = {
    81: ("Barça", "Barca", "FC Barcelona"),
    78: ("Atleti", "Atletico", "Atlético de Madrid", "Atletico Madrid"),
    86: ("Real Madrid CF",),
    65: ("Man City", "City"),
    66: ("Man United", "Man Utd", "Manchester Utd"),
    73: ("Spurs",),
    76: ("Wolves",),
    524: ("PSG", "Paris SG"),
    5: ("Bayern München", "FC Bayern"),
    18: ("Gladbach",),
    108: ("Internazionale", "Inter Milan"),
    109: ("Juve",),
    98: ("Milan",),
    1903: ("SL Benfica",),
    498: ("Sporting Lisbon", "Sporting"),
    7: ("HSV", "Hamburg"),
    69: ("Queens Park Rangers",),
}


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    plain = plain.replace("ø", "o").replace("ß", "ss").replace("ı", "i")
    return re.sub(r"[^0-9a-z]+", " ", plain).strip()


@dataclass(frozen=True)
class TeamHit:
    team: Competitor
    tournaments: tuple[str, ...]

    def to_json(self) -> dict:
        return {**self.team.to_json(), "tournaments": list(self.tournaments)}


@dataclass(frozen=True)
class SearchResult:
    teams: list[TeamHit]
    events: list[FootballEvent]
    indexed: list[str]  # tournament ids whose calendars are in the index
    total: int  # tournament ids served

    @property
    def complete(self) -> bool:
        return len(self.indexed) == self.total


class SearchIndex:
    """Season calendars of the served competitions, searchable by team."""

    def __init__(self, load_calendar: Callable, tournament_ids: Callable[[], list[str]],
                 clock: Callable[[], float], sleep: Callable = asyncio.sleep, *,
                 spacing_s: float = WARM_SPACING_S, refresh_after_s: float = REFRESH_AFTER_S) -> None:
        self._load = load_calendar  # async (tournament_id) -> CalendarResult | None (uses the shared cache)
        self._ids = tournament_ids
        self._clock = clock
        self._sleep = sleep
        self.spacing_s = spacing_s
        self.refresh_after_s = refresh_after_s
        self._seasons: dict[str, tuple[float, list[FootballEvent]]] = {}
        self._warm: asyncio.Task | None = None

    def store(self, tournament_id: str, events: list, fetched_at: float) -> None:
        """Called whenever a calendar is loaded (by a user or by the warm-up)."""
        self._seasons[tournament_id] = (fetched_at, [e for e in events if isinstance(e, FootballEvent)])

    def _fresh(self) -> dict[str, list[FootballEvent]]:
        now = self._clock()
        return {t: events for t, (at, events) in self._seasons.items() if now - at <= MAX_AGE_S}

    def indexed(self) -> list[str]:
        fresh = self._fresh()
        return [t for t in self._ids() if t in fresh]

    def _due(self) -> list[str]:
        now = self._clock()
        return [t for t in self._ids() if t not in self._seasons or now - self._seasons[t][0] > self.refresh_after_s]

    @property
    def warming(self) -> bool:
        return self._warm is not None and not self._warm.done()

    def ensure_warm(self) -> asyncio.Task | None:
        if self._warm is None and self._due():
            self._warm = asyncio.create_task(self._run())
        return self._warm

    async def _run(self) -> None:
        try:
            first = True
            for tournament_id in self._due():
                if not first:
                    await self._sleep(self.spacing_s)
                first = False
                try:
                    await self._load(tournament_id)
                except UpstreamError as exc:
                    logger.info("search.warm_skipped tournament=%s code=%s", tournament_id, exc.code)
                    if exc.low_priority_only:
                        break  # the background budget is spent: the next search starts a new pass
                except Exception as exc:  # optional background work: log and go on
                    logger.warning("search.warm_failed tournament=%s type=%s", tournament_id, type(exc).__name__)
        finally:
            self._warm = None

    async def wait_for_some(self, timeout_s: float = FIRST_WAIT_S) -> None:
        """On a cold index, give the warm-up a short head start (never longer than timeout_s)."""
        task = self.ensure_warm()
        if task is None or self._fresh():
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self._fresh() and not task.done() and loop.time() < deadline:
            await asyncio.sleep(0.05)

    def _teams(self) -> dict[str, tuple[Competitor, set[str]]]:
        teams: dict[str, tuple[Competitor, set[str]]] = {}
        for tournament_id, events in self._fresh().items():
            for event in events:
                for side in (event.home, event.away):
                    known = teams.get(side.id)
                    if known is None:
                        teams[side.id] = (side, {tournament_id})
                    else:
                        known[1].add(tournament_id)
        return teams

    @staticmethod
    def _names(team: Competitor) -> list[str]:
        number = football_data.team_number(team.id)
        names = [team.name, team.short_name or "", *ALIASES.get(number or -1, ())]
        return [n for n in (normalize(x) for x in names) if n]

    @staticmethod
    def _score(query: str, names: list[str]) -> int:
        """0 = no match; higher is better (exact > word prefix > substring)."""
        best = 0
        tokens = query.split()
        for name in names:
            if name == query:
                return 3
            words = name.split()
            if all(any(w.startswith(t) for w in words) for t in tokens):
                best = max(best, 2)
            elif all(t in name for t in tokens):
                best = max(best, 1)
        return best

    def search(self, raw_query: str) -> SearchResult:
        query = normalize(raw_query)
        teams = self._teams()
        scored = []
        for team, tournaments in teams.values():
            score = self._score(query, self._names(team))
            if score:
                scored.append((score, team, tournaments))
        scored.sort(key=lambda x: (-x[0], x[1].short_name or x[1].name))
        hits = [TeamHit(team, tuple(sorted(t))) for _, team, t in scored[:MAX_TEAMS]]
        ids = {h.team.id for h in hits}
        events = self._events_of(ids) if ids else self._fixtures_matching(query)
        return SearchResult(hits, events, self.indexed(), len(self._ids()))

    def _fixtures_matching(self, query: str) -> list[FootballEvent]:
        """A query naming both sides ("arsenal chelsea"): every word must match one of the two teams."""
        tokens = query.split()
        if len(tokens) < 2:
            return []
        found: dict[str, FootballEvent] = {}
        for events in self._fresh().values():
            for e in events:
                home, away = self._names(e.home), self._names(e.away)
                on_home = [bool(self._score(t, home)) for t in tokens]
                on_away = [bool(self._score(t, away)) for t in tokens]
                # Every word names one of the two sides, and each side is named at least once.
                if all(h or a for h, a in zip(on_home, on_away, strict=True)) and any(on_home) and any(on_away):
                    found[e.id] = e
        return self._ordered(list(found.values()))

    def team(self, team_id: str) -> tuple[TeamHit | None, list[FootballEvent]]:
        found = self._teams().get(team_id)
        if found is None:
            return None, []
        return TeamHit(found[0], tuple(sorted(found[1]))), self._events_of({team_id}, limit=None)

    def _events_of(self, team_ids: set[str], limit: int | None = MAX_EVENTS) -> list[FootballEvent]:
        """Upcoming and live first (soonest first), then played ones (most recent first)."""
        seen: dict[str, FootballEvent] = {}
        for events in self._fresh().values():
            for e in events:
                if e.home.id in team_ids or e.away.id in team_ids:
                    seen[e.id] = e
        ordered = self._ordered(list(seen.values()), limit=None)
        return ordered if limit is None else ordered[:limit]

    @staticmethod
    def _ordered(events: list[FootballEvent], limit: int | None = MAX_EVENTS) -> list[FootballEvent]:
        ahead = sorted((e for e in events if e.status in ("live", "upcoming")), key=lambda e: e.starts_at)
        ahead.sort(key=lambda e: e.status != "live")  # stable: live first, then soonest
        rest = sorted((e for e in events if e.status not in ("live", "upcoming")), key=lambda e: e.starts_at, reverse=True)
        return ahead + rest if limit is None else (ahead + rest)[:limit]
