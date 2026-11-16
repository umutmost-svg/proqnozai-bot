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


def search_pacing(competitions: int, per_minute: int, per_day: int) -> tuple[float, float, bool]:
    """(spacing between warm-up loads, refresh age, whether previous seasons are warmed too) for this many
    competitions and request budget. Ten competitions on the free tier keep the original 15 s / 30 min;
    more competitions on a paid plan load faster and refresh less often, staying inside WARM_*_SHARE of
    the budget. A previous season (the goal model's history) costs one request a day per competition; it
    is warmed only when that leaves the calendars at least as much of the day's share."""
    day_share = per_day * WARM_DAY_SHARE
    # Two requests a step must fit the minute's share even at the slowest pace (the free tier does not:
    # there the warm-up keeps to calendars and the original pace).
    past = day_share >= 2 * competitions and per_minute * WARM_MINUTE_SHARE * WARM_SPACING_S / 60.0 >= 2
    per_load = 2 if past else 1  # requests one warm-up step may make (calendar + previous season)
    spacing = min(WARM_SPACING_S, max(MIN_WARM_SPACING_S, 60.0 * per_load / max(1.0, per_minute * WARM_MINUTE_SHARE)))
    calendars = day_share - competitions if past else day_share
    refresh = max(REFRESH_AFTER_S, competitions * 86400.0 / max(1.0, calendars))
    return spacing, refresh, past
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


# Search only (the odds matcher keeps `normalize`): Cyrillic of the app's languages (ru, kz, uz) to Latin, the way
# people type club names, and the Azerbaijani schwa. Applied to the query AND to the names it is compared with.
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i", "ў": "o",
    "ҳ": "h", "ə": "e",
}


def search_key(text: str) -> str:
    """normalize() after transliteration: 'Ливерпуль' -> 'liverpul', 'Qarabağ' -> 'qarabag', 'Fənərbaxça' ->
    'fenerbaxca'. Typos and transliteration variants are left to the fuzzy tier of the score."""
    lowered = text.casefold()
    return normalize("".join(_CYRILLIC.get(c, c) for c in lowered))


def query_letters(text: str) -> int:
    """Letters and digits the user typed (before transliteration: 'Ш' is one letter, not 'sh')."""
    return sum(c.isalnum() for c in text)


def _close(token: str, word: str) -> bool:
    """One edit for 4-7 letters, two from 8 (Levenshtein, early exit): 'barselona' ~ 'barcelona'."""
    if len(token) < 4:
        return False
    limit = 1 if len(token) < 8 else 2
    if abs(len(token) - len(word)) > limit:
        return False
    previous = list(range(len(word) + 1))
    for i, a in enumerate(token, 1):
        current = [i]
        for j, b in enumerate(word, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


# Names people type in the app's languages (ru, az, tr), keyed by the provider's stable team id (resolved from the real
# calendars on 14.11): a namesake (Liverpool FC of Uruguay, a club's women's team) never gets them.
LOCAL_ALIASES: dict[int, tuple[str, ...]] = {
    57: ("арсенал",),  # arsenal fc
    61: ("челси",),  # chelsea fc
    64: ("ливерпуль",),  # liverpool fc
    65: ("манчестер сити", "ман сити"),  # manchester city fc
    66: ("манчестер юнайтед", "ман юнайтед", "мю"),  # manchester united fc
    73: ("тоттенхэм", "тоттенхем"),  # tottenham hotspur fc
    67: ("ньюкасл",),  # newcastle united fc
    58: ("астон вилла",),  # aston villa fc
    62: ("эвертон",),  # everton fc
    81: ("барселона", "барса"),  # fc barcelona
    86: ("реал мадрид", "реал"),  # real madrid cf
    78: ("атлетико",),  # club atletico de madrid
    559: ("севилья",),  # sevilla fc
    95: ("валенсия",),  # valencia cf
    94: ("вильярреал",),  # villarreal cf
    90: ("бетис",),  # real betis balompie
    77: ("атлетик", "атлетик бильбао"),  # athletic club
    5: ("бавария", "bavariya"),  # fc bayern munchen
    4: ("боруссия дортмунд", "боруссия"),  # borussia dortmund
    3: ("байер", "байер леверкузен"),  # bayer 04 leverkusen
    721: ("лейпциг",),  # rb leipzig
    19: ("айнтрахт",),  # eintracht frankfurt
    109: ("ювентус",),  # juventus fc
    108: ("интер",),  # fc internazionale milano
    98: ("милан",),  # ac milan
    113: ("наполи",),  # ssc napoli
    100: ("рома",),  # as roma
    110: ("лацио",),  # ss lazio
    102: ("аталанта",),  # atalanta bc
    99: ("фиорентина",),  # acf fiorentina
    524: ("псж", "пари сен жермен"),  # paris saint germain fc
    516: ("марсель",),  # olympique de marseille
    523: ("лион",),  # olympique lyonnais
    548: ("монако",),  # as monaco fc
    521: ("лилль",),  # lille osc
    678: ("аякс",),  # afc ajax
    674: ("псв",),  # psv
    675: ("фейеноорд",),  # feyenoord rotterdam
    1903: ("бенфика", "benfica"),  # sport lisboa e benfica
    503: ("порту",),  # fc porto
    498: ("спортинг",),  # sporting clube de portugal
    5613: ("брага",),  # sporting clube de braga
    610: ("галатасарай",),  # galatasaray sk
    613: ("фенербахче",),  # fenerbahce sk
    600: ("бешикташ",),  # besiktas jk
    602: ("трабзонспор",),  # trabzonspor
    731: ("зенит",),  # fk zenit sankt petersburg
    754: ("спартак",),  # fk spartak moskva
    1900: ("цска", "cska"),  # pfc cska moskva
    5455: ("локомотив",),  # fk lokomotiv moskva
    5443: ("динамо москва",),  # fk dinamo moskva
    5452: ("краснодар",),  # fk krasnodar
    882: ("рубин",),  # fk rubin kazan
    760: ("испания", "ispaniya", "ispanya"),  # spain
    784: ("италия", "italiya", "italya"),  # italy
    759: ("германия", "almaniya", "almanya"),  # germany
    773: ("франция", "fransa"),  # france
    770: ("англия", "ingiltere"),  # england
    765: ("португалия", "portuqaliya", "portekiz"),  # portugal
    8601: ("нидерланды", "голландия", "niderland", "hollanda"),  # netherlands
    805: ("бельгия", "belcika"),  # belgium
    799: ("хорватия", "xorvatiya", "hirvatistan"),  # croatia
    788: ("швейцария", "isvecre", "isvicre"),  # switzerland
    782: ("дания",),  # denmark
    816: ("австрия",),  # austria
    794: ("польша",),  # poland
    790: ("украина", "ukrayna"),  # ukraine
    1971: ("азербайджан", "azerbaycan"),  # azerbaijan
    1980: ("казахстан", "qazaxistan"),  # kazakhstan
    1978: ("грузия", "gurcustan"),  # georgia
    1970: ("армения", "ermenistan"),  # armenia
    803: ("турция", "turkiye", "türkiye"),  # turkey
    8873: ("шотландия",),  # scotland
    833: ("уэльс",),  # wales
    806: ("ирландия",),  # ireland
    8872: ("норвегия",),  # norway
    792: ("швеция",),  # sweden
    780: ("сербия",),  # serbia
    798: ("чехия",),  # czechia
    827: ("венгрия",),  # hungary
    775: ("греция",),  # greece
    1972: ("беларусь", "белоруссия"),  # belarus
    764: ("бразилия", "braziliya", "brezilya"),  # brazil
    762: ("аргентина", "arjantin"),  # argentina
    758: ("уругвай",),  # uruguay
    818: ("колумбия",),  # colombia
}


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
                 spacing_s: float = WARM_SPACING_S, refresh_after_s: float = REFRESH_AFTER_S,
                 warm_past_seasons: bool = False) -> None:
        self._load = load_calendar  # async (tournament_id) -> CalendarResult | None (uses the shared cache)
        self._ids = tournament_ids
        self._clock = clock
        self._sleep = sleep
        self.spacing_s = spacing_s
        self.refresh_after_s = refresh_after_s
        self.warm_past_seasons = warm_past_seasons
        self._seasons: dict[str, tuple[float, list[FootballEvent]]] = {}
        self._warm: asyncio.Task | None = None
        self._name_cache: dict[tuple[str, str, str | None], list[str]] = {}  # (id, name, short) -> search keys

    def store(self, tournament_id: str, events: list, fetched_at: float) -> None:
        """Called whenever a calendar is loaded (by a user or by the warm-up)."""
        self._seasons[tournament_id] = (fetched_at, [e for e in events if isinstance(e, FootballEvent)])

    def _fresh(self) -> dict[str, list[FootballEvent]]:
        now = self._clock()
        return {t: events for t, (at, events) in self._seasons.items() if now - at <= MAX_AGE_S}

    def upcoming(self) -> list[FootballEvent]:
        """Every upcoming match of the indexed (fresh) calendars: the odds catalogue is matched against these."""
        return [e for events in self._fresh().values() for e in events if e.status == "upcoming"]

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

    def _names(self, team: Competitor) -> list[str]:
        """Search keys of a team (cached: computed once per name, not per keystroke)."""
        key = (team.id, team.name, team.short_name)
        cached = self._name_cache.get(key)
        if cached is None:
            if len(self._name_cache) > 50_000:
                self._name_cache.clear()
            number = football_data.team_number(team.id)
            names = [team.name, team.short_name or "", *ALIASES.get(number or -1, ()), *LOCAL_ALIASES.get(number or -1, ())]
            cached = self._name_cache[key] = [n for n in (search_key(x) for x in names) if n]
        return cached

    @staticmethod
    def _score(query: str, names: list[str], fuzzy: bool = True) -> int:
        """0 = no match; higher is better (exact > word prefix > substring or a near spelling)."""
        best = 0
        tokens = query.split()
        if not tokens:
            return 0  # nothing to match (every word was punctuation)
        for name in names:
            if name == query:
                return 3
            words = name.split()
            if all(any(w.startswith(t) for w in words) for t in tokens):
                best = max(best, 2)
            elif all(t in name for t in tokens):
                best = max(best, 1)
            elif fuzzy and all(t in name or any(_close(t, w) for w in words) for t in tokens):
                best = max(best, 1)
        return best

    def search(self, raw_query: str) -> SearchResult:
        query = search_key(raw_query)
        teams = self._teams()
        scored = []
        for team, tournaments in teams.values():
            score = self._score(query, self._names(team), fuzzy=False)
            if score:
                scored.append((score, team, tournaments))
        if len(scored) < MAX_TEAMS:
            # Near spellings only when exact, prefix and substring hits leave room: the costly pass is the rare one.
            found = {team.id for _, team, _ in scored}
            for team, tournaments in teams.values():
                if team.id not in found and self._score(query, self._names(team)):
                    scored.append((1, team, tournaments))
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
        # Which words name each team, worked out once per team (a few thousand) rather than once per event
        # (~35k, both sides): the per-event check is then a lookup. The fuzzy pass per event blocked the event loop
        # for 1-2 s on a full index (Fable audit 16.11).
        named: dict[str, tuple[bool, ...]] = {}

        def words_of(team: Competitor) -> tuple[bool, ...]:
            got = named.get(team.id)
            if got is None:
                names = self._names(team)
                got = named[team.id] = tuple(bool(self._score(t, names)) for t in tokens)
            return got

        for events in self._fresh().values():
            for e in events:
                on_home, on_away = words_of(e.home), words_of(e.away)
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
