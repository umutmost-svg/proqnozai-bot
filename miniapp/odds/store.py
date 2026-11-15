"""Latest odds per feed line, in memory, and their lookup for one football-data match.

Lines are kept from the push until a little after their kickoff. A newer push of the
same line replaces it only when its fetch time is newer (pushes may arrive out of order).
A lookup serves odds only for a match that has not started, from a line matched strictly
(matching.py) whose prices are at most MAX_AGE_S old. Nothing here makes any request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from miniapp.sports.models import FootballEvent
from miniapp.sports.service import parse_instant

from .feed import TRANSIT_ALLOWANCE_S, CatalogLine, FeedLine
from miniapp.sports.football_data import KNOWN_COMPETITIONS

from .matching import Candidate, Leagues, all_country_keys, leagues, match, senior, unmatched_names

# Women's competitions of the catalogue never get a line outside leagues.json (the feed often names women's teams
# like the clubs' senior teams).
NO_CATALOGUE_ODDS = frozenset({"WSL", "ECF", "WCF"})
COUNTRIES = all_country_keys(area for _, area in KNOWN_COMPETITIONS.values())


def area_of(code: str) -> str | None:
    known = KNOWN_COMPETITIONS.get(code)
    return known[1] if known else None

logger = logging.getLogger("miniapp.odds")

# Prices older than this are never served (the bot refreshes lines every 5 minutes near
# kickoff and every 10 minutes further out).
MAX_AGE_S = 15 * 60.0
# Catalogue mode: a match further than NEAR_S from kickoff is priced less often (FAR_REFRESH_S), so its prices may
# be served up to FAR_MAX_AGE_S old; near kickoff the 15-minute limit stays.
NEAR_S = 6 * 3600.0
NEAR_REFRESH_S = 10 * 60.0
FAR_REFRESH_S = 45 * 60.0
FAR_MAX_AGE_S = 60 * 60.0
# Matches further than WEEK_TIER_S (the rest of the week): prices move slowly that far out, so they refresh every
# WEEK_REFRESH_S and may be served up to WEEK_MAX_AGE_S old — this keeps the whole week inside the request budget,
# near matches first. A client keeps a price at most MAX_VALID_FOR_S (it asks again on the next page load).
WEEK_TIER_S = 48 * 3600.0
WEEK_REFRESH_S = 3 * 3600.0
WEEK_MAX_AGE_S = 4 * 3600.0
MAX_VALID_FOR_S = 3600.0
# The catalogue governs serving while the last complete list is at most this old (the source reads it every 15 min).
CATALOG_TTL_S = 40 * 60.0
# How far ahead the catalogue prices matches (the whole week the Mini App lists; far ones refresh every FAR_REFRESH_S).
CATALOG_WINDOW_S = 7 * 86400.0
MAX_CATALOG = 6000
SAMPLES_PER_REASON = 30


def max_age(kickoff: float, now: float, catalog: bool) -> float:
    """How old a price may be when served: 15 min near kickoff, an hour further out (catalogue mode only)."""
    if not catalog or kickoff - now <= NEAR_S:
        return MAX_AGE_S
    return WEEK_MAX_AGE_S if kickoff - now > WEEK_TIER_S else FAR_MAX_AGE_S


def refresh_every(kickoff: float, now: float) -> float:
    """How often the own source re-prices a line (catalogue mode): near / far / the rest of the week."""
    ahead = kickoff - now
    return NEAR_REFRESH_S if ahead <= NEAR_S else WEEK_REFRESH_S if ahead > WEEK_TIER_S else FAR_REFRESH_S
# No prices in the last minutes before the (earlier) kickoff: the bot learns that a match has
# gone live from its match list, refreshed every 15 minutes, so a match starting a little
# early could otherwise still show pre-match prices.
CUTOFF_BEFORE_KICKOFF_S = 15 * 60.0
KEEP_AFTER_KICKOFF_S = 3 * 3600.0
MAX_LINES = 3000


@dataclass(frozen=True)
class MatchOdds:
    line_id: int
    fetched_at: float
    book: dict[str, float]
    # Seconds these prices stay servable (the age limit or the kickoff, whichever comes first):
    # the client hides them after that, measured on its own monotonic clock.
    valid_for: float


@dataclass(frozen=True)
class _Stored:
    candidate: Candidate
    live: bool
    fetched_at: float
    book: dict[str, float]
    own: bool = False  # from the Mini App's own source (else the bot's feed)


@dataclass(frozen=True)
class IngestReport:
    accepted: int
    unknown_competition: int
    without_odds: int

    def to_json(self) -> dict:
        return {"accepted": self.accepted, "unknownCompetition": self.unknown_competition,
                "withoutOdds": self.without_odds}


@dataclass(frozen=True)
class CatalogReport:
    """One catalogue push matched against the Mini App's own upcoming matches."""
    lines: int
    events: int
    reasons: dict[str, int]  # per Mini App match: matched / no_line / unknown_names / ambiguous / reversed
    wanted: list[tuple[int, float]]  # (line id, refresh seconds), nearest kickoff first
    samples: dict[str, list[str]]  # a few names per failure reason, for the alias table (public team names)


class OddsStore:
    def __init__(self, clock: Callable[[], float], table: Leagues | None = None, *, enabled: bool = True,
                 catalog: bool = False) -> None:
        self.enabled = enabled  # switched off: nothing is ever served
        # Catalogue mode (MINIAPP_ODDS_CATALOG): lines outside leagues.json are kept and matched by time and names
        # against every competition, and the bot is told which lines to price (wanted).
        self.catalog = catalog
        self._clock = clock
        self._table = table
        self._lines: dict[int, _Stored] = {}
        self._catalog: dict[int, tuple[Candidate, float]] = {}  # line id -> (candidate, last seen)
        # Line id -> the one Mini App match the last catalogue match gave it: a line outside leagues.json is served
        # for that match only (two matches claiming one line get neither, in serving as in pricing).
        self._assigned: dict[int, str] = {}
        # When the Mini App's own source last stored a COMPLETE list: while that is recent, the catalogue decides
        # every line (a withdrawn or ambiguous line is served for no match); without it, mapped lines from the
        # bot's feed are served as before (the fallback).
        self._complete_at = float("-inf")

    @property
    def table(self) -> Leagues:
        return self._table or leagues()

    def governed(self) -> bool:
        """The Mini App's own source has a recent complete list: the catalogue decides what is served."""
        return self.catalog and self._clock() - self._complete_at <= CATALOG_TTL_S

    def ingest(self, lines: Iterable[FeedLine], *, own: bool = False) -> IngestReport:
        """`own`: prices from the Mini App's own source. While it governs, the bot's feed adds only live markers
        (its parsing let second-half markets overwrite the match's prices, 15.11)."""
        accepted = unknown = empty = 0
        governed = not own and self.governed()
        for line in lines:
            if governed and not line.live:
                continue
            code = self.table.code(line.country, line.league)
            if code is None and (not self.catalog or not senior(line.league, line.home, line.away, line.country)):
                unknown += 1
                continue
            if not line.book:
                empty += 1
            known = self._lines.get(line.line_id)
            # A line once seen live stays live (an early start reported by either side): no pre-match price
            # replaces the marker. Otherwise a newer fetch replaces an older one, and an own price a bot price.
            if known is not None and known.live and not line.live:
                continue
            if known is not None and known.fetched_at >= line.fetched_at and not (own and not known.own):
                continue
            candidate = Candidate(line.line_id, code, line.kickoff, line.home, line.away, line.country, line.league)
            self._lines[line.line_id] = _Stored(candidate, line.live, line.fetched_at, line.book, own)
            accepted += 1
        self._evict()
        return IngestReport(accepted, unknown, empty)

    def own_lines(self) -> list[FeedLine]:
        """The own source's priced pre-match lines, for its snapshot (prices survive a restart)."""
        return [FeedLine(i, s.candidate.country, s.candidate.league, s.candidate.home, s.candidate.away,
                         s.candidate.kickoff, False, s.fetched_at, dict(s.book))
                for i, s in self._lines.items() if s.own and not s.live and s.book]

    def _evict(self) -> None:
        now = self._clock()
        for line_id in [i for i, s in self._lines.items() if now - s.candidate.kickoff > KEEP_AFTER_KICKOFF_S]:
            del self._lines[line_id]
        if len(self._lines) > MAX_LINES:
            # The furthest kickoffs go first: they matter least now.
            for line_id in sorted(self._lines, key=lambda i: -self._lines[i].candidate.kickoff)[: len(self._lines) - MAX_LINES]:
                del self._lines[line_id]

    def lookup(self, event: FootballEvent) -> MatchOdds | None:
        """Odds of this match: strictly matched, fresh, before kickoff; otherwise None."""
        if not self.enabled or event.status != "upcoming":
            return None
        now = self._clock()
        kickoff = parse_instant(event.starts_at).timestamp()
        if now >= kickoff:
            return None
        competition = event.tournament_id.removeprefix("fd:")
        catalogue = self.catalog and competition not in NO_CATALOGUE_ODDS
        # Expired lines take no part: an old copy of a fixture under a previous line id must not
        # make a fresh one ambiguous (live markers are recent, so they still take part).
        age = max_age(kickoff, now, self.catalog)
        recent = (s.candidate for s in self._lines.values() if now - s.fetched_at < age - TRANSIT_ALLOWANCE_S)
        line, reason = match(recent, competition=competition, kickoff=kickoff, home=event.home, away=event.away,
                             table=self.table, area=area_of(competition) if catalogue else None, countries=COUNTRIES)
        governed = self.governed()
        if line is not None and (line.competition is None or governed) and self._assigned.get(line.line_id) != event.id:
            line, reason = None, "unassigned"  # withdrawn, ambiguous, or given to another match by the catalogue
        if line is None:
            if reason not in ("no_line",):
                logger.info("odds.unmatched event=%s reason=%s", event.id, reason)
            return None
        stored = self._lines[line.line_id]
        if governed and not stored.own and not stored.live:
            return None  # a bot price stored before the own source took over (its parsing bug, 15.11)
        # Both kickoffs count: the sources may disagree by up to the matching tolerance.
        starts = min(kickoff, stored.candidate.kickoff)
        # Never more than the age limit: the client rejects a longer validity as impossible.
        valid_for = min(stored.fetched_at + age - TRANSIT_ALLOWANCE_S, starts - CUTOFF_BEFORE_KICKOFF_S,
                        now + min(age, MAX_VALID_FOR_S)) - now
        # Under a second left: not served (the client counts validity in whole seconds).
        if stored.live or not stored.book or valid_for < 1:
            return None
        return MatchOdds(line.line_id, stored.fetched_at, dict(stored.book), valid_for)

    # --- the catalogue: which of the bot's lines stand for the Mini App's matches -------------------------

    def ingest_catalog(self, lines: Iterable[CatalogLine]) -> int:
        """Replaces the catalogue with Mostbet's whole list (the Mini App's own source reads it to its end): a line
        the list no longer has is gone at once."""
        now = self._clock()
        count = 0
        self._catalog.clear()
        self._complete_at = now
        for line in lines:
            if not now < line.kickoff <= now + CATALOG_WINDOW_S:
                continue
            code = self.table.code(line.country, line.league)  # None outside leagues.json: checked by area
            if code is None and not senior(line.league, line.home, line.away, line.country):
                continue  # women's, youth or reserve football: never a senior match
            self._catalog[line.line_id] = (
                Candidate(line.line_id, code, line.kickoff, line.home, line.away, line.country, line.league), now)
            count += 1
        if len(self._catalog) > MAX_CATALOG:
            for line_id in sorted(self._catalog, key=lambda i: -self._catalog[i][0].kickoff)[: len(self._catalog) - MAX_CATALOG]:
                del self._catalog[line_id]
        return count

    def match_catalog(self, events: Iterable[FootballEvent]) -> CatalogReport:
        """Each upcoming Mini App match against the catalogue (the strict matcher, any competition): the lines to
        price, nearest first, and why the others have none. Never makes a request."""
        now = self._clock()
        buckets: dict[int, list[Candidate]] = {}
        for candidate, _ in self._catalog.values():
            buckets.setdefault(int(candidate.kickoff // 900), []).append(candidate)
        reasons: dict[str, int] = {}
        samples: dict[str, list[str]] = {}
        chosen: dict[int, float] = {}  # line id -> kickoff
        claimed: dict[int, int] = {}  # line id -> how many matches it stands for
        owner: dict[int, str] = {}  # line id -> the match it stands for
        counted = 0
        for event in events:
            if not isinstance(event, FootballEvent) or event.status != "upcoming":
                continue
            kickoff = parse_instant(event.starts_at).timestamp()
            if not now + CUTOFF_BEFORE_KICKOFF_S < kickoff <= now + CATALOG_WINDOW_S:
                continue
            counted += 1
            slot = int(kickoff // 900)
            near = [c for k in (slot - 1, slot, slot + 1) for c in buckets.get(k, ())]
            code = event.tournament_id.removeprefix("fd:")
            area = None if code in NO_CATALOGUE_ODDS else area_of(code)
            line, reason = match(near, competition=code, kickoff=kickoff, home=event.home, away=event.away,
                                 table=self.table, area=area, countries=COUNTRIES)
            reasons[reason] = reasons.get(reason, 0) + 1
            if line is not None:
                chosen[line.line_id] = line.kickoff
                claimed[line.line_id] = claimed.get(line.line_id, 0) + 1
                owner[line.line_id] = event.id
            elif reason in ("unknown_names", "ambiguous", "reversed") and len(samples.get(reason, [])) < SAMPLES_PER_REASON:
                samples.setdefault(reason, []).append(
                    f"{event.tournament_id}: {event.home.name} [{event.home.id}] - {event.away.name} [{event.away.id}]"
                    f" ~ {unmatched_names(near)}")  # the provider's public team ids: the alias table's keys
        # A line claimed by two matches (two fixtures of the same teams at once) is priced for neither.
        for line_id in [i for i, n in claimed.items() if n > 1]:
            del chosen[line_id]
            owner.pop(line_id, None)
            reasons["duplicate"] = reasons.get("duplicate", 0) + claimed[line_id]
            reasons["matched"] -= claimed[line_id]
        self._assigned = owner
        wanted = sorted(((i, k) for i, k in chosen.items()), key=lambda x: (x[1], x[0]))
        return CatalogReport(len(self._catalog), counted, reasons,
                             [(i, refresh_every(k, now)) for i, k in wanted], samples)

    def __len__(self) -> int:
        return len(self._lines)
