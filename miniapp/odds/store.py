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

from .feed import TRANSIT_ALLOWANCE_S, FeedLine
from .matching import Candidate, Leagues, leagues, match

logger = logging.getLogger("miniapp.odds")

# Prices older than this are never served (the bot refreshes lines every 5 minutes near
# kickoff and every 10 minutes further out).
MAX_AGE_S = 15 * 60.0
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


@dataclass(frozen=True)
class IngestReport:
    accepted: int
    unknown_competition: int
    without_odds: int

    def to_json(self) -> dict:
        return {"accepted": self.accepted, "unknownCompetition": self.unknown_competition,
                "withoutOdds": self.without_odds}


class OddsStore:
    def __init__(self, clock: Callable[[], float], table: Leagues | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled  # switched off: nothing is ever served
        self._clock = clock
        self._table = table
        self._lines: dict[int, _Stored] = {}

    @property
    def table(self) -> Leagues:
        return self._table or leagues()

    def ingest(self, lines: Iterable[FeedLine]) -> IngestReport:
        accepted = unknown = empty = 0
        for line in lines:
            code = self.table.code(line.country, line.league)
            if code is None:
                unknown += 1
                continue
            if not line.book:
                empty += 1
            known = self._lines.get(line.line_id)
            if known is not None and known.fetched_at >= line.fetched_at:
                continue
            candidate = Candidate(line.line_id, code, line.kickoff, line.home, line.away)
            self._lines[line.line_id] = _Stored(candidate, line.live, line.fetched_at, line.book)
            accepted += 1
        self._evict()
        return IngestReport(accepted, unknown, empty)

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
        # Expired lines take no part: an old copy of a fixture under a previous line id must not
        # make a fresh one ambiguous (live markers are recent, so they still take part).
        recent = (s.candidate for s in self._lines.values() if now - s.fetched_at < MAX_AGE_S - TRANSIT_ALLOWANCE_S)
        line, reason = match(recent, competition=competition,
                             kickoff=kickoff, home=event.home, away=event.away, table=self.table)
        if line is None:
            if reason not in ("no_line",):
                logger.info("odds.unmatched event=%s reason=%s", event.id, reason)
            return None
        stored = self._lines[line.line_id]
        # Both kickoffs count: the sources may disagree by up to the matching tolerance.
        starts = min(kickoff, stored.candidate.kickoff)
        # Never more than MAX_AGE_S: the client rejects a longer validity as impossible.
        valid_for = min(stored.fetched_at + MAX_AGE_S - TRANSIT_ALLOWANCE_S, starts - CUTOFF_BEFORE_KICKOFF_S,
                        now + MAX_AGE_S) - now
        # Under a second left: not served (the client counts validity in whole seconds).
        if stored.live or not stored.book or valid_for < 1:
            return None
        return MatchOdds(line.line_id, stored.fetched_at, dict(stored.book), valid_for)

    def __len__(self) -> int:
        return len(self._lines)
