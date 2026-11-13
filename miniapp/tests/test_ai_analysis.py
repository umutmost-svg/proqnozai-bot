"""AI analysis (M3.2, v3 since M3.5): offline — the provider, the goal model and the language model are fakes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from miniapp.ai.analysis import (
    SCHEMA,
    SYSTEM_PROMPT,
    AiError,
    AiService,
    AnthropicModelClient,
    Completion,
    FACTORS,
    MemoryLedger,
    build_ai_service,
    validate_output,
)
from miniapp.store import Store
from miniapp.app import create_app
from miniapp.settings import Settings, SettingsError
from miniapp.sports.cache import SportsCache
from miniapp.sports.football_data import FootballDataAdapter, normalize_form, normalize_standings
from miniapp.sports.service import SportsService, build_service
from miniapp.forecast.engine import Unavailable
from miniapp.forecast.history import ForecastHistory
from miniapp.tests.conftest import ORIGIN, FakeClock, fresh, login
from miniapp.tests.forecast_fakes import FakeForecaster, fake_forecast
from miniapp.tests.signing import TEST_BOT_TOKEN
from miniapp.tests.test_sports import DAY, KEY, FakeTransport, make_client, match, no_sleep, run

GOOD = json.dumps({"factors": ["home_form", "away_table"], "reasons": []})
SHOWN = fake_forecast().display  # the fake goal model's whole percents (1 51 · X 25 · 2 24)


def answer(*factors: str, reasons: tuple[str, ...] = ()) -> str:
    return json.dumps({"factors": list(factors), "reasons": list(reasons)})
ALL = frozenset(FACTORS)


class FakeModel:
    model = "claude-opus-5-5"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers) or [GOOD]
        self.calls: list[tuple[str, str, dict]] = []

    async def complete(self, system: str, user: str, schema: dict) -> Completion:
        self.calls.append((system, user, schema))
        return Completion(self.answers.pop(0) if len(self.answers) > 1 else self.answers[0], self.model)


def finished(mid: int, home: tuple, away: tuple, score: tuple, day: int) -> dict:
    return {"id": mid, "status": "FINISHED", "utcDate": f"2027-01-{day:02d}T15:00:00Z",
            "competition": {"code": "PL"}, "homeTeam": {"id": home[0], "name": home[1]},
            "awayTeam": {"id": away[0], "name": away[1]}, "score": {"fullTime": {"home": score[0], "away": score[1]}}}


ARSENAL, CHELSEA = (57, "Arsenal FC"), (61, "Chelsea FC")


def arsenal_form() -> dict:  # newest first after sorting: W W D W W → 13 pts, 9:3
    return {"matches": [
        finished(101, ARSENAL, (1, "A"), (2, 0), 13), finished(102, (2, "B"), ARSENAL, (1, 3), 11),
        finished(103, ARSENAL, (3, "C"), (1, 1), 9), finished(104, (4, "D"), ARSENAL, (0, 2), 7),
        finished(105, ARSENAL, (5, "E"), (1, 0), 5),
        {"id": 106, "status": "SCHEDULED", "utcDate": "2027-01-20T15:00:00Z"},  # not played: ignored
    ]}


def chelsea_form(n: int = 5) -> dict:
    games = [finished(200 + i, CHELSEA, (10 + i, f"T{i}"), (1, 1), 13 - i) for i in range(n)]
    return {"matches": games}


def standings() -> dict:
    row = lambda pos, team, pts: {"position": pos, "team": {"id": team[0], "name": team[1]}, "playedGames": 20,  # noqa: E731
                                  "won": 10, "draw": 5, "lost": 5, "points": pts, "goalsFor": 30, "goalsAgainst": 20}
    return {"competition": {"code": "PL"},
            "standings": [{"type": "HOME", "table": []}, {"type": "TOTAL", "table": [row(2, ARSENAL, 41), row(7, CHELSEA, 33)]}]}


def make_ai(clock: FakeClock, model: FakeModel | None, *, per_user: int = 5, budget: int = 50, chelsea_games: int = 5,
            status: str = "TIMED", ledger=None, table=None, forecaster=None, history=None):
    t = FakeTransport()
    t.script = [t.ok(match(7, status)), t.ok(arsenal_form()), t.ok(chelsea_form(chelsea_games)), table or t.ok(standings())]
    adapter = FootballDataAdapter(make_client(t, clock), ("PL", "PD"), clock)
    sports = SportsService(adapter, cache=SportsCache(clock=clock), clock=clock)
    sports._known_ids.add("fd:7")
    return AiService(sports, model, per_user_daily=per_user, daily_budget=budget, ledger=ledger, clock=clock,
                     forecaster=forecaster or FakeForecaster(), history=history), t


# --- verified data -------------------------------------------------------------------

def test_form_is_from_the_teams_point_of_view_newest_first_finished_only() -> None:
    form = normalize_form(arsenal_form()["matches"], 57)
    assert [m.result for m in form] == ["W", "W", "D", "W", "W"]
    assert [(m.venue, m.goals_for, m.goals_against) for m in form[:2]] == [("home", 2, 0), ("away", 3, 1)]
    with pytest.raises(Exception):
        normalize_form("nope", 57)


def test_standings_use_the_total_table() -> None:
    rows = normalize_standings(standings())
    assert rows["fd:team:57"].position == 2 and rows["fd:team:61"].points == 33


# --- readiness and the model call ----------------------------------------------------------

def test_partial_analysis_from_verified_form_with_python_computed_basis(clock: FakeClock) -> None:
    model = FakeModel()
    ai, t = make_ai(clock, model)
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "partial" and result.estimated is True  # no odds → estimated probabilities
    # Every probability is the goal model's, never the language model's.
    assert result.probabilities == {"home": SHOWN["1"], "draw": SHOWN["X"], "away": SHOWN["2"]}
    assert result.forecast["markets"] == SHOWN and result.pick.market == "NO_BET"
    assert result.reasons == ("no_odds_source",)
    home = result.basis["home"]
    assert (home["form"], home["points"], home["goalsFor"], home["goalsAgainst"], home["tablePosition"]) == ("WWDWW", 13, 9, 2, 2)
    assert result.basis["away"]["form"] == "DDDDD" and result.basis["away"]["points"] == 5
    system, user, schema = model.calls[0]
    assert system == SYSTEM_PROMPT and schema == SCHEMA and "Language" not in user
    assert result.factors == ("home_form", "away_table")
    facts = json.loads(user.split("Facts (JSON):\n", 1)[1])
    assert facts["home"]["form"] == "WWDWW" and facts["fixture"]["kickoffUtc"] == f"{DAY}T15:00:00Z"
    assert facts["model"]["estimate1x2"] == result.probabilities and facts["model"]["recommended"] == "NO_BET"
    assert facts["allowedReasons"] == [] and "away_table" in facts["allowedFactors"]
    assert not {"odds", "injuries", "lineups"} & set(json.dumps(facts).lower().split('"'))


def test_cached_analysis_costs_no_new_model_call_or_user_budget(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model, per_user=1)
    first = run(ai.analyse("fd:7", user_id=1))
    again = run(ai.analyse("fd:7", user_id=1))  # user 1 is at the limit: a cache hit is free
    other = run(ai.analyse("fd:7", user_id=2))
    assert first == again == other and len(model.calls) == 1


def test_insufficient_form_never_calls_the_model(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model, chelsea_games=2)
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "insufficient" and result.probabilities is None and model.calls == []
    assert set(result.reasons) == {"no_verified_form", "no_odds_source"}


@pytest.mark.parametrize("status", ["FINISHED", "IN_PLAY", "POSTPONED", "CANCELLED", "SUSPENDED"])
def test_only_upcoming_matches_are_analysed(clock: FakeClock, status: str) -> None:
    model = FakeModel()
    ai, t = make_ai(clock, model, status=status)
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "insufficient" and result.reasons == ("not_upcoming",)
    assert model.calls == [] and len(t.calls) == 1  # no form or table requested either


def test_without_an_anthropic_key_a_ready_match_is_an_error_not_a_demo(clock: FakeClock) -> None:
    ai, _ = make_ai(clock, None)
    with pytest.raises(AiError) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert exc.value.code == "not_configured"


def test_unknown_event_is_none(clock: FakeClock) -> None:
    ai, t = make_ai(clock, FakeModel())
    assert run(ai.analyse("not-an-id", user_id=1)) is None and t.calls == []


@pytest.mark.parametrize("bad", [
    json.dumps({"factors": ["home_form"], "reasons": []}),  # one factor
    json.dumps({"factors": ["home_form", "home_form"], "reasons": []}),  # duplicate
    json.dumps({"factors": ["home_form", "injuries"], "reasons": []}),  # not in the list
    json.dumps({"factors": ["home_form", "away_form"], "reasons": [], "summary": "x"}),  # extra key
    json.dumps({"factors": ["home_form", "away_form"]}),  # missing key
    json.dumps({"factors": ["home_form", "away_form"], "reasons": [], "home_win": 50}),  # a number of its own
    json.dumps({"factors": ["home_form", "away_form"], "reasons": ["home_better_form"]}),  # not offered
    "not json",
])
def test_model_output_outside_the_contract_is_rejected_not_repaired(bad: str) -> None:
    with pytest.raises(AiError) as exc:
        validate_output(bad, ALL)
    assert exc.value.code == "bad_output"


def test_malformed_factor_items_are_bad_output_not_a_crash() -> None:
    for factors in ([["home_form"], "away_form"], [{"k": 1}, "away_form"], [None, "home_form"]):
        with pytest.raises(AiError) as exc:
            validate_output(json.dumps({"factors": factors, "reasons": []}), ALL)
        assert exc.value.code == "bad_output"


def test_reasons_must_be_offered_unique_at_most_three_and_absent_for_no_bet() -> None:
    offered = ("home_better_form", "home_more_goals", "home_stronger_rating", "home_advantage")
    ok = answer("home_form", "home_rating", reasons=("home_stronger_rating", "home_better_form"))
    assert validate_output(ok, ALL, offered) == (("home_form", "home_rating"), ("home_stronger_rating", "home_better_form"))
    for reasons in (("home_better_form",) * 2, offered, ("away_better_form",)):
        with pytest.raises(AiError):
            validate_output(answer("home_form", "away_form", reasons=reasons), ALL, offered)
    with pytest.raises(AiError):  # NO_BET: nothing to argue for
        validate_output(answer("home_form", "away_form", reasons=("home_better_form",)), ALL, offered, no_bet=True)


def test_a_match_past_kickoff_is_never_analysed_even_if_still_listed_upcoming(clock: FakeClock) -> None:
    ledger = MemoryLedger()
    model = FakeModel()
    ai, _ = make_ai(clock, model, ledger=ledger)
    clock.advance(7 * 3600 + 1)  # 15:00:01 UTC: kickoff passed, the source still says TIMED
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "insufficient" and result.reasons == ("not_upcoming",)
    assert not model.calls and not ledger.used


def test_kickoff_passing_during_evidence_collection_charges_nothing(clock: FakeClock) -> None:
    ledger = MemoryLedger()
    model = FakeModel()
    ai, _ = make_ai(clock, model, ledger=ledger)
    real_form = ai.sports.team_form

    async def form_arriving_after_kickoff(team_id):
        result = await real_form(team_id)
        clock.now = clock.now + 7 * 3600 + 1  # the provider answered after 15:00 UTC
        return result

    ai.sports.team_form = form_arriving_after_kickoff  # type: ignore[method-assign]
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.reasons == ("not_upcoming",) and not model.calls and not ledger.used


def test_a_cancelled_request_still_charges_and_starts_together(clock: FakeClock) -> None:
    ledger = MemoryLedger()
    model = FakeModel()
    ai, _ = make_ai(clock, model, per_user=1, ledger=ledger)

    async def scenario():
        task = asyncio.ensure_future(ai.analyse("fd:7", user_id=1))
        for _ in range(200):  # until the reservation is made
            await asyncio.sleep(0)
            if ledger.used:
                break
        task.cancel()
        await asyncio.sleep(0.05)  # the shielded load finishes on its own
        return await ai.analyse("fd:7", user_id=1)  # the user's paid call is there, free

    result = run(scenario())
    assert result.kind == "partial" and len(model.calls) == 1
    assert ledger.used[(ai._today(), "user:1")] == 1


def test_a_cached_pre_match_answer_is_not_served_once_kickoff_passed(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model)
    assert run(ai.analyse("fd:7", user_id=1)).kind == "partial"
    real_table = ai.sports.standings

    async def table_arriving_after_kickoff(tournament_id):
        result = await real_table(tournament_id)  # served from the sports cache
        clock.now = clock.now + 7 * 3600 + 1  # the last evidence request: kickoff passes now
        return result

    ai.sports.standings = table_arriving_after_kickoff  # type: ignore[method-assign]
    assert run(ai.analyse("fd:7", user_id=2)).reasons == ("not_upcoming",)
    assert len(model.calls) == 1


def test_an_entry_expiring_between_decision_and_read_never_starts_an_unpaid_call(clock: FakeClock) -> None:
    ledger = MemoryLedger()
    model = FakeModel()
    ai, _ = make_ai(clock, model, budget=1, ledger=ledger)
    run(ai.analyse("fd:7", user_id=1))
    real_existing = ai._cache.existing

    def existing_then_expire(key):
        found = real_existing(key)
        ai._cache._entries.clear()  # evicted right after the free-read decision
        return found

    ai._cache.existing = existing_then_expire  # type: ignore[method-assign]
    assert run(ai.analyse("fd:7", user_id=2)).kind == "partial"  # served from what was found
    assert len(model.calls) == 1 and ledger.used.get((ai._today(), "user:2"), 0) == 0


def test_a_ledger_failure_after_cancellation_is_retrieved(clock: FakeClock, caplog) -> None:
    class BrokenLedger(MemoryLedger):
        def reserve_ai_call(self, day, limits):
            import time as _t
            _t.sleep(0.02)
            raise RuntimeError("disk")

    ai, _ = make_ai(clock, FakeModel(), ledger=BrokenLedger())

    async def scenario():
        task = asyncio.ensure_future(ai.analyse("fd:7", user_id=1))
        await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.1)

    run(scenario())
    assert "never retrieved" not in caplog.text


def test_without_the_table_the_form_alone_still_gives_a_partial_estimate(clock: FakeClock) -> None:
    from miniapp.sports.client import HttpResponse

    ai, _ = make_ai(clock, FakeModel(answer("home_form", "away_form")), table=HttpResponse(403, {}, b""))
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "partial" and result.basis["home"]["tablePosition"] is None
    ai2, _ = make_ai(clock, FakeModel(answer("home_form", "home_table")), table=HttpResponse(403, {}, b""))
    with pytest.raises(Exception) as exc:  # a table factor without a table row is off-contract
        run(ai2.analyse("fd:7", user_id=1))
    assert getattr(exc.value, "code", None) == "bad_output"


def test_a_slow_table_does_not_use_up_the_forms_deadline(clock: FakeClock, monkeypatch) -> None:
    import miniapp.ai.analysis as analysis

    monkeypatch.setattr(analysis, "STANDINGS_TIMEOUT_S", 0.01)
    ai, t = make_ai(clock, FakeModel(answer("home_form", "away_form")))

    async def hanging_table(_tournament_id):
        await asyncio.sleep(5)

    ai.sports.standings = hanging_table  # type: ignore[method-assign]
    t.script.pop()  # the table is never fetched from the transport here
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "partial" and result.basis["away"]["tablePosition"] is None


def test_both_forms_are_requested_before_the_optional_table(clock: FakeClock) -> None:
    t = FakeTransport()
    # Scripted in the order the provider must be asked: event, both forms, then the table.
    t.script = [t.ok(match(7)), t.ok(arsenal_form()), t.ok(chelsea_form()), t.ok(standings())]
    adapter = FootballDataAdapter(make_client(t, clock), ("PL", "PD"), clock)
    sports = SportsService(adapter, cache=SportsCache(clock=clock), clock=clock)
    sports._known_ids.add("fd:7")
    ai = AiService(sports, FakeModel(), per_user_daily=5, daily_budget=50, clock=clock, forecaster=FakeForecaster())
    assert run(ai.analyse("fd:7", user_id=1)).basis["home"]["tablePosition"] == 2
    paths = [url.split("/v4/", 1)[1].split("?")[0] for url, _ in t.calls]
    assert paths[1:] == ["teams/57/matches", "teams/61/matches", "competitions/PL/standings"]


def test_minutes_before_kickoff_count_as_started(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model)
    clock.advance(7 * 3600 - 4 * 60)  # 14:56 UTC, kickoff 15:00: a paid call could end after it
    assert run(ai.analyse("fd:7", user_id=1)).reasons == ("not_upcoming",) and not model.calls


def test_a_model_call_that_hangs_is_cut_at_the_wall_clock_limit(clock: FakeClock, monkeypatch) -> None:
    import miniapp.ai.analysis as analysis

    class Hanging(FakeModel):
        async def complete(self, system: str, user: str, schema: dict) -> Completion:
            await asyncio.sleep(5)
            return Completion(GOOD, self.model)

    monkeypatch.setattr(analysis, "MODEL_TIMEOUT_S", 0.01)
    ai, _ = make_ai(clock, Hanging())
    with pytest.raises(Exception) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert getattr(exc.value, "code", None) == "timeout"


def test_a_table_factor_needs_that_teams_table_row() -> None:
    text = answer("home_form", "away_table")
    with pytest.raises(AiError):
        validate_output(text, ALL - {"away_table"})
    assert validate_output(text, ALL) == (("home_form", "away_table"), ())


def new_analysis(ai: AiService, clock: FakeClock) -> None:
    """Forget cached analyses (as when the facts change), keeping the cached sports data."""
    ai._cache = SportsCache(clock=clock, max_stale_s=0)


def test_personal_limit_applies_per_user_and_is_never_shared(clock: FakeClock) -> None:
    model = FakeModel()
    ai, t = make_ai(clock, model, per_user=1)
    run(ai.analyse("fd:7", user_id=1))
    new_analysis(ai, clock)  # a new analysis for the same user: over the limit
    with pytest.raises(AiError) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert exc.value.code == "limit_reached"
    assert run(ai.analyse("fd:7", user_id=2)).kind == "partial"  # someone else is not blocked
    assert len(model.calls) == 2


def test_global_daily_budget(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model, budget=1)
    run(ai.analyse("fd:7", user_id=1))
    new_analysis(ai, clock)
    with pytest.raises(Exception) as exc:
        run(ai.analyse("fd:7", user_id=2))
    assert getattr(exc.value, "code", None) == "budget_exhausted" and len(model.calls) == 1


def test_joining_a_running_analysis_does_not_charge_the_personal_limit(clock: FakeClock) -> None:
    class SlowModel(FakeModel):
        async def complete(self, system: str, user: str, schema: dict) -> Completion:
            await asyncio.sleep(0.01)  # both requests arrive while this one call runs
            return await super().complete(system, user, schema)

    model = SlowModel()
    ai, _ = make_ai(clock, model, per_user=1)

    async def both():
        return await asyncio.gather(ai.analyse("fd:7", user_id=1), ai.analyse("fd:7", user_id=1))

    first, second = run(both())
    assert first == second and len(model.calls) == 1


def test_daily_limits_survive_a_restart(tmp_path, clock: FakeClock) -> None:
    store = Store(tmp_path / "miniapp.db")
    store.init_schema()
    ai, _ = make_ai(clock, FakeModel(), per_user=1, budget=2, ledger=store)
    run(ai.analyse("fd:7", user_id=1))
    restarted, _ = make_ai(clock, FakeModel(), per_user=1, budget=2, ledger=store)  # new process, same DB
    with pytest.raises(AiError) as exc:
        run(restarted.analyse("fd:7", user_id=1))
    assert exc.value.code == "limit_reached"
    run(restarted.analyse("fd:7", user_id=2))  # the 2nd paid call of the day
    again, _ = make_ai(clock, FakeModel(), per_user=1, budget=2, ledger=store)
    with pytest.raises(Exception) as exc2:
        run(again.analyse("fd:7", user_id=3))
    assert getattr(exc2.value, "code", None) == "budget_exhausted"


def test_a_failing_ledger_gives_the_attempt_back(clock: FakeClock) -> None:
    import sqlite3

    class LockedOnce(MemoryLedger):
        fails = 3

        def reserve_ai_call(self, day: str, limits: dict[str, int]) -> str | None:
            if self.fails:
                self.fails -= 1
                raise sqlite3.OperationalError("database is locked")
            return super().reserve_ai_call(day, limits)

    model = FakeModel()
    ai, _ = make_ai(clock, model, ledger=LockedOnce())
    for _ in range(3):  # more failures than MAX_PAID_ATTEMPTS: none of them may use up an attempt
        with pytest.raises(AiError) as exc:
            run(ai.analyse("fd:7", user_id=1))
        assert exc.value.code == "upstream_error"
    assert model.calls == []
    run(ai.analyse("fd:7", user_id=1))  # the ledger works again: the analysis is still possible
    assert len(model.calls) == 1


def test_quota_ledger_counts_per_day_and_drops_old_days(tmp_path) -> None:
    store = Store(tmp_path / "miniapp.db")
    store.init_schema()
    limits = {"user:1": 1, "all": 5}
    assert store.reserve_ai_call("2027-01-01", limits) is None
    assert store.reserve_ai_call("2027-01-01", limits) == "user:1"
    assert store.reserve_ai_call("2027-01-02", limits) is None  # a new UTC day
    with store._connect() as conn:
        assert conn.execute("SELECT DISTINCT day FROM ai_usage").fetchall() == [("2027-01-02",)]
    # All or nothing: a used-up global budget charges no personal unit either.
    assert store.reserve_ai_call("2027-01-02", {"user:2": 3, "all": 1}) == "all"
    with store._connect() as conn:
        assert conn.execute("SELECT used FROM ai_usage WHERE subject = 'user:2'").fetchone() is None
    memory = MemoryLedger()
    assert memory.reserve_ai_call("d", {"all": 1}) is None and memory.reserve_ai_call("d", {"all": 1}) == "all"


def test_global_budget_refusal_keeps_the_personal_allowance(clock: FakeClock) -> None:
    ledger = MemoryLedger()
    ai, _ = make_ai(clock, FakeModel(), per_user=1, budget=1, ledger=ledger)
    run(ai.analyse("fd:7", user_id=1))
    new_analysis(ai, clock)
    with pytest.raises(AiError) as exc:
        run(ai.analyse("fd:7", user_id=2))
    assert exc.value.code == "budget_exhausted"
    assert ledger.used.get((ai._today(), "user:2"), 0) == 0  # not charged: no call was made for user 2


def test_a_slow_source_times_out_before_anything_is_charged(clock: FakeClock, monkeypatch) -> None:
    import miniapp.ai.analysis as analysis

    monkeypatch.setattr(analysis, "EVIDENCE_TIMEOUT_S", 0.01)
    ledger = MemoryLedger()
    model = FakeModel()
    ai, t = make_ai(clock, model, ledger=ledger)

    async def slow(_tournament_id):
        await asyncio.sleep(1)

    ai.sports.standings = slow  # type: ignore[method-assign]
    t.script.pop()  # the table is never fetched from the transport here
    with pytest.raises(AiError) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert exc.value.code == "timeout" and not model.calls and not ledger.used


def test_a_slow_event_lookup_shares_the_same_free_deadline(clock: FakeClock, monkeypatch) -> None:
    import miniapp.ai.analysis as analysis

    monkeypatch.setattr(analysis, "EVIDENCE_TIMEOUT_S", 0.01)
    ledger = MemoryLedger()
    ai, _ = make_ai(clock, FakeModel(), ledger=ledger)

    async def slow_event(_event_id):
        await asyncio.sleep(1)

    ai.sports.event = slow_event  # type: ignore[method-assign]
    with pytest.raises(AiError) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert exc.value.code == "timeout" and not ledger.used


def test_restoring_a_backup_never_gives_back_spent_ai_calls(tmp_path) -> None:
    from miniapp.store import BurnWindow

    live = Store(tmp_path / "live.db")
    live.init_schema()
    backup_store = Store(tmp_path / "old.db")
    backup_store.init_schema()
    for _ in range(3):
        backup_store.reserve_ai_call("2027-01-15", {"all": 10})
    live.reserve_ai_call("2027-01-15", {"all": 10})
    backup = tmp_path / "backup.db"
    backup_store.backup(backup)
    live.restore_from(backup, 1_800_000_000, BurnWindow(0, 1_800_000_300))
    with live._connect() as conn:
        assert conn.execute("SELECT used FROM ai_usage WHERE subject = 'all'").fetchone() == (3,)


# --- the Claude client (official SDK, faked transport) --------------------------------------

class _Block:
    def __init__(self, text: str) -> None:
        self.type, self.text = "text", text


class _Response:
    def __init__(self, stop_reason: str, text: str | None, model: str = "claude-opus-5-5") -> None:
        self.stop_reason, self.model = stop_reason, model
        self.content = [_Block(text)] if text is not None else []


def anthropic_client_with(response) -> tuple[AnthropicModelClient, list]:
    client = AnthropicModelClient("sk-ant-test-0000000000000000", "claude-opus-5-5")
    sent: list[dict] = []

    async def create(**kwargs):
        sent.append(kwargs)
        if isinstance(response, Exception):
            raise response
        return response

    client._client.beta.messages.create = create  # type: ignore[method-assign]
    return client, sent


def test_claude_request_uses_structured_output_effort_and_refusal_fallback() -> None:
    client, sent = anthropic_client_with(_Response("end_turn", GOOD))
    assert run(client.complete(SYSTEM_PROMPT, "facts", SCHEMA)) == Completion(GOOD, "claude-opus-5-5")
    kw = sent[0]
    assert kw["model"] == "claude-opus-5-5" and kw["system"] == SYSTEM_PROMPT
    assert kw["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw and "thinking" not in kw
    assert client._client.max_retries == 0 and client._client.timeout == 60.0  # bounded under the browser's 90 s


def test_a_fallback_answer_reports_the_model_that_served_it(clock: FakeClock) -> None:
    client, _ = anthropic_client_with(_Response("end_turn", GOOD, model="claude-sonnet-5-5"))
    assert run(client.complete(SYSTEM_PROMPT, "facts", SCHEMA)).model == "claude-sonnet-5-5"

    class FallbackModel(FakeModel):
        async def complete(self, system: str, user: str, schema: dict) -> Completion:
            answer = await super().complete(system, user, schema)
            return Completion(answer.text, "claude-sonnet-5-5")

    ai, _ = make_ai(clock, FallbackModel())
    assert run(ai.analyse("fd:7", user_id=1)).model == "claude-sonnet-5-5"


@pytest.mark.parametrize(("response", "code"), [
    (_Response("refusal", None), "refused"),
    (_Response("max_tokens", '{"home_win": 4'), "bad_output"),
    (_Response("end_turn", None), "bad_output"),
])
def test_claude_stop_reasons_are_errors_not_results(response, code: str) -> None:
    client, _ = anthropic_client_with(response)
    with pytest.raises(AiError) as exc:
        run(client.complete(SYSTEM_PROMPT, "facts", SCHEMA))
    assert exc.value.code == code


def test_claude_connection_failure_is_a_timeout_error() -> None:
    import anthropic
    import httpx2

    client, _ = anthropic_client_with(anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")))
    with pytest.raises(AiError) as exc:
        run(client.complete(SYSTEM_PROMPT, "facts", SCHEMA))
    assert exc.value.code == "timeout"


# --- settings and HTTP API -------------------------------------------------------------------

def base_settings(tmp_path, **kw) -> Settings:
    return Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "m.db", trusted_origins=frozenset({ORIGIN}),
                    environment="development", session_ttl_s=3600, **kw)


def test_ai_settings(tmp_path) -> None:
    secret = "sk-ant-secret-value-not-in-repr-1234"
    s = base_settings(tmp_path, anthropic_api_key=secret)
    assert secret not in repr(s) and s.ai_model == "claude-opus-5-5"
    for bad in ({"ai_model": "gpt-4o"}, {"ai_model": "claude opus"}, {"anthropic_api_key": "short"},
                {"ai_per_user_daily": 0}, {"ai_daily_budget": 0}):
        with pytest.raises(SettingsError):
            base_settings(tmp_path, **bad)
    env = {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
           "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development", "ANTHROPIC_API_KEY": secret}
    assert Settings.from_env(env).anthropic_api_key is None  # the bot's variable is never read


@pytest.fixture()
def api(tmp_path, clock: FakeClock):
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY)
    sports = build_service(settings, clock=clock, transport=t)
    sports.football.client._sleep = no_sleep
    model = FakeModel()
    ai = build_ai_service(settings, sports, clock=clock, client=model, forecaster=FakeForecaster())
    client = TestClient(create_app(settings, clock=clock, sports=sports, ai=ai), base_url="http://127.0.0.1:8000")
    return client, t, model


def post(client: TestClient, body: dict, csrf: str | None, origin: str | None = ORIGIN):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return client.post("/api/ai/analysis", content=json.dumps(body), headers=headers)


def test_ai_endpoint_requires_origin_session_and_csrf(api, clock: FakeClock) -> None:
    client, t, model = api
    body = {"event_id": "fd:7"}
    assert post(client, body, None).status_code == 401
    csrf = login(client, fresh(clock)).json()["session"]["csrf_token"]
    assert post(client, body, csrf, origin="https://evil.example").status_code == 403
    assert post(client, body, None).status_code == 403
    assert post(client, {}, csrf).status_code == 400
    assert post(client, {"event_id": "fd:7", "lang": "ru"}, csrf).status_code == 400  # exact key set
    assert post(client, {"event_id": 7}, csrf).status_code == 400
    assert t.calls == [] and model.calls == []


def test_ai_endpoint_returns_the_analysis(api, clock: FakeClock) -> None:
    client, t, model = api
    csrf = login(client, fresh(clock)).json()["session"]["csrf_token"]
    t.script = [t.ok(match(7)), t.ok(arsenal_form()), t.ok(chelsea_form()), t.ok(standings())]
    r = post(client, {"event_id": "fd:7"}, csrf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "partial" and body["estimated"] is True and body["probabilities"]["home"] == SHOWN["1"]
    assert body["schema"] == "v3" and body["forecast"]["markets"]["O2.5"] == SHOWN["O2.5"]
    assert body["alternatives"] == [] and body["forecast"]["version"] == "goals-1"
    assert body["factors"] == ["home_form", "away_table"] and "summary" not in body
    assert body["pick"] == {"market": "NO_BET", "reasons": [], "probability": None}
    assert KEY not in r.text


def test_ai_status_is_reported_with_sports_data(tmp_path, clock: FakeClock) -> None:
    settings = base_settings(tmp_path, football_data_key=KEY)
    client = TestClient(create_app(settings, clock=clock), base_url="http://127.0.0.1:8000")
    login(client, fresh(clock))
    assert client.get("/api/sports/coverage").json()["ai"] == {"status": "not_connected"}


def test_the_models_pick_reaches_the_result_with_only_true_supporting_reasons(clock: FakeClock) -> None:
    forecaster = FakeForecaster(fake_forecast("1X", alternatives=("H2+1.5",)))
    model = FakeModel(answer("home_form", "home_rating", reasons=("home_stronger_rating", "home_better_form")))
    ai, _ = make_ai(clock, model, forecaster=forecaster)
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.pick.to_json() == {"market": "1X", "reasons": ["home_stronger_rating", "home_better_form"],
                                     "probability": SHOWN["1X"]}
    assert result.alternatives == ({"market": "H2+1.5", "probability": SHOWN["H2+1.5"]},)
    facts = json.loads(model.calls[0][1].split("Facts (JSON):\n", 1)[1])
    # Offered: true for these numbers and on the home side only (Arsenal 13 pts, 2nd vs Chelsea 5 pts, 7th).
    assert facts["allowedReasons"] == ["home_better_form", "home_higher_table", "home_more_goals",
                                       "home_fewer_conceded", "home_stronger_rating", "home_advantage"]
    assert all(k.startswith("ai:v3:goals-1:") for k in ai._cache._entries)
    again = run(ai.analyse("fd:7", user_id=2))  # another user or language: the same cached answer, no call
    assert again.pick == result.pick and len(model.calls) == 1


def test_a_reason_against_the_pick_is_bad_output_never_kept(clock: FakeClock) -> None:
    ai, _ = make_ai(clock, FakeModel(answer("home_form", "away_form", reasons=("away_better_form",))),
                    forecaster=FakeForecaster(fake_forecast("1X")))
    with pytest.raises(Exception) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert getattr(exc.value, "code", None) == "bad_output"


@pytest.mark.parametrize("reason", ["small_sample", "team_history"])
def test_without_the_goal_model_form_or_odds_nothing_is_guessed_or_charged(clock: FakeClock, reason: str) -> None:
    # The model-less PARTIAL is in test_ai_partial.py; with neither form nor odds it stays INSUFFICIENT.
    ledger, model = MemoryLedger(), FakeModel()
    ai, _ = make_ai(clock, model, ledger=ledger, chelsea_games=2, forecaster=FakeForecaster(Unavailable(reason)))
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "insufficient" and result.probabilities is None and result.pick is None
    assert result.reasons == ("no_verified_form", "no_odds_source") and not model.calls and not ledger.used


def test_each_forecast_shown_is_recorded_once_and_cache_hits_add_nothing(tmp_path, clock: FakeClock) -> None:
    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    model = FakeModel(answer("home_form", "home_rating", reasons=("home_better_form",)))
    ai, _ = make_ai(clock, model, history=history, forecaster=FakeForecaster(fake_forecast("1X")))
    run(ai.analyse("fd:7", user_id=1))
    run(ai.analyse("fd:7", user_id=2))
    assert history.count() == 1
    new_analysis(ai, clock)  # evicted / restarted: rebuilt from the history, never paid again
    again = run(ai.analyse("fd:7", user_id=3))
    assert history.count() == 1 and len(model.calls) == 1
    assert again.factors == ("home_form", "home_rating") and again.pick.reasons == ("home_better_form",)


def test_a_forecast_that_cannot_be_stored_is_never_shown(tmp_path, clock: FakeClock, caplog) -> None:
    class Failing(ForecastHistory):
        def record(self, r):
            raise OSError("disk full")

    history = Failing(tmp_path / "x.db", clock)
    history.init_schema()
    ai, _ = make_ai(clock, FakeModel(), history=history)
    with pytest.raises(Exception) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert getattr(exc.value, "code", None) == "upstream_error"
    assert "ai.history_write_failed type=OSError" in caplog.text and history.count() == 0


def test_an_unwritable_history_refuses_before_anything_is_paid(tmp_path, clock: FakeClock) -> None:
    class Locked(ForecastHistory):
        def writable(self) -> bool:
            return False

    ledger, model = MemoryLedger(), FakeModel()
    ai, _ = make_ai(clock, model, ledger=ledger, history=Locked(tmp_path / "x.db", clock))
    with pytest.raises(Exception) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert getattr(exc.value, "code", None) == "upstream_error" and not model.calls and not ledger.used


def test_a_changed_market_percentage_is_a_new_analysis_not_a_stale_cache_hit(clock: FakeClock) -> None:
    model = FakeModel()
    forecaster = FakeForecaster(fake_forecast())
    ai, t = make_ai(clock, model, forecaster=forecaster)
    run(ai.analyse("fd:7", user_id=1))
    # Same 1X2 and figures shown to the model, but another market's number differs.
    base = fake_forecast()
    forecaster.result = replace(base, exact={**base.exact, "O2.5": base.exact["O2.5"] + 0.01})
    run(ai.analyse("fd:7", user_id=2))
    assert len(model.calls) == 2


def test_group_tables_give_the_ai_no_cross_group_positions(clock: FakeClock) -> None:
    groups = {"competition": {"code": "PL"}, "standings": [
        {"type": "TOTAL", "stage": "GROUP_STAGE", "group": "GROUP_A", "table": [standings()["standings"][1]["table"][0]]},
        {"type": "TOTAL", "stage": "GROUP_STAGE", "group": "GROUP_B", "table": [standings()["standings"][1]["table"][1]]},
    ]}
    t = FakeTransport()
    ai, t = make_ai(clock, FakeModel(answer("home_form", "away_form")), table=t.ok(groups))
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.basis["home"]["tablePosition"] is None and result.basis["away"]["tablePosition"] is None


def test_an_unchanged_analysis_is_kept_until_kickoff_and_never_paid_twice(clock: FakeClock) -> None:
    model = FakeModel()
    ai, t = make_ai(clock, model)
    run(ai.analyse("fd:7", user_id=1))
    clock.advance(6 * 3600 + 60)  # past the old 6-hour lifetime, still before kickoff
    t.script = [t.ok(match(7)), t.ok(arsenal_form()), t.ok(chelsea_form()), t.ok(standings())]  # source refreshed
    run(ai.analyse("fd:7", user_id=2))
    assert len(model.calls) == 1


def test_an_evicted_answer_is_restored_without_a_new_paid_call(tmp_path, clock: FakeClock) -> None:
    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    ledger = MemoryLedger()
    model = FakeModel(answer("home_form", "away_form"), answer("home_rating", "home_form"))
    ai, _ = make_ai(clock, model, history=history, ledger=ledger)
    first = run(ai.analyse("fd:7", user_id=1))
    new_analysis(ai, clock)
    second = run(ai.analyse("fd:7", user_id=2))
    assert len(model.calls) == 1 and history.count() == 1 and second.factors == first.factors
    assert ledger.used[(ai._today(), "all")] == 1  # the restore is free


def test_an_analysis_that_keeps_failing_is_not_paid_for_again_and_again(clock: FakeClock) -> None:
    ledger, model = MemoryLedger(), FakeModel("not json")
    ai, _ = make_ai(clock, model, ledger=ledger)
    for _ in range(4):
        new_analysis(ai, clock)  # the failure backoff has passed
        with pytest.raises(Exception):
            run(ai.analyse("fd:7", user_id=1))
    assert len(model.calls) == 2 and ledger.used[(ai._today(), "all")] == 2


def test_an_attempt_that_cannot_be_counted_is_refused_before_any_payment(tmp_path, clock: FakeClock) -> None:
    class NoCount(ForecastHistory):
        def add_attempt(self, key, kickoff_at):
            raise OSError("disk full")

    history = NoCount(tmp_path / "x.db", clock)
    history.init_schema()
    ledger, model = MemoryLedger(), FakeModel()
    ai, _ = make_ai(clock, model, history=history, ledger=ledger)
    with pytest.raises(Exception) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert getattr(exc.value, "code", None) == "upstream_error" and not model.calls and not ledger.used


def test_a_paid_answer_that_cannot_be_stored_is_never_paid_for_again(tmp_path, clock: FakeClock) -> None:
    class Failing(ForecastHistory):
        def record(self, r):
            raise OSError("disk full")

    history = Failing(tmp_path / "x.db", clock)
    history.init_schema()
    model = FakeModel()
    ai, _ = make_ai(clock, model, history=history)
    for _ in range(3):
        new_analysis(ai, clock)
        with pytest.raises(Exception):
            run(ai.analyse("fd:7", user_id=1))
    assert len(model.calls) == 1


def test_an_attempt_is_counted_before_the_call_so_a_crash_still_counts(tmp_path, clock: FakeClock) -> None:
    class Crashing(FakeModel):
        async def complete(self, system, user, schema):
            self.calls.append((system, user, schema))
            raise RuntimeError("SDK crashed after dispatch")

    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    model = Crashing()
    ai, _ = make_ai(clock, model, history=history)
    for _ in range(4):
        new_analysis(ai, clock)
        with pytest.raises(Exception):
            run(ai.analyse("fd:7", user_id=1))
    assert len(model.calls) == 2


def test_a_quota_refusal_does_not_use_up_an_attempt(tmp_path, clock: FakeClock) -> None:
    class Refusing(MemoryLedger):
        def reserve_ai_call(self, day, limits):
            return "all"

    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    ai, _ = make_ai(clock, FakeModel(), history=history, ledger=Refusing())
    for _ in range(3):
        with pytest.raises(Exception) as exc:
            run(ai.analyse("fd:7", user_id=1))
        assert getattr(exc.value, "code", None) == "budget_exhausted"
    import sqlite3
    with sqlite3.connect(history.path) as conn:
        assert conn.execute("SELECT COALESCE(SUM(attempts), 0) FROM ai_paid_attempts").fetchone()[0] == 0


# --- read-only lookup: showing the AI screen never pays ----------------------------------------------

def test_reading_never_calls_the_model_or_reserves_a_limit(clock: FakeClock) -> None:
    ledger, model = MemoryLedger(), FakeModel()
    ai, _ = make_ai(clock, model, ledger=ledger)
    with pytest.raises(AiError) as exc:
        run(ai.analyse("fd:7", user_id=1, create=False))
    assert exc.value.code == "not_created" and model.calls == [] and not ledger.used
    made = run(ai.analyse("fd:7", user_id=1))  # the explicit "create" pays once
    read = run(ai.analyse("fd:7", user_id=2, create=False))
    assert read == made and len(model.calls) == 1
    assert (ai._today(), "user:2") not in ledger.used


def test_reading_restores_a_stored_answer_after_a_restart_for_free(tmp_path, clock: FakeClock) -> None:
    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    model = FakeModel()
    ai, _ = make_ai(clock, model, history=history, forecaster=FakeForecaster(fake_forecast("1X")))
    made = run(ai.analyse("fd:7", user_id=1))
    ledger = MemoryLedger()
    restarted, _ = make_ai(clock, model, history=history, ledger=ledger, forecaster=FakeForecaster(fake_forecast("1X")))
    read = run(restarted.analyse("fd:7", user_id=1, create=False))
    assert read.pick == made.pick and read.factors == made.factors and len(model.calls) == 1 and not ledger.used


def test_reading_returns_free_answers_without_creating(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model, status="FINISHED")
    assert run(ai.analyse("fd:7", user_id=1, create=False)).reasons == ("not_upcoming",)
    ai, _ = make_ai(clock, model, chelsea_games=2)
    assert run(ai.analyse("fd:7", user_id=1, create=False)).kind == "insufficient" and model.calls == []


def test_get_endpoint_reads_only_and_needs_a_session(api, clock: FakeClock) -> None:
    client, t, model = api
    url = "/api/ai/analysis?event_id=fd:7"
    assert client.get(url).status_code == 401
    csrf = login(client, fresh(clock)).json()["session"]["csrf_token"]
    for bad in ("/api/ai/analysis", url + "&x=1", url + "&event_id=fd:8", "/api/ai/analysis?event_id=" + "9" * 41):
        assert client.get(bad).status_code == 400, bad
    t.script = [t.ok(match(7)), t.ok(arsenal_form()), t.ok(chelsea_form()), t.ok(standings())]
    r = client.get(url)
    assert r.status_code == 404 and r.json() == {"error": "ai_not_created"} and model.calls == []
    assert post(client, {"event_id": "fd:7"}, csrf).status_code == 200 and len(model.calls) == 1
    again = client.get(url)
    assert again.status_code == 200 and again.json()["kind"] == "partial" and len(model.calls) == 1


def test_a_made_analysis_is_read_even_without_the_model(tmp_path, clock: FakeClock) -> None:
    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    model = FakeModel()
    ai, _ = make_ai(clock, model, history=history, forecaster=FakeForecaster(fake_forecast("1X")))
    made = run(ai.analyse("fd:7", user_id=1))
    # The key is gone (outage): stored answers are still read, for free, by both paths.
    offline, _ = make_ai(clock, None, history=history, forecaster=FakeForecaster(fake_forecast("1X")))
    assert run(offline.analyse("fd:7", user_id=1, create=False)).pick == made.pick
    offline2, _ = make_ai(clock, None, history=history, forecaster=FakeForecaster(fake_forecast("1X")))
    assert run(offline2.analyse("fd:7", user_id=1)).pick == made.pick and len(model.calls) == 1
    # Nothing made: reading says "not created", creating says "not configured".
    empty, _ = make_ai(clock, None)
    with pytest.raises(AiError) as exc:
        run(empty.analyse("fd:7", user_id=1, create=False))
    assert exc.value.code == "not_created"


def test_a_remembered_paid_failure_reads_as_not_created(clock: FakeClock) -> None:
    class Failing(FakeModel):
        async def complete(self, system, user, schema):
            self.calls.append((system, user, schema))
            raise AiError("timeout")

    model = Failing()
    ai, _ = make_ai(clock, model)
    with pytest.raises(Exception):
        run(ai.analyse("fd:7", user_id=1))
    with pytest.raises(AiError) as exc:  # within the failure backoff: still only "not created"
        run(ai.analyse("fd:7", user_id=1, create=False))
    assert exc.value.code == "not_created" and len(model.calls) == 1


def test_reading_never_waits_on_a_creation_in_progress(clock: FakeClock) -> None:
    class Slow(FakeModel):
        def __init__(self) -> None:
            super().__init__()
            self.gate = asyncio.Event()

        async def complete(self, system, user, schema):
            await self.gate.wait()
            return await super().complete(system, user, schema)

    model = Slow()
    ai, _ = make_ai(clock, model)

    async def scenario():
        creating = asyncio.create_task(ai.analyse("fd:7", user_id=1))
        for _ in range(50):  # until the paid load is running
            await asyncio.sleep(0)
            if model.calls or any(isinstance(v, asyncio.Future) for v in ai._cache._inflight.values()):
                break
        with pytest.raises(AiError) as exc:
            await asyncio.wait_for(ai.analyse("fd:7", user_id=2, create=False), 1)
        assert exc.value.code == "not_created"
        model.gate.set()
        made = await creating
        assert await ai.analyse("fd:7", user_id=2, create=False) == made

    run(scenario())


def test_a_stored_answer_is_read_directly_even_while_a_paid_load_of_its_key_runs(tmp_path, clock: FakeClock) -> None:
    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    model = FakeModel()
    ai, _ = make_ai(clock, model, history=history, forecaster=FakeForecaster(fake_forecast("1X")))
    made = run(ai.analyse("fd:7", user_id=1))
    ai._cache._entries.clear()

    async def scenario():
        never = asyncio.get_running_loop().create_future()  # a paid load of the same key, unfinished
        # The cache reports that unfinished load for the AI key, and would hand it to anyone starting one.
        original = ai._cache.existing

        def existing(k):
            return never if k.startswith("ai:") else original(k)

        ai._cache.existing = existing  # type: ignore[method-assign]
        ai._cache.start = lambda *a, **kw: never  # type: ignore[method-assign]
        read = await asyncio.wait_for(ai.analyse("fd:7", user_id=2, create=False), 1)
        assert read.pick == made.pick and read.factors == made.factors

    run(scenario())
    assert len(model.calls) == 1
