"""Forecast history: every model forecast a user was shown, immutable, settled later.

Two origins share the table: AI analyses (key `<version>:<event>:<digest>:<answer>`) and the daily slate
(key `slate:<version>:<event>:<digest>`, no explanation): the model's forecast of every match of the validated
competitions on its match day, recorded in the background whatever users open (slate.py) — the prospective
sample for `evaluate`. Operator lists and counts show AI analyses unless asked otherwise; `evaluate` uses both.

Its own SQLite file next to the Mini App database (``<db>.forecasts.db``): the app
database keeps its table whitelist, and a rollback to an older release — which knows
nothing of this file — keeps working untouched.

- One row per forecast key (model version + event + digest of the inputs): reopening
  a cached analysis writes nothing new; new inputs before kickoff are a new row, and
  evaluation uses the latest row of an event created before its kickoff.
- Triggers forbid changing any forecast column and deleting rows. Settlement may only
  write the result columns; every change of a result is journalled with a revision.
- Settlement (`settle_events`) takes events the app already holds — no request of its
  own; `settlement.py` runs it on every loaded season calendar and in a low-priority pass.
- No user identifiers: a forecast's quality does not depend on who opened it.
- No automatic retraining: `evaluate` only reads (Brier score, log loss, calibration
  per model version, competition and market) for an offline review.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from miniapp.forecast.selection import SELECTION_RULES_SINCE
from miniapp.sports.models import SportEvent

from .markets import KEYS, settle
from .metrics import (brier_1x2, calibration, expected_calibration_error, log_loss_1x2, log_loss_binary,
                      outcome_1x2, paired_block_bootstrap)

logger = logging.getLogger("miniapp.forecast")

_IMMUTABLE = ("forecast_key", "event_id", "tournament_id", "home_team_id", "away_team_id", "created_at",
              "kickoff_at", "model_version", "parameters", "data_until", "sample", "inputs_digest",
              "probabilities_json", "baseline_json", "gate_eligible", "display_json", "expected_home", "expected_away", "pick_market",
              "pick_probability", "no_bet_reason", "alternatives_json", "explanation_json", "llm_model", "source")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ai_forecasts (
    forecast_key       TEXT PRIMARY KEY,
    event_id           TEXT NOT NULL,
    tournament_id      TEXT NOT NULL,
    home_team_id       TEXT NOT NULL,
    away_team_id       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    kickoff_at         TEXT NOT NULL,
    model_version      TEXT NOT NULL,
    parameters         TEXT NOT NULL,
    data_until         TEXT NOT NULL,
    sample             INTEGER NOT NULL,
    inputs_digest      TEXT NOT NULL,
    probabilities_json TEXT NOT NULL,
    baseline_json      TEXT NOT NULL,
    gate_eligible      INTEGER NOT NULL CHECK (gate_eligible IN (0, 1)),
    display_json       TEXT NOT NULL,
    expected_home      REAL NOT NULL,
    expected_away      REAL NOT NULL,
    pick_market        TEXT NOT NULL,
    pick_probability   REAL,
    no_bet_reason      TEXT,
    alternatives_json  TEXT NOT NULL,
    explanation_json   TEXT NOT NULL,
    llm_model          TEXT,
    source             TEXT NOT NULL,
    final_home         INTEGER,
    final_away         INTEGER,
    settled_status     TEXT NOT NULL DEFAULT 'pending' CHECK (settled_status IN ('pending', 'settled', 'void')),
    settled_at         TEXT,
    revision           INTEGER NOT NULL DEFAULT 0,
    CHECK (created_at < kickoff_at),
    CHECK ((pick_market = 'NO_BET') = (pick_probability IS NULL)),
    CHECK ((settled_status = 'settled') = (final_home IS NOT NULL AND final_away IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ai_forecasts_event ON ai_forecasts (event_id);
CREATE INDEX IF NOT EXISTS ai_forecasts_pending ON ai_forecasts (settled_status, kickoff_at);
-- Paid analysis calls per evidence key, counted BEFORE dispatch (survive restarts; dropped after kickoff).
CREATE TABLE IF NOT EXISTS ai_paid_attempts (
    analysis_key TEXT PRIMARY KEY,
    kickoff_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_settlements (
    forecast_key   TEXT NOT NULL REFERENCES ai_forecasts(forecast_key),
    revision       INTEGER NOT NULL,
    final_home     INTEGER,
    final_away     INTEGER,
    settled_status TEXT NOT NULL,
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (forecast_key, revision)
);
CREATE TRIGGER IF NOT EXISTS forecasts_immutable BEFORE UPDATE ON ai_forecasts
WHEN {" OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _IMMUTABLE)}
BEGIN SELECT RAISE(ABORT, 'forecast columns are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_revision_forward BEFORE UPDATE ON ai_forecasts
WHEN NEW.revision <> OLD.revision + 1
BEGIN SELECT RAISE(ABORT, 'a result change must bump the revision by one'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_journal AFTER UPDATE ON ai_forecasts
BEGIN
    INSERT INTO forecast_settlements (forecast_key, revision, final_home, final_away, settled_status, recorded_at)
    VALUES (NEW.forecast_key, NEW.revision, NEW.final_home, NEW.final_away, NEW.settled_status,
            COALESCE(NEW.settled_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
END;
-- A row with an existing key is never replaced, whatever the connection's pragmas
-- (INSERT OR REPLACE would otherwise delete it without firing the delete trigger).
CREATE TRIGGER IF NOT EXISTS forecasts_no_replace BEFORE INSERT ON ai_forecasts
WHEN EXISTS (SELECT 1 FROM ai_forecasts WHERE forecast_key = NEW.forecast_key)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS forecasts_append_only BEFORE DELETE ON ai_forecasts
BEGIN SELECT RAISE(ABORT, 'forecast history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS settlements_no_replace BEFORE INSERT ON forecast_settlements
WHEN EXISTS (SELECT 1 FROM forecast_settlements WHERE forecast_key = NEW.forecast_key AND revision = NEW.revision)
BEGIN SELECT RAISE(ABORT, 'settlement journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS settlements_no_update BEFORE UPDATE ON forecast_settlements
BEGIN SELECT RAISE(ABORT, 'settlement journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS settlements_no_delete BEFORE DELETE ON forecast_settlements
BEGIN SELECT RAISE(ABORT, 'settlement journal is append-only'); END;
"""
_OWN = frozenset({"ai_forecasts", "forecast_settlements", "ai_paid_attempts"})


class ForeignHistoryError(RuntimeError):
    """The configured history file holds tables that are not the forecast history's."""


@dataclass(frozen=True)
class ForecastRecord:
    forecast_key: str
    event_id: str
    tournament_id: str
    home_team_id: str
    away_team_id: str
    created_at: str
    kickoff_at: str
    model_version: str
    parameters: str
    data_until: str
    sample: int
    inputs_digest: str
    probabilities: dict[str, float]
    baseline: dict[str, float]  # B1 1X2 on the same window (prospective gate)
    gate_eligible: bool  # validated competition with the previous season in the window
    display: dict[str, int]
    expected_goals: tuple[float, float]
    pick_market: str
    pick_probability: float | None
    no_bet_reason: str | None
    alternatives: list[dict]
    explanation: dict
    llm_model: str | None
    source: str


def _values(r: ForecastRecord) -> tuple:
    return (
        r.forecast_key, r.event_id, r.tournament_id, r.home_team_id, r.away_team_id, r.created_at,
        r.kickoff_at, r.model_version, r.parameters, r.data_until, r.sample, r.inputs_digest,
        json.dumps(r.probabilities, sort_keys=True), json.dumps(r.baseline, sort_keys=True), int(r.gate_eligible),
        json.dumps(r.display, sort_keys=True),
        r.expected_goals[0], r.expected_goals[1], r.pick_market, r.pick_probability, r.no_bet_reason,
        json.dumps(r.alternatives, sort_keys=True), json.dumps(r.explanation, sort_keys=True), r.llm_model,
        r.source,
    )


SLATE_PREFIX = "slate:"
_AI_ONLY = "forecast_key NOT LIKE 'slate:%'"


def model_record(event_id: str, tournament_id: str, home_id: str, away_id: str, kickoff_at: str, forecast,
                 *, key: str, created_at: str, digest: str, explanation: dict, llm_model: str | None,
                 source: str) -> ForecastRecord:
    """The stored row of one model forecast (`forecast`: engine.MatchForecast), for either origin."""
    chosen = forecast.selection.pick
    return ForecastRecord(
        forecast_key=key, event_id=event_id, tournament_id=tournament_id, home_team_id=home_id,
        away_team_id=away_id, created_at=created_at, kickoff_at=kickoff_at, model_version=forecast.model_version,
        parameters=forecast.parameters, data_until=forecast.data_until, sample=forecast.sample, inputs_digest=digest,
        probabilities={k: round(v, 6) for k, v in forecast.exact.items()},
        baseline={k: round(v, 6) for k, v in forecast.baseline_1x2.items()},
        gate_eligible=forecast.gate_eligible, display=forecast.display,
        expected_goals=forecast.expected_goals, pick_market=chosen.market if chosen else "NO_BET",
        pick_probability=round(chosen.probability, 6) if chosen else None,
        no_bet_reason=forecast.selection.no_bet_reason,
        alternatives=[c.to_json() for c in forecast.selection.alternatives],
        explanation=explanation, llm_model=llm_model, source=source,
    )


def history_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.stem}.forecasts.db")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ForecastHistory:
    def __init__(self, path: Path, clock=None, *, read_only: bool = False) -> None:
        self.path = path
        self._clock = clock
        self._read_only = read_only  # operator reads: the file is opened mode=ro, never written

    def _iso_now(self) -> str:
        if self._clock is None:
            return _now()
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10,
                                   isolation_level=None)
        else:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA recursive_triggers=ON")  # replacement deletes fire the delete trigger too
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and os.stat(self.path).st_nlink > 1:
            raise ForeignHistoryError("the history file has several hard links")
        with self._connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if tables - _OWN:
                raise ForeignHistoryError("the history file is not a forecast history")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def record(self, r: ForecastRecord) -> bool:
        """Stores the forecast once; True when this call created it."""
        values = _values(r)
        with self._write() as conn:
            cursor = conn.execute(
                f"INSERT INTO ai_forecasts ({', '.join(_IMMUTABLE)}) VALUES ({', '.join('?' * len(_IMMUTABLE))}) "
                "ON CONFLICT(forecast_key) DO NOTHING", values)
            return cursor.rowcount == 1

    def record_many(self, records: Iterable[ForecastRecord]) -> int:
        """Stores several forecasts in one transaction (existing keys are left as they are); returns how many
        were new."""
        created = 0
        with self._write() as conn:
            for r in records:
                created += conn.execute(
                    f"INSERT INTO ai_forecasts ({', '.join(_IMMUTABLE)}) VALUES ({', '.join('?' * len(_IMMUTABLE))}) "
                    "ON CONFLICT(forecast_key) DO NOTHING", _values(r)).rowcount
        return created

    def settle_events(self, events: Iterable[SportEvent]) -> int:
        """Writes the results of finished (or voided) events into their forecasts; returns
        how many rows changed. Postponed and other open states stay pending."""
        targets: dict[str, tuple[int | None, int | None, str]] = {}
        kickoffs: dict[str, str | None] = {}
        for event in events:
            if getattr(event, "sport", None) != "football":
                continue
            kickoffs[event.id] = getattr(event, "starts_at", None)
            if event.status == "finished" and getattr(event, "awarded", False):
                targets[event.id] = (None, None, "void")  # decided at the table, not played
            elif event.status == "finished" and getattr(event, "regular_score", None) is not None:
                targets[event.id] = (*event.regular_score, "settled")
            elif event.status == "cancelled":
                targets[event.id] = (None, None, "void")
        if not targets:
            return 0
        # A read first: most calendars hold no forecast that needs a change, and then no
        # write lock is taken at all.
        ids = list(targets)
        with self._connect() as conn:
            found = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                found += conn.execute(
                    "SELECT forecast_key, event_id, final_home, final_away, settled_status, kickoff_at, created_at "
                    "FROM ai_forecasts "
                    f"WHERE event_id IN ({', '.join('?' * len(chunk))})", chunk).fetchall()

        def target(event_id: str, kickoff_at: str, created_at: str) -> tuple[int | None, int | None, str]:
            # The kickoff the match was really played at decides: a forecast made for another UTC day (postponed,
            # moved) or made at/after the real kickoff (moved earlier) is not a prospective match-day forecast of
            # that match — void, never scored.
            played = kickoffs.get(event_id)
            if played is not None and (played[:10] != kickoff_at[:10]
                                       or _instant(created_at) >= _instant(played)):
                return (None, None, "void")
            return targets[event_id]

        expected = {key: target(event_id, kickoff, created) for key, event_id, _, _, _, kickoff, created in found}
        due = [key for key, _, fh, fa, st, _, _ in found if (fh, fa, st) != expected[key]]
        if not due:
            return 0
        changed = 0
        now = self._iso_now()
        with self._write() as conn:
            for key in due:
                row = conn.execute("SELECT event_id, final_home, final_away, settled_status, revision FROM ai_forecasts "
                                   "WHERE forecast_key = ?", (key,)).fetchone()
                event_id, fh, fa, st, rev = row
                home, away, status = expected[key]
                if (fh, fa, st) == (home, away, status):
                    continue  # settled meanwhile by another process
                # The journal row is written by the forecasts_journal trigger, in this transaction.
                conn.execute(
                    "UPDATE ai_forecasts SET final_home = ?, final_away = ?, settled_status = ?, settled_at = ?, "
                    "revision = ? WHERE forecast_key = ?", (home, away, status, now, rev + 1, key))
                changed += 1
        return changed

    def stored_answer(self, prefix: str) -> dict | None:
        """The latest shown answer whose key starts with `prefix` (model:event:evidence:)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT explanation_json, llm_model, created_at FROM ai_forecasts "
                "WHERE substr(forecast_key, 1, ?) = ? ORDER BY rowid DESC LIMIT 1", (len(prefix), prefix)).fetchone()
        if row is None:
            return None
        explanation = json.loads(row[0])
        return {"factors": explanation["factors"], "reasons": explanation["reasons"], "model": row[1],
                "created_at": row[2]}

    def attempts(self, key: str) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT attempts FROM ai_paid_attempts WHERE analysis_key = ?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def add_attempt(self, key: str, kickoff_at: str, cap: int | None = None) -> int | None:
        """Counts one paid attempt for this analysis; returns the new total, or None when `cap` attempts are
        already counted (checked inside the same write, so two processes cannot both pass it)."""
        with self._write() as conn:
            conn.execute("DELETE FROM ai_paid_attempts WHERE kickoff_at < ?", (self._iso_now(),))
            changed = conn.execute(
                "INSERT INTO ai_paid_attempts (analysis_key, kickoff_at, attempts) VALUES (?, ?, 1) "
                "ON CONFLICT(analysis_key) DO UPDATE SET attempts = attempts + 1 WHERE ? IS NULL OR attempts < ?",
                (key, kickoff_at, cap, cap)).rowcount
            if not changed:
                return None
            return int(conn.execute("SELECT attempts FROM ai_paid_attempts WHERE analysis_key = ?",
                                    (key,)).fetchone()[0])

    def remove_attempt(self, key: str) -> None:
        """Undoes add_attempt when no call followed (the quota refused it)."""
        with self._write() as conn:
            conn.execute("UPDATE ai_paid_attempts SET attempts = attempts - 1 WHERE analysis_key = ? AND attempts > 0",
                         (key,))

    def writable(self) -> bool:
        """A write lock can be taken right now (checked before a paid analysis starts)."""
        try:
            with self._write():
                return True
        except sqlite3.Error:
            return False

    def is_forecast_history(self) -> bool:
        with self._connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return "ai_forecasts" in tables and not tables - _OWN

    def pending_tournaments(self, kicked_off_before: str, kicked_off_after: str) -> list[str]:
        """Competitions with forecasts still pending whose kickoff lies in the given window."""
        with self._connect() as conn:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT tournament_id FROM ai_forecasts WHERE settled_status = 'pending' "
                "AND kickoff_at <= ? AND kickoff_at >= ? ORDER BY tournament_id",
                (kicked_off_before, kicked_off_after))]

    def status_counts(self, include_slate: bool = False) -> dict[str, int]:
        """By settlement status; AI analyses only unless `include_slate` (the daily slate's model forecasts)."""
        where = "" if include_slate else f" WHERE {_AI_ONLY}"
        with self._connect() as conn:
            counts = dict(conn.execute(
                f"SELECT settled_status, COUNT(*) FROM ai_forecasts{where} GROUP BY settled_status"))
        return {status: int(counts.get(status, 0)) for status in ("pending", "settled", "void")}

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM ai_forecasts").fetchone()[0])

    # ---- operator reads (admin panel) ----

    _LIST_SORTS = {"kickoff": "kickoff_at", "created": "created_at", "status": "settled_status",
                   "tournament": "tournament_id"}

    def list_forecasts(self, *, status: str | None = None, tournament: str | None = None,
                       created_since: str | None = None, kickoff_from: str | None = None,
                       event_ids: list[str] | None = None, sort: str = "kickoff",
                       descending: bool = True, limit: int = 50, offset: int = 0,
                       include_slate: bool = False) -> tuple[int, list[dict]]:
        """A page of stored forecasts (one row per forecast key) without the explanation texts; AI analyses only
        unless `include_slate`."""
        where, args = ["1 = 1" if include_slate else _AI_ONLY], []
        if status in ("pending", "settled", "void"):
            where.append("settled_status = ?")
            args.append(status)
        if tournament:
            where.append("tournament_id = ?")
            args.append(tournament)
        if created_since:
            where.append("created_at >= ?")
            args.append(created_since)
        if kickoff_from:
            where.append("kickoff_at >= ?")
            args.append(kickoff_from)
        if event_ids is not None:
            if not event_ids:
                return 0, []
            where.append(f"event_id IN ({', '.join('?' * len(event_ids))})")
            args += event_ids
        order = self._LIST_SORTS.get(sort, "kickoff_at")
        direction = "DESC" if descending else "ASC"
        sql_where = " AND ".join(where)
        with self._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM ai_forecasts WHERE {sql_where}", args).fetchone()[0])
            rows = conn.execute(
                "SELECT forecast_key, event_id, tournament_id, home_team_id, away_team_id, created_at, kickoff_at, "
                "model_version, pick_market, pick_probability, no_bet_reason, final_home, final_away, "
                f"settled_status, settled_at, revision FROM ai_forecasts WHERE {sql_where} "
                f"ORDER BY {order} {direction}, rowid {direction} LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return total, [_list_row(r) for r in rows]

    def forecast_detail(self, key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT forecast_key, event_id, tournament_id, home_team_id, away_team_id, created_at, kickoff_at, "
                "model_version, pick_market, pick_probability, no_bet_reason, final_home, final_away, "
                "settled_status, settled_at, revision, display_json, expected_home, expected_away, sample, "
                "data_until, llm_model, source FROM ai_forecasts WHERE forecast_key = ?", (key,)).fetchone()
            if row is None:
                return None
            journal = conn.execute(
                "SELECT revision, final_home, final_away, settled_status, recorded_at FROM forecast_settlements "
                "WHERE forecast_key = ? ORDER BY revision", (key,)).fetchall()
        detail = _list_row(row[:16])
        detail.update({"display": json.loads(row[16]), "expectedGoals": [row[17], row[18]], "sample": row[19],
                       "dataUntil": row[20], "llmModel": row[21], "source": row[22],
                       "settlements": [{"revision": r[0], "score": None if r[1] is None else [r[1], r[2]],
                                        "status": r[3], "recordedAt": r[4]} for r in journal]})
        return detail

    def tournaments(self) -> list[str]:
        with self._connect() as conn:
            return [r[0] for r in conn.execute(f"SELECT DISTINCT tournament_id FROM ai_forecasts WHERE {_AI_ONLY} ORDER BY 1")]

    def evaluate(self) -> dict:
        """Quality of settled forecasts (latest row per event created before kickoff):
        1X2 log loss / Brier, per-market log loss, calibration, picks — per model version
        and competition — and the prospective gate. Read-only; nothing here changes a model.

        The gate is the acceptance rule of the validation protocol applied to the daily slate's
        forecasts stored before kickoff (the only independent sample for goals-1; AI analyses follow
        what users open and stay out of it): paired 1X2 log
        loss, model − B1, block bootstrap by calendar week, 95% interval fully below 0,
        with at least GATE_MIN_FORECASTS settled matches. Passing it is what may justify
        an owner-approved change of recommendationStatus to "confirmed"; it never changes
        anything by itself."""
        with self._connect() as conn:
            rows = conn.execute(
                # One row per event and model version: the latest before kickoff, ties broken by insertion order.
                "SELECT model_version, tournament_id, probabilities_json, baseline_json, final_home, final_away, "
                "pick_market, kickoff_at, gate_eligible FROM ("
                "  SELECT *, ROW_NUMBER() OVER (PARTITION BY event_id, model_version"
                "    ORDER BY created_at DESC, rowid DESC) AS n FROM ai_forecasts WHERE created_at < kickoff_at"
                ") WHERE n = 1 AND settled_status = 'settled'"
            ).fetchall()
            # The prospective gate: the daily slate only (every validated match, whatever users open). AI analyses
            # follow users' choices; they count in the metrics above, never in the gate.
            slate_rows = conn.execute(
                "SELECT model_version, tournament_id, probabilities_json, baseline_json, final_home, final_away, "
                "pick_market, kickoff_at, gate_eligible FROM ("
                "  SELECT *, ROW_NUMBER() OVER (PARTITION BY event_id, model_version"
                "    ORDER BY created_at DESC, rowid DESC) AS n FROM ai_forecasts"
                f"   WHERE created_at < kickoff_at AND forecast_key LIKE '{SLATE_PREFIX}%'"
                ") WHERE n = 1 AND settled_status = 'settled'"
            ).fetchall()

        def grouped(found: list) -> dict[str, list[tuple]]:
            groups: dict[str, list[tuple]] = {}
            for version, tournament, probs, base, h, a, pick, kickoff, eligible in found:
                groups.setdefault(version, []).append(
                    (tournament, json.loads(probs), h, a, pick, json.loads(base) if eligible else {}, kickoff))
            return groups

        gated = grouped(slate_rows)
        return {version: {**_evaluate(items), "prospectiveGate": _gate(gated.get(version, []))}
                for version, items in grouped(rows).items()}


GATE_MIN_FORECASTS = 300


def _gate(items: list[tuple]) -> dict:
    diffs = []
    for _, p, h, a, _, base, kickoff in items:
        if not base:  # not gate-eligible (CL, unvalidated competition, incomplete window)
            continue
        o = outcome_1x2(h, a)
        when = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp()
        diffs.append((when, log_loss_1x2(p, o) - log_loss_1x2(base, o)))
    if len(diffs) < GATE_MIN_FORECASTS:
        return {"forecasts": len(diffs), "minimum": GATE_MIN_FORECASTS, "result": None, "passed": False}
    result = paired_block_bootstrap(diffs)
    return {"forecasts": len(diffs), "minimum": GATE_MIN_FORECASTS, "result": result.to_json(),
            "passed": result.candidate_better}


def _evaluate(items: list[tuple]) -> dict:
    n = len(items)
    ll = sum(log_loss_1x2(p, outcome_1x2(h, a)) for _, p, h, a, *_ in items) / n
    br = sum(brier_1x2(p, outcome_1x2(h, a)) for _, p, h, a, *_ in items) / n
    markets = {}
    for key in KEYS:
        pairs = [(p[key], settle(key, h, a) == "win") for _, p, h, a, *_ in items if key in p]
        if pairs:
            markets[key] = {"logLoss": round(sum(log_loss_binary(q, w) for q, w in pairs) / len(pairs), 4),
                            "ece": round(expected_calibration_error(calibration(pairs)), 4)}
    # Only recommendations made under the current selection rules (rule 6 since SELECTION_RULES_SINCE; a pick is only
    # ever made on its match day, so the kickoff date tells the rules apart).
    picks = [(pick, settle(pick, h, a) == "win") for _, _, h, a, pick, _, kickoff in items
             if pick != "NO_BET" and kickoff[:10] >= SELECTION_RULES_SINCE]
    by_tournament: dict[str, list[float]] = {}
    for tournament, p, h, a, *_ in items:
        by_tournament.setdefault(tournament, []).append(log_loss_1x2(p, outcome_1x2(h, a)))
    return {
        "forecasts": n,
        "logLoss1x2": round(ll, 4), "brier1x2": round(br, 4),
        "markets": markets,
        "picks": {"count": len(picks), "hitRate": round(sum(w for _, w in picks) / len(picks), 4)
                  if picks else math.nan, "since": SELECTION_RULES_SINCE},
        "byTournament": {t: {"forecasts": len(v), "logLoss1x2": round(sum(v) / len(v), 4)}
                         for t, v in by_tournament.items()},
    }


def _list_row(r: tuple) -> dict:
    return {"key": r[0], "eventId": r[1], "tournamentId": r[2], "homeTeamId": r[3], "awayTeamId": r[4],
            "createdAt": r[5], "kickoffAt": r[6], "modelVersion": r[7], "pick": r[8], "pickProbability": r[9],
            "noBetReason": r[10], "score": None if r[11] is None else [r[11], r[12]], "status": r[13],
            "settledAt": r[14], "revision": r[15]}
