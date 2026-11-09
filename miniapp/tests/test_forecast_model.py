"""Goal model, score distribution, markets and their invariants (M3.5) — pure maths, offline."""

from __future__ import annotations

import math
import random

import pytest

from miniapp.forecast.baselines import frequencies, league_poisson
from miniapp.forecast.data import BACKTEST_LAG_S, MatchResult, known_before, results_from_payload
from miniapp.forecast.markets import FAMILY, KEYS, PAIRS, display_percentages, probabilities, settle
from miniapp.forecast.metrics import (brier_1x2, calibration, log_loss_1x2, paired_block_bootstrap, rps_1x2)
from miniapp.forecast.model import ModelParams, dc_tau, fit
from miniapp.forecast.scores import score_matrix

DAY = 86400.0
T0 = 1_780_000_000.0


def poisson_draw(rng: random.Random, rate: float) -> int:
    # Knuth: fine for football-sized rates.
    limit, k, p = math.exp(-rate), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def simulate(teams: int = 12, rounds: int = 4, seed: int = 7, start: float = T0, ids_from: int = 1):
    rng = random.Random(seed)
    attack = {ids_from + i: math.exp(rng.gauss(0, 0.3)) for i in range(teams)}
    defence = {ids_from + i: math.exp(rng.gauss(0, 0.25)) for i in range(teams)}
    out, mid, day = [], 1, 0
    ids = list(attack)
    for _ in range(rounds):
        for h in ids:
            for a in ids:
                if h == a:
                    continue
                lam, mu = 1.15 * 1.3 * attack[h] * defence[a], 1.15 * attack[a] * defence[h]
                out.append(MatchResult(mid, start + day * DAY, h, a, poisson_draw(rng, lam), poisson_draw(rng, mu)))
                mid += 1
                day += 0.25
    return out, attack, defence


# --- data rules ----------------------------------------------------------------------

def raw(mid, status="FINISHED", duration="REGULAR", ft=(2, 1), rt=None, day="2026-09-01T15:00:00Z"):
    score = {"duration": duration, "fullTime": {"home": ft[0], "away": ft[1]}}
    if rt is not None:
        score["regularTime"] = {"home": rt[0], "away": rt[1]}
    return {"id": mid, "status": status, "utcDate": day, "homeTeam": {"id": 1}, "awayTeam": {"id": 2}, "score": score}


def test_only_matches_played_to_the_end_count_with_their_90_minute_score() -> None:
    got = results_from_payload([
        raw(1), raw(2, status="AWARDED"), raw(3, status="TIMED"),
        raw(4, duration="EXTRA_TIME", ft=(3, 2), rt=(3, 0)),       # 90 minutes: 3:0
        raw(5, duration="PENALTY_SHOOTOUT", ft=(6, 5), rt=(1, 1)),  # shoot-out goals never count
        raw(6, duration="EXTRA_TIME", ft=(2, 2)),                   # no regular-time score: unusable
    ])
    assert [(r.match_id, r.home_goals, r.away_goals) for r in got] == [(1, 2, 1), (4, 3, 0), (5, 1, 1)]


def test_a_backtest_result_is_known_only_a_full_day_after_kickoff() -> None:
    r = MatchResult(1, T0, 1, 2, 1, 0)
    assert BACKTEST_LAG_S == 24 * 3600  # covers extra time, shoot-outs and late finishes
    assert known_before([r], T0 + BACKTEST_LAG_S - 1) == []
    assert known_before([r], T0 + BACKTEST_LAG_S) == [r]


# --- score distribution ----------------------------------------------------------------

@pytest.mark.parametrize(("lam", "mu", "rho"), [(1.4, 1.1, 0.0), (0.2, 0.15, 0.0), (3.9, 0.4, -0.1), (7.9, 6.8, 0.0)])
def test_score_matrix_is_a_valid_distribution_with_a_negligible_tail(lam, mu, rho) -> None:
    m = score_matrix(lam, mu, rho)
    total = sum(p for *_, p in m.cells())
    assert abs(total - 1.0) < 1e-12 and all(p >= 0 for *_, p in m.cells())
    assert m.tail < 1e-9  # the grid grew until the left-out mass is negligible
    mean_home = sum(x * p for x, _, p in m.cells())
    assert mean_home == pytest.approx(lam, rel=1e-6 if rho == 0 else 0.05)


def test_dixon_coles_moves_only_low_scores_and_keeps_the_mass() -> None:
    plain, adjusted = score_matrix(1.3, 1.0), score_matrix(1.3, 1.0, -0.1)
    assert adjusted.probs[0][0] > plain.probs[0][0] and adjusted.probs[1][1] > plain.probs[1][1]
    assert adjusted.probs[3][2] == pytest.approx(plain.probs[3][2], rel=1e-9)
    assert dc_tau(2, 0, 1.3, 1.0, -0.1) == 1.0


def test_an_invalid_dixon_coles_correction_falls_back_to_independent_poisson() -> None:
    assert dc_tau(0, 0, 4.0, 4.0, 0.25) < 0  # this ρ is not valid for these rates
    assert score_matrix(4.0, 4.0, 0.25).probs == score_matrix(4.0, 4.0, 0.0).probs


@pytest.mark.parametrize("bad", [(0, 1), (-1, 1), (math.inf, 1), (math.nan, 1), (8.5, 1), (1, 9)])
def test_invalid_rates_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        score_matrix(*bad)


# --- markets ------------------------------------------------------------------------------

@pytest.mark.parametrize(("lam", "mu"), [(1.5, 1.1), (0.6, 2.4), (2.9, 0.3), (1.0, 1.0)])
def test_markets_are_consistent_with_each_other(lam, mu) -> None:
    p = probabilities(score_matrix(lam, mu, -0.05))
    assert p["1"] + p["X"] + p["2"] == pytest.approx(1.0, abs=1e-12)
    assert p["1X"] == pytest.approx(p["1"] + p["X"]) and p["X2"] == pytest.approx(p["X"] + p["2"])
    assert p["12"] == pytest.approx(p["1"] + p["2"])
    assert p["H1-0.5"] == pytest.approx(p["1"]) and p["H2+0.5"] == pytest.approx(p["X2"])
    for a, b in PAIRS:
        assert p[a] + p[b] == pytest.approx(1.0, abs=1e-12)
    assert p["O1.5"] >= p["O2.5"] >= p["O3.5"] and p["H1-1.5"] <= p["1"]
    assert p["HO0.5"] >= p["HO1.5"] and p["BTTS_Y"] <= min(p["HO0.5"], p["AO0.5"])
    # BTTS and totals come from the score distribution, not from the three 1X2 numbers.
    other = probabilities(score_matrix(lam * 1.5, mu * 1.5, -0.05))
    assert other["O2.5"] > p["O2.5"]


@pytest.mark.parametrize(("lam", "mu"), [(1.5, 1.1), (0.6, 2.4), (2.9, 0.3), (1.37, 1.37), (0.05, 0.05)])
def test_whole_percents_keep_every_identity(lam, mu) -> None:
    d = display_percentages(probabilities(score_matrix(lam, mu)))
    assert d["1"] + d["X"] + d["2"] == 100
    assert (d["1X"], d["X2"], d["12"]) == (d["1"] + d["X"], d["X"] + d["2"], d["1"] + d["2"])
    assert d["H1-0.5"] == d["1"] and d["H2+0.5"] == d["X2"]
    assert all(d[a] + d[b] == 100 for a, b in PAIRS)
    assert d["O1.5"] >= d["O2.5"] >= d["O3.5"] and d["H1-1.5"] <= d["1"]
    assert set(d) == set(KEYS) and all(0 <= v <= 100 for v in d.values())


@pytest.mark.parametrize(("key", "score", "expected"), [
    ("1", (2, 1), "win"), ("X", (1, 1), "win"), ("12", (0, 0), "lose"),
    ("O2.5", (2, 1), "win"), ("U2.5", (2, 1), "lose"), ("O1.5", (1, 0), "lose"),
    ("BTTS_Y", (1, 1), "win"), ("BTTS_N", (3, 0), "win"),
    ("H1-1.5", (2, 0), "win"), ("H1-1.5", (2, 1), "lose"), ("H2+1.5", (2, 1), "win"), ("H2+0.5", (1, 1), "win"),
    ("HO0.5", (1, 3), "win"), ("AU1.5", (1, 3), "lose"), ("AO1.5", (0, 2), "win"),
])
def test_settlement_by_the_90_minute_score(key, score, expected) -> None:
    assert settle(key, *score) == expected


def test_whole_lines_push_and_are_kept_apart_from_binary_markets() -> None:
    assert settle("O2", 1, 1) == "push" and settle("H1-1", 2, 1) == "push" and settle("H2+1", 2, 1) == "push"
    assert settle("HO1", 1, 0) == "push"
    with pytest.raises(ValueError):
        settle("CORNERS", 1, 0)
    assert all("." in k or FAMILY[k] in ("1x2", "double", "btts") for k in KEYS)  # only half lines are offered


# --- the fit ------------------------------------------------------------------------------

def test_fit_recovers_team_strengths_and_home_advantage() -> None:
    results, attack, defence = simulate(teams=12, rounds=6)
    r = fit(results, results[-1].kickoff + DAY, ModelParams(None, 1.0, False))
    true_strength = {t: math.log(attack[t]) - math.log(defence[t]) for t in attack}
    fitted = {t: math.log(r.attack[t]) - math.log(r.defence[t]) for t in attack}
    order = lambda d: sorted(d, key=d.get)  # noqa: E731
    top_true, top_fit = set(order(true_strength)[-3:]), set(order(fitted)[-3:])
    assert len(top_true & top_fit) >= 2
    assert r.home_advantage == pytest.approx(1.3, abs=0.12)


def test_shrinkage_keeps_a_team_with_few_results_near_its_prior() -> None:
    results, *_ = simulate(teams=10, rounds=2)
    lonely = [MatchResult(99_000, results[-1].kickoff + DAY, 500, 1, 6, 0)]
    moment = lonely[0].kickoff + DAY
    loose = fit(results + lonely, moment, ModelParams(None, 0.5, False))
    tight = fit(results + lonely, moment, ModelParams(None, 20.0, False))
    assert abs(math.log(tight.attack[500])) < abs(math.log(loose.attack[500]))


def test_newcomers_start_from_the_weakest_quarter_not_the_average() -> None:
    old, *_ = simulate(teams=12, rounds=2, start=T0)
    season = old[-1].kickoff + 30 * DAY
    new_team = [MatchResult(70_000 + i, season + i * DAY, 900, 1 + i, 1, 1) for i in range(2)]
    r = fit(old + new_team, season + 10 * DAY, ModelParams(None, 5.0, False), season_start=season)
    assert r.newcomers == frozenset({900})
    assert math.log(r.newcomer[0]) - math.log(r.newcomer[1]) < 0  # weaker than average
    unseen = r.expected_goals(901, 1)  # a team without any result gets the newcomer prior too
    assert unseen[0] < r.expected_goals(1, 2)[0] * 3


def test_warm_start_changes_speed_not_the_answer() -> None:
    results, *_ = simulate(teams=10, rounds=3)
    params = ModelParams(180.0, 5.0, True)
    moment = results[-1].kickoff + DAY
    cold = fit(results, moment, params)
    warm = fit(results, moment, params, warm=fit(results[:-20], moment - DAY, params))
    for t in cold.attack:
        assert warm.attack[t] == pytest.approx(cold.attack[t], rel=1e-3)
    assert warm.rho == cold.rho


def test_fit_without_results_is_none() -> None:
    assert fit([], T0, ModelParams(None, 5.0, False)) is None


# --- baselines and metrics ------------------------------------------------------------------

def test_baselines_are_valid_distributions_from_the_window_only() -> None:
    results, *_ = simulate(teams=8, rounds=2)
    for p in (frequencies(results), league_poisson(results)):
        assert p["1"] + p["X"] + p["2"] == pytest.approx(1.0)
        assert all(p[a] + p[b] == pytest.approx(1.0) for a, b in PAIRS)
    assert frequencies([]) is None and league_poisson([]) is None


def test_scoring_rules() -> None:
    p = {"1": 0.5, "X": 0.3, "2": 0.2}
    assert log_loss_1x2(p, "1") == pytest.approx(-math.log(0.5))
    assert brier_1x2(p, "X") == pytest.approx(0.25 + 0.49 + 0.04)
    assert rps_1x2({"1": 1.0, "X": 0.0, "2": 0.0}, "1") == 0.0
    bins = calibration([(0.05, False), (0.15, True), (0.95, True), (1.0, True)])
    assert [b.count for b in bins] == [1, 1, 2]


def test_block_bootstrap_sees_a_clear_difference_and_not_noise() -> None:
    rng = random.Random(1)
    better = [(T0 + i * 3 * DAY, -0.05 + rng.gauss(0, 0.2)) for i in range(400)]
    noise = [(T0 + i * 3 * DAY, rng.gauss(0, 0.2)) for i in range(400)]
    assert paired_block_bootstrap(better).candidate_better
    r = paired_block_bootstrap(noise)
    assert r.low < 0 < r.high and r.weeks > 100 and r.block_weeks == 4


def test_without_low_scores_rho_is_not_identified_and_stays_zero() -> None:
    results = [MatchResult(i, T0 + i * DAY, 1 + i % 6, 1 + (i + 1) % 6, 2, 2) for i in range(100)]
    r = fit(results, results[-1].kickoff + DAY, ModelParams(None, 5.0, True))
    assert r.rho == 0.0


def test_bootstrap_resamples_keep_the_observed_number_of_weeks() -> None:
    rng = random.Random(3)
    diffs = [(T0 + i * 7 * DAY, rng.gauss(0, 0.2)) for i in range(37)]  # 37 weeks, not divisible by 4
    r = paired_block_bootstrap(diffs, resamples=200)
    assert r.weeks == 37 and r.low <= r.mean_difference <= r.high
    from miniapp.forecast.metrics import resample_weeks

    for seed in range(20):
        drawn = resample_weeks(random.Random(seed), 37, 4)
        assert len(drawn) == 37 and all(0 <= k < 37 for k in drawn)
