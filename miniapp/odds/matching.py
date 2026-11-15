"""Strict matching of a feed line to a football-data match.

A line gets a match's odds only when ALL of these hold:
1. its (country, league) pair maps to that match's competition (leagues.json, exact
   after normalization — never a substring); in catalogue mode a line whose pair is not
   in the table may stand for a match of ANY competition (the names and the time decide),
   but a line whose pair maps to ANOTHER competition never does;
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
    keys = {k for k in (team_key(n) for n in names) if k}
    for key in list(keys):  # national teams: "Czechia" is the feed's "Czech Republic" (both ways)
        for country, others in COUNTRY_ALIASES.items():
            if key == country or key in others:
                keys |= {country, *others}
    return frozenset(keys)


# Areas of football-data competitions that are not countries: their lines carry the competition's name in the feed's
# country field ("UEFA Nations League"), never a country.
INTERNATIONAL_AREAS = frozenset({"africa", "asia", "europe", "south america", "world", "north central america", "oceania"})
# The feed's spelling of a few areas that football-data spells differently (normalized both sides).
COUNTRY_ALIASES: dict[str, frozenset[str]] = {
    "china pr": frozenset({"china"}), "czech republic": frozenset({"czechia"}),
    "united states": frozenset({"usa", "united states of america"}), "turkey": frozenset({"turkiye"}),
}


def _country_keys(area: str) -> frozenset[str]:
    key = normalize(area)
    return frozenset({key}) | COUNTRY_ALIASES.get(key, frozenset())


# An international competition's lines carry its name in the feed's country field: one of these words must be there
# (a country spelt in a way the alias table does not know is then never taken for an international competition).
_INTERNATIONAL_WORDS = re.compile(r"\b(uefa|fifa|conmebol|concacaf|caf|afc|nations|world|europe|european|euro|africa|"
                                  r"african|asia|asian|america|americas|copa|libertadores|sudamericana|olympic|olympics|"
                                  r"international|champions|club|qualification|qualifying|friendlies)\b")
# Cups and one-off finals of a country: their lines must say "cup" (in some language), league lines must not.
DOMESTIC_CUPS = frozenset({"CAR", "SCA", "SDB", "CDB", "COM", "FAC", "FLC", "CDF", "TDC", "DFB", "GSC", "CIT", "ISC",
                           "JCS", "KNV", "SCO", "TDL", "TDP", "CDR", "SDE", "SC"})
_CUP_WORDS = re.compile(r"\b(cup|pokal|copa|coppa|coupe|taca|beker|supercup|supercopa|supercoppa|supercoupe|supertaca|"
                        r"shield|trophee|trophy|schaal|kubok)\b")


def area_fits(line_country: str, area: str | None, countries: frozenset[str]) -> bool:
    """A line outside leagues.json may stand for a match of this competition only if its country is the
    competition's country, or — for an international competition — names an international competition (never a
    country). `countries`: every country area of the catalogue (normalized, with aliases). Unknown: no."""
    if not area:
        return False
    key = normalize(area)
    feed = normalize(line_country)
    if key in INTERNATIONAL_AREAS:
        return bool(feed) and feed not in countries and bool(_INTERNATIONAL_WORDS.search(feed))
    return feed in _country_keys(area)


def kind_fits(line_league: str, competition: str, area: str | None) -> bool:
    """Within a country, a cup line prices only that country's cup and a league line only its league."""
    if not area or normalize(area) in INTERNATIONAL_AREAS:
        return True
    return bool(_CUP_WORDS.search(normalize(line_league))) == (competition in DOMESTIC_CUPS)


def all_country_keys(areas: Iterable[str]) -> frozenset[str]:
    keys: set[str] = set()
    for area in areas:
        if area and normalize(area) not in INTERNATIONAL_AREAS:
            keys |= _country_keys(area)
    return frozenset(keys)


# Women's, youth and reserve football: never the senior match of the same clubs (catalogue mode). Leagues are checked
# for words, team names also for the suffixes clubs use ("Arsenal W", "Bayern II", "Barcelona B", "Chelsea U21").
# Also the local words the feed keeps in some league and team names (after normalize(): accents dropped).
_LOCAL_NON_SENIOR = (r"frauen|damen|dames|femenina|femenino|femminile|feminine|feminin|feminino|feminina|mulheres|"
                     r"kadin|kadinlar|vrouwen|kobiet|kvinner|kvinnor|naiset|zhenshchiny|zhenskaya|jugend|juvenil|"
                     r"juveniles|primavera|juniores|juniorinnen|espoirs|sub ?1[5-9]|sub ?2[0-3]|"
                     r"zhensk\w*|molodezh\w*|yunior\w*|zhenshchin\w*")
_NON_SENIOR_LEAGUE = re.compile(r"\b(" + _LOCAL_NON_SENIOR + r"|women|womens|ladies|female|youth|junior|juniors|reserve|reserves|amateur|"
                                r"u ?1[3-9]|u ?2[0-3]|olympic)\b")
_NON_SENIOR_TEAM = re.compile(r"\b(" + _LOCAL_NON_SENIOR + r"|women|womens|ladies|u ?1[3-9]|u ?2[0-3]|reserves?|youth|academy)\b"
                              r"|\b(w|ii|iii|b)$")  # a suffix only: "B. Dortmund" stays, "Barcelona B" does not


def senior(league: str, home: str, away: str, country: str = "") -> bool:
    """Not women's, youth or reserve football. International competitions name themselves in the feed's country
    field ("UEFA Women's Euro"), so it is checked like the league."""
    return (not _NON_SENIOR_LEAGUE.search(normalize(league)) and not _NON_SENIOR_LEAGUE.search(normalize(country))
            and not any(_NON_SENIOR_TEAM.search(normalize(n)) for n in (home, away)))


@dataclass(frozen=True)
class Candidate:
    """What the matcher needs of a stored line."""
    line_id: int
    competition: str | None  # None: a pair outside leagues.json (catalogue mode only)
    kickoff: float
    home: str
    away: str
    country: str = ""  # the feed's country (or international competition) of a line outside leagues.json
    league: str = ""  # the feed's league (or stage) of such a line


def match(candidates: Iterable[Candidate], *, competition: str, kickoff: float, home: Competitor,
          away: Competitor, table: Leagues | None = None, area: str | None = None,
          countries: frozenset[str] = frozenset()) -> tuple[Candidate | None, str]:
    """(the one line for this match, or None; a reason code for the log). Candidates without a competition
    (catalogue mode) take part for every competition; two lines naming this match (a duplicate in the feed)
    are ambiguous, never a choice."""
    table = table or leagues()
    home_names, away_names = team_names(home, table), team_names(away, table)
    near = [c for c in candidates
            if abs(c.kickoff - kickoff) <= KICKOFF_TOLERANCE_S
            and (c.competition == competition
                 or (c.competition is None and area_fits(c.country, area, countries)
                     and kind_fits(c.league, competition, area)))]
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
