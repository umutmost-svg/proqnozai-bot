"""Offline walk-forward backtest of the goal model (manual; no network, no app state).

    python -m miniapp.forecast.backtest validate --data DIR --out DIR
    python -m miniapp.forecast.backtest test     --data DIR --out DIR

DIR holds saved football-data.org answers (`competitions_<C>_matches.json` for the
current season, `competitions_<C>_matches__season-<YYYY>.json` for earlier ones).

Protocol (no information from the future):
- every match is forecast at 00:00 UTC of its match day from results of matches that
  kicked off at least 24 h before that moment (data.BACKTEST_LAG_S): certainly final;
- the window is what production has: the previous season and the current one;
- no table, odds or any other source enters — results only;
- validation (the second half of every competition's 2025 season, forecast with the
  2024 season in the window) chooses the model's hyper-parameters AND the reference
  baseline, and produces the calibration table and the selection rules;
  everything is written to frozen.json before the `test` phase runs;
- the `test` phase (each competition's 2026 season to date) reads only frozen.json and
  nothing is tuned on it. For goals-1 it is a DIAGNOSTIC, not a held-out confirmation:
  that season was inspected before the final freeze (docs/miniapp/m35-goal-model.md).
  Confirmation comes from forecasts stored before kickoff on future matches.
The Champions League is scored separately (limited-data mode).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .baselines import frequencies, league_poisson
from .data import MatchResult, known_before, results_from_payload
from .markets import FAMILY, KEYS, probabilities, settle
from .metrics import (brier_1x2, brier_binary, calibration, expected_calibration_error, log_loss_1x2,
                      log_loss_binary, outcome_1x2, paired_block_bootstrap, rps_1x2)
from .model import ModelParams, fit
from .scores import score_matrix
from .selection import DUPLICATES, MIN_SAMPLE, select
from .version import MIN_TEAM_SHOW, MODEL_VERSION, stability_half_lives

GRID_HALF_LIFE: tuple[float | None, ...] = (90.0, 180.0, 365.0, None)
GRID_PRIOR = (2.0, 5.0, 10.0, 20.0)
VALIDATION_YEAR = 2025
SELECTION_MIN_COUNT = 100
SELECTION_MAX_OVER = 0.03
LIMITED = "CL"
DAY_S = 86400.0
_FILE = re.compile(r"^competitions_([A-Z0-9]+)_matches(?:__season-(\d{4}))?\.json$")
BINARY_KEYS = tuple(k for k in KEYS if FAMILY[k] not in ("1x2",) and k not in DUPLICATES)


@dataclass(frozen=True)
class Season:
    competition: str
    year: int
    start: float
    results: tuple[MatchResult, ...]
    end: float | None = None

    @property
    def midpoint(self) -> float:
        """The middle of the season by its official dates (start of the validation half)."""
        if self.end is None:
            raise ValueError(f"{self.competition} {self.year}: no season end date")
        return self.start + (self.end - self.start) / 2


def load(data_dir: Path) -> dict[str, dict[int, Season]]:
    seasons: dict[str, dict[int, Season]] = {}
    for path in sorted(data_dir.iterdir()):
        m = _FILE.match(path.name)
        if not m:
            continue
        payload = json.loads(path.read_text())
        matches = payload.get("matches") or []
        if not matches:
            continue
        node = matches[0]["season"]
        year = int(node["startDate"][:4])
        start = datetime.fromisoformat(node["startDate"]).replace(tzinfo=UTC).timestamp()
        if any(x["season"]["startDate"] != node["startDate"] for x in matches):
            raise ValueError(f"{path.name}: mixed seasons")
        comp = m.group(1)
        end = (datetime.fromisoformat(node["endDate"]).replace(tzinfo=UTC).timestamp() + DAY_S
               if node.get("endDate") else None)
        seasons.setdefault(comp, {})[year] = Season(comp, year, start, tuple(results_from_payload(matches)), end)
    return seasons


def _match_days(targets: Iterable[MatchResult]) -> list[tuple[float, list[MatchResult]]]:
    days: dict[float, list[MatchResult]] = {}
    for r in targets:
        days.setdefault(math.floor(r.kickoff / DAY_S) * DAY_S, []).append(r)
    return sorted(days.items())


def _task(job: tuple) -> list[dict]:
    """One competition-season under one (half-life, prior): forecasts of every evaluable match."""
    seasons, comp, year, half_lives, prior, with_baselines = job
    current = seasons[comp][year]
    previous = seasons[comp].get(year - 1)
    window = (previous.results if previous else ()) + current.results
    warm: dict = {}
    out: list[dict] = []
    for moment, targets in _match_days(current.results):
        hist = known_before(window, moment)
        if len(hist) < MIN_SAMPLE:
            continue
        fits = {}
        for hl in half_lives:
            fits[hl] = fit(hist, moment, ModelParams(hl, prior, True), current.start, warm.get(hl))
            warm[hl] = fits[hl]
        main = fits[half_lives[0]]
        base = {"B0": frequencies(hist), "B1": league_poisson(hist)} if with_baselines else {}
        n_home = sum(r.home_goals for r in hist) / len(hist)
        n_away = sum(r.away_goals for r in hist) / len(hist)
        for t in targets:
            hm, am = main.matches.get(t.home, 0), main.matches.get(t.away, 0)
            if min(hm, am) < MIN_TEAM_SHOW:
                continue
            preds: dict[str, dict[str, float]] = {}
            rates: dict[str, tuple[float, float]] = {}
            for hl, r in fits.items():
                lam, mu = r.expected_goals(t.home, t.away)
                tag = _tag(hl, prior)
                preds[f"P{tag}"] = probabilities(score_matrix(lam, mu, 0.0))
                preds[f"DC{tag}"] = probabilities(score_matrix(lam, mu, r.rho))
                rates[f"P{tag}"] = rates[f"DC{tag}"] = (lam, mu)
            for name, p in base.items():
                preds[name] = p
            if with_baselines:
                rates["B1"] = (n_home, n_away)
            out.append({"id": t.match_id, "comp": comp, "year": year, "complete": previous is not None,
                        "kickoff": t.kickoff, "hg": t.home_goals,
                        "ag": t.away_goals, "hm": hm, "am": am, "sample": len(hist), "preds": preds,
                        "rates": rates, "rho": main.rho, "leagueRates": base.get("B0") or frequencies(hist)})
    return out


def _tag(hl: float | None, prior: float) -> str:
    return f"[hl={'inf' if hl is None else int(hl)},k={prior:g}]"


def _merge(chunks: Iterable[list[dict]]) -> list[dict]:
    merged: dict[tuple[str, int], dict] = {}
    for chunk in chunks:
        for rec in chunk:
            key = (rec["comp"], rec["id"])
            if key in merged:
                merged[key]["preds"].update(rec["preds"])
                merged[key]["rates"].update(rec["rates"])
            else:
                merged[key] = rec
    return sorted(merged.values(), key=lambda r: (r["kickoff"], r["comp"], r["id"]))


def _run(seasons, pairs: list[tuple[str, int]], configs: list[tuple[tuple, float]], workers: int) -> list[dict]:
    jobs = []
    for comp, year in pairs:
        for i, (half_lives, prior) in enumerate(configs):
            jobs.append((seasons, comp, year, half_lives, prior, i == 0))
    if workers <= 1:
        return _merge(map(_task, jobs))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return _merge(pool.map(_task, jobs))


# --- scoring ------------------------------------------------------------------------

def score(records: list[dict], model: str) -> dict:
    ll = br = rps = 0.0
    mae, n_mae = 0.0, 0
    binary: dict[str, list[float]] = {}
    for r in records:
        p, o = r["preds"][model], outcome_1x2(r["hg"], r["ag"])
        ll += log_loss_1x2(p, o)
        br += brier_1x2(p, o)
        rps += rps_1x2(p, o)
        if model in r["rates"]:
            lam, mu = r["rates"][model]
            mae += (abs(lam - r["hg"]) + abs(mu - r["ag"])) / 2
            n_mae += 1
        for key in BINARY_KEYS:
            won = settle(key, r["hg"], r["ag"]) == "win"
            acc = binary.setdefault(FAMILY[key], [0.0, 0.0, 0])
            acc[0] += log_loss_binary(p[key], won)
            acc[1] += brier_binary(p[key], won)
            acc[2] += 1
    n = len(records)
    return {
        "matches": n,
        "logLoss1x2": ll / n, "brier1x2": br / n, "rps1x2": rps / n,
        "maeGoals": mae / n_mae if n_mae else None,
        "families": {f: {"logLoss": v[0] / v[2], "brier": v[1] / v[2], "selections": v[2]} for f, v in binary.items()},
    }


def calibration_table(records: list[dict], model: str) -> dict[str, list]:
    """market -> [(low, high, matches, |observed − predicted|)] — the selection rule's input.

    Per market, never pooled across markets: each match counts once in a bin, and errors
    of different markets cannot cancel each other."""
    table: dict[str, list] = {}
    for key in KEYS:
        if key in DUPLICATES:
            continue
        pairs = [(r["preds"][model][key], settle(key, r["hg"], r["ag"]) == "win") for r in records]
        table[key] = [(b.low, b.high, b.count, round(abs(b.observed - b.mean_predicted), 4)) for b in calibration(pairs)]
    return table


def _ece(records: list[dict], model: str) -> dict[str, float]:
    out = {}
    for key in ("1", "X", "2", "O2.5", "BTTS_Y"):
        pairs = [(r["preds"][model][key], settle(key, r["hg"], r["ag"]) == "win") for r in records]
        out[key] = round(expected_calibration_error(calibration(pairs)), 4)
    return out


def _paired(records: list[dict], a: str, b: str, family: str | None = None):
    diffs = []
    for r in records:
        if family is None:
            o = outcome_1x2(r["hg"], r["ag"])
            d = log_loss_1x2(r["preds"][a], o) - log_loss_1x2(r["preds"][b], o)
        else:
            d = 0.0
            for key in BINARY_KEYS:
                if FAMILY[key] != family:
                    continue
                won = settle(key, r["hg"], r["ag"]) == "win"
                d += log_loss_binary(r["preds"][a][key], won) - log_loss_binary(r["preds"][b][key], won)
        diffs.append((r["kickoff"], d))
    return paired_block_bootstrap(diffs)


# --- phases -------------------------------------------------------------------------

def _validation_records(records: list[dict], seasons) -> list[dict]:
    """The second half of each 2025 season, forecast with the previous season in the window —
    the same kind of window production recommends from (an incomplete one is NO_BET there)."""
    return [r for r in records if r["complete"] and r["kickoff"] >= seasons[r["comp"]][r["year"]].midpoint]


def validate(data: Path, out: Path, workers: int) -> dict:
    seasons = load(data)
    # Only competitions whose previous season is saved: the window must be complete.
    pairs = [(c, VALIDATION_YEAR) for c in sorted(seasons)
             if VALIDATION_YEAR in seasons[c] and VALIDATION_YEAR - 1 in seasons[c]]
    configs = [((hl,), k) for hl in GRID_HALF_LIFE for k in GRID_PRIOR]
    records = _validation_records(_run(seasons, pairs, configs, workers), seasons)
    main = [r for r in records if r["comp"] != LIMITED]
    names = sorted(records[0]["preds"]) if records else []
    table = {name: score(main, name) for name in names}
    team_models = [n for n in names if n.startswith(("P[", "DC["))]
    candidate = min(team_models, key=lambda n: table[n]["logLoss1x2"])
    baseline = min(("B0", "B1"), key=lambda n: table[n]["logLoss1x2"])
    # Market families the candidate predicts better than the chosen baseline on validation;
    # only these may ever be recommended (decided here, before the test exists).
    pick_families = sorted(f for f, v in table[candidate]["families"].items()
                           if v["logLoss"] < table[baseline]["families"][f]["logLoss"])
    if table[candidate]["logLoss1x2"] < table[baseline]["logLoss1x2"]:
        pick_families = sorted({"1x2", *pick_families})
    hl_text, k_text = re.match(r"^(?:P|DC)\[hl=(\w+),k=([\d.]+)\]$", candidate).groups()
    hl = None if hl_text == "inf" else float(hl_text)
    calib = calibration_table(main, candidate)
    # Selected outcomes are biased upwards (ranking by the difference from the league picks the
    # model's overestimates too): a family stays selectable only if, on validation, its picks and
    # alternatives were calibrated as a group.
    variants = (hl, *stability_half_lives(hl))
    chosen = _validation_records(_run(seasons, pairs, [(variants, float(k_text))], workers), seasons)
    family_of = "DC" if candidate.startswith("DC") else "P"
    picks = _selection_outcomes(chosen, candidate, [family_of + _tag(h, float(k_text)) for h in variants[1:]],
                                {f: [tuple(b) for b in bins] for f, bins in calib.items()},
                                frozenset(pick_families), per_selection=True)
    selection_check = {}
    for family in list(pick_families):
        items = [(p, w) for m, p, w in picks if FAMILY[m] == family]
        over = (sum(p for p, _ in items) / len(items) - sum(1 for _, w in items if w) / len(items)) if items else None
        ok = len(items) >= SELECTION_MIN_COUNT and over is not None and over <= SELECTION_MAX_OVER
        selection_check[family] = {"count": len(items), "overestimate": None if over is None else round(over, 4),
                                   "kept": ok}
        if not ok:
            pick_families.remove(family)
    frozen = {
        "modelVersion": MODEL_VERSION,
        "frozenAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate": {"name": candidate, "halfLifeDays": hl,
                      "priorMatches": float(k_text), "dixonColes": candidate.startswith("DC")},
        "baseline": baseline,
        "pickFamilies": pick_families,
        "selectionCheck": selection_check,
        "selectionMetric": "mean 1X2 log loss, validation, competitions other than CL",
        "calibration": calib,
        "calibrationUnit": "market",
        # What produced these numbers, so a later reader can tell versions apart.
        "algorithm": {"fit": "weighted Maher MLE, Gamma shrinkage, weak-quarter newcomer prior",
                      "scoreMatrix": "Dixon-Coles, invalid rho for the rates falls back to independent Poisson",
                      "selection": "selection.py: families + per-market calibration + stability + league difference",
                      "backtestLagHours": 24},
        "validationWindow": "second half of each 2025 season, previous season in the window",
        # Owner's decision (2026-11-08): the 2026 test was already inspected before these rules
        # were re-frozen, so recommendations stay experimental until new matches confirm them.
        "recommendationStatus": "experimental",
        "availabilityLagHours": 24,
        "validation": {"pairs": pairs, "matches": len(main), "cl_matches": len(records) - len(main)},
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "frozen.json").write_text(json.dumps(frozen, indent=1))
    report = {"frozen": frozen, "table": table,
              "ranking": sorted(names, key=lambda n: table[n]["logLoss1x2"])}
    (out / "validation.json").write_text(json.dumps(report, indent=1))
    return report


def test(data: Path, out: Path, workers: int) -> dict:
    frozen = json.loads((out / "frozen.json").read_text())
    seasons = load(data)
    pairs = [(c, 2026) for c in sorted(seasons) if 2026 in seasons[c]]
    cand = frozen["candidate"]
    hl, prior = cand["halfLifeDays"], cand["priorMatches"]
    half_lives = (hl, *stability_half_lives(hl))
    records = _run(seasons, pairs, [(half_lives, prior)], workers)
    name = ("DC" if cand["dixonColes"] else "P") + _tag(hl, prior)
    other = ("P" if cand["dixonColes"] else "DC") + _tag(hl, prior)
    alt_names = [("DC" if cand["dixonColes"] else "P") + _tag(h, prior) for h in half_lives[1:]]
    baseline = frozen["baseline"]
    calib = {f: [tuple(b) for b in bins] for f, bins in frozen["calibration"].items()}
    # Only the competitions the rules were validated on are compared and selected from;
    # the rest (CL, anything added later) are reported apart as limited, as in production.
    validated = {c for c, _ in frozen["validation"]["pairs"]} - {LIMITED}
    main = [r for r in records if r["comp"] in validated]
    limited = [r for r in records if r["comp"] not in validated]
    models = [name, other, "B0", "B1"]
    report: dict = {
        "frozen": frozen, "candidate": name, "baseline": baseline,
        "overall": {m: score(main, m) for m in models},
        # For goals-1 the 2026 season was inspected before the final freeze: this paired
        # interval is a diagnostic, never an acceptance (see history.ForecastHistory.evaluate
        # for the prospective gate on forecasts stored before kickoff).
        "diagnosticVsBaseline": {**_paired(main, name, baseline).to_json(),
                                 "status": "diagnostic only, not an acceptance"},
        "secondary": {
            f"{name} vs {other}": _paired(main, name, other).to_json(),
            **{f"{name} vs {baseline} [{fam}]": _paired(main, name, baseline, fam).to_json()
               for fam in ("double", "total", "btts", "handicap", "team_total")},
        },
        "perCompetition": {},
        "calibrationTest": {"ece": _ece(main, name), "bins": calibration_table(main, name)},
        "limited": {m: score(limited, m) for m in models} if limited else None,
    }
    for comp in sorted({r["comp"] for r in records}):
        subset = [r for r in records if r["comp"] == comp]
        report["perCompetition"][comp] = {
            "matches": len(subset),
            **{m: round(score(subset, m)["logLoss1x2"], 4) for m in models},
        }
    report["selection"] = _selection_outcomes(main, name, alt_names, calib, frozenset(frozen["pickFamilies"]))
    out.mkdir(parents=True, exist_ok=True)
    (out / "test.json").write_text(json.dumps(report, indent=1))
    return report


def _selection_outcomes(records, name, alt_names, calib, families, per_selection: bool = False):
    picks, alts, reasons = [], [], {}
    for r in records:
        sel = select(r["preds"][name], r["leagueRates"], competition=r["comp"], home_matches=r["hm"],
                     away_matches=r["am"], sample=r["sample"], calibration=calib,
                     alternatives=[r["preds"][a] for a in alt_names], families=families)
        if sel.pick is None:
            reasons[sel.no_bet_reason] = reasons.get(sel.no_bet_reason, 0) + 1
            continue
        picks.append((sel.pick.market, sel.pick.probability, settle(sel.pick.market, r["hg"], r["ag"]) == "win"))
        alts += [(c.market, c.probability, settle(c.market, r["hg"], r["ag"]) == "win") for c in sel.alternatives]

    def summary(items):
        if not items:
            return {"count": 0}
        by_family: dict[str, list] = {}
        for market, p, won in items:
            by_family.setdefault(FAMILY[market], []).append((p, won))
        return {
            "count": len(items),
            "meanProbability": round(sum(p for _, p, _ in items) / len(items), 4),
            "hitRate": round(sum(1 for *_, w in items if w) / len(items), 4),
            "byFamily": {f: {"count": len(v), "meanProbability": round(sum(p for p, _ in v) / len(v), 4),
                             "hitRate": round(sum(1 for _, w in v if w) / len(v), 4)} for f, v in by_family.items()},
        }

    if per_selection:
        return picks + alts
    return {"matches": len(records), "noBet": reasons, "picks": summary(picks), "alternatives": summary(alts)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("phase", choices=("validate", "test"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args(argv)
    if args.phase == "test" and (args.out / "test.json").exists():
        print("test.json exists: the held-out test is run once; remove it deliberately to re-run", file=sys.stderr)
        return 2
    report = (validate if args.phase == "validate" else test)(args.data, args.out, args.workers)
    print(json.dumps({k: report[k] for k in report if k in ("frozen", "diagnosticVsBaseline", "candidate", "baseline")},
                     indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
