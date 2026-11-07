"""Normalized sports data returned by the Mini App API.

Every sport keeps its own scoring shape (football goals, tennis sets, basketball
quarters, MMA rounds); nothing is filled in when a provider does not supply it:
missing values are ``None`` and the availability is stated explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Sport = Literal["football", "tennis", "basketball", "mma"]
SPORTS: tuple[Sport, ...] = ("football", "tennis", "basketball", "mma")

# Real event states. Postponed / suspended / cancelled are never folded into
# "upcoming"; anything a provider reports that we do not recognise is "unknown".
EventStatus = Literal["upcoming", "live", "finished", "postponed", "suspended", "cancelled", "unknown"]

Availability = Literal["available", "unavailable"]
CoverageStatus = Literal["connected", "not_connected", "unavailable"]


@dataclass(frozen=True)
class Provenance:
    """Where an event came from and how fresh it is."""

    provider: str
    fetched_at: str  # ISO-8601 UTC, when we received it from the provider
    source_updated_at: str | None  # the provider's own "last updated", if given
    stale: bool = False  # served from cache after a failed refresh

    def to_json(self) -> dict:
        return {
            "provider": self.provider,
            "fetchedAt": self.fetched_at,
            "sourceUpdatedAt": self.source_updated_at,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class Competitor:
    id: str
    name: str
    short_name: str | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "shortName": self.short_name}


@dataclass(frozen=True)
class Tournament:
    id: str
    sport: Sport
    name: str
    area: str | None = None
    provider: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "sport": self.sport, "name": self.name, "area": self.area, "provider": self.provider}


@dataclass(frozen=True)
class FootballEvent:
    id: str
    tournament_id: str
    starts_at: str
    status: EventStatus
    home: Competitor
    away: Competitor
    provenance: Provenance
    # Provider's raw status code, kept for diagnostics and honest edge cases.
    source_status: str
    score: tuple[int, int] | None = None
    half_time: tuple[int, int] | None = None
    minute: int | None = None
    awarded: bool = False
    stats: dict | None = None
    stats_availability: Availability = "unavailable"
    sport: Sport = field(default="football", init=False)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "sport": self.sport,
            "tournamentId": self.tournament_id,
            "startsAt": self.starts_at,
            "status": self.status,
            "sourceStatus": self.source_status,
            "home": self.home.to_json(),
            "away": self.away.to_json(),
            "score": list(self.score) if self.score is not None else None,
            "halfTime": list(self.half_time) if self.half_time is not None else None,
            "minute": self.minute,
            "awarded": self.awarded,
            "stats": self.stats,
            "statsAvailability": self.stats_availability,
            "source": self.provenance.to_json(),
        }


def _pairs(pairs: tuple[tuple[int, int], ...]) -> list[list[int]]:
    return [list(p) for p in pairs]


@dataclass(frozen=True)
class TennisEvent:
    """Sets and games, never goals. ``sets`` holds games per set, the last one in progress."""

    id: str
    tournament_id: str
    starts_at: str
    status: EventStatus
    home: Competitor
    away: Competitor
    provenance: Provenance
    source_status: str
    format: Literal["singles", "doubles"] | None = None
    surface: Literal["hard", "clay", "grass"] | None = None
    best_of: Literal[3, 5] | None = None
    sets: tuple[tuple[int, int], ...] = ()
    current_game: str | None = None  # points in the game in progress, e.g. "40:30"
    stats: dict | None = None
    stats_availability: Availability = "unavailable"
    sport: Sport = field(default="tennis", init=False)

    def to_json(self) -> dict:
        return {
            "id": self.id, "sport": self.sport, "tournamentId": self.tournament_id,
            "startsAt": self.starts_at, "status": self.status, "sourceStatus": self.source_status,
            "home": self.home.to_json(), "away": self.away.to_json(),
            "format": self.format, "surface": self.surface, "bestOf": self.best_of,
            "sets": _pairs(self.sets), "currentGame": self.current_game,
            "stats": self.stats, "statsAvailability": self.stats_availability,
            "source": self.provenance.to_json(),
        }


@dataclass(frozen=True)
class BasketballEvent:
    """Quarters plus overtimes, never sets."""

    id: str
    tournament_id: str
    starts_at: str
    status: EventStatus
    home: Competitor
    away: Competitor
    provenance: Provenance
    source_status: str
    quarters: tuple[tuple[int, int], ...] = ()
    overtimes: tuple[tuple[int, int], ...] = ()
    clock: str | None = None
    stats: dict | None = None
    stats_availability: Availability = "unavailable"
    sport: Sport = field(default="basketball", init=False)

    def to_json(self) -> dict:
        return {
            "id": self.id, "sport": self.sport, "tournamentId": self.tournament_id,
            "startsAt": self.starts_at, "status": self.status, "sourceStatus": self.source_status,
            "home": self.home.to_json(), "away": self.away.to_json(),
            "quarters": _pairs(self.quarters), "overtimes": _pairs(self.overtimes), "clock": self.clock,
            "stats": self.stats, "statsAvailability": self.stats_availability,
            "source": self.provenance.to_json(),
        }


MmaMethod = Literal["ko_tko", "submission", "decision", "draw", "no_contest", "dq"]


@dataclass(frozen=True)
class MmaResult:
    winner: Literal["home", "away"] | None
    method: MmaMethod
    round: int
    time: str  # "m:ss" into the round


@dataclass(frozen=True)
class MmaEvent:
    """A fight: rounds and a method of finish, never goals."""

    id: str
    tournament_id: str
    starts_at: str
    status: EventStatus
    home: Competitor  # red corner
    away: Competitor  # blue corner
    provenance: Provenance
    source_status: str
    weight_class: str | None = None
    scheduled_rounds: Literal[3, 5] | None = None
    current_round: int | None = None
    result: MmaResult | None = None
    stats: dict | None = None
    stats_availability: Availability = "unavailable"
    sport: Sport = field(default="mma", init=False)

    def to_json(self) -> dict:
        r = self.result
        return {
            "id": self.id, "sport": self.sport, "tournamentId": self.tournament_id,
            "startsAt": self.starts_at, "status": self.status, "sourceStatus": self.source_status,
            "home": self.home.to_json(), "away": self.away.to_json(),
            "weightClass": self.weight_class, "scheduledRounds": self.scheduled_rounds,
            "currentRound": self.current_round,
            "result": None if r is None else {"winner": r.winner, "method": r.method, "round": r.round, "time": r.time},
            "stats": self.stats, "statsAvailability": self.stats_availability,
            "source": self.provenance.to_json(),
        }


SportEvent = FootballEvent | TennisEvent | BasketballEvent | MmaEvent
