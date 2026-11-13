"""Readiness with real odds, and the PARTIAL analysis without the goal model — offline (fakes only)."""

from __future__ import annotations

import json

import pytest

from miniapp.ai.analysis import BASIS_PROMPT, SYSTEM_PROMPT, AiError, MemoryLedger
from miniapp.forecast.engine import Unavailable
from miniapp.forecast.history import ForecastHistory
from miniapp.odds.store import MAX_AGE_S, OddsStore
from miniapp.odds.value import book_json, market_1x2_percent
from miniapp.tests.conftest import FakeClock
from miniapp.tests.forecast_fakes import FakeForecaster
from miniapp.tests.test_ai_analysis import FakeModel, answer, make_ai
from miniapp.tests.test_odds import ingest, iso, line
from miniapp.tests.test_sports import run

UNAVAILABLE = ["small_sample", "team_history", "extreme_rates"]


def facts_of(model: FakeModel, i: int = 0) -> dict:
    return json.loads(model.calls[i][1].split("Facts (JSON):\n", 1)[1])


def with_odds(ai, clock: FakeClock, *lines: dict) -> OddsStore:
    store = OddsStore(clock)
    ai.odds = store
    if lines:
        ingest(store, clock, *lines)
    return store


# --- goal model unavailable, verified form → PARTIAL -----------------------------------------------

@pytest.mark.parametrize("reason", UNAVAILABLE)
def test_model_unavailable_with_verified_form_is_a_partial_ai_analysis(tmp_path, clock: FakeClock, reason: str) -> None:
    history = ForecastHistory(tmp_path / "h.db", clock)
    history.init_schema()
    ledger, model = MemoryLedger(), FakeModel(answer("home_form", "away_table"))
    ai, _ = make_ai(clock, model, ledger=ledger, history=history, forecaster=FakeForecaster(Unavailable(reason)))
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "partial" and result.estimated is True
    assert result.reasons == (f"model_{reason}", "no_odds_source")
    # No model numbers and no odds: nothing to show as a probability, no pick, nothing invented.
    assert result.probabilities is None and result.pick is None and result.forecast is None
    assert result.alternatives == () and result.market is None and result.estimate_from is None
    assert result.factors == ("home_form", "away_table") and result.basis["home"]["form"] == "WWDWW"
    system, _, _ = model.calls[0]
    assert system == BASIS_PROMPT
    facts = facts_of(model)
    assert facts["goalModel"] == {"available": False, "reason": reason} and "model" not in facts
    assert facts["allowedReasons"] == [] and not {"home_rating", "away_rating", "home_advantage"} & set(facts["allowedFactors"])
    assert "market" not in facts  # no odds in the prompt without a line in the store
    assert ledger.used[(ai._today(), "user:1")] == 1
    # Not a goal-model forecast: never in the history that is evaluated and settled.
    assert history.count() == 0
    assert result.to_json()["forecast"] is None and result.to_json()["pick"] is None
    # Cached for everyone: no second call, no second charge.
    again = run(ai.analyse("fd:7", user_id=2))
    assert again == result and len(model.calls) == 1 and (ai._today(), "user:2") not in ledger.used


def test_the_partial_prompts_require_the_estimated_marking() -> None:
    for prompt in (BASIS_PROMPT, SYSTEM_PROMPT):
        assert "Оценка вероятностей" in prompt and "estimated probabilities" in prompt
    # The anti-fabrication directives stay.
    for prompt in (BASIS_PROMPT, SYSTEM_PROMPT):
        assert "no injuries, lineups, head-to-head" in prompt and "Do not assume any" in prompt
        assert "you write no free text" in prompt


def test_the_basis_prompt_facts_carry_the_estimated_label(clock: FakeClock) -> None:
    model = FakeModel(answer("home_form", "away_form"))
    ai, _ = make_ai(clock, model, forecaster=FakeForecaster(Unavailable("small_sample")))
    run(ai.analyse("fd:7", user_id=1))
    label = facts_of(model)["probabilityLabel"]
    assert "Оценка вероятностей" in label and "estimated probabilities" in label


@pytest.mark.parametrize("bad", [
    answer("home_form", "away_form", reasons=("home_better_form",)),  # no pick to argue for
    answer("home_form", "home_rating"),  # a model figure that does not exist here
    answer("home_form"),  # too few
])
def test_a_basis_answer_outside_its_contract_is_rejected(clock: FakeClock, bad: str) -> None:
    ai, _ = make_ai(clock, FakeModel(bad), forecaster=FakeForecaster(Unavailable("small_sample")))
    with pytest.raises(Exception) as exc:
        run(ai.analyse("fd:7", user_id=1))
    assert getattr(exc.value, "code", None) == "bad_output"


def test_reading_never_creates_a_basis_analysis(clock: FakeClock) -> None:
    ledger, model = MemoryLedger(), FakeModel()
    ai, _ = make_ai(clock, model, ledger=ledger, forecaster=FakeForecaster(Unavailable("team_history")))
    with pytest.raises(AiError) as exc:
        run(ai.analyse("fd:7", user_id=1, create=False))
    assert exc.value.code == "not_created" and not model.calls and not ledger.used
    made = run(ai.analyse("fd:7", user_id=1))
    assert run(ai.analyse("fd:7", user_id=2, create=False)) == made


def test_a_successful_basis_call_does_not_use_up_the_attempt_cap(clock: FakeClock) -> None:
    from miniapp.tests.test_ai_analysis import new_analysis

    model = FakeModel(answer("home_form", "away_form"))
    ai, _ = make_ai(clock, model, forecaster=FakeForecaster(Unavailable("small_sample")))
    for _ in range(3):  # as after restarts: the in-memory answer is gone, a new call is allowed
        new_analysis(ai, clock)
        assert run(ai.analyse("fd:7", user_id=1)).kind == "partial"
    assert len(model.calls) == 3


# --- odds as evidence -----------------------------------------------------------------------------

def test_model_unavailable_with_odds_shows_the_markets_estimate_computed_in_python(clock: FakeClock) -> None:
    model = FakeModel(answer("home_form", "away_form"))
    ai, _ = make_ai(clock, model, forecaster=FakeForecaster(Unavailable("small_sample")))
    store = with_odds(ai, clock, line())
    result = run(ai.analyse("fd:7", user_id=1))
    book = store.lookup(ai_event(ai)).book
    # Margin removed, whole percents summing to 100 — from the stored prices, never from the model.
    assert result.probabilities == market_1x2_percent(book) and sum(result.probabilities.values()) == 100
    assert result.kind == "partial" and result.estimated is True and result.reasons == ("model_small_sample",)
    assert result.pick is None and result.forecast is None
    # The estimate expires with its prices (the client hides it then).
    found = store.lookup(ai_event(ai))
    assert result.to_json()["estimateFrom"] == {"fetchedAt": iso(found.fetched_at), "validFor": int(found.valid_for)}
    facts = facts_of(model)
    assert facts["market"] == book_json(book)  # the real odds go in as data


def test_odds_prices_are_attached_when_served_and_never_change_the_cached_answer(clock: FakeClock) -> None:
    model = FakeModel(answer("home_form", "away_form"))
    ai, _ = make_ai(clock, model, forecaster=FakeForecaster(Unavailable("small_sample")))
    with_odds(ai, clock)
    first = run(ai.analyse("fd:7", user_id=1))
    assert first.probabilities is None and "no_odds_source" in first.reasons
    ingest(ai.odds, clock, line())
    priced = run(ai.analyse("fd:7", user_id=1))
    assert priced.probabilities is not None and priced.reasons == ("model_small_sample",)
    assert priced.factors == first.factors and len(model.calls) == 1
    ai.odds = OddsStore(clock)  # the line is gone (expired or dropped): no estimate any more
    stale = run(ai.analyse("fd:7", user_id=1))
    assert stale.probabilities is None and stale.reasons == ("model_small_sample", "no_odds_source")


def test_odds_without_verified_form_or_model_are_partial_without_any_model_call(clock: FakeClock) -> None:
    ledger, model = MemoryLedger(), FakeModel()
    ai, _ = make_ai(clock, model, ledger=ledger, chelsea_games=2, forecaster=FakeForecaster(Unavailable("team_history")))
    with_odds(ai, clock, line())
    result = run(ai.analyse("fd:7", user_id=1, create=False))  # costs nothing: even a read gives it
    assert result.kind == "partial" and result.estimated is True and result.factors == ()
    assert result.reasons == ("model_team_history", "no_verified_form")
    assert result.probabilities is not None and result.pick is None
    assert not model.calls and not ledger.used


def test_odds_without_verified_form_but_with_the_model_keep_the_models_numbers(clock: FakeClock) -> None:
    from miniapp.tests.test_ai_analysis import SHOWN

    model = FakeModel(answer("home_rating", "home_advantage"))
    ai, _ = make_ai(clock, model, chelsea_games=2)
    with_odds(ai, clock, line())
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "partial" and result.estimated is True and result.reasons == ("no_verified_form",)
    assert result.probabilities == {"home": SHOWN["1"], "draw": SHOWN["X"], "away": SHOWN["2"]}
    assert result.market is not None  # has_odds: the prices against the model are shown
    facts = facts_of(model)
    assert not {"home_form", "away_form", "home_goals", "away_goals"} & set(facts["allowedFactors"])
    assert "market" in facts


def test_odds_only_partial_whose_line_disappears_during_the_call_is_insufficient(clock: FakeClock) -> None:
    class LineGoneModel(FakeModel):
        async def complete(self, system, user, schema):
            ai.odds = OddsStore(clock)  # the line expires while the model answers
            return await super().complete(system, user, schema)

    model = LineGoneModel(answer("home_rating", "home_advantage"))
    ai, _ = make_ai(clock, model, chelsea_games=2)
    store = with_odds(ai, clock, line())
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "insufficient" and result.reasons == ("no_verified_form", "no_odds_source")
    assert result.probabilities is None and result.pick is None
    ai.odds = store  # the line is back: the paid answer is served from the cache, no new call
    again = run(ai.analyse("fd:7", user_id=1))
    assert again.kind == "partial" and again.market is not None and len(model.calls) == 1


def test_ready_needs_odds_and_verified_form_and_the_odds_reach_the_prompt(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model)
    store = with_odds(ai, clock, line())
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "ready" and result.reasons == () and result.estimated is False
    assert facts_of(model)["market"] == book_json(store.lookup(ai_event(ai)).book)


def test_no_odds_in_the_prompt_unless_the_store_holds_a_current_line_for_this_match(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model)
    with_odds(ai, clock, line(id=9, home="Liverpool", away="Everton"))  # another match
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "partial" and result.reasons == ("no_odds_source",)
    assert "market" not in facts_of(model)
    assert not {"odds", "market"} & set(json.dumps(facts_of(model)).lower().split('"'))


def test_neither_form_nor_odds_is_insufficient_with_the_reason_codes(clock: FakeClock) -> None:
    model = FakeModel()
    ai, _ = make_ai(clock, model, chelsea_games=2, forecaster=FakeForecaster(Unavailable("small_sample")))
    store = with_odds(ai, clock, line())
    clock.advance(MAX_AGE_S)  # only an expired line: not evidence
    assert store.lookup(ai_event(ai)) is None
    result = run(ai.analyse("fd:7", user_id=1))
    assert result.kind == "insufficient" and result.reasons == ("no_verified_form", "no_odds_source")
    assert not model.calls


def ai_event(ai):
    event, _ = run(ai.sports.event("fd:7"))
    return event
