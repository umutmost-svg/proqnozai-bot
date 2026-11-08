"""football-data.org v4 adapter (football only).

Docs: https://docs.football-data.org/general/v4/index.html — auth header
``X-Auth-Token``. Only fields the API actually returns are used; match
statistics, lineups, injuries, odds and probabilities are not taken from here
and stay ``None`` / "unavailable".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .client import ProviderClient, UpstreamError
from .crests import crest_source
from .models import Competitor, EventStatus, FootballEvent, Provenance, Tournament

PROVIDER = "football-data.org"
# Required by the football-data.org terms (§7): shown wherever the data is shown.
ATTRIBUTION = "Football data provided by the Football-Data.org API"
BASE_URL = "https://api.football-data.org/v4"
ID_PREFIX = "fd"

# Codes and names of competitions football-data.org documents; the operator
# chooses which of them to serve (MINIAPP_FOOTBALL_DATA_COMPETITIONS).
KNOWN_COMPETITIONS: dict[str, tuple[str, str]] = {
    "PL": ("Premier League", "England"),
    "PD": ("LaLiga", "Spain"),
    "SA": ("Serie A", "Italy"),
    "BL1": ("Bundesliga", "Germany"),
    "FL1": ("Ligue 1", "France"),
    "CL": ("UEFA Champions League", "Europe"),
    "DED": ("Eredivisie", "Netherlands"),
    "PPL": ("Primeira Liga", "Portugal"),
    "ELC": ("Championship", "England"),
    "BSA": ("Campeonato Brasileiro Série A", "Brazil"),
    "EC": ("European Championship", "Europe"),
    "WC": ("FIFA World Cup", "World"),
}

_STATUS: dict[str, EventStatus] = {
    "SCHEDULED": "upcoming",
    "TIMED": "upcoming",
    "IN_PLAY": "live",
    "PAUSED": "live",  # half-time or another break inside a live match
    "EXTRA_TIME": "live",
    "PENALTY_SHOOTOUT": "live",
    "LIVE": "live",
    "FINISHED": "finished",
    "AWARDED": "finished",
    "POSTPONED": "postponed",
    "SUSPENDED": "suspended",
    "CANCELLED": "cancelled",
}

# Cache lifetimes by state. The free tier allows a handful of requests per
# minute; live data is refreshed at most every LIVE_TTL_S, never per user.
LIVE_TTL_S = 60.0
UPCOMING_TTL_S = 15 * 60.0
FINISHED_TTL_S = 6 * 3600.0
PAST_DAY_TTL_S = 12 * 3600.0
NOT_FOUND_TTL_S = 15 * 60.0
# A season calendar (~150-400 matches, one request) is browsed, not watched live:
# refreshed every 15 minutes, every 2 minutes while one of its matches is live.
CALENDAR_TTL_S = 15 * 60.0
CALENDAR_LIVE_TTL_S = 120.0
# Competition emblems rarely change: the competition list is asked at most once a day.
EMBLEMS_TTL_S = 24 * 3600.0
EMBLEMS_RETRY_S = 15 * 60.0


# Display names: the provider's `shortName` (Latin) unless it is an unfamiliar
# abbreviation; these well-known forms replace it, keyed by the provider's stable team
# id (never by string matching, so namesakes cannot be confused). Reviewed for all ten
# default competitions on 2026-11-08. The official full name stays for search and details.
DISPLAY_NAMES: dict[int, str] = {
    # Premier League
    397: "Brighton", 65: "Manchester City", 66: "Manchester United", 351: "Nottingham Forest",
    67: "Newcastle United",
    # LaLiga
    78: "Atlético Madrid", 81: "Barcelona", 77: "Athletic Club", 559: "Sevilla", 558: "Celta Vigo",
    560: "Deportivo La Coruña", 5335: "Racing Santander",
    # Serie A
    98: "AC Milan", 108: "Inter", 7397: "Como", 454: "Venezia",
    # Bundesliga
    5: "Bayern Munich", 4: "Borussia Dortmund", 18: "Borussia Mönchengladbach", 3: "Bayer Leverkusen",
    19: "Eintracht Frankfurt", 12: "Werder Bremen", 7: "Hamburger SV", 1: "1. FC Köln", 15: "Mainz 05",
    29: "Paderborn", 2: "Hoffenheim",
    # Ligue 1
    523: "Lyon", 529: "Rennes", 532: "Angers", 546: "Lens", 548: "Monaco",
    # Champions League (others are in their leagues above or below)
    1887: "Shakhtar Donetsk", 1899: "AEK Athens", 7509: "Slovan Bratislava", 10233: "Sabah", 2016: "LASK",
    # Championship
    1081: "Preston North End", 356: "Sheffield United", 70: "Stoke City", 72: "Swansea City",
    68: "Norwich City", 715: "Cardiff City", 348: "Charlton Athletic", 76: "Wolverhampton",
    # Eredivisie
    682: "AZ Alkmaar", 666: "Twente", 1920: "Fortuna Sittard", 718: "Go Ahead Eagles", 1915: "NEC Nijmegen",
    684: "PEC Zwolle", 6806: "Sparta Rotterdam", 1909: "Cambuur", 680: "ADO Den Haag",
    # Primeira Liga
    5527: "Académico de Viseu", 1903: "Benfica", 5613: "Braga", 582: "Estoril", 9136: "Estrela da Amadora",
    5529: "Nacional", 5543: "Vitória de Guimarães",
    # Brasileirão
    1766: "Atlético Mineiro", 1768: "Athletico Paranaense", 4287: "Remo", 4286: "Red Bull Bragantino",
}
MAX_SHORT_NAME = 28


def short_name(team: int, full: str, provided: object) -> str | None:
    if team in DISPLAY_NAMES:
        return DISPLAY_NAMES[team]
    if isinstance(provided, str) and provided.strip() and len(provided.strip()) <= MAX_SHORT_NAME:
        return provided.strip()
    return None


def tournament_id(code: str) -> str:
    return f"{ID_PREFIX}:{code}"


def event_id(match_id: int) -> str:
    return f"{ID_PREFIX}:{match_id}"


def parse_event_id(value: str) -> int | None:
    prefix, _, raw = value.partition(":")
    # ASCII digits only: str.isdigit() also accepts "²", which int() rejects.
    if prefix != ID_PREFIX or not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
        return None
    # Canonical positive ids only: "fd:0" or "fd:0001" would cost a lookup for nothing
    # (or a second one for an id already known as "fd:1").
    if raw[0] == "0":
        return None
    return int(raw)


def tournaments(codes: Iterable[str]) -> list[Tournament]:
    return [
        Tournament(tournament_id(code), "football", KNOWN_COMPETITIONS[code][0], KNOWN_COMPETITIONS[code][1], PROVIDER)
        for code in codes
    ]


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _pair(node: object) -> tuple[int, int] | None:
    if not isinstance(node, dict):
        return None
    home, away = _int_or_none(node.get("home")), _int_or_none(node.get("away"))
    return (home, away) if home is not None and away is not None else None


def _iso_utc(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id(value: object) -> int | None:
    # bool is an int subclass: True must never become "fd:True".
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _team(node: object) -> Competitor | None:
    if not isinstance(node, dict) or _id(node.get("id")) is None:
        return None
    name = node.get("name") or node.get("shortName")
    if not isinstance(name, str) or not name.strip():
        return None
    number = node["id"]
    source = crest_source(node.get("crest"))
    return Competitor(f"{ID_PREFIX}:team:{number}", name.strip(), short_name(number, name.strip(), node.get("shortName")),
                      crest_source=source, crest_path=f"/api/crests/team/{number}" if source else None)


def normalize_match(raw: object, fetched_at: str, allowed_codes: frozenset[str]) -> FootballEvent | None:
    """One API match → FootballEvent, or None when it is unusable or out of scope."""
    if not isinstance(raw, dict) or _id(raw.get("id")) is None:
        return None
    competition = raw.get("competition")
    code = competition.get("code") if isinstance(competition, dict) else None
    if not isinstance(code, str) or code not in allowed_codes:
        return None
    starts_at = _iso_utc(raw.get("utcDate"))
    home, away = _team(raw.get("homeTeam")), _team(raw.get("awayTeam"))
    if starts_at is None or home is None or away is None:
        return None
    source_status = raw.get("status") if isinstance(raw.get("status"), str) else "UNKNOWN"
    status = _STATUS.get(source_status, "unknown")
    score_node = raw.get("score") if isinstance(raw.get("score"), dict) else {}
    score = _pair(score_node.get("fullTime")) if status in ("live", "finished", "suspended") else None
    half_time = _pair(score_node.get("halfTime")) if status in ("live", "finished", "suspended") else None
    minute = _int_or_none(raw.get("minute")) if status == "live" else None
    matchday = raw.get("matchday")
    return FootballEvent(
        id=event_id(raw["id"]),
        tournament_id=tournament_id(code),
        starts_at=starts_at,
        status=status,
        source_status=source_status,
        home=home,
        away=away,
        score=score,
        half_time=half_time,
        minute=minute,
        awarded=source_status == "AWARDED",
        matchday=matchday if _id(matchday) is not None and matchday <= 100 else None,
        provenance=Provenance(PROVIDER, fetched_at, _iso_utc(raw.get("lastUpdated"))),
    )


def _safe_normalize(raw: object, fetched_at: str, allowed: frozenset[str]) -> FootballEvent | None:
    # Any shape the parser did not foresee is a broken payload (bad_response upstream),
    # never a 500 that would bypass the cache's failure backoff and re-spend quota.
    try:
        return normalize_match(raw, fetched_at, allowed)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _out_of_scope(raw: object, allowed: frozenset[str]) -> bool:
    """A well-formed record of a competition we do not serve (filtered, not broken)."""
    if not isinstance(raw, dict) or _id(raw.get("id")) is None:
        return False
    competition = raw.get("competition")
    code = competition.get("code") if isinstance(competition, dict) else None
    return isinstance(code, str) and code not in allowed


def ttl_for(events: list[FootballEvent], day: date, today: date) -> float:
    if any(e.status == "live" for e in events):
        return LIVE_TTL_S
    if day < today and all(e.status in ("finished", "cancelled", "postponed") for e in events):
        return PAST_DAY_TTL_S
    if events and all(e.status == "finished" for e in events):
        return FINISHED_TTL_S
    return UPCOMING_TTL_S


_NOT_STARTED = frozenset({"SCHEDULED", "TIMED"})


def _undetermined(raw: object, allowed: frozenset[str]) -> bool:
    """A future fixture whose teams are not known yet (e.g. a knockout tie, even a final with
    both sides open): not broken, just not shown. Everything else must be valid — id, status,
    competition, kickoff, and each side that IS known — so a truncated record is still broken."""
    if not isinstance(raw, dict) or _id(raw.get("id")) is None:
        return False
    status, competition = raw.get("status"), raw.get("competition")
    code = competition.get("code") if isinstance(competition, dict) else None
    # isinstance first: an unhashable value (list/dict) must read as broken, never raise.
    if not isinstance(status, str) or status not in _NOT_STARTED or not isinstance(code, str) or code not in allowed:
        return False
    if _iso_utc(raw.get("utcDate")) is None:
        return False
    sides = [raw.get("homeTeam"), raw.get("awayTeam")]
    if not all(isinstance(t, dict) for t in sides):
        return False
    # An open side says so explicitly ({"id": null, "name": null}); {} or a non-string name is broken.
    tbd = [("id" in t and t["id"] is None and "name" in t and t["name"] is None) for t in sides]
    known_ok = all(tbd[i] or (_id(t.get("id")) is not None and isinstance(t.get("name"), str) and t["name"].strip())
                   for i, t in enumerate(sides))
    return any(tbd) and known_ok


def _day(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class Season:
    start: str | None
    end: str | None
    current_matchday: int | None

    def to_json(self) -> dict:
        return {"startDate": self.start, "endDate": self.end, "currentMatchday": self.current_matchday}


def season_of(raw_matches: list) -> Season:
    """Season facts as given; a value that is present but unreadable makes the answer broken."""
    nodes = [m["season"] for m in raw_matches if isinstance(m, dict) and "season" in m]  # null too: broken
    if any(not isinstance(n, dict) for n in nodes):
        raise UpstreamError("bad_response")
    if not nodes:
        return Season(None, None, None)
    if len(nodes) != sum(1 for m in raw_matches if isinstance(m, dict)):
        raise UpstreamError("bad_response")  # some matches carry a season and some do not
    # Every match's season must be readable and the same one: a mixed answer is broken.
    facts = [(n.get("id"), _season_facts(n)) for n in nodes]
    if any(f != facts[0] for f in facts[1:]):
        raise UpstreamError("bad_response")
    return facts[0][1]


def _season_facts(node: dict) -> Season:
    start, end, current = node.get("startDate"), node.get("endDate"), node.get("currentMatchday")
    if (start is not None and _day(start) is None) or (end is not None and _day(end) is None):
        raise UpstreamError("bad_response")
    if current is not None and (_id(current) is None or current > 100):
        raise UpstreamError("bad_response")
    return Season(_day(start), _day(end), current)


def event_ttl(event: FootballEvent) -> float:
    return {"live": LIVE_TTL_S, "finished": FINISHED_TTL_S}.get(event.status, UPCOMING_TTL_S)


class FootballDataAdapter:
    sport = "football"
    provider = PROVIDER

    def __init__(self, client: ProviderClient, competitions: Iterable[str],
                 clock: Callable[[], float]) -> None:
        codes = tuple(dict.fromkeys(competitions))
        unknown = [c for c in codes if c not in KNOWN_COMPETITIONS]
        if unknown or not codes:
            raise ValueError(f"unsupported football-data.org competitions: {unknown or 'none'}")
        self.client = client
        self.codes = codes
        self._allowed = frozenset(codes)
        self._clock = clock
        # Competition emblem URLs as the provider gives them (validated), learned from payloads.
        self.emblems: dict[str, str] = {}
        self._withdrawn: set[str] = set()  # codes the competition list gave no emblem for

    def _learn_emblem(self, node: object) -> None:
        competition = node.get("competition") if isinstance(node, dict) else None
        code = competition.get("code") if isinstance(competition, dict) else None
        if isinstance(code, str) and code in self._allowed and code not in self._withdrawn:
            source = crest_source(competition.get("emblem"))
            if source is not None:
                self.emblems[code] = source

    def tournaments(self) -> list[Tournament]:
        return tournaments(self.codes)

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def day(self, day: date) -> tuple[list[FootballEvent], float]:
        """All configured-competition matches whose kickoff is on this UTC day."""
        data, _ = await self.client.get_json("/matches", {
            "competitions": ",".join(self.codes),
            "dateFrom": day.isoformat(),
            "dateTo": (day + timedelta(days=1)).isoformat(),
        })
        matches = data.get("matches")
        if not isinstance(matches, list):
            raise UpstreamError("bad_response")
        fetched = self._now_iso()
        seen: set[str] = set()
        events: list[FootballEvent] = []
        parsed = malformed = 0
        for raw in matches:
            self._learn_emblem(raw)
            event = _safe_normalize(raw, fetched, self._allowed)
            if event is None:
                if _undetermined(raw, self._allowed):
                    continue
                if not _out_of_scope(raw, self._allowed):
                    malformed += 1
                continue
            parsed += 1
            # The range is inclusive on the provider side; keep exactly this UTC day.
            if event.id in seen or not event.starts_at.startswith(day.isoformat()):
                continue
            seen.add(event.id)
            events.append(event)
        if malformed:
            # An incomplete answer could silently hide a match: treat it as broken, so the
            # cache serves the last good data (stale) or an error — never a gap as "no match".
            raise UpstreamError("bad_response")
        today = datetime.fromtimestamp(self._clock(), UTC).date()
        return events, ttl_for(events, day, today)

    async def team_form(self, team: int) -> tuple[list[FormMatch], float]:
        data, _ = await self.client.get_json(f"/teams/{team}/matches", {"status": "FINISHED", "limit": str(FORM_MATCHES)})
        return normalize_form(data.get("matches"), team), FORM_TTL_S

    async def standings(self, code: str) -> tuple[dict[str, StandingRow], float]:
        if code not in self._allowed:
            raise UpstreamError("not_found")
        data, _ = await self.client.get_json(f"/competitions/{code}/standings")
        return normalize_standings(data), STANDINGS_TTL_S

    async def competition_emblems(self) -> tuple[dict[str, str], float]:
        """Emblem URLs of the served competitions, from the provider's competition list."""
        data, _ = await self.client.get_json("/competitions")
        items = data.get("competitions")
        if not isinstance(items, list):
            raise UpstreamError("bad_response")
        found: dict[str, str] = {}
        for item in items:
            code = item.get("code") if isinstance(item, dict) else None
            if not isinstance(code, str):
                raise UpstreamError("bad_response")  # a truncated entry could hide a served competition
            if code in self._allowed:
                source = crest_source(item.get("emblem"))
                if source is not None:
                    found[code] = source
        # Applied only after the whole list validated. The list is the authority: a served
        # competition it omits (or gives no usable emblem) has none, and match payloads may not
        # bring it back until the next list says otherwise.
        self._withdrawn = set(self._allowed) - set(found)
        for code in self._withdrawn:
            self.emblems.pop(code, None)
        self.emblems.update(found)
        return found, EMBLEMS_TTL_S

    async def calendar(self, code: str) -> tuple[tuple[list[FootballEvent], Season], float]:
        """The whole current season of one competition: one request, shared by every user."""
        if code not in self._allowed:
            raise UpstreamError("not_found")
        data, _ = await self.client.get_json(f"/competitions/{code}/matches")
        matches = data.get("matches")
        top = data.get("competition")
        # The answer must be about the competition asked for, even with no matches at all.
        if not isinstance(matches, list) or not isinstance(top, dict) or top.get("code") != code:
            raise UpstreamError("bad_response")
        # The provider always states how many matches it sent: without that, a cut answer could not
        # be told from a complete one.
        result_set = data.get("resultSet")
        count = result_set.get("count") if isinstance(result_set, dict) else None
        if not isinstance(count, int) or isinstance(count, bool) or count != len(matches):
            raise UpstreamError("bad_response")
        self._learn_emblem(data)
        fetched = self._now_iso()
        wanted = tournament_id(code)
        events: dict[str, FootballEvent] = {}
        open_ids: set[object] = set()
        for raw in matches:
            if _undetermined(raw, frozenset({code})):  # a TBD fixture of another competition is broken
                if raw["id"] in open_ids or event_id(raw["id"]) in events:
                    raise UpstreamError("bad_response")
                open_ids.add(raw["id"])
                continue
            event = _safe_normalize(raw, fetched, self._allowed)
            # A match of another competition in this answer is as broken as an unreadable one:
            # a calendar with a silent gap would read as "no match that day".
            # Two records under one id: one of them would vanish silently.
            if event is None or event.tournament_id != wanted or event.id in events or raw["id"] in open_ids:
                raise UpstreamError("bad_response")
            events[event.id] = event
        ordered = sorted(events.values(), key=lambda e: (e.starts_at, e.id))
        ttl = CALENDAR_LIVE_TTL_S if any(e.status == "live" for e in ordered) else CALENDAR_TTL_S
        return (ordered, season_of(matches)), ttl

    async def match(self, match_id: int) -> tuple[FootballEvent | None, float]:
        try:
            data, _ = await self.client.get_json(f"/matches/{match_id}")
        except UpstreamError as exc:
            if exc.code == "not_found":
                return None, NOT_FOUND_TTL_S
            raise
        # First of all: an answer about another match is broken, whatever else it holds
        # (never a cached "not found" or another match under this id).
        if data.get("id") != match_id:
            raise UpstreamError("bad_response")
        self._learn_emblem(data)
        event = _safe_normalize(data, self._now_iso(), self._allowed)
        if event is None:
            if _out_of_scope(data, self._allowed):
                return None, NOT_FOUND_TTL_S  # a real match of a competition we do not serve
            raise UpstreamError("bad_response")  # broken payload: not a cached "not found"
        return event, event_ttl(event)


# --- verified context for the AI analysis (M3.2) ---------------------------------
# Form and standings change at most after each round: cached for hours, shared by
# every match of those teams, so an analysis rarely costs more than one request.
FORM_TTL_S = 6 * 3600.0
STANDINGS_TTL_S = 6 * 3600.0
FORM_MATCHES = 10


@dataclass(frozen=True)
class FormMatch:
    """One finished match of a team, from that team's point of view."""

    date: str  # ISO-8601 UTC kickoff
    opponent: str
    venue: str  # "home" | "away"
    goals_for: int
    goals_against: int
    competition: str | None

    @property
    def result(self) -> str:
        return "W" if self.goals_for > self.goals_against else "L" if self.goals_for < self.goals_against else "D"


@dataclass(frozen=True)
class StandingRow:
    position: int
    team_id: str
    played: int
    won: int
    draw: int
    lost: int
    points: int
    goals_for: int
    goals_against: int


def team_number(team_id: str) -> int | None:
    prefix, _, raw = team_id.rpartition(":")
    if prefix != f"{ID_PREFIX}:team" or not (raw.isascii() and raw.isdigit()) or raw[0] == "0" or len(raw) > 12:
        return None
    return int(raw)


def normalize_form(raw_matches: object, team: int) -> list[FormMatch]:
    """Finished matches of `team`, newest first; anything incomplete is left out."""
    if not isinstance(raw_matches, list):
        raise UpstreamError("bad_response")
    form: list[FormMatch] = []
    for raw in raw_matches:
        if not isinstance(raw, dict) or raw.get("status") != "FINISHED":
            continue
        home, away = raw.get("homeTeam"), raw.get("awayTeam")
        if not isinstance(home, dict) or not isinstance(away, dict):
            continue
        score = _pair((raw.get("score") or {}).get("fullTime") if isinstance(raw.get("score"), dict) else None)
        kickoff = _iso_utc(raw.get("utcDate"))
        if score is None or kickoff is None:
            continue
        if _id(home.get("id")) == team:
            them, venue, goals = away, "home", score
        elif _id(away.get("id")) == team:
            them, venue, goals = home, "away", (score[1], score[0])
        else:
            continue
        name = them.get("name") or them.get("shortName")
        if not isinstance(name, str) or not name.strip():
            continue
        competition = raw.get("competition")
        code = competition.get("code") if isinstance(competition, dict) else None
        form.append(FormMatch(kickoff, name.strip(), venue, goals[0], goals[1], code if isinstance(code, str) else None))
    form.sort(key=lambda m: m.date, reverse=True)
    return form[:FORM_MATCHES]


def normalize_standings(data: object) -> dict[str, StandingRow]:
    """The overall ("TOTAL") table, keyed by our team id."""
    standings = data.get("standings") if isinstance(data, dict) else None
    if not isinstance(standings, list):
        raise UpstreamError("bad_response")
    total = next((s for s in standings if isinstance(s, dict) and s.get("type") == "TOTAL"), None)
    table = total.get("table") if isinstance(total, dict) else None
    if not isinstance(table, list):
        return {}
    rows: dict[str, StandingRow] = {}
    for raw in table:
        if not isinstance(raw, dict) or not isinstance(raw.get("team"), dict):
            continue
        team = _id(raw["team"].get("id"))
        values = [_int_or_none(raw.get(k)) for k in
                  ("position", "playedGames", "won", "draw", "lost", "points", "goalsFor", "goalsAgainst")]
        if team is None or any(v is None for v in values):
            continue
        tid = f"{ID_PREFIX}:team:{team}"
        rows[tid] = StandingRow(values[0], tid, *values[1:])  # type: ignore[arg-type]
    return rows
