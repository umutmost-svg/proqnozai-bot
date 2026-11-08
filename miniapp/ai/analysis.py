"""AI analysis of a real upcoming match (M3.2).

Order of decisions, all in Python before any model call:
1. the event must exist and be an upcoming football match;
2. verified form of both teams must exist (readiness: READY / PARTIAL /
   INSUFFICIENT — without odds the best possible level is PARTIAL);
3. INSUFFICIENT never calls the model;
4. a model call is cached per (event, language, evidence), runs once per key at a
   time, and is charged against a per-user and a global daily budget.

The model gets only the verified numbers and returns strict JSON: three whole
percentages and 2–4 keys of the factors that weighed most, chosen from a fixed list.
It writes no free text, so it cannot attach a number to the wrong team or name data
we never gave; every sentence the user sees is a template filled with verified
numbers by the frontend. Anything outside the contract is rejected, never repaired.
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

from miniapp.sports.cache import Cached, SportsCache
from miniapp.sports.client import UpstreamError
from miniapp.sports.models import FootballEvent, Tournament
from miniapp.sports.service import SportsService, parse_instant

from .evidence import TeamSummary, has_verified_form, summarize
from .readiness import NOT_ANALYSED, forecast_readiness

logger = logging.getLogger("miniapp.ai")

ANALYSIS_TTL_S = 6 * 3600.0
# Whole request under the frontend's AI_TIMEOUT_MS = 120 s: evidence ≤ 20 s (table ≤ 8 s of
# it, in parallel with the form), quota reservation ≤ ~10 s (SQLite busy timeout), one model
# attempt ≤ 60 s.
EVIDENCE_TIMEOUT_S = 20.0
STANDINGS_TIMEOUT_S = 8.0
MODEL_TIMEOUT_S = 60.0
# Closer to kickoff than this counts as started: covers the reservation wait and the call
# itself, so no paid call can start (or finish) for a match already underway.
KICKOFF_MARGIN_S = 5 * 60.0
# Each key is rendered by the frontend from the basis numbers of that team only.
FACTORS = ("home_form", "away_form", "home_goals", "away_goals", "home_table", "away_table")

SCHEMA = {
    "type": "object",
    "properties": {
        "home_win": {"type": "integer"},
        "draw": {"type": "integer"},
        "away_win": {"type": "integer"},
        "factors": {"type": "array", "items": {"type": "string", "enum": list(FACTORS)}},
    },
    "required": ["home_win", "draw", "away_win", "factors"],
    "additionalProperties": False,
}

# Stable across requests (cache-friendly): everything that varies goes in the user turn.
SYSTEM_PROMPT = """You estimate one upcoming football match for the ProqnozAI app.

Use ONLY the facts in the JSON you are given: the fixture, each team's most recent
finished results (form string newest first: W/D/L; venues H/A; goals; points) and,
when present, their rows in the competition table. Nothing else is known: no
injuries, lineups, odds, head-to-head history or any other data. Do not assume any.

Return:
- "home_win", "draw", "away_win": whole percentages that sum to exactly 100. The
  data is limited (no odds, no lineups), so keep the estimate moderate and avoid
  extreme values unless the numbers clearly support them.
- "factors": the 2 to 4 facts that weighed most in your estimate, most important
  first, as keys: home_form / away_form (recent results and points), home_goals /
  away_goals (goals scored and conceded), home_table / away_table (table position;
  only when that team's tablePosition is present). No duplicates."""


class AiError(Exception):
    """The analysis could not be produced; ``code`` is safe to return and log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code  # not_configured | limit_reached | budget_exhausted | refused | bad_output | upstream_error | timeout | rate_limited


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

    def to_json(self) -> dict:
        return {
            "eventId": self.event_id,
            "kind": self.kind,
            "reasons": list(self.reasons),
            "generatedAt": self.generated_at,
            "model": self.model,
            "probabilities": self.probabilities,
            "factors": list(self.factors),
            "basis": self.basis,
            "estimated": self.estimated,
        }


def validate_output(text: str, allowed: frozenset[str]) -> tuple[dict, tuple[str, ...]]:
    """Strict check of the model's JSON. Nothing is clamped, renormalised or dropped."""
    try:
        data = json.loads(text)
    except ValueError:
        raise AiError("bad_output") from None
    if not isinstance(data, dict) or set(data) != set(SCHEMA["required"]):
        raise AiError("bad_output")
    numbers = [data["home_win"], data["draw"], data["away_win"]]
    if any(not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 100 for n in numbers) or sum(numbers) != 100:
        raise AiError("bad_output")
    factors = data["factors"]
    if not isinstance(factors, list) or not 2 <= len(factors) <= 4:
        raise AiError("bad_output")
    if any(not isinstance(f, str) or f not in allowed for f in factors) or len(set(factors)) != len(factors):
        raise AiError("bad_output")
    probabilities = {"home": numbers[0], "draw": numbers[1], "away": numbers[2]}
    return probabilities, tuple(factors)


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
                 clock: Callable[[], float] = time.time) -> None:
        self.sports = sports
        self.client = client
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

    async def analyse(self, event_id: str, user_id: int) -> AnalysisResult | None:
        """None when the event does not exist. Raises AiError / UpstreamError."""
        try:
            # Event lookup and evidence share one deadline; nothing is charged yet,
            # so a slow source fails here for free.
            found = await asyncio.wait_for(self._evidence(event_id), EVIDENCE_TIMEOUT_S)
        except TimeoutError:
            raise AiError("timeout") from None
        if found is None or isinstance(found, AnalysisResult):
            return found
        event, tournament, home, away = found
        basis = {"home": home.to_json(), "away": away.to_json()}
        # No odds source in the Mini App: verified form alone gives PARTIAL at best.
        readiness = forecast_readiness(has_odds=False, has_verified_data=has_verified_form(home, away))
        if readiness == "insufficient":
            return AnalysisResult(event.id, "insufficient", ("no_verified_form", "no_odds_source"), basis=basis)
        if self.client is None:
            raise AiError("not_configured")
        facts = {
            "fixture": {"home": home.name, "away": away.name, "competition": tournament.name if tournament else None,
                        "kickoffUtc": event.starts_at},
            "home": basis["home"],
            "away": basis["away"],
        }
        digest = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:16]
        key = f"ai:{event.id}:{digest}"  # language-free: the frontend renders the factors
        allowed = frozenset(f for f in FACTORS if not f.endswith("_table")
                            or (home if f.startswith("home") else away).table_position is not None)
        client = self.client

        async def load() -> tuple[AnalysisResult, float]:
            request = f"Facts (JSON):\n{json.dumps(facts, ensure_ascii=False, sort_keys=True)}"
            try:
                # Wall-clock bound: the SDK timeout bounds network waits, not the whole call.
                answer = await asyncio.wait_for(client.complete(SYSTEM_PROMPT, request, SCHEMA), MODEL_TIMEOUT_S)
            except TimeoutError:
                raise AiError("timeout") from None
            probabilities, factors = validate_output(answer.text, allowed)
            logger.info("ai.analysis event=%s readiness=%s", event.id, readiness)
            return AnalysisResult(event.id, readiness, ("no_odds_source",), self._iso(), answer.model,
                                  probabilities, factors, basis, estimated=readiness == "partial"), ANALYSIS_TTL_S

        loader = _as_upstream(load)
        kickoff = parse_instant(event.starts_at).timestamp()

        async def obtain() -> AnalysisResult | Cached | asyncio.Future:
            # One step under the lock: the cached answer, the running load, or a paid
            # reservation followed by the start of a new load — never a second cache look
            # in which an entry could expire and start an unpaid call.
            async with self._start_lock:
                if kickoff - KICKOFF_MARGIN_S <= self._clock():  # also for cached answers
                    return AnalysisResult(event.id, "insufficient", ("not_upcoming",))
                found = self._cache.existing(key)
                if found is not None:
                    return found  # joining or reading is free
                # Charged outside the shared cache: a personal refusal is never
                # remembered as everyone's answer.
                user = f"user:{user_id}"
                limits = {user: self._per_user_daily, "all": self._daily_budget}
                used_up = await asyncio.to_thread(self._ledger.reserve_ai_call, self._today(), limits)
                if used_up is not None:
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
        home, away = await self._summaries(event, tournament)
        return event, tournament, home, away

    async def _summaries(self, event: FootballEvent, tournament: Tournament | None) -> tuple[TeamSummary, TeamSummary]:
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

        # Forms first: they take the provider's two slots before the optional table, so a
        # hanging table (its load keeps a slot after our wait ends) never queues them.
        *forms, table = await asyncio.gather(
            *(self.sports.team_form(side.id) for side in (event.home, event.away)), optional_table())
        return (summarize(event.home.name, forms[0], table.get(event.home.id)),
                summarize(event.away.name, forms[1], table.get(event.away.id)))


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
                     client: ModelClient | None = None, ledger: QuotaLedger | None = None) -> AiService:
    """No Anthropic key → the AI is reported "not connected"; no model is ever called."""
    if client is None and settings.anthropic_api_key:
        client = AnthropicModelClient(settings.anthropic_api_key, settings.ai_model)
    return AiService(sports, client, per_user_daily=settings.ai_per_user_daily,
                     daily_budget=settings.ai_daily_budget, ledger=ledger, clock=clock)
