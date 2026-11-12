"""`miniapp.telemetry.db` — events written by the Mini App only (admin-dashboard.md §3, ads-architecture.md §2).

Not authoritative: it can be lost or restored wholesale without any effect on access or money. It holds
neutral app activity per user and day (for activity segments), ad exposures (impression, control) and
clicks, and the user's own age confirmation for partners that require one. Nothing is written unless
MINIAPP_ADS_ENABLED is on. No message texts, no IP addresses; the country is the edge's two-letter code.

Writes are short write-through transactions on low-frequency paths (app open, a quick pick, an ad
event); the admin process opens this file read-only (`mode=ro`).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
DAY = 86400
EVENTS_RETENTION_DAYS = 90
ACTIVITY_RETENTION_DAYS = 400
CLICK_DEDUP_S = 10
ACTIVITY_FIELDS = ("requests", "ai_opened", "ai_created", "quick_single_ok", "quick_coupon_ok", "quick_failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_activity (
    day             TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    first_at        INTEGER NOT NULL,
    last_at         INTEGER NOT NULL,
    requests        INTEGER NOT NULL DEFAULT 0,
    ai_opened       INTEGER NOT NULL DEFAULT 0,
    ai_created      INTEGER NOT NULL DEFAULT 0,
    quick_single_ok INTEGER NOT NULL DEFAULT 0,
    quick_coupon_ok INTEGER NOT NULL DEFAULT 0,
    quick_failed    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id)
);
CREATE INDEX IF NOT EXISTS user_activity_user ON user_activity(user_id, day);
CREATE TABLE IF NOT EXISTS ad_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          INTEGER NOT NULL,
    day         TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    variant     TEXT NOT NULL,
    placement   TEXT NOT NULL,
    event_key   TEXT NOT NULL,
    country     TEXT,
    kind        TEXT NOT NULL CHECK (kind IN ('impression', 'control', 'click'))
);
-- One impression (or control exposure) per user, campaign, variant, event and day (§6).
CREATE UNIQUE INDEX IF NOT EXISTS ad_events_one_exposure
    ON ad_events(day, user_id, campaign_id, variant, event_key, kind) WHERE kind IN ('impression', 'control');
CREATE INDEX IF NOT EXISTS ad_events_campaign ON ad_events(campaign_id, day);
CREATE INDEX IF NOT EXISTS ad_events_user ON ad_events(user_id, kind, at);
-- The user's own statement "I am N or older" for a partner's age gate (the highest N kept).
CREATE TABLE IF NOT EXISTS age_confirmations (
    user_id      INTEGER PRIMARY KEY,
    age          INTEGER NOT NULL,
    confirmed_at INTEGER NOT NULL
);
"""


class TelemetrySchemaError(RuntimeError):
    """The file was written by a newer release: refuse rather than guess (§3.2)."""


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def telemetry_path(db_path: Path) -> Path:
    """`miniapp.db` -> `miniapp.telemetry.db` next to it (same directory, the shared data volume)."""
    return db_path.with_name(db_path.stem + ".telemetry" + (db_path.suffix or ".db"))


class Telemetry:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        else:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
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
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] > SCHEMA_VERSION:
                raise TelemetrySchemaError(f"telemetry schema {row[0]} is newer than {SCHEMA_VERSION}")

    def exists(self) -> bool:
        return self.path.is_file()

    # ---- activity ---------------------------------------------------------------------------

    def record_activity(self, user_id: int, now: int, field: str = "requests") -> None:
        if field not in ACTIVITY_FIELDS:
            raise ValueError(field)
        with self._write() as conn:
            conn.execute(
                f"INSERT INTO user_activity (day, user_id, first_at, last_at, {field}) VALUES (?, ?, ?, ?, 1) "
                f"ON CONFLICT(day, user_id) DO UPDATE SET last_at = MAX(last_at, excluded.last_at), "
                f"{field} = {field} + 1", (day_of(now), user_id, now, now))

    def activity(self, user_id: int, since_day: str) -> list[dict]:
        """Rows of one user from `since_day` on (UTC days), oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, requests, quick_single_ok, quick_coupon_ok FROM user_activity WHERE user_id = ? "
                "AND day >= ? ORDER BY day", (user_id, since_day)).fetchall()
        return [{"day": r[0], "requests": r[1], "forecasts": r[2], "coupons": r[3]} for r in rows]

    def last_visit_before(self, user_id: int, before_day: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(day) FROM user_activity WHERE user_id = ? AND day < ?",
                               (user_id, before_day)).fetchone()
        return row[0] if row else None

    # ---- ads --------------------------------------------------------------------------------

    def exposure(self, kind: str, user_id: int, campaign_id: int, variant: str, placement: str, event_key: str,
                 country: str | None, now: int) -> bool:
        """An impression or a control exposure; False if this one was already counted today."""
        if kind not in ("impression", "control"):
            raise ValueError(kind)
        with self._write() as conn:
            return conn.execute(
                "INSERT OR IGNORE INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, country, "
                "kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, day_of(now), user_id, campaign_id, variant, placement, event_key, country, kind)).rowcount > 0

    def click(self, user_id: int, campaign_id: int, variant: str, placement: str, event_key: str,
              country: str | None, now: int) -> bool:
        """A click; repeated taps on the same offer within CLICK_DEDUP_S count once."""
        with self._write() as conn:
            recent = conn.execute(
                "SELECT 1 FROM ad_events WHERE user_id = ? AND kind = 'click' AND campaign_id = ? AND variant = ? "
                "AND event_key = ? AND at > ?", (user_id, campaign_id, variant, event_key, now - CLICK_DEDUP_S)).fetchone()
            if recent is not None:
                return False
            conn.execute(
                "INSERT INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, country, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'click')",
                (now, day_of(now), user_id, campaign_id, variant, placement, event_key, country))
        return True

    def clicks_since(self, user_id: int, since: int) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM ad_events WHERE user_id = ? AND kind = 'click' AND at >= ?",
                                (user_id, since)).fetchone()[0]

    def confirm_age(self, user_id: int, age: int, now: int) -> None:
        with self._write() as conn:
            conn.execute("INSERT INTO age_confirmations (user_id, age, confirmed_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET age = MAX(age, excluded.age), "
                         "confirmed_at = excluded.confirmed_at", (user_id, age, now))

    def confirmed_age(self, user_id: int) -> int:
        """The highest age the user confirmed (0 = never)."""
        with self._connect() as conn:
            row = conn.execute("SELECT age FROM age_confirmations WHERE user_id = ?", (user_id,)).fetchone()
        return int(row[0]) if row else 0

    # ---- analytics reads (the admin opens this file read-only) -------------------------------

    def ad_totals(self, since_day: str, group: str = "variant", campaign_id: int | None = None) -> list[dict]:
        """Events, distinct users per (campaign, <group>, kind) from `since_day`; group: variant | day | country."""
        column = {"variant": "variant", "day": "day", "country": "COALESCE(country, '')"}[group]
        where, args = "day >= ?", [since_day]
        if campaign_id is not None:
            where += " AND campaign_id = ?"
            args.append(campaign_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT campaign_id, {column}, kind, COUNT(*), COUNT(DISTINCT user_id) FROM ad_events WHERE {where} "
                f"GROUP BY campaign_id, {column}, kind ORDER BY campaign_id, {column}", args).fetchall()
        return [{"campaignId": r[0], "key": r[1], "kind": r[2], "events": r[3], "users": r[4]} for r in rows]

    def last_click_variants(self, campaign_id: int, user_ids: list[int], since: int) -> dict[int, str]:
        """The variant each user last clicked on for this campaign (attribution of a confirmed conversion)."""
        if not user_ids:
            return {}
        out: dict[int, str] = {}
        with self._connect() as conn:
            for start in range(0, len(user_ids), 500):
                chunk = user_ids[start:start + 500]
                rows = conn.execute(
                    f"SELECT user_id, variant FROM ad_events WHERE kind = 'click' AND campaign_id = ? AND at >= ? "
                    f"AND user_id IN ({','.join('?' * len(chunk))}) ORDER BY at", (campaign_id, since, *chunk)).fetchall()
                out.update({uid: variant for uid, variant in rows})
        return out

    def purge(self, now: int) -> None:
        events_before = day_of(now - EVENTS_RETENTION_DAYS * DAY)
        activity_before = day_of(now - ACTIVITY_RETENTION_DAYS * DAY)
        with self._write() as conn:
            conn.execute("DELETE FROM ad_events WHERE day < ?", (events_before,))
            conn.execute("DELETE FROM user_activity WHERE day < ?", (activity_before,))


def days_back(now: int, days: int) -> str:
    """The UTC day `days` days before today (today = 0)."""
    return (datetime.fromtimestamp(now, UTC).date() - timedelta(days=days)).strftime("%Y-%m-%d")
