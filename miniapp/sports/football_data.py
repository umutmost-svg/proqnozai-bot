"""football-data.org v4 adapter (football only).

Docs: https://docs.football-data.org/general/v4/index.html — auth header
``X-Auth-Token``. Only fields the API actually returns are used; match
statistics, lineups, injuries, odds and probabilities are not taken from here
and stay ``None`` / "unavailable".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta

from .client import ProviderClient, UpstreamError
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
    short = node.get("shortName") if isinstance(node.get("shortName"), str) else None
    return Competitor(f"{ID_PREFIX}:team:{node['id']}", name.strip(), short)


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
            event = _safe_normalize(raw, fetched, self._allowed)
            if event is None:
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
        event = _safe_normalize(data, self._now_iso(), self._allowed)
        if event is None:
            if _out_of_scope(data, self._allowed):
                return None, NOT_FOUND_TTL_S  # a real match of a competition we do not serve
            raise UpstreamError("bad_response")  # broken payload: not a cached "not found"
        return event, event_ttl(event)
