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
- Without a pick, a short preview: the nearest upcoming matches of the validated competitions
  (PREVIEW_DAYS ahead, from their season calendars as already cached — the warm-up keeps them —
  never a provider request) with the model's 1/X/2 and the UTC day their recommendation becomes
  possible. A preview is never a pick: the match-day rule and the validated-competition rule are
  unchanged. It is optional: bounded by PREVIEW_WAIT_S (model time), else left out.
- Without a pick, an ESTIMATE (owner, 15.11: every match may get a forecast): the period's matches of ANY competition
  and ANY day, nearest first — the model's probabilities when the competition's history is already cached (never a
  provider request), else the market's own (Mostbet prices, margin removed). The most probable outcome within the
  risk level's band (ESTIMATE_BANDS: the floor of the level, and a ceiling so a near-certain 1.10 outcome is never
  offered), team-disjoint for a coupon. Always labelled an estimate, never a selected bet; the strict pick and its
  rules are unchanged and always come first.
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
from miniapp.forecast.markets import FAMILY, KEYS
from miniapp.forecast.quick import (STATUS_NO_BET, STATUS_NOT_ENOUGH, STATUS_UNAVAILABLE, Criteria, Leg, Option,
                                    QuickResult, choose, coupon, frechet)
from miniapp.forecast.selection import LIMITED_COMPETITIONS, NO_BET_EARLY, NO_BET_UNVALIDATED, P_MAX, below_min_odds
from miniapp.forecast.version import MODEL_VERSION
from miniapp.odds.store import OddsStore
from miniapp.odds.value import MarketQuote, compare, market_book
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
PREVIEW_MAX = 5
PREVIEW_DAYS = 14
PREVIEW_WAIT_S = 8.0
# The estimate: (lowest, highest) probability per risk level. The floor is the strict rules' own level; the ceiling
# keeps near-certain outcomes (fair odds below MIN_ODDS, 1.30) out, which are no forecast worth giving.
ESTIMATE_BANDS = {"conservative": (0.70, P_MAX), "balanced": (0.60, 0.75), "elevated": (0.55, 0.65)}
ESTIMATE_FAMILIES = frozenset({"1x2", "double", "total", "btts"})
ESTIMATE_MAX_MATCHES = 80  # the period's nearest matches the estimate looks at (bounded model time; documented)
ESTIMATE_WAIT_S = 8.0


@dataclass(frozen=True)
class EstimateLeg:
    event: FootballEvent
    market: str
    probability: float  # exact, for ranking and bounds
    display: int  # the whole percent shown
    source: str  # "model" | "market"
    price: tuple[float, float, float] | None  # odds, fetched at, valid for


@dataclass(frozen=True)
class Estimate:
    legs: tuple[EstimateLeg, ...]
    suitable: int  # matches with an outcome inside the band

    def to_json(self, coupon: bool) -> dict:
        exact = [leg.probability for leg in self.legs]
        return {
            "legs": [{"event": leg.event.to_json(), "market": leg.market, "family": FAMILY[leg.market],
                      "probability": leg.display, "source": leg.source,
                      "price": ({"odds": round(leg.price[0], 2), "fetchedAt": _iso(leg.price[1]),
                                 "validFor": int(leg.price[2])} if leg.price else None)} for leg in self.legs],
            "suitable": self.suitable,
            # Assumption-free (Fréchet) bounds of the exact probabilities, rounded outward as for a strict coupon.
            "combined": _outward(frechet(exact)) if coupon and exact else None,
        }


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
    # Without a pick: nearest later matches of validated competitions with the model's 1/X/2 (never a pick).
    preview: tuple[tuple[FootballEvent, MatchForecast], ...] = ()
    # Without a pick: the estimate over every match of the period (labelled, never a selected bet).
    estimate: Estimate | None = None

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
            "preview": [{
                "event": e.to_json(),
                "probabilities": {"home": f.display["1"], "draw": f.display["X"], "away": f.display["2"]},
                # The UTC day the match-day rule allows a recommendation for it (and only if it passes the rules).
                "recommendationDay": e.starts_at[:10],
            } for e, f in self.preview],
            "estimate": self.estimate.to_json(self.criteria.mode == "coupon") if self.estimate else None,
        }

    def _bounds(self) -> list[int]:
        """Exact Fréchet bounds rounded outward to whole percents (still valid bounds)."""
        return _outward(self.result.combined)  # type: ignore[arg-type]

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
                 odds: OddsStore | None = None, estimator: ModelForecaster | None = None) -> None:
        self.sports = sports
        self.odds = odds
        self._clock = clock
        # Its own engine: the batch runs in a thread, the AI analysis on the event loop.
        self.forecaster = forecaster or ModelForecaster(sports, ForecastEngine(), clock=clock)
        self.reserve = reserve
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quick-model")
        # The estimate has its own worker and engine: a slow or timed-out estimate never delays a strict pick.
        self._estimator = estimator or ModelForecaster(sports, ForecastEngine(), clock=clock)  # never shared
        self._estimate_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quick-estimate")
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

    def first_cost(self, start: datetime, end: datetime) -> int:
        """1 when the first UTC day a pick lists is cold (a provider request), else 0 (for the user's share)."""
        now_dt = datetime.fromtimestamp(self._clock(), UTC)
        first = max(start, now_dt)
        if end <= first or not self.sports.connected("football"):
            return 0
        midnight = datetime.combine(first.date() + timedelta(days=1), datetime.min.time(), UTC)
        return self.sports.events_cost(first, min(end, midnight))

    async def pick(self, user_id: int, start: datetime, end: datetime, tournaments: frozenset[str] | None,
                   criteria: Criteria, budget: Callable[[int], bool] | None = None) -> QuickAnswer:
        """`budget(cost)`: reserves that many provider requests in the caller's share, False when it has no
        room (the step is then skipped like one over the shared limit)."""
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
                return await self._pick(max(start, now_dt), end, tournaments, criteria, now_dt.date(), budget)
            finally:
                LOW_PRIORITY_RESERVE.reset(token)
        finally:
            self._running.discard(user_id)

    async def _pick(self, start: datetime, end: datetime, tournaments: frozenset[str] | None,
                    criteria: Criteria, today: date, budget: Callable[[int], bool] | None = None) -> QuickAnswer:
        """`today`: the UTC day of the single clock reading that started the pick; a later day
        change makes the answer "unavailable" (checked at the end)."""
        def affordable(cost: int, user_cost: int | None = None) -> bool:
            """`cost`: requests for the shared limit; `user_cost`: for the user's share, where a load already
            running counts (it may be a refused background load the cache repeats for this caller)."""
            mine = cost if user_cost is None else user_cost
            if cost and cost > self.sports.upstream_headroom() - self.reserve:
                return False
            return mine == 0 or budget is None or budget(mine)

        days = _utc_days(start, end)
        events: dict[str, FootballEvent] = {}
        unchecked = 0

        async def list_day(day: date) -> bool:
            if not affordable(self.sports.day_cost(day), self.sports.day_user_cost(day)):
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
            if not affordable(self.sports.history_cost(tournament), self.sports.history_cost(tournament, user=True)):
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
        result = choose(options, criteria, self._books(options, {e.id: e for e in usable}))
        # Nothing could be evaluated because data is missing (today's, or every day loaded was
        # empty while others were not checked): retry later, never "no bet" / "no matches".
        # Missing data of today could have changed an unsuccessful answer (any of those matches
        # might qualify): then it is "retry later", never a conclusive "no bet" / "not enough".
        partial = missing or today_failed or (unchecked and not events)
        if result.status != "ok" and partial:
            result = QuickResult(STATUS_UNAVAILABLE, (), result.suitable, None)
        preview: tuple[tuple[FootballEvent, MatchForecast], ...] = ()
        estimate: Estimate | None = None
        if result.status != "ok":
            async def previewed() -> tuple[tuple[FootballEvent, MatchForecast], ...]:
                try:
                    return await asyncio.wait_for(self._preview(tournaments, today), PREVIEW_WAIT_S)
                except TimeoutError:
                    logger.info("quick.preview_skipped reason=timeout")
                except Exception:  # optional: a failing preview never breaks the answer
                    logger.exception("quick.preview_failed")
                return ()

            async def estimated() -> Estimate | None:
                # Only after a conclusive "nothing qualifies": with data missing ("unavailable") the strict
                # selection could not decide, and the estimate would read as if it had.
                if result.status not in (STATUS_NO_BET, STATUS_NOT_ENOUGH) or unchecked:
                    return None  # an unchecked day: the estimate would not cover the period it names
                try:
                    return await asyncio.wait_for(self._estimate(list(events.values()), histories, criteria),
                                                  ESTIMATE_WAIT_S)
                except TimeoutError:
                    logger.info("quick.estimate_skipped reason=timeout")
                except Exception:  # optional: a failing estimate never breaks the answer
                    logger.exception("quick.estimate_failed")
                return None

            preview, estimate = await asyncio.gather(previewed(), estimated())
        # Read the clock again after the preview: a day change during it would make a "later" match today's.
        now = max(now, self._clock())
        if datetime.fromtimestamp(now, UTC).date() != today:
            preview, estimate = (), None
            # The UTC day changed while the data loaded: "today's" picks and "later" matches were
            # judged against the previous day. Never a mixed answer; asked again it is consistent.
            result, next_match_at = QuickResult(STATUS_UNAVAILABLE, (), 0, None), None
        matches = sum(1 for e in events.values() if parse_instant(e.starts_at).timestamp() > now)
        by_id = {e.id: e for e in usable}
        return QuickAnswer(criteria, result, by_id, matches, forecasts, reasons,
                           tuple(unavailable), unchecked,
                           datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), next_match_at,
                           self._prices(result, by_id), preview, _started(estimate, now))

    async def _preview(self, tournaments: frozenset[str] | None, today: date,
                       ) -> tuple[tuple[FootballEvent, MatchForecast], ...]:
        """Nearest later matches of validated competitions with the model's figures; never a pick."""
        served = {t.id for t in self.sports.tournaments("football")}
        candidates = sorted(f"fd:{code}" for code in FROZEN.validated
                            if f"fd:{code}" in served and (tournaments is None or f"fd:{code}" in tournaments))
        horizon = datetime.combine(today + timedelta(days=PREVIEW_DAYS + 1), datetime.min.time(), UTC)
        found: list[tuple[FootballEvent, ModelHistory]] = []
        for tournament in candidates:
            # Cache only: a preview never costs a provider request (the warm-up keeps calendars cached).
            history = self.sports.cached_model_history(tournament)
            if history is None:
                continue
            for e in history.events:
                if (isinstance(e, FootballEvent) and e.status == "upcoming" and e.tournament_id == tournament
                        and today < parse_instant(e.starts_at).date() and parse_instant(e.starts_at) < horizon):
                    found.append((e, history))
        found.sort(key=lambda x: (x[0].starts_at, x[0].id))
        chosen = found[:PREVIEW_MAX * 3]  # some may have no model figures: keep looking a little further
        if not chosen:
            return ()
        histories = {e.tournament_id: h for e, h in chosen}
        events = [e for e, _ in chosen]
        outcomes = await asyncio.get_running_loop().run_in_executor(self._worker, self._forecast_all, events, histories)
        return tuple((e, f) for e, f in zip(events, outcomes, strict=True) if isinstance(f, MatchForecast))[:PREVIEW_MAX]

    async def _estimate(self, events: list[FootballEvent], histories: dict[str, ModelHistory | None],
                        criteria: Criteria) -> Estimate | None:
        """The most probable outcome of each upcoming match within the risk band (model when its history is
        cached, else the market's own probability); the best ones, team-disjoint. Never a provider request."""
        now = self._clock()
        nearest = sorted((e for e in events if parse_instant(e.starts_at).timestamp() > now),
                         key=lambda e: (e.starts_at, e.id))[:ESTIMATE_MAX_MATCHES]
        if not nearest:
            return None
        known = dict(histories)
        for e in nearest:
            if e.tournament_id not in known:
                known[e.tournament_id] = self.sports.cached_model_history(e.tournament_id)
        modelled = [e for e in nearest if known.get(e.tournament_id) is not None]
        forecasts = await asyncio.get_running_loop().run_in_executor(
            self._estimate_worker, lambda: [self._estimator.compute(e, known[e.tournament_id]) for e in modelled])
        by_event = {e.id: f for e, f in zip(modelled, forecasts, strict=True) if isinstance(f, MatchForecast)}
        low, high = ESTIMATE_BANDS[criteria.risk]
        best: list[EstimateLeg] = []
        for e in nearest:
            found = self.odds.lookup(e) if self.odds is not None else None
            book = market_book(found.book) if found is not None else {}
            forecast = by_event.get(e.id)
            if forecast is not None:
                probs, source = forecast.exact, "model"
            elif book:
                probs, source = {k: fair for k, (_, fair, _) in book.items()}, "market"
            else:
                continue
            fits = [k for k, p in probs.items()
                    if k in FAMILY and FAMILY[k] in ESTIMATE_FAMILIES and low <= p <= high
                    and (criteria.market == "any" or FAMILY[k] == criteria.market)
                    and not below_min_odds(book[k][0] if k in book else None)]
            if not fits:
                continue
            market = max(fits, key=lambda k: (probs[k], -KEYS.index(k)))
            shown = forecast.display[market] if forecast is not None else round(probs[market] * 100)
            price = (book[market][0], found.fetched_at, found.valid_for) if found is not None and market in book else None
            best.append(EstimateLeg(e, market, probs[market], shown, source, price))
        best.sort(key=lambda leg: (-leg.probability, leg.event.starts_at, leg.event.id))
        # The strict coupon's own search (best-ranked first, past team conflicts), on the estimate's legs.
        ranked = [_Teams(leg) for leg in best]
        chosen = [t.leg for t in coupon(ranked, criteria.size)]  # type: ignore[arg-type]
        if len(chosen) < criteria.size:
            return None
        return Estimate(tuple(chosen), len(best))

    def _books(self, options: list[Option], events: dict[str, FootballEvent]) -> dict[str, dict[str, float]]:
        """Checked bookmaker odds of the candidate matches (no request: what the odds store holds), so a market
        priced below the minimum odds is never chosen."""
        if self.odds is None:
            return {}
        books: dict[str, dict[str, float]] = {}
        for option in options:
            found = self.odds.lookup(events[option.event_id])
            if found is not None:
                books[option.event_id] = dict(found.book)
        return books

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


def _outward(bounds: tuple[float, float]) -> list[int]:
    low, high = bounds
    return [max(0, math.floor(low * 100 + 1e-9)), min(100, math.ceil(high * 100 - 1e-9))]


class _Teams:
    """An estimate leg in the shape forecast.quick.coupon searches (option.teams)."""

    def __init__(self, leg: EstimateLeg) -> None:
        self.leg = leg
        self.option = self
        self.teams = (leg.event.home.id, leg.event.away.id)


def _started(estimate: Estimate | None, now: float) -> Estimate | None:
    """No estimate leg after its kickoff (the clock is read again at the end of a pick)."""
    if estimate is None or any(parse_instant(leg.event.starts_at).timestamp() <= now for leg in estimate.legs):
        return None
    return estimate


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
