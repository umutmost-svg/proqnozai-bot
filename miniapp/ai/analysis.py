"""AI analysis of a real upcoming match (M3.2, statistical model since M3.5).

Order of decisions, all in Python before any model call:
1. the event must exist and be an upcoming football match;
2. verified form of both teams must exist (readiness: READY / PARTIAL /
   INSUFFICIENT — without odds the best possible level is PARTIAL);
3. the goal model (miniapp/forecast) must have enough history for both teams; it
   computes every probability and, by frozen rules, the recommended market or NO_BET;
4. INSUFFICIENT never calls the language model;
5. a language-model call is cached per (event, evidence), shared by every user and
   language, runs once per key at a time, and is charged against a per-user and a
   global daily budget.

The language model gets only verified numbers and the model's result and returns
strict JSON: 2–4 keys of the factors that explain the estimate best and up to three
reason codes, both from lists computed here (every offered reason is true and
supports the recommended market). It returns no number and no free text: it cannot
change a probability, add a market, lift NO_BET or name data we never gave; every
sentence the user sees is a template filled with verified numbers by the frontend.
Anything outside the contract is rejected, never repaired.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from miniapp.forecast.data import results_from_events
from miniapp.forecast.engine import FROZEN, ForecastEngine, MatchForecast, Unavailable
from miniapp.forecast.history import ForecastHistory, ForecastRecord
from miniapp.forecast.version import MODEL_VERSION
from miniapp.sports import football_data
from miniapp.sports.cache import Cached, SportsCache
from miniapp.sports.client import UpstreamError
from miniapp.sports.models import FootballEvent, Tournament
from miniapp.sports.service import ModelHistory, SportsService, parse_instant

from .evidence import TeamSummary, has_verified_form, summarize
from .pick import MAX_REASONS, NO_BET, REASONS, Pick, allowed_reasons
from .readiness import NOT_ANALYSED, forecast_readiness

logger = logging.getLogger("miniapp.ai")

# An answer is kept until its match starts (its key already changes with any new evidence);
# after an eviction or a restart it is rebuilt from the forecast history, never paid again.
# Whole request under the frontend's AI_TIMEOUT_MS = 120 s: evidence ≤ 20 s (table ≤ 8 s of
# it, in parallel with the form), quota reservation ≤ ~10 s (SQLite busy timeout), one model
# attempt ≤ 60 s.
EVIDENCE_TIMEOUT_S = 20.0
STANDINGS_TIMEOUT_S = 8.0
MODEL_TIMEOUT_S = 60.0
# Closer to kickoff than this counts as started: covers the reservation wait and the call
# itself, so no paid call can start (or finish) for a match already underway.
KICKOFF_MARGIN_S = 5 * 60.0
# Paid attempts per unchanged analysis (one evidence digest): a model call that keeps failing
# (refusal, bad output, timeouts) is not paid for again and again until kickoff.
# Paid calls per unchanged analysis, counted durably before each call: a successful answer is
# restored from the history (never paid again), so this caps calls that failed in any way.
MAX_PAID_ATTEMPTS = 2
# Each key is rendered by the frontend from the basis numbers (form, goals, table) of
# that team, or from the goal model's figures (rating, home advantage).
FACTORS = ("home_form", "away_form", "home_goals", "away_goals", "home_table", "away_table",
           "home_rating", "away_rating", "home_advantage")

SCHEMA = {
    "type": "object",
    "properties": {
        "factors": {"type": "array", "items": {"type": "string", "enum": list(FACTORS)}},
        "reasons": {"type": "array", "items": {"type": "string", "enum": list(REASONS)}},
    },
    "required": ["factors", "reasons"],
    "additionalProperties": False,
}
# Bumped whenever the answer's shape changes: a cached answer of an older shape is never
# served as a newer one (the cache lives in memory; nothing persisted needs migrating).
SCHEMA_VERSION = "v3"

# Stable across requests (cache-friendly): everything that varies goes in the user turn.
SYSTEM_PROMPT = """You explain a statistical forecast of one upcoming football match for the
ProqnozAI app. A goal model computed every probability from verified past results and,
by fixed rules, chose the recommended market or NO_BET. You do not estimate, change or
restate any number, and you write no free text.

Use ONLY the facts in the JSON you are given: the fixture; each team's most recent
finished results (form string newest first: W/D/L; venues H/A; goals; points); their
rows in the competition table when present; and the model's figures — expected goals,
attack/defence ratings relative to the competition average (1.0 = average; attack
above 1 scores more, defence above 1 concedes more), the competition's home advantage,
its 1X2 estimate, the recommended market and the alternatives. Nothing else is known:
no injuries, lineups, odds, head-to-head history or any other data. Do not assume any.

Return:
- "factors": the 2 to 4 facts that best explain the model's estimate, most important
  first, chosen ONLY from "allowedFactors". No duplicates.
- "reasons": up to 3 codes chosen ONLY from "allowedReasons" (each one is already
  verified true and supports the recommended market) that explain it best, most
  important first. Empty when the recommendation is NO_BET or "allowedReasons" is empty.

There are no bookmaker odds: nothing here is a claim of value, profit or certainty."""


class AiError(Exception):
    """The analysis could not be produced; ``code`` is safe to return and log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code  # not_created | not_configured | limit_reached | budget_exhausted | refused | bad_output | upstream_error | timeout | rate_limited


@dataclass(frozen=True)
class Completion:
    text: str  # JSON that should satisfy the schema (validated by the caller)
    model: str  # the model that actually answered (a server-side fallback may differ)


class ModelClient(Protocol):
    model: str

    async def complete(self, system: str, user: str, schema: dict) -> Completion:
        """The model's answer, or AiError."""


@dataclass(frozen=True)
class AnalysisResult:
    event_id: str
    kind: str  # "ready" | "partial" | "insufficient"
    reasons: tuple[str, ...]
    generated_at: str | None = None
    model: str | None = None
    probabilities: dict | None = None  # {"home": int, "draw": int, "away": int}
    factors: tuple[str, ...] = ()  # keys from FACTORS
    basis: dict | None = None  # the verified numbers shown under the result
    estimated: bool = False
    pick: Pick | None = None  # None only for results without a model answer
    forecast: dict | None = None  # the goal model's figures (MatchForecast.to_json)
    alternatives: tuple[dict, ...] = ()  # [{"market", "probability"}], never with NO_BET

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "eventId": self.event_id,
            "kind": self.kind,
            "reasons": list(self.reasons),
            "generatedAt": self.generated_at,
            "model": self.model,
            "probabilities": self.probabilities,
            "factors": list(self.factors),
            "basis": self.basis,
            "estimated": self.estimated,
            "pick": self.pick.to_json() if self.pick else None,
            "forecast": self.forecast,
            "alternatives": list(self.alternatives),
        }


def validate_output(text: str, allowed: frozenset[str], reasons_allowed: tuple[str, ...] = (),
                    no_bet: bool = False) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Strict check of the language model's JSON → (factors, reasons). Nothing is clamped,
    reordered or dropped: anything outside the contract is bad_output."""
    try:
        data = json.loads(text)
    except ValueError:
        raise AiError("bad_output") from None
    if not isinstance(data, dict) or set(data) != set(SCHEMA["required"]):
        raise AiError("bad_output")
    factors = data["factors"]
    if not isinstance(factors, list) or not 2 <= len(factors) <= 4:
        raise AiError("bad_output")
    if any(not isinstance(f, str) or f not in allowed for f in factors) or len(set(factors)) != len(factors):
        raise AiError("bad_output")
    reasons = data["reasons"]
    if not isinstance(reasons, list) or len(reasons) > MAX_REASONS or (no_bet and reasons):
        raise AiError("bad_output")
    if any(not isinstance(r, str) or r not in reasons_allowed for r in reasons) or len(set(reasons)) != len(reasons):
        raise AiError("bad_output")
    return tuple(factors), tuple(reasons)


class Forecaster(Protocol):
    async def forecast(self, event: FootballEvent) -> MatchForecast | Unavailable:
        """The goal model's forecast, or why there is none. May raise UpstreamError."""


class ModelForecaster:
    """The frozen goal model on the tournament's previous and current season (shared cache)."""

    def __init__(self, sports: SportsService, engine: ForecastEngine | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.sports = sports
        self.engine = engine or ForecastEngine()
        self._clock = clock

    async def forecast(self, event: FootballEvent) -> MatchForecast | Unavailable:
        return self.compute(event, await self.sports.model_history(event.tournament_id))

    def compute(self, event: FootballEvent, history: ModelHistory | None) -> MatchForecast | Unavailable:
        """The CPU part, from a loaded history (no I/O: may run in a worker thread)."""
        home, away = football_data.team_number(event.home.id), football_data.team_number(event.away.id)
        if history is None or home is None or away is None:
            return Unavailable("small_sample")
        start = (datetime.fromisoformat(history.season_start).replace(tzinfo=UTC).timestamp()
                 if history.season_start else None)
        code = event.tournament_id.removeprefix(f"{football_data.ID_PREFIX}:")
        return self.engine.forecast(code, results_from_events(history.events), start, home, away, self._clock(),
                                    complete_history=history.previous_season,
                                    kickoff=parse_instant(event.starts_at).timestamp())


class QuotaLedger(Protocol):
    def reserve_ai_call(self, day: str, limits: dict[str, int]) -> str | None:
        """Charges every {subject: limit} atomically, or none; returns the used-up subject or None."""


@dataclass
class MemoryLedger:
    """In-process ledger (tests, live check). The app uses the Mini App store, which survives restarts."""

    used: dict[tuple[str, str], int] = field(default_factory=dict)

    def reserve_ai_call(self, day: str, limits: dict[str, int]) -> str | None:
        for subject, limit in limits.items():
            if self.used.get((day, subject), 0) >= limit:
                return subject
        for subject in limits:
            self.used[(day, subject)] = self.used.get((day, subject), 0) + 1
        return None


class AiService:
    def __init__(self, sports: SportsService, client: ModelClient | None, *, per_user_daily: int,
                 daily_budget: int, ledger: QuotaLedger | None = None,
                 clock: Callable[[], float] = time.time, forecaster: Forecaster | None = None,
                 history: ForecastHistory | None = None) -> None:
        self.sports = sports
        self.client = client
        self.forecaster = forecaster or ModelForecaster(sports, clock=clock)
        self.history = history
        self._paid_attempts: dict[str, int] = {}  # only without a history store (tests, live check)
        # Analyses whose failed paid call could not be counted: fail closed in this process.
        self._uncounted: set[str] = set()
        self._clock = clock
        self._cache = SportsCache(clock=clock, max_stale_s=0)
        self._ledger = ledger or MemoryLedger()
        self._per_user_daily = per_user_daily
        self._daily_budget = daily_budget
        # Deciding "this request starts a paid load" and starting it is one step.
        self._start_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.client is not None

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")

    def _iso(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def analyse(self, event_id: str, user_id: int, *, create: bool = True) -> AnalysisResult | None:
        """None when the event does not exist. Raises AiError / UpstreamError.

        `create=False` only reads: an answer already made (cache or stored history) or one that
        costs nothing (insufficient data, not upcoming); otherwise AiError("not_created"). It never
        reserves a limit and never calls the model, so opening a screen can never pay."""
        try:
            # Event lookup and evidence share one deadline; nothing is charged yet,
            # so a slow source fails here for free.
            found = await asyncio.wait_for(self._evidence(event_id), EVIDENCE_TIMEOUT_S)
        except TimeoutError:
            raise AiError("timeout") from None
        if found is None or isinstance(found, AnalysisResult):
            return found
        event, tournament, home, away, forecast = found
        basis = {"home": home.to_json(), "away": away.to_json()}
        # No odds source in the Mini App: verified form alone gives PARTIAL at best.
        readiness = forecast_readiness(has_odds=False, has_verified_data=has_verified_form(home, away))
        if readiness == "insufficient":
            return AnalysisResult(event.id, "insufficient", ("no_verified_form", "no_odds_source"), basis=basis)
        if isinstance(forecast, Unavailable):
            # Too little history for the goal model: no probabilities at all, never a guess.
            return AnalysisResult(event.id, "insufficient", (f"model_{forecast.reason}", "no_odds_source"),
                                  basis=basis)
        figures = forecast.to_json()
        shown = forecast.display
        probabilities = {"home": shown["1"], "draw": shown["X"], "away": shown["2"]}
        chosen = forecast.selection.pick
        market = chosen.market if chosen else "NO_BET"
        alternatives = tuple({"market": c.market, "probability": shown[c.market]}
                             for c in forecast.selection.alternatives)
        reasons_allowed = allowed_reasons(market, basis, figures)
        allowed = tuple(f for f in FACTORS if not f.endswith("_table")
                        or (home if f.startswith("home") else away).table_position is not None)
        facts = {
            "fixture": {"home": home.name, "away": away.name, "competition": tournament.name if tournament else None,
                        "kickoffUtc": event.starts_at},
            "home": basis["home"],
            "away": basis["away"],
            "model": {
                "expectedGoals": figures["expectedGoals"], "ratings": figures["ratings"],
                "homeAdvantage": figures["homeAdvantage"], "estimate1x2": probabilities,
                "recommended": market, "alternatives": [a["market"] for a in alternatives],
                "limitedData": forecast.limited, "results": forecast.sample, "dataUntil": forecast.data_until,
            },
            "allowedFactors": list(allowed),
            "allowedReasons": list(reasons_allowed),
        }
        # The identity covers everything shown, not only what the language model reads: any change
        # of a displayed percentage (e.g. a corrected old result) is a new analysis.
        shown_forecast = {"figures": {k: v for k, v in figures.items() if k != "computedAt"},
                          "exact": {k: round(v, 9) for k, v in forecast.exact.items()},
                          "selection": forecast.selection.to_json()}
        digest = hashlib.sha256(json.dumps({"facts": facts, "forecast": shown_forecast},
                                           sort_keys=True).encode()).hexdigest()[:16]
        # Language-free (the frontend renders every sentence), versioned by answer shape and model.
        key = f"ai:{SCHEMA_VERSION}:{MODEL_VERSION}:{event.id}:{digest}"
        client = self.client

        def build(factors: tuple[str, ...], reasons: tuple[str, ...], model: str | None,
                  generated_at: str) -> AnalysisResult:
            pick = Pick(market, reasons, shown[market]) if chosen else NO_BET
            return AnalysisResult(event.id, readiness, ("no_odds_source",), generated_at, model,
                                  probabilities, factors, basis, estimated=readiness == "partial",
                                  pick=pick, forecast=figures, alternatives=alternatives)

        def until_kickoff() -> float:
            return max(60.0, kickoff - self._clock())

        async def load() -> tuple[AnalysisResult, float]:
            request = f"Facts (JSON):\n{json.dumps(facts, ensure_ascii=False, sort_keys=True)}"
            assert client is not None  # obtain() refuses a new analysis without the model
            try:
                # Wall-clock bound: the SDK timeout bounds network waits, not the whole call.
                answer = await asyncio.wait_for(client.complete(SYSTEM_PROMPT, request, SCHEMA), MODEL_TIMEOUT_S)
            except TimeoutError:
                raise AiError("timeout") from None
            factors, reasons = validate_output(answer.text, frozenset(allowed), reasons_allowed,
                                               no_bet=chosen is None)
            result = build(factors, reasons, answer.model, self._iso())
            logger.info("ai.analysis event=%s readiness=%s pick=%s", event.id, readiness, result.pick.market)
            try:
                await self._remember(event, forecast, result, digest)
            except AiError:
                self._uncounted.add(key)  # paid but not storable: never paid for again in this process
                raise
            return result, until_kickoff()

        async def restore(stored: dict) -> tuple[AnalysisResult, float]:
            # The same evidence digest = the same numbers; only the stored explanation is reused.
            return build(tuple(stored["factors"]), tuple(stored["reasons"]), stored["model"],
                         stored["created_at"]), until_kickoff()

        loader = _as_upstream(load)
        prefix = f"{MODEL_VERSION}:{event.id}:{digest}:"
        kickoff = parse_instant(event.starts_at).timestamp()

        async def obtain() -> AnalysisResult | Cached | asyncio.Future:
            # One step under the lock: the cached answer, the running load, or a paid
            # reservation followed by the start of a new load — never a second cache look
            # in which an entry could expire and start an unpaid call.
            async with self._start_lock:
                if kickoff - KICKOFF_MARGIN_S <= self._clock():  # also for cached answers
                    return AnalysisResult(event.id, "insufficient", ("not_upcoming",))
                try:
                    found = self._cache.existing(key)
                except UpstreamError:
                    if create:
                        raise
                    # A remembered failure of a paid attempt is not an answer: reading goes on
                    # to the stored history and otherwise says "not created".
                    found = None
                if not create and isinstance(found, asyncio.Future):
                    # A creation still running is not an answer yet: reading never waits on a paid
                    # call (nor reports its failure). A stored answer is still read below.
                    found = None
                if found is not None:
                    return found  # joining or reading is free
                if not create:
                    if self.history is not None:
                        stored = await asyncio.to_thread(self.history.stored_answer, prefix)
                        if stored is not None:
                            # Rebuilt here, not through the cache: a paid load of the same key
                            # still in flight (single flight) must never be joined by a read.
                            restored, _ = await restore(stored)
                            return restored
                    raise AiError("not_created")
                # Charged outside the shared cache: a personal refusal is never
                # remembered as everyone's answer.
                user = f"user:{user_id}"
                limits = {user: self._per_user_daily, "all": self._daily_budget}
                # Every shown forecast must enter the history (it is how the experimental rules get
                # confirmed): an unwritable history refuses before anything is paid.
                if self.history is not None and not await asyncio.to_thread(self.history.writable):
                    raise AiError("upstream_error")
                if self.history is not None:
                    stored = await asyncio.to_thread(self.history.stored_answer, prefix)
                    if stored is not None:  # evicted or restarted: rebuilt for free, never paid again
                        return self._cache.start(key, _as_upstream(lambda: restore(stored)))
                # Only a NEW analysis needs the model: a made one (cache, history) is read without it.
                if client is None:
                    raise AiError("not_configured")
                if key in self._uncounted or await self._attempts(key) >= MAX_PAID_ATTEMPTS:
                    raise AiError("upstream_error")  # this exact analysis was already paid for twice
                # Counted durably BEFORE the call: an SDK crash or a process exit after dispatch
                # still counts. A failing count refuses before anything is paid.
                if not await self._count_attempt(key, event.starts_at):
                    raise AiError("upstream_error")
                used_up = await asyncio.to_thread(self._ledger.reserve_ai_call, self._today(), limits)
                if used_up is not None:
                    await self._uncount_attempt(key)  # no call followed
                    raise AiError("limit_reached" if used_up == user else "budget_exhausted")
                return self._cache.start(key, loader)

        # Shielded: a request cancelled mid-way still either charges AND starts, or neither.
        step = asyncio.ensure_future(obtain())
        step.add_done_callback(_retrieve)
        found = await asyncio.shield(step)
        if isinstance(found, AnalysisResult):
            return found
        if isinstance(found, Cached):
            return found.value
        return (await asyncio.shield(found)).value

    async def _evidence(self, event_id: str):
        event, tournament = await self.sports.event(event_id)
        if event is None:
            return None
        # A stale cached "upcoming" after kickoff is not upcoming: the clock decides too.
        if (not isinstance(event, FootballEvent) or event.status in NOT_ANALYSED or event.status != "upcoming"
                or parse_instant(event.starts_at).timestamp() - KICKOFF_MARGIN_S <= self._clock()):
            return AnalysisResult(event.id, "insufficient", ("not_upcoming",))
        home, away, forecast = await self._summaries(event, tournament)
        return event, tournament, home, away, forecast

    async def _attempts(self, key: str) -> int:
        if self.history is None:
            return self._paid_attempts.get(key, 0)
        return await asyncio.to_thread(self.history.attempts, key)

    async def _count_attempt(self, key: str, kickoff_at: str) -> bool:
        if self.history is None:
            self._paid_attempts[key] = self._paid_attempts.get(key, 0) + 1
            return True
        try:
            await asyncio.to_thread(self.history.add_attempt, key, kickoff_at)
        except Exception as exc:  # noqa: BLE001 — log the type only
            logger.warning("ai.attempt_count_failed type=%s", type(exc).__name__)
            return False
        return True

    async def _uncount_attempt(self, key: str) -> None:
        if self.history is None:
            self._paid_attempts[key] = max(0, self._paid_attempts.get(key, 1) - 1)
            return
        try:
            await asyncio.to_thread(self.history.remove_attempt, key)
        except Exception as exc:  # noqa: BLE001 — could not undo: fail closed for this key
            logger.warning("ai.attempt_uncount_failed type=%s", type(exc).__name__)
            self._uncounted.add(key)

    async def _remember(self, event: FootballEvent, forecast: MatchForecast, result: AnalysisResult,
                        digest: str) -> None:
        """Writes the forecast the user is about to see into the history (once per key).
        A forecast that cannot be stored is not shown: the load fails (upstream_error)."""
        if self.history is None or result.generated_at is None:
            return
        chosen = forecast.selection.pick
        record = ForecastRecord(
            # The shown answer is part of the identity: a regenerated answer for the same evidence
            # (e.g. after a restart emptied the cache) is a row of its own, never silently dropped.
            forecast_key=f"{MODEL_VERSION}:{event.id}:{digest}:{_answer_digest(result)}", event_id=event.id,
            tournament_id=event.tournament_id, home_team_id=event.home.id, away_team_id=event.away.id,
            created_at=result.generated_at, kickoff_at=event.starts_at, model_version=MODEL_VERSION,
            parameters=FROZEN.digest, data_until=forecast.data_until, sample=forecast.sample, inputs_digest=digest,
            probabilities={k: round(v, 6) for k, v in forecast.exact.items()},
            baseline={k: round(v, 6) for k, v in forecast.baseline_1x2.items()},
            gate_eligible=forecast.gate_eligible, display=forecast.display,
            expected_goals=forecast.expected_goals, pick_market=chosen.market if chosen else "NO_BET",
            pick_probability=round(chosen.probability, 6) if chosen else None,
            no_bet_reason=forecast.selection.no_bet_reason,
            alternatives=[c.to_json() for c in forecast.selection.alternatives],
            explanation={"factors": list(result.factors), "reasons": list(result.pick.reasons if result.pick else ())},
            llm_model=result.model, source=football_data.PROVIDER,
        )
        try:
            await asyncio.to_thread(self.history.record, record)
        except Exception as exc:  # noqa: BLE001 — log the type only, never the data
            logger.warning("ai.history_write_failed type=%s", type(exc).__name__)
            raise AiError("upstream_error") from None

    async def _summaries(self, event: FootballEvent, tournament: Tournament | None,
                         ) -> tuple[TeamSummary, TeamSummary, MatchForecast | Unavailable]:
        async def optional_table() -> dict:
            # The table is optional evidence: form alone still gives PARTIAL (no *_table
            # factors). Its own short deadline keeps a slow table from using up the form's.
            if not tournament:
                return {}
            try:
                return await asyncio.wait_for(self.sports.standings(event.tournament_id), STANDINGS_TIMEOUT_S)
            except UpstreamError as exc:
                logger.info("ai.standings_unavailable tournament=%s code=%s", event.tournament_id, exc.code)
            except TimeoutError:
                logger.info("ai.standings_unavailable tournament=%s code=timeout", event.tournament_id)
            return {}

        # Forms and the model's history first: they take the provider's slots before the
        # optional table, so a hanging table (its load keeps a slot after our wait ends)
        # never queues them.
        *forms, forecast, table = await asyncio.gather(
            *(self.sports.team_form(side.id) for side in (event.home, event.away)),
            self.forecaster.forecast(event), optional_table())
        return (summarize(event.home.name, forms[0], table.get(event.home.id)),
                summarize(event.away.name, forms[1], table.get(event.away.id)), forecast)


def _answer_digest(result: AnalysisResult) -> str:
    shown = {"factors": list(result.factors), "reasons": list(result.pick.reasons if result.pick else ())}
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()[:8]


def _retrieve(task: asyncio.Future) -> None:
    """Marks a shielded step's exception as seen when its request was cancelled meanwhile."""
    if not task.cancelled():
        task.exception()


def _as_upstream(load: Callable[[], Awaitable[tuple[AnalysisResult, float]]]):
    """The shared cache remembers failures of UpstreamError only; carry AiError inside it."""

    async def wrapped() -> tuple[AnalysisResult, float]:
        try:
            return await load()
        except AiError as exc:
            raise _AiUpstream(exc.code) from None

    return wrapped


class _AiUpstream(UpstreamError):
    """An AiError travelling through the cache (keeps the code)."""


class AnthropicModelClient:
    """Claude via the official SDK: structured JSON output, refusal fallback on by default."""

    def __init__(self, api_key: str, model: str, *, effort: str = "medium", timeout_s: float = MODEL_TIMEOUT_S) -> None:
        import anthropic  # local: only the AI path needs the SDK

        self._anthropic = anthropic
        # No SDK retries: one bounded attempt keeps the wait under the browser's AI timeout.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self.model = model
        self._effort = effort

    async def complete(self, system: str, user: str, schema: dict) -> Completion:
        a = self._anthropic
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.RateLimitError:
            raise AiError("rate_limited") from None
        except a.APITimeoutError:
            raise AiError("timeout") from None
        except a.APIStatusError as exc:
            logger.warning("event=ai_upstream_error status=%s", exc.status_code)
            raise AiError("upstream_error") from None
        except a.APIConnectionError:
            raise AiError("timeout") from None
        if response.stop_reason == "refusal":
            raise AiError("refused")
        if response.stop_reason == "max_tokens":
            raise AiError("bad_output")
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise AiError("bad_output")
        served = getattr(response, "model", None)
        return Completion(text, served if isinstance(served, str) and served else self.model)


AI_ERROR_CODES = frozenset({"not_configured", "limit_reached", "budget_exhausted", "refused", "bad_output",
                            "upstream_error", "timeout", "rate_limited"})


def build_ai_service(settings, sports: SportsService, *, clock: Callable[[], float] = time.time,
                     client: ModelClient | None = None, ledger: QuotaLedger | None = None,
                     forecaster: Forecaster | None = None, history: ForecastHistory | None = None) -> AiService:
    """No Anthropic key → the AI is reported "not connected"; no model is ever called."""
    if client is None and settings.anthropic_api_key:
        client = AnthropicModelClient(settings.anthropic_api_key, settings.ai_model)
    return AiService(sports, client, per_user_daily=settings.ai_per_user_daily,
                     daily_budget=settings.ai_daily_budget, ledger=ledger, clock=clock, forecaster=forecaster,
                     history=history)
