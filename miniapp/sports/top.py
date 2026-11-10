"""The day's top matches: a deterministic ranking from source facts only.

Score of a match = competition weight + stage weight + table weight (+ a leaders'
meeting bonus) + a status weight: upcoming, plus a "kicks off soon" bonus within a few
hours, or live (= upcoming + soon, so a match keeps its place when it kicks off);
finished gets none. The weights live in top_weights.json. The ranking never uses the
model's or Claude's confidence, and never a live score or minute: an update of a live
match cannot move it in the list. Ties: earlier kickoff, then id.

The table weight uses only standings already in the shared cache (a table someone
opened, or an AI analysis loaded): ranking never makes a provider request of its own.
Without a known table that part is 0 for both teams of that competition.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .football_data import StandingRow
from .models import FootballEvent

RANKED_STATUSES = frozenset({"upcoming", "live", "finished"})


@dataclass(frozen=True)
class TopWeights:
    max_matches: int
    max_per_competition: int  # while other competitions still have matches to offer
    competition: Mapping[str, float]
    other_competition: float
    other_stage_cap: float  # the most a knockout stage adds for a competition without its own weight
    stage: Mapping[str, float]
    table: float
    leaders: float
    leaders_top: int
    min_table_teams: int
    standings_max_age_s: float
    upcoming: float
    live: float  # = upcoming + kickoff_soon keeps a match's place when it kicks off
    kickoff_soon: float  # an upcoming match starting within kickoff_soon_s of now
    kickoff_soon_s: float


def load_weights(path: Path) -> TopWeights:
    raw = json.loads(path.read_text())

    def number(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ValueError("top-match weights must be non-negative numbers")
        return float(value)

    weights = TopWeights(
        max_matches=int(number(raw["maxMatches"])),
        max_per_competition=int(number(raw["maxPerCompetition"])),
        competition={str(k): number(v) for k, v in raw["competition"].items()},
        other_competition=number(raw["otherCompetition"]),
        other_stage_cap=number(raw.get("otherStageCap", 6)),
        stage={str(k): number(v) for k, v in raw["stage"].items()},
        table=number(raw["table"]),
        leaders=number(raw["leaders"]),
        leaders_top=int(number(raw["leadersTop"])),
        min_table_teams=int(number(raw["minTableTeams"])),
        standings_max_age_s=number(raw["standingsMaxAgeHours"]) * 3600,
        upcoming=number(raw["upcoming"]),
        live=number(raw["live"]),
        kickoff_soon=number(raw["kickoffSoon"]),
        kickoff_soon_s=number(raw["kickoffSoonHours"]) * 3600,
    )
    if not 1 <= weights.max_matches <= 5 or weights.max_per_competition < 1 or weights.leaders_top < 1 or weights.min_table_teams < 2:
        raise ValueError("top-match limits out of range")
    if weights.live != weights.upcoming + weights.kickoff_soon:
        raise ValueError("live must equal upcoming + kickoffSoon (a match keeps its place at kickoff)")
    return weights


WEIGHTS = load_weights(Path(__file__).with_name("top_weights.json"))


@dataclass(frozen=True)
class Table:
    """One competition's single overall table: team id -> position, and its size."""

    positions: Mapping[str, int]
    teams: int


@dataclass(frozen=True)
class Ranked:
    event: FootballEvent
    score: float
    leaders: bool  # both teams in the top `leaders_top` of the table


def table_of(rows: Mapping[str, StandingRow] | None) -> Table | None:
    if not rows:
        return None
    return Table({team: row.position for team, row in rows.items()}, len(rows))


def _closeness(position: int, teams: int) -> float:
    """1 for the leader, 0 for the last place."""
    return max(0.0, min(1.0, 1 - (position - 1) / (teams - 1)))


def score(event: FootballEvent, code: str, table: Table | None, w: TopWeights = WEIGHTS,
          now: float | None = None) -> Ranked:
    total = w.competition.get(code, w.other_competition)
    bonus = w.stage.get(event.stage or "", 0.0)
    leaders = False
    if table is not None and table.teams >= w.min_table_teams:
        home, away = table.positions.get(event.home.id), table.positions.get(event.away.id)
        if home is not None and away is not None:
            bonus += w.table * (_closeness(home, table.teams) + _closeness(away, table.teams)) / 2
            leaders = home <= w.leaders_top and away <= w.leaders_top
            if leaders:
                bonus += w.leaders
    # Stage and table lift a match within its own level: for a competition without its own weight they add
    # at most other_stage_cap, so a minor final or a lower-tier top-of-table game cannot outrank the main leagues.
    total += bonus if code in w.competition else min(bonus, w.other_stage_cap)
    if event.status == "live":
        total += w.live
    elif event.status == "upcoming":
        total += w.upcoming
        kickoff = datetime.fromisoformat(event.starts_at.replace("Z", "+00:00")).timestamp()
        # An overdue "upcoming" (the source has not marked it live yet) keeps the bonus:
        # its place does not change between the scheduled kickoff and the status update.
        if now is not None and kickoff - now <= w.kickoff_soon_s:
            total += w.kickoff_soon
    return Ranked(event, round(total, 6), leaders)


def rank(events: Iterable[FootballEvent], code_of: Callable[[str], str],
         tables: Mapping[str, Table | None], w: TopWeights = WEIGHTS, now: float | None = None) -> list[Ranked]:
    """Up to `max_matches` of the given events (one day, chosen by the caller), best first.
    Postponed, suspended, cancelled or unknown matches are never top matches. At most
    `max_per_competition` from one competition while others have matches left; only then
    are the remaining places filled in plain score order."""
    ranked = [score(e, code_of(e.tournament_id), tables.get(e.tournament_id), w, now)
              for e in events if e.status in RANKED_STATUSES]
    ranked.sort(key=lambda r: (-r.score, r.event.starts_at, r.event.id))
    chosen: list[Ranked] = []
    per: dict[str, int] = {}
    for r in ranked:
        if len(chosen) < w.max_matches and per.get(r.event.tournament_id, 0) < w.max_per_competition:
            chosen.append(r)
            per[r.event.tournament_id] = per.get(r.event.tournament_id, 0) + 1
    for r in ranked:
        if len(chosen) >= w.max_matches:
            break
        if r not in chosen:
            chosen.append(r)
    chosen.sort(key=lambda r: (-r.score, r.event.starts_at, r.event.id))
    return chosen
