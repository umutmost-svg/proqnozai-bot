"""Strict matching of a feed line to a football-data match.

A line gets a match's odds only when ALL of these hold:
1. its (country, league) pair maps to that match's competition (leagues.json, exact
   after normalization — never a substring);
2. its kickoff is within KICKOFF_TOLERANCE_S of the match's;
3. its home team is the match's home team and its away team the match's away team
   (same order: a reversed pair would swap the 1 and 2 prices);
4. exactly one line of that competition and time passes 3, and no line of that
   competition and time names the two teams the other way round.

Team names are equal after normalization and after dropping club-form tokens ("FC",
"AC", "CF"…), against football-data's name, its short name, the search aliases and the
odds aliases of that team id. There is no fuzzy score: a name that is not known exactly
gives no odds (and a log line, so the alias table can be extended), never a guess.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from miniapp.sports import football_data
from miniapp.sports.models import Competitor
from miniapp.sports.search import ALIASES as SEARCH_ALIASES
from miniapp.sports.search import normalize

KICKOFF_TOLERANCE_S = 15 * 60.0
LEAGUES_PATH = Path(__file__).with_name("leagues.json")
# Legal-form and filler tokens that clubs carry or drop between sources.
NOISE = frozenset({"fc", "cf", "afc", "sc", "ac", "as", "ssc", "ss", "sv", "cd", "ud", "sd", "rc", "rcd",
                   "fk", "sk", "bk", "club", "de", "calcio", "1"})


# The feed appends the season to some league names ("Premier League. Season 26/27"); the
# season is not part of the competition. Same rule as mostbet_feed.league_key in the bot.
_SEASON = re.compile(r" season \d{2,4}(?: \d{2,4})?$")


def league_key(text: str) -> str:
    return _SEASON.sub("", normalize(text))


def team_key(name: str) -> str:
    return " ".join(t for t in normalize(name).split() if t not in NOISE)


@dataclass(frozen=True)
class Leagues:
    pairs: dict[tuple[str, str], str]  # (country, league) -> code; no wildcards
    aliases: dict[int, tuple[str, ...]]
    codes: frozenset[str]

    def code(self, country: str, league: str) -> str | None:
        return self.pairs.get((normalize(country), league_key(league)))


def load_leagues(path: Path = LEAGUES_PATH) -> Leagues:
    raw = json.loads(path.read_text())
    pairs: dict[tuple[str, str], str] = {}
    for entry in raw["competitions"]:
        code = entry["code"]
        if "anyLeague" in entry:
            raise ValueError("wildcard stages are not allowed: list every accepted stage")
        for country in entry["country"]:
            for league in entry["league"]:
                key = (normalize(country), league_key(league))
                if pairs.get(key, code) != code:
                    raise ValueError(f"{key} maps to two competitions")
                pairs[key] = code
    aliases = {int(k): tuple(v) for k, v in raw.get("aliases", {}).items()}
    return Leagues(pairs, aliases, frozenset(e["code"] for e in raw["competitions"]))


@lru_cache(maxsize=1)
def leagues() -> Leagues:
    return load_leagues()


def team_names(team: Competitor, table: Leagues) -> frozenset[str]:
    number = football_data.team_number(team.id)
    names: list[str] = [team.name, team.short_name or ""]
    if number is not None:
        names += [*SEARCH_ALIASES.get(number, ()), *table.aliases.get(number, ())]
    return frozenset(k for k in (team_key(n) for n in names) if k)


@dataclass(frozen=True)
class Candidate:
    """What the matcher needs of a stored line."""
    line_id: int
    competition: str
    kickoff: float
    home: str
    away: str


def match(candidates: Iterable[Candidate], *, competition: str, kickoff: float, home: Competitor,
          away: Competitor, table: Leagues | None = None) -> tuple[Candidate | None, str]:
    """(the one line for this match, or None; a reason code for the log)."""
    table = table or leagues()
    home_names, away_names = team_names(home, table), team_names(away, table)
    near = [c for c in candidates
            if c.competition == competition and abs(c.kickoff - kickoff) <= KICKOFF_TOLERANCE_S]
    if not near:
        return None, "no_line"
    straight: list[Candidate] = []
    reversed_: list[Candidate] = []
    for c in near:
        h, a = team_key(c.home), team_key(c.away)
        if h in home_names and a in away_names:
            straight.append(c)
        elif h in away_names and a in home_names:
            reversed_.append(c)
    if reversed_:
        return None, "reversed"
    if not straight:
        return None, "unknown_names"
    if len(straight) > 1:
        return None, "ambiguous"
    return straight[0], "matched"


def unmatched_names(candidates: Sequence[Candidate]) -> list[str]:
    """For the log: the feed's names near a match that found no line (no user data)."""
    return sorted({f"{c.home} - {c.away}" for c in candidates})[:5]
