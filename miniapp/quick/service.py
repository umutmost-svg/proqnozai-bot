"""Quick prediction: the goal model over real upcoming matches, no Claude call at all.

Data comes through the shared cache only: the day listings the catalogue uses and each
competition's model history (current season + previous one), the same entries the AI
analysis reads. Nothing is ever guessed:

- Recommendations exist only on a match's UTC day (M3.5 rule), so the model runs only on
  today's matches; later matches of the period are counted as `early_forecast` and cost no
  history request at all.
- Every provider attempt of a pick (retries included) runs at low priority: the provider
  client refuses it once fewer than UPSTREAM_RESERVE requests are left in the current
  minute (shared by all users and features), so the catalogue, tables and AI analysis keep
  that capacity; the check is atomic inside the client. A load is not even started when the
  minute obviously has no room for it. Order:
  today's listing, the histories of today's competitions (most matches first), then the
  later days. What does not fit is reported (unavailable competitions, unchecked days) and
  can be retried later, when the cache is warm; it never turns into "no bet".
- The model runs on one dedicated worker thread with its own engine; each user may
  run one pick at a time and a few per minute.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from miniapp.ai.analysis import ModelForecaster
from miniapp.forecast.engine import FROZEN, ForecastEngine, MatchForecast
from miniapp.forecast.markets import FAMILY
from miniapp.forecast.quick import STATUS_UNAVAILABLE, Criteria, Leg, Option, QuickResult, choose
from miniapp.forecast.selection import LIMITED_COMPETITIONS, NO_BET_EARLY, NO_BET_UNVALIDATED
from miniapp.forecast.version import MODEL_VERSION
from miniapp.odds.store import OddsStore
from miniapp.odds.value import MarketQuote, compare
from miniapp.sports.client import LOW_PRIORITY_RESERVE, UpstreamError
from miniapp.sports.models import FootballEvent
from miniapp.sports.service import ModelHistory, RequestError, SportsService, parse_instant

logger = logging.getLogger("miniapp.quick")

# The longest period offered: 7 days (the "today" period is at most one local day).
MAX_PERIOD = timedelta(days=7, hours=1)
# How far `from` may be from the server's "now" (client clock drift).
CLOCK_SLACK = timedelta(hours=1)
PER_USER_PER_MINUTE = 6
# Picks running or waiting for the model at once, for everyone (more: HTTP 429).
MAX_IN_FLIGHT = 4
# Provider requests always left for everything else in the same minute (catalogue, AI analysis).
UPSTREAM_RESERVE = 3


class QuickBusy(Exception):
    """This user already has a pick running or asked too often (HTTP 429)."""


@dataclass(frozen=True)
class QuickAnswer:
    criteria: Criteria
    result: QuickResult
    events: dict[str, FootballEvent]
    matches: int  # upcoming matches in the period and tournaments
    forecasts: int  # of them, matches the model evaluated
    reasons: Counter  # why the others were not recommendable (stable codes)
    unavailable: tuple[str, ...]  # tournaments whose data could not be loaded now
    unchecked_days: int  # days of the period whose listing could not be loaded now
    computed_at: str
    # Kickoff of the earliest match of the period not on today's UTC day (it can be picked only
    # on its own match day): tells the user when to come back. None when there is none.
    next_match_at: str | None = None
    # Current prices of the chosen legs, by event id (strictly matched, fresh; see miniapp/odds).
    prices: dict[str, tuple[MarketQuote, float, float]] = field(default_factory=dict)  # quote, fetched at, valid for

    def to_json(self) -> dict:
        combined = self.result.combined
        return {
            "mode": self.criteria.mode,
            "status": self.result.status,
            "legs": [self._leg(leg) for leg in self.result.legs],
            "size": self.criteria.size,
            "suitable": self.result.suitable,
            # Fréchet bounds [max(0, Σp − (n−1)), min p] of the exact probabilities, rounded
            # outward — valid for any dependence between the matches (nothing is multiplied).
            "combined": self._bounds() if combined is not None else None,
            "counts": {"matches": self.matches, "forecasts": self.forecasts},
            "reasons": dict(sorted(self.reasons.items())),
            "unavailableTournaments": list(self.unavailable),
            "uncheckedDays": self.unchecked_days,
            "nextMatchAt": self.next_match_at,
            # The product of the legs' current prices, only when every leg has one. A figure
            # computed from separate quotes, not a price offered for the combination.
            "couponOdds": self._coupon_odds(),
            "experimental": FROZEN.experimental,
            "modelVersion": MODEL_VERSION,
            "computedAt": self.computed_at,
        }

    def _bounds(self) -> list[int]:
        """Exact Fréchet bounds rounded outward to whole percents (still valid bounds)."""
        low, high = self.result.combined  # type: ignore[misc]
        return [max(0, math.floor(low * 100 + 1e-9)), min(100, math.ceil(high * 100 - 1e-9))]

    def _coupon_odds(self) -> dict | None:
        legs = self.result.legs
        if self.criteria.mode != "coupon" or not legs or any(l.option.event_id not in self.prices for l in legs):
            return None
        product = math.prod(self.prices[l.option.event_id][0].odds for l in legs)
        oldest = min(self.prices[l.option.event_id][1] for l in legs)
        valid = min(self.prices[l.option.event_id][2] for l in legs)
        return {"odds": round(product, 2), "fetchedAt": _iso(oldest), "validFor": int(valid)}

    def _leg(self, leg: Leg) -> dict:
        f: MatchForecast = leg.option.forecast
        market = leg.candidate.market
        price = self.prices.get(leg.option.event_id)
        return {
            "price": {**price[0].to_json(), "fetchedAt": _iso(price[1]), "validFor": int(price[2])} if price else None,
            "event": self.events[leg.option.event_id].to_json(),
            "market": market,
            "family": FAMILY[market],
            # The same whole percent the AI screen shows for this market.
            "probability": f.display[market],
            "leagueRate": round(leg.candidate.league_rate * 100),
            "expectedGoals": {"home": round(f.expected_goals[0], 2), "away": round(f.expected_goals[1], 2)},
            "sample": f.sample,
            "teamMatches": {"home": f.home_matches, "away": f.away_matches},
            "dataUntil": f.data_until,
        }


class QuickService:
    def __init__(self, sports: SportsService, *, clock: Callable[[], float] = time.time,
                 forecaster: ModelForecaster | None = None, reserve: int = UPSTREAM_RESERVE,
                 odds: OddsStore | None = None) -> None:
        self.sports = sports
        self.odds = odds
        self._clock = clock
        # Its own engine: the batch runs in a thread, the AI analysis on the event loop.
        self.forecaster = forecaster or ModelForecaster(sports, ForecastEngine(), clock=clock)
        self.reserve = reserve
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quick-model")
        self._running: set[int] = set()
        self._recent: dict[int, deque[float]] = {}

    def _admit(self, user_id: int) -> None:
        now = self._clock()
        recent = self._recent.setdefault(user_id, deque())
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if user_id in self._running or len(recent) >= PER_USER_PER_MINUTE or len(self._running) >= MAX_IN_FLIGHT:
            raise QuickBusy()
        recent.append(now)
        if len(self._recent) > 10_000:  # bounded memory: forget idle users
            for uid in [u for u, q in self._recent.items() if not q or now - q[-1] >= 60]:
                del self._recent[uid]

    async def pick(self, user_id: int, start: datetime, end: datetime, tournaments: frozenset[str] | None,
                   criteria: Criteria) -> QuickAnswer:
        now = self._clock()
        now_dt = datetime.fromtimestamp(now, UTC)
        if end <= start or end - start > MAX_PERIOD or abs(start - now_dt) > CLOCK_SLACK:
            raise RequestError("bad_range")
        catalog = {t.id for t in self.sports.tournaments("football")}
        if tournaments is not None and not tournaments <= catalog:
            raise RequestError("unknown_tournament")
        self._admit(user_id)
        if not self.sports.connected("football"):
            # No source at all: "data unavailable", never "no matches in this period".
            return QuickAnswer(criteria, QuickResult(STATUS_UNAVAILABLE, (), 0, None), {}, 0, 0, Counter(), (), 0,
                               now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._running.add(user_id)
        try:
            token = LOW_PRIORITY_RESERVE.set(self.reserve)
            try:
                return await self._pick(max(start, now_dt), end, tournaments, criteria, now_dt.date())
            finally:
                LOW_PRIORITY_RESERVE.reset(token)
        finally:
            self._running.discard(user_id)

    async def _pick(self, start: datetime, end: datetime, tournaments: frozenset[str] | None,
                    criteria: Criteria, today: date) -> QuickAnswer:
        """`today`: the UTC day of the single clock reading that started the pick; a later day
        change makes the answer "unavailable" (checked at the end)."""
        def affordable(cost: int) -> bool:
            return cost == 0 or cost <= self.sports.upstream_headroom() - self.reserve

        days = _utc_days(start, end)
        events: dict[str, FootballEvent] = {}
        unchecked = 0

        async def list_day(day: date) -> bool:
            if not affordable(self.sports.day_cost(day)):
                return False
            lo = max(start, datetime.combine(day, datetime.min.time(), UTC))
            hi = min(end, datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC))
            try:
                result = await self.sports.events("football", lo, hi, None)
            except UpstreamError as exc:
                logger.info("quick.day_unavailable day=%s code=%s", day, exc.code)
                return False
            if result.stale:
                # Served from cache after a failed refresh: statuses may have changed, an empty
                # day proves nothing. Not checked, never a pick or "no matches" from it.
                return False
            for e in result.events:
                if (isinstance(e, FootballEvent) and e.status == "upcoming"
                        and (tournaments is None or e.tournament_id in tournaments)
                        and parse_instant(e.starts_at) >= start):
                    events[e.id] = e
            return True

        # Today first: only today's matches can be recommended at all.
        today_failed = today in days and not await list_day(today)
        unchecked += today_failed
        todays = [e for e in events.values() if parse_instant(e.starts_at).date() == today]
        histories: dict[str, ModelHistory | None] = {}
        unavailable: list[str] = []
        counts = Counter(e.tournament_id for e in todays)
        # Competitions the frozen rules were never validated on can never yield a pick: they are
        # classified here, before any history request, so they cannot use up the minute's budget
        # that validated competitions need.
        unvalidated = {t for t in counts
                       if t.split(":", 1)[-1] not in FROZEN.validated and t.split(":", 1)[-1] not in LIMITED_COMPETITIONS}
        for tournament in sorted(counts, key=lambda t: (-counts[t], t)):
            if tournament in unvalidated:
                continue
            if not affordable(self.sports.history_cost(tournament)):
                unavailable.append(tournament)
                continue
            try:
                history = await self.sports.model_history(tournament)
            except UpstreamError as exc:
                logger.info("quick.history_unavailable tournament=%s code=%s", tournament, exc.code)
                unavailable.append(tournament)
                continue
            if history is not None and history.previous_missing == "transient":
                # Without the previous season nothing could be recommended (partial_history),
                # but a later try may load it: "retry later", never a conclusive "no bet".
                unavailable.append(tournament)
                continue
            histories[tournament] = history
        for day in days:
            if day != today and not await list_day(day):
                unchecked += 1

        usable = sorted((e for e in todays if e.tournament_id in histories), key=lambda e: (e.starts_at, e.id))
        # One dedicated worker: batches never overlap on the engine, even when a request is
        # cancelled while its batch is still running.
        outcomes = await asyncio.get_running_loop().run_in_executor(self._worker, self._forecast_all, usable, histories)
        # A match may have kicked off while the data loaded: it is never offered after kickoff.
        now = self._clock()
        reasons: Counter = Counter()
        options: list[Option] = []
        forecasts = 0
        for event, outcome in zip(usable, outcomes, strict=True):
            if parse_instant(event.starts_at).timestamp() <= now:
                continue
            if not isinstance(outcome, MatchForecast):
                reasons[f"model_{outcome.reason}"] += 1
                continue
            forecasts += 1
            options.append(Option(event.id, event.starts_at, (event.home.id, event.away.id), outcome))
            if outcome.selection.no_bet_reason is not None:
                reasons[outcome.selection.no_bet_reason] += 1
        skipped = sum(1 for e in todays if e.tournament_id in unvalidated and parse_instant(e.starts_at).timestamp() > now)
        if skipped:
            reasons[NO_BET_UNVALIDATED] += skipped
        later_events = [e for e in events.values() if parse_instant(e.starts_at).date() > today]
        later = len(later_events)
        # "The next match" only when every day of the period was checked: an unchecked earlier day
        # could hold a nearer one.
        next_match_at = None if unchecked else min((e.starts_at for e in later_events), default=None)
        if later:
            reasons[NO_BET_EARLY] += later
        missing = sum(1 for e in todays if e.tournament_id in unavailable)
        if missing:
            reasons["source_unavailable"] = missing
        result = choose(options, criteria)
        # Nothing could be evaluated because data is missing (today's, or every day loaded was
        # empty while others were not checked): retry later, never "no bet" / "no matches".
        # Missing data of today could have changed an unsuccessful answer (any of those matches
        # might qualify): then it is "retry later", never a conclusive "no bet" / "not enough".
        partial = missing or today_failed or (unchecked and not events)
        if result.status != "ok" and partial:
            result = QuickResult(STATUS_UNAVAILABLE, (), result.suitable, None)
        if datetime.fromtimestamp(now, UTC).date() != today:
            # The UTC day changed while the data loaded: "today's" picks and "later" matches were
            # judged against the previous day. Never a mixed answer; asked again it is consistent.
            result, next_match_at = QuickResult(STATUS_UNAVAILABLE, (), 0, None), None
        matches = sum(1 for e in events.values() if parse_instant(e.starts_at).timestamp() > now)
        by_id = {e.id: e for e in usable}
        return QuickAnswer(criteria, result, by_id, matches, forecasts, reasons,
                           tuple(unavailable), unchecked,
                           datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), next_match_at,
                           self._prices(result, by_id))

    def _prices(self, result: QuickResult, events: dict[str, FootballEvent]) -> dict[str, tuple[MarketQuote, float, float]]:
        """The chosen legs' prices: no request, only what the bot has pushed (matched strictly)."""
        prices: dict[str, tuple[MarketQuote, float, float]] = {}
        if self.odds is None:
            return prices
        for leg in result.legs:
            found = self.odds.lookup(events[leg.option.event_id])
            if found is None:
                continue
            quote = compare(found.book, leg.option.forecast.exact).get(leg.candidate.market)
            if quote is not None:
                prices[leg.option.event_id] = (quote, found.fetched_at, found.valid_for)
        return prices

    def _forecast_all(self, events: list[FootballEvent], histories: dict[str, ModelHistory | None]):
        return [self.forecaster.compute(e, histories[e.tournament_id]) for e in events]


def _utc_days(start: datetime, end: datetime) -> list[date]:
    days, day = [], start.date()
    while datetime.combine(day, datetime.min.time(), UTC) < end:
        days.append(day)
        day += timedelta(days=1)
    return days


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
