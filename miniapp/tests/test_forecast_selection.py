"""Recommended market / NO_BET rules, the frozen engine and the backtest protocol (M3.5)."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from miniapp.forecast import backtest
from miniapp.forecast.data import MatchResult
from miniapp.forecast.engine import FROZEN, ForecastEngine, MatchForecast, Unavailable, load_frozen
from miniapp.forecast.markets import KEYS
from miniapp.forecast.selection import (DELTA_MIN, NO_BET_LIMITED, NO_BET_NONE, NO_BET_SAMPLE, P_MIN, select)
from miniapp.forecast.version import MODEL_VERSION, UNPROVEN_FAMILIES
from miniapp.tests.test_forecast_model import DAY, simulate

CAL_OK = {k: [(i / 10, (i + 1) / 10, 500, 0.01) for i in range(10)] for k in KEYS}
FAMILIES = frozenset({"1x2", "double", "handicap"})


def probs(**over: float) -> dict[str, float]:
    base = {k: 0.3 for k in KEYS}
    base.update(over)
    return base


def choose(p, rates=None, **kw):
    args = dict(competition="PL", home_matches=20, away_matches=20, sample=300, calibration=CAL_OK,
                alternatives=[], families=FAMILIES)
    args.update(kw)
    return select(p, rates or {k: 0.3 for k in KEYS}, **args)


def test_the_pick_is_the_largest_difference_from_the_league_not_the_largest_probability() -> None:
    p = probs(**{"1": 0.62, "1X": 0.80, "H2+1.5": 0.97})
    rates = {k: 0.3 for k in KEYS} | {"1X": 0.70, "H2+1.5": 0.93}
    sel = choose(p, rates)
    # 1X has 80 % and H2+1.5 97 %, but "1" differs most from what this league usually gives.
    assert sel.pick.market == "1" and [c.market for c in sel.alternatives] == ["1X"]
    assert sel.no_bet_reason is None


def test_a_very_likely_but_uninformative_market_is_never_picked() -> None:
    p = probs(**{"H2+1.5": 0.95})
    sel = choose(p, {k: 0.3 for k in KEYS} | {"H2+1.5": 0.92})
    assert sel.pick is None and sel.no_bet_reason == NO_BET_NONE


@pytest.mark.parametrize(("kw", "reason"), [
    ({"competition": "CL"}, NO_BET_LIMITED),
    ({"home_matches": 7}, NO_BET_SAMPLE), ({"sample": 79}, NO_BET_SAMPLE),
])
def test_limited_competitions_and_small_samples_are_no_bet(kw, reason) -> None:
    sel = choose(probs(**{"1": 0.9}), **kw)
    assert sel.pick is None and sel.no_bet_reason == reason and sel.alternatives == ()


def test_each_rule_alone_can_block_a_market() -> None:
    assert choose(probs(**{"1": P_MIN - 0.01})).pick is None
    assert choose(probs(**{"1": 0.3 + DELTA_MIN - 0.01, "X": 0.0})).pick is None
    uncalibrated = {k: [(0.6, 0.7, 500, 0.08)] for k in CAL_OK}
    assert choose(probs(**{"1": 0.65}), calibration=uncalibrated).pick is None
    few = {k: [(0.6, 0.7, 99, 0.0)] for k in CAL_OK}
    assert choose(probs(**{"1": 0.65}), calibration=few).pick is None
    # Calibration is per market: another market's good bin never vouches for this one.
    other_only = {k: v for k, v in CAL_OK.items() if k != "1"}
    assert choose(probs(**{"1": 0.65}), calibration=other_only).pick is None
    assert choose(probs(**{"1": 0.65}), alternatives=[probs(**{"1": 0.59})]).pick is None  # unstable
    assert choose(probs(**{"O2.5": 0.8})).pick is None  # totals: not a selectable family
    assert choose(probs(**{"H1-0.5": 0.8})).pick is None  # the same event as "1" under another name


def test_alternatives_come_from_other_families_at_most_three() -> None:
    p = probs(**{"1": 0.70, "2": 0.0, "1X": 0.85, "12": 0.75, "H1-1.5": 0.66, "X": 0.0})
    sel = choose(p, families=frozenset({"1x2", "double", "handicap"}))
    fams = [sel.pick.market] + [c.market for c in sel.alternatives]
    assert len(sel.alternatives) <= 3 and len({m if m in ("1", "X", "2") else m[:2] for m in fams}) == len(fams)


# --- the frozen model ---------------------------------------------------------------------------

def test_the_shipped_parameters_are_the_validated_ones(tmp_path: Path) -> None:
    raw = json.loads(Path(backtest.__file__).with_name(f"frozen_{MODEL_VERSION}.json").read_text())
    assert raw["candidate"] == {"name": "DC[hl=365,k=2]", "halfLifeDays": 365.0, "priorMatches": 2.0,
                                "dixonColes": True}
    assert raw["recommendationStatus"] == "experimental" and FROZEN.experimental
    assert raw["validationWindow"].startswith("second half of each 2025 season")
    assert raw["baseline"] == "B1" and set(raw["pickFamilies"]) == {"1x2", "double", "handicap", "team_total"}
    assert raw["calibrationUnit"] == "market" and raw["availabilityLagHours"] == 24
    assert FROZEN.pick_families.isdisjoint(UNPROVEN_FAMILIES)
    other = tmp_path / "f.json"
    other.write_text(json.dumps(raw | {"modelVersion": "other"}))
    with pytest.raises(ValueError):  # parameters of another model version are never loaded
        load_frozen(other)


def test_engine_forecasts_from_known_results_only() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    engine = ForecastEngine()
    now = results[-1].kickoff + 3 * DAY
    got = engine.forecast("PL", results, None, 1, 2, now)
    assert isinstance(got, MatchForecast) and got.sample == len(results)
    assert got.display["1"] + got.display["X"] + got.display["2"] == 100
    assert got.to_json()["version"] == MODEL_VERSION and got.to_json()["unprovenFamilies"] == ["btts", "total"]
    # A record dated after `now` (it cannot be final) changes nothing.
    late = MatchResult(999_999, now + 600, 1, 2, 9, 0)
    assert engine.forecast("PL", [*results, late], None, 1, 2, now).exact == got.exact


def test_engine_refuses_small_samples_and_teams_without_history() -> None:
    results, *_ = simulate(teams=4, rounds=1)
    engine = ForecastEngine()
    now = results[-1].kickoff + DAY
    assert engine.forecast("PL", results, None, 1, 2, now) == Unavailable("small_sample")
    big, *_ = simulate(teams=10, rounds=3)
    assert engine.forecast("PL", big, None, 1, 4242, big[-1].kickoff + DAY) == Unavailable("team_history")


def test_the_champions_league_is_shown_as_limited_and_never_picked() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    got = ForecastEngine().forecast("CL", results, None, 1, 2, results[-1].kickoff + DAY)
    assert got.limited and got.selection.pick is None and got.selection.no_bet_reason == NO_BET_LIMITED


# --- the backtest protocol ------------------------------------------------------------------------

def _season_payload(results: list[MatchResult], start: datetime) -> dict:
    node = {"id": 1, "startDate": start.date().isoformat(), "endDate": (start + timedelta(days=44)).date().isoformat()}
    return {"matches": [{
        "id": r.match_id, "status": "FINISHED", "season": node,
        "utcDate": datetime.fromtimestamp(r.kickoff, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "homeTeam": {"id": r.home}, "awayTeam": {"id": r.away},
        "score": {"duration": "REGULAR", "fullTime": {"home": r.home_goals, "away": r.away_goals}},
    } for r in results]}


def _write_league(tmp: Path, code: str, seed: int) -> None:
    for year, name in ((2024, f"competitions_{code}_matches__season-2024.json"),
                       (2025, f"competitions_{code}_matches__season-2025.json"),
                       (2026, f"competitions_{code}_matches.json")):
        start = datetime(year, 8, 1, tzinfo=UTC)
        # One team id range per league: real ids never repeat across competitions.
        results, *_ = simulate(teams=10, rounds=2, seed=seed + year, start=start.timestamp(), ids_from=1 + seed * 100)
        results = [replace(r, match_id=r.match_id + year * 10_000) for r in results]
        (tmp / name).write_text(json.dumps(_season_payload(results, start)))


def test_backtest_validates_freezes_then_tests_once(tmp_path: Path) -> None:
    data, out = tmp_path / "raw", tmp_path / "out"
    data.mkdir()
    for i, code in enumerate(("PL", "PD")):
        _write_league(data, code, seed=10 + i)
    report = backtest.validate(data, out, workers=1)
    frozen = json.loads((out / "frozen.json").read_text())
    assert frozen["candidate"]["name"] in report["table"] and frozen["baseline"] in ("B0", "B1")
    assert frozen["validation"]["pairs"] == [["PD", 2025], ["PL", 2025]]
    result = backtest.test(data, out, workers=1)
    assert result["diagnosticVsBaseline"]["matches"] > 0 and set(result["perCompetition"]) == {"PD", "PL"}
    assert backtest.main(["test", "--data", str(data), "--out", str(out), "--workers", "1"]) == 2  # run once


def test_backtest_forecasts_never_see_the_future(tmp_path: Path) -> None:
    data = tmp_path / "raw"
    data.mkdir()
    _write_league(data, "PL", seed=3)
    seasons = backtest.load(data)
    base = backtest._task((seasons, "PL", 2026, (365.0,), 5.0, True))
    # Rewrite every result from the middle of the season on: earlier forecasts must not move.
    cut = base[len(base) // 2]["kickoff"]
    current = seasons["PL"][2026]
    changed = tuple(replace(r, home_goals=r.home_goals + 5) if r.kickoff >= cut else r for r in current.results)
    seasons["PL"][2026] = replace(current, results=changed)
    again = backtest._task((seasons, "PL", 2026, (365.0,), 5.0, True))
    before = {r["id"]: r["preds"] for r in base if r["kickoff"] < cut - DAY}
    after = {r["id"]: r["preds"] for r in again if r["id"] in before}
    assert before and before == after


def test_without_the_previous_season_probabilities_stay_but_nothing_is_recommended() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    now = results[-1].kickoff + DAY
    engine = ForecastEngine()
    full = engine.forecast("PL", results, None, 1, 2, now)
    partial = engine.forecast("PL", results, None, 1, 2, now, complete_history=False)
    assert partial.exact == full.exact
    assert partial.selection.pick is None and partial.selection.no_bet_reason == "partial_history"
    cl = engine.forecast("CL", results, None, 1, 2, now, complete_history=False)
    assert cl.selection.no_bet_reason == "limited_competition"


def test_a_corrected_old_score_is_a_new_fit_not_a_stale_cache_hit() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    now = results[-1].kickoff + DAY
    engine = ForecastEngine()
    before = engine.forecast("PL", results, None, 1, 2, now)
    first = results[0]
    corrected = [replace(first, home_goals=first.home_goals + 4), *results[1:]]
    assert engine.forecast("PL", corrected, None, 1, 2, now).exact != before.exact


def test_validation_uses_only_second_halves_with_the_previous_season_in_the_window(tmp_path: Path) -> None:
    data = tmp_path / "raw"
    data.mkdir()
    _write_league(data, "PL", seed=4)
    seasons = backtest.load(data)
    records = backtest._task((seasons, "PL", 2025, (365.0,), 5.0, True))
    kept = backtest._validation_records(records, seasons)
    mid = seasons["PL"][2025].midpoint
    assert kept and all(r["kickoff"] >= mid and r["complete"] for r in kept)
    assert len(kept) < len(records)
    no_history = backtest._task((seasons, "PL", 2024, (365.0,), 5.0, True))  # 2023 is not saved
    assert backtest._validation_records(no_history, seasons) == []


def test_expected_goals_beyond_the_supported_range_are_refused_not_capped() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    now = results[-1].kickoff + DAY
    engine = ForecastEngine()
    fitted = engine._fit("PL", results, now, None, FROZEN.params.half_life_days)
    big = replace(fitted, scale=fitted.scale * 20)
    engine._fit = lambda *a, **k: big  # type: ignore[method-assign]
    assert engine.forecast("PL", results, None, 1, 2, now) == Unavailable("extreme_rates")


def test_a_stability_refit_beyond_the_supported_range_is_refused_too() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    now = results[-1].kickoff + DAY
    engine = ForecastEngine()
    real_fit = engine._fit

    def fit(competition, known, moment, season_start, half_life):
        fitted = real_fit(competition, known, moment, season_start, half_life)
        main = half_life == FROZEN.params.half_life_days
        return fitted if main else replace(fitted, scale=fitted.scale * 20)

    engine._fit = fit  # type: ignore[method-assign]
    assert engine.forecast("PL", results, None, 1, 2, now) == Unavailable("extreme_rates")


def test_a_competition_outside_the_validation_gets_no_recommendation() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    now = results[-1].kickoff + DAY
    got = ForecastEngine().forecast("WC", results, None, 1, 2, now)
    assert got.limited and got.selection.pick is None and got.selection.no_bet_reason == "unvalidated_competition"
    assert not got.gate_eligible
    pl = ForecastEngine().forecast("PL", results, None, 1, 2, now, complete_history=False)
    assert not pl.gate_eligible and ForecastEngine().forecast("PL", results, None, 1, 2, now).gate_eligible


def test_every_forecast_of_one_day_uses_the_same_fit_whatever_the_hour() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    day_start = (int(results[-1].kickoff // 86400) + 2) * 86400.0
    morning = ForecastEngine().forecast("PL", results, None, 1, 2, day_start + 8 * 3600)
    evening = ForecastEngine().forecast("PL", results, None, 1, 2, day_start + 20 * 3600)  # a fresh engine
    assert morning.exact == evening.exact


def test_production_uses_exactly_the_validated_window() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    day_start = (int(results[-1].kickoff // 86400) + 3) * 86400.0
    finished_today = MatchResult(888_888, day_start + 9 * 3600, 1, 2, 7, 0)  # ends between the requests
    yesterday = MatchResult(888_889, day_start - 6 * 3600, 3, 4, 6, 0)  # within the 24 h lag
    engine = ForecastEngine()
    morning = engine.forecast("PL", results, None, 1, 2, day_start + 8 * 3600)
    evening = engine.forecast("PL", [*results, yesterday, finished_today], None, 1, 2, day_start + 20 * 3600)
    assert morning.exact == evening.exact and morning.sample == evening.sample == len(results)


def test_before_the_match_day_probabilities_are_shown_but_nothing_is_recommended() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    now = results[-1].kickoff + 3 * DAY
    same_day = math.floor(now / 86400) * 86400.0 + 20 * 3600
    engine = ForecastEngine()
    early = engine.forecast("PL", results, None, 1, 2, now, kickoff=now + 5 * DAY)
    today = engine.forecast("PL", results, None, 1, 2, now, kickoff=same_day)
    assert early.selection.pick is None and early.selection.no_bet_reason == "early_forecast"
    assert early.exact == today.exact and today.selection.no_bet_reason != "early_forecast"
    assert not early.gate_eligible and today.gate_eligible  # only match-day forecasts are gated
