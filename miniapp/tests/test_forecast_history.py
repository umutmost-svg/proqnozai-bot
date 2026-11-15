"""Forecast history: once per key, immutable, append-only, settled from calendars (M3.5)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from miniapp.forecast.history import ForecastHistory, ForecastRecord, ForeignHistoryError, history_path
from miniapp.sports.models import Competitor, FootballEvent, Provenance
from miniapp.tests.conftest import FakeClock


def record(key: str = "goals-1:fd:7:abc", event: str = "fd:7", created: str = "2027-01-14T08:00:00Z",
           pick: str = "1X") -> ForecastRecord:
    probs = {"1": 0.5, "X": 0.26, "2": 0.24, "1X": 0.76, "O2.5": 0.55}
    return ForecastRecord(
        forecast_key=key, event_id=event, tournament_id="fd:PL", home_team_id="fd:team:57",
        away_team_id="fd:team:61", created_at=created, kickoff_at="2027-01-14T15:00:00Z", model_version="goals-1",
        parameters="bc465e0d53f2", data_until="2027-01-13T17:00:00Z", sample=300, inputs_digest="abc",
        probabilities=probs, baseline={"1": 0.45, "X": 0.27, "2": 0.28}, gate_eligible=True, display={"1": 50, "X": 26, "2": 24}, expected_goals=(1.6, 1.0), pick_market=pick,
        pick_probability=None if pick == "NO_BET" else 0.76, no_bet_reason="no_qualifying_market" if pick == "NO_BET"
        else None, alternatives=[], explanation={"factors": ["home_form", "away_form"], "reasons": []},
        llm_model="claude-opus-5-5", source="football-data.org")


def event(status: str = "finished", score=(2, 1), awarded: bool = False, eid: str = "fd:7") -> FootballEvent:
    side = lambda n: Competitor(f"fd:team:{n}", f"T{n}")  # noqa: E731
    return FootballEvent(eid, "fd:PL", "2027-01-14T15:00:00Z", status, side(57), side(61),
                         Provenance("football-data.org", "2027-01-14T18:00:00Z", None), "FINISHED",
                         score=score, awarded=awarded, regular_score=score if status == "finished" else None)


@pytest.fixture()
def history(tmp_path) -> ForecastHistory:
    h = ForecastHistory(tmp_path / "m.forecasts.db", FakeClock())
    h.init_schema()
    return h


def test_history_lives_next_to_the_database_in_its_own_file(tmp_path) -> None:
    assert history_path(tmp_path / "miniapp.db") == tmp_path / "miniapp.forecasts.db"


def test_a_forecast_is_stored_once(history: ForecastHistory) -> None:
    assert history.record(record()) is True
    assert history.record(record()) is False  # reopening the cached analysis
    assert history.record(record(key="goals-1:fd:7:def")) is True  # new inputs before kickoff: new row
    assert history.count() == 2


def test_forecast_columns_cannot_be_changed_or_deleted(history: ForecastHistory) -> None:
    history.record(record())
    with sqlite3.connect(history.path) as conn:
        for sql in ("UPDATE ai_forecasts SET probabilities_json = '{}'", "UPDATE ai_forecasts SET pick_market = 'NO_BET'",
                    "UPDATE ai_forecasts SET created_at = '2020-01-01T00:00:00Z'", "DELETE FROM ai_forecasts",
                    "UPDATE ai_forecasts SET final_home = 1, final_away = 0, settled_status = 'settled'"):  # no revision
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_a_forecast_after_kickoff_or_an_inconsistent_pick_is_refused(history: ForecastHistory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        history.record(record(created="2027-01-14T15:00:00Z"))
    with pytest.raises(sqlite3.IntegrityError):
        history.record(replace(record(), pick_probability=None))


def test_settlement_writes_results_and_journals_every_change(history: ForecastHistory) -> None:
    history.record(record())
    history.record(record(key="goals-1:fd:8:x", event="fd:8", pick="NO_BET"))
    assert history.settle_events([event(), event(status="postponed", score=None, eid="fd:8")]) == 1
    assert history.settle_events([event()]) == 0  # nothing new: no write
    assert history.settle_events([event(score=(2, 2))]) == 1  # the source corrected the score
    assert history.settle_events([event(awarded=True)]) == 1  # later awarded: void, not a loss
    with sqlite3.connect(history.path) as conn:
        journal = conn.execute("SELECT revision, final_home, final_away, settled_status FROM forecast_settlements "
                               "ORDER BY revision").fetchall()
        pending = conn.execute("SELECT settled_status FROM ai_forecasts WHERE event_id = 'fd:8'").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM forecast_settlements")
    assert journal == [(1, 2, 1, "settled"), (2, 2, 2, "settled"), (3, None, None, "void")]
    assert pending == ("pending",)


def test_evaluation_reads_settled_forecasts_only(history: ForecastHistory) -> None:
    history.record(record())
    history.record(record(key="goals-1:fd:9:x", event="fd:9"))
    history.settle_events([event()])
    report = history.evaluate()["goals-1"]
    assert report["forecasts"] == 1 and report["picks"] == {"count": 1, "hitRate": 1.0}
    assert report["logLoss1x2"] == pytest.approx(0.6931, abs=1e-3) and "O2.5" in report["markets"]


def test_a_foreign_file_is_never_used_as_history(tmp_path) -> None:
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE users (id INTEGER)")
    with pytest.raises(ForeignHistoryError):
        ForecastHistory(path).init_schema()


def test_every_result_change_is_journalled_by_the_database_itself(history: ForecastHistory) -> None:
    history.record(record())
    with sqlite3.connect(history.path) as conn:
        conn.execute("UPDATE ai_forecasts SET final_home = 1, final_away = 0, settled_status = 'settled', "
                     "settled_at = '2027-01-14T18:00:00Z', revision = 1")
        journal = conn.execute("SELECT revision, final_home, final_away, settled_status, recorded_at "
                               "FROM forecast_settlements").fetchall()
    assert journal == [(1, 1, 0, "settled", "2027-01-14T18:00:00Z")]


def test_evaluation_takes_one_latest_row_per_event_even_within_one_second(history: ForecastHistory) -> None:
    history.record(record(key="goals-1:fd:7:a"))
    history.record(record(key="goals-1:fd:7:b"))  # same created_at second, other inputs
    history.settle_events([event()])
    assert history.evaluate()["goals-1"]["forecasts"] == 1


def test_a_stored_forecast_cannot_be_replaced_by_any_connection(history: ForecastHistory) -> None:
    history.record(record())
    with sqlite3.connect(history.path) as conn:  # a plain connection: default pragmas
        conn.execute("INSERT OR REPLACE INTO ai_forecasts (forecast_key, event_id, tournament_id, home_team_id, "
                     "away_team_id, created_at, kickoff_at, model_version, parameters, data_until, sample, "
                     "inputs_digest, probabilities_json, baseline_json, gate_eligible, display_json, expected_home, expected_away, pick_market, "
                     "pick_probability, no_bet_reason, alternatives_json, explanation_json, llm_model, source) "
                     "SELECT forecast_key, event_id, tournament_id, home_team_id, away_team_id, created_at, "
                     "kickoff_at, model_version, parameters, data_until, sample, inputs_digest, '{}', baseline_json, gate_eligible, display_json, "
                     "expected_home, expected_away, pick_market, pick_probability, no_bet_reason, alternatives_json, "
                     "explanation_json, llm_model, source FROM ai_forecasts")
        assert conn.execute("SELECT probabilities_json FROM ai_forecasts").fetchone()[0] != "{}"
    assert history.count() == 1


def test_the_settlement_journal_cannot_be_replaced_either(history: ForecastHistory) -> None:
    history.record(record())
    history.settle_events([event()])
    with sqlite3.connect(history.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT OR REPLACE INTO forecast_settlements VALUES "
                         "('goals-1:fd:7:abc', 1, 9, 9, 'settled', '2027-01-15T00:00:00Z')")
        assert conn.execute("SELECT final_home, final_away FROM forecast_settlements").fetchall() == [(2, 1)]


def test_the_prospective_gate_needs_enough_settled_forecasts_and_the_paired_rule(history: ForecastHistory) -> None:
    history.record(record())
    history.settle_events([event()])
    gate = history.evaluate()["goals-1"]["prospectiveGate"]
    assert gate == {"forecasts": 1, "minimum": 300, "result": None, "passed": False}


def test_the_gate_compares_with_the_stored_baseline_on_the_same_matches() -> None:
    from miniapp.forecast.history import _gate

    good = [("fd:PL", {"1": 0.7, "X": 0.2, "2": 0.1}, 2, 0, "1", {"1": 0.45, "X": 0.27, "2": 0.28},
             f"2027-0{1 + i // 120}-{1 + (i % 28):02d}T15:00:00Z") for i in range(360)]
    assert _gate(good)["passed"] is True
    bad = [(t, base, h, a, pk, p, k) for t, p, h, a, pk, base, k in good]  # roles swapped
    assert _gate(bad)["passed"] is False


def test_only_validated_complete_window_forecasts_count_toward_the_gate(history: ForecastHistory) -> None:
    history.record(record())
    history.record(replace(record(key="goals-1:fd:8:x", event="fd:8"), gate_eligible=False))
    history.settle_events([event(), event(eid="fd:8")])
    gate = history.evaluate()["goals-1"]["prospectiveGate"]
    assert gate["forecasts"] == 1  # the CL / incomplete-window forecast is evaluated, never gated


def test_paid_attempts_survive_a_restart(tmp_path) -> None:
    path = tmp_path / "h.db"
    first = ForecastHistory(path)
    first.init_schema()
    assert first.add_attempt("k", "2099-01-01T00:00:00Z") == 1
    again = ForecastHistory(path)
    again.init_schema()
    assert again.attempts("k") == 1 and again.add_attempt("k", "2099-01-01T00:00:00Z") == 2
