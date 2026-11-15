"""Team-strength goal model: independent Poisson with an optional Dixon–Coles correction.

    home goals ~ Poisson(c · γ · α_home · β_away)
    away goals ~ Poisson(c · α_away · β_home)

α is a team's attack, β its defence (above 1 = concedes more than average), γ the
competition's home advantage and c its scoring level. The fit is a weighted maximum
likelihood (Maher's fixed-point iteration) with:

- time decay: a match `d` days old weighs 0.5 ** (d / half_life_days);
- shrinkage: every α and β carries a Gamma prior worth `prior_matches` average
  matches, so a team with few results stays close to its prior centre;
- newcomers (teams with no match before the current season while the window holds an
  earlier one — promoted sides, first-time participants) start from the average
  rating of the weakest quarter of established teams instead of the league average;
- Dixon–Coles: the low-score dependence ρ is fitted by a CONDITIONAL likelihood on the
  same weighted matches, with α, β, γ, c held at their independent-Poisson fit (not a
  joint or profile fit; validated as such).

Only results passed in are used: the caller decides what was known at forecast time.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .data import MatchResult

DAY_S = 86400.0
MAX_ITER = 300
TOLERANCE = 1e-6
RHO_GRID = tuple(round(-0.25 + 0.01 * i, 2) for i in range(51))  # -0.25 … 0.25
NEWCOMER_QUANTILE = 0.25


@dataclass(frozen=True)
class ModelParams:
    half_life_days: float | None  # None: every match weighs the same
    prior_matches: float
    dixon_coles: bool

    def to_json(self) -> dict:
        return {"halfLifeDays": self.half_life_days, "priorMatches": self.prior_matches,
                "dixonColes": self.dixon_coles}


@dataclass(frozen=True)
class Ratings:
    attack: dict[int, float]
    defence: dict[int, float]
    home_advantage: float  # γ
    scale: float  # c
    rho: float
    matches: dict[int, int]  # unweighted number of results per team in the window
    newcomer: tuple[float, float]  # (α, β) for a team without any result
    sample: int  # results in the window
    league_home_goals: float  # weighted means of the window
    league_away_goals: float
    newcomers: frozenset[int] = field(default_factory=frozenset)

    def expected_goals(self, home: int, away: int) -> tuple[float, float]:
        a_h = self.attack.get(home, self.newcomer[0])
        d_h = self.defence.get(home, self.newcomer[1])
        a_a = self.attack.get(away, self.newcomer[0])
        d_a = self.defence.get(away, self.newcomer[1])
        return self.scale * self.home_advantage * a_h * d_a, self.scale * a_a * d_h


def _weights(results: Sequence[MatchResult], moment: float, half_life_days: float | None) -> list[float]:
    if half_life_days is None:
        return [1.0] * len(results)
    return [0.5 ** (max(0.0, moment - r.kickoff) / DAY_S / half_life_days) for r in results]


def fit(results: Sequence[MatchResult], moment: float, params: ModelParams,
        season_start: float | None = None, warm: Ratings | None = None) -> Ratings | None:
    """Ratings from `results` as seen at `moment`; None without any result.

    `warm` (an earlier fit) only sets the starting point of the iteration — the fixed
    point it converges to is the same, it is just reached in fewer steps."""
    if not results:
        return None
    weights = _weights(results, moment, params.half_life_days)
    teams = sorted({r.home for r in results} | {r.away for r in results})
    newcomers: frozenset[int] = frozenset()
    if season_start is not None and any(r.kickoff < season_start for r in results):
        before = {t for r in results if r.kickoff < season_start for t in (r.home, r.away)}
        newcomers = frozenset(t for t in teams if t not in before)
    centre = {t: (1.0, 1.0) for t in teams}
    first = _fit_once(results, weights, teams, params.prior_matches, centre, warm)
    newcomer = _weak_quarter(first, [t for t in teams if t not in newcomers])
    if newcomers:
        for t in newcomers:
            centre[t] = newcomer
        fitted = _fit_once(results, weights, teams, params.prior_matches, centre, warm)
    else:
        fitted = first
    attack, defence, gamma, scale = fitted
    rho = _fit_rho(results, weights, attack, defence, gamma, scale) if params.dixon_coles else 0.0
    total_w = sum(weights)
    counts: dict[int, int] = {t: 0 for t in teams}
    for r in results:
        counts[r.home] += 1
        counts[r.away] += 1
    return Ratings(
        attack=attack, defence=defence, home_advantage=gamma, scale=scale, rho=rho, matches=counts,
        newcomer=newcomer, sample=len(results),
        league_home_goals=sum(w * r.home_goals for w, r in zip(weights, results)) / total_w,
        league_away_goals=sum(w * r.away_goals for w, r in zip(weights, results)) / total_w,
        newcomers=newcomers,
    )


def _fit_once(results: Sequence[MatchResult], weights: list[float], teams: list[int], prior: float,
              centre: dict[int, tuple[float, float]], warm: Ratings | None = None):
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    hs = [index[r.home] for r in results]
    as_ = [index[r.away] for r in results]
    hg = [r.home_goals for r in results]
    ag = [r.away_goals for r in results]
    total_w = sum(weights)
    mean_home = sum(w * g for w, g in zip(weights, hg)) / total_w
    mean_away = sum(w * g for w, g in zip(weights, ag)) / total_w
    # Prior strength in goal units: `prior` matches of an average side at the window's scoring level.
    per_match = max((mean_home + mean_away) / 2, 0.1)
    k = prior * per_match
    attack = [centre[t][0] for t in teams]
    defence = [centre[t][1] for t in teams]
    scale = max(mean_away, 0.05)
    gamma = max(mean_home, 0.05) / scale
    if warm is not None:
        attack = [warm.attack.get(t, a) for t, a in zip(teams, attack)]
        defence = [warm.defence.get(t, d) for t, d in zip(teams, defence)]
        scale, gamma = warm.scale, warm.home_advantage
    scored = [0.0] * n
    conceded = [0.0] * n
    for w, h, a, x, y in zip(weights, hs, as_, hg, ag):
        scored[h] += w * x
        scored[a] += w * y
        conceded[h] += w * y
        conceded[a] += w * x
    goals_home = sum(w * x for w, x in zip(weights, hg))
    goals_all = goals_home + sum(w * y for w, y in zip(weights, ag))
    for _ in range(MAX_ITER):
        exp_att = [0.0] * n
        for w, h, a in zip(weights, hs, as_):
            exp_att[h] += w * scale * gamma * defence[a]
            exp_att[a] += w * scale * defence[h]
        new_attack = [(scored[i] + k * centre[teams[i]][0]) / (exp_att[i] + k) for i in range(n)]
        exp_def = [0.0] * n
        for w, h, a in zip(weights, hs, as_):
            exp_def[a] += w * scale * gamma * new_attack[h]
            exp_def[h] += w * scale * new_attack[a]
        new_defence = [(conceded[i] + k * centre[teams[i]][1]) / (exp_def[i] + k) for i in range(n)]
        home_exp = sum(w * new_attack[h] * new_defence[a] for w, h, a in zip(weights, hs, as_))
        away_exp = sum(w * new_attack[a] * new_defence[h] for w, h, a in zip(weights, hs, as_))
        new_gamma = goals_home / (scale * home_exp) if home_exp > 0 and goals_home > 0 else 1.0
        denominator = new_gamma * home_exp + away_exp
        new_scale = goals_all / denominator if denominator > 0 and goals_all > 0 else scale
        delta = max(
            max(abs(x - y) for x, y in zip(new_attack, attack)),
            max(abs(x - y) for x, y in zip(new_defence, defence)),
            abs(new_gamma - gamma), abs(new_scale - scale),
        )
        attack, defence, gamma, scale = new_attack, new_defence, new_gamma, new_scale
        if delta < TOLERANCE:
            break
    return ({t: attack[i] for i, t in enumerate(teams)}, {t: defence[i] for i, t in enumerate(teams)},
            gamma, scale)


def _weak_quarter(fitted, established: list[int]) -> tuple[float, float]:
    attack, defence, _, _ = fitted
    if len(established) < 4:
        return (1.0, 1.0)
    ranked = sorted(established, key=lambda t: math.log(attack[t]) - math.log(defence[t]))
    weakest = ranked[:max(1, int(len(ranked) * NEWCOMER_QUANTILE))]
    geo = lambda values: math.exp(sum(math.log(v) for v in values) / len(values))  # noqa: E731
    return geo([attack[t] for t in weakest]), geo([defence[t] for t in weakest])


def dc_tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _fit_rho(results, weights, attack, defence, gamma, scale) -> float:
    low = []
    for w, r in zip(weights, results):
        if r.home_goals <= 1 and r.away_goals <= 1:
            lam = scale * gamma * attack[r.home] * defence[r.away]
            mu = scale * attack[r.away] * defence[r.home]
            low.append((w, r.home_goals, r.away_goals, lam, mu))
    if not low:
        return 0.0  # no low-score result: ρ is not identified, so no correction at all
    best, best_ll = 0.0, -math.inf
    for rho in RHO_GRID:
        ll = 0.0
        for w, x, y, lam, mu in low:
            tau = dc_tau(x, y, lam, mu, rho)
            if tau <= 0:
                ll = -math.inf
                break
            ll += w * math.log(tau)
        if ll > best_ll:
            best, best_ll = rho, ll
    return best
