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
# Daily aggregates of ad events and partner conversions: 13 months of reports after the raw events are gone.
AGGREGATE_RETENTION_DAYS = 400
ATTRIBUTION_DAYS = 30  # a partner conversion counts for a click at most this old
SEGMENT_CLASSES = ("new", "active", "other")  # coarse viewer class stored with each event (analytics only)
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
-- Matches the server recommended to the user (quick pick): offers are issued only for these, until `until`.
CREATE TABLE IF NOT EXISTS recent_picks (
    user_id   INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    until     INTEGER NOT NULL,
    PRIMARY KEY (user_id, event_key)
);
-- The last country the edge reported at sign-in (two letters, no IP) and the Telegram client platform (D1).
CREATE TABLE IF NOT EXISTS user_edge (
    user_id    INTEGER PRIMARY KEY,
    country    TEXT,
    platform   TEXT,
    updated_at INTEGER NOT NULL
);
-- Active (UTC day, UTC hour, user): only the fact, for the activity-by-hour chart (D1).
CREATE TABLE IF NOT EXISTS activity_hours (
    day     TEXT NOT NULL,
    hour    INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    user_id INTEGER NOT NULL,
    PRIMARY KEY (day, hour, user_id)
);
-- Matches the server recommended to the user, per placement (quick pick, AI recommended bet), with the market
-- it recommended (the offer's price is looked up for exactly that market). Replaces recent_picks.
CREATE TABLE IF NOT EXISTS offer_picks (
    user_id   INTEGER NOT NULL,
    placement TEXT NOT NULL,
    event_key TEXT NOT NULL,
    market    TEXT,
    until     INTEGER NOT NULL,
    PRIMARY KEY (user_id, placement, event_key)
);
-- One row per click with its own id: the id goes into the partner link ({click_id}) and comes back in the
-- partner's postback. No IP, no device data.
CREATE TABLE IF NOT EXISTS ad_clicks (
    click_id    TEXT PRIMARY KEY,
    at          INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    variant     TEXT NOT NULL,
    partner_id  INTEGER NOT NULL,
    placement   TEXT NOT NULL,
    event_key   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ad_clicks_at ON ad_clicks(at);
CREATE INDEX IF NOT EXISTS ad_clicks_user ON ad_clicks(user_id, at);
-- Partner conversions (postback): one per (partner, partner's transaction id) — a repeated postback is a no-op.
CREATE TABLE IF NOT EXISTS ad_postbacks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id  INTEGER NOT NULL,
    txn_id      TEXT NOT NULL,
    click_id    TEXT NOT NULL,
    event       TEXT NOT NULL,
    at          INTEGER NOT NULL,
    day         TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    variant     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'postback',
    UNIQUE (partner_id, txn_id)
);
CREATE INDEX IF NOT EXISTS ad_postbacks_campaign ON ad_postbacks(campaign_id, day);
-- Completed UTC days of ad_events, rolled up once (events by kind, variant, country and viewer class).
CREATE TABLE IF NOT EXISTS ad_daily (
    day         TEXT NOT NULL,
    campaign_id INTEGER NOT NULL,
    variant     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    country     TEXT NOT NULL,
    segment     TEXT NOT NULL,
    events      INTEGER NOT NULL,
    PRIMARY KEY (day, campaign_id, variant, kind, country, segment)
);
CREATE TABLE IF NOT EXISTS telemetry_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- The user's own statement "I am N or older" for a partner's age gate (the highest N kept).
CREATE TABLE IF NOT EXISTS age_confirmations (
    user_id      INTEGER PRIMARY KEY,
    age          INTEGER NOT NULL,
    confirmed_at INTEGER NOT NULL
);
"""


_MIGRATIONS = (
    "ALTER TABLE ad_events ADD COLUMN segment TEXT",  # viewer class (SEGMENT_CLASSES) when the event was recorded
)


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
        self._purged_day: str | None = None
        self._purge_retry_at = 0

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
            for ddl in _MIGRATIONS:  # additive: a column that is already there is fine
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise
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
            conn.execute("INSERT OR IGNORE INTO activity_hours (day, hour, user_id) VALUES (?, ?, ?)",
                         (day_of(now), datetime.fromtimestamp(now, UTC).hour, user_id))

    def record_edge(self, user_id: int, country: str | None, platform: str | None, now: int) -> None:
        """Sign-in facts for the admin's geography / platform cards: a value not reported keeps the previous one."""
        if country is None and platform is None:
            return
        with self._write() as conn:
            conn.execute(
                "INSERT INTO user_edge (user_id, country, platform, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET country = COALESCE(excluded.country, country), "
                "platform = COALESCE(excluded.platform, platform), updated_at = excluded.updated_at",
                (user_id, country, platform, now))

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
                 country: str | None, now: int, segment: str | None = None, caps: tuple[int, int] = (0, 0)) -> bool:
        """An impression or a control exposure; False if this one was already counted today, or if it would
        exceed the campaign's frequency caps (per day, in all; 0 = none). The cap is checked inside the same
        write transaction as the insert, so concurrent impressions of different matches cannot both pass it."""
        if kind not in ("impression", "control"):
            raise ValueError(kind)
        with self._write() as conn:
            per_day, total = caps
            if kind == "impression" and (per_day or total):
                today, in_all = self._counts(conn, user_id, campaign_id, event_key, now)
                if (per_day and today >= per_day) or (total and in_all >= total):
                    return False
            return conn.execute(
                "INSERT OR IGNORE INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, country, "
                "kind, segment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, day_of(now), user_id, campaign_id, variant, placement, event_key, country, kind,
                 segment)).rowcount > 0

    def click(self, user_id: int, campaign_id: int, variant: str, placement: str, event_key: str,
              country: str | None, now: int, segment: str | None = None, click_id: str | None = None,
              partner_id: int | None = None) -> bool:
        """A click; the same offer (user, campaign, variant, match) counts once per UTC day. A signed click link lives
        two days and could be replayed without limit (CRM audit B3: ~8,600 counted clicks a day from one user); every
        tap still opens the partner. Every click id is kept in ad_clicks (the partner link carries that id; its
        postback finds the click by it); a repeated tap reuses the day's id (`click_id_for`), so replays add no rows."""
        with self._write() as conn:
            if click_id is not None and partner_id is not None:
                conn.execute("INSERT OR IGNORE INTO ad_clicks (click_id, at, user_id, campaign_id, variant, partner_id, placement, "
                             "event_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (click_id, now, user_id, campaign_id, variant, partner_id, placement, event_key))
            recent = conn.execute(
                "SELECT 1 FROM ad_events WHERE user_id = ? AND kind = 'click' AND campaign_id = ? AND variant = ? "
                "AND event_key = ? AND day = ?", (user_id, campaign_id, variant, event_key, day_of(now))).fetchone()
            if recent is not None:
                return False
            conn.execute(
                "INSERT INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, country, kind, "
                "segment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'click', ?)",
                (now, day_of(now), user_id, campaign_id, variant, placement, event_key, country, segment))
        return True

    def click_id_for(self, user_id: int, campaign_id: int, variant: str, partner_id: int, event_key: str,
                     now: int) -> str | None:
        """The click id this offer already got today (UTC day), if any: a replayed tap opens the same partner link
        instead of adding a row (Codex ads merge r2 P1)."""
        start = now - now % DAY
        with self._connect() as conn:
            row = conn.execute("SELECT click_id FROM ad_clicks WHERE user_id = ? AND campaign_id = ? AND variant = ? "
                               "AND partner_id = ? AND event_key = ? AND at >= ? AND at < ? ORDER BY at LIMIT 1",
                               (user_id, campaign_id, variant, partner_id, event_key, start, start + DAY)).fetchone()
        return row[0] if row else None

    def cap_reached(self, user_id: int, campaign_id: int, event_key: str, now: int, caps: tuple[int, int]) -> bool:
        """Whether the campaign's frequency caps (per day, in all; 0 = none) leave no room for this match now."""
        per_day, total = caps
        if not (per_day or total):
            return False
        today, in_all = self.impression_counts(user_id, campaign_id, event_key, now)
        return bool((per_day and today >= per_day) or (total and in_all >= total))

    def impression_counts(self, user_id: int, campaign_id: int, event_key: str, now: int) -> tuple[int, int]:
        """Impressions of this campaign for the user: (today on OTHER matches, in all but today's one of this
        match) — frequency caps. Today's impression of the match on screen does not count against itself (the card
        stays visible when reopened); the same match on an earlier day does count toward the total."""
        with self._connect() as conn:
            return self._counts(conn, user_id, campaign_id, event_key, now)

    @staticmethod
    def _counts(conn: sqlite3.Connection, user_id: int, campaign_id: int, event_key: str, now: int) -> tuple[int, int]:
        today, total = conn.execute(
            "SELECT COALESCE(SUM(day = ?1 AND event_key != ?4), 0), COALESCE(SUM(NOT (day = ?1 AND event_key = ?4)), 0) "
            "FROM ad_events WHERE user_id = ?2 AND kind = 'impression' AND campaign_id = ?3",
            (day_of(now), user_id, campaign_id, event_key)).fetchone()
        return int(today), int(total)

    # ---- partner conversions --------------------------------------------------------------------

    def record_conversion(self, partner_id: int, txn_id: str, click_id: str, event: str, now: int,
                          source: str = "postback") -> str:
        """A partner's conversion for one of our clicks. Idempotent by (partner, txn id). Returns accepted |
        duplicate | unknown_click (no such click, another partner's click, or older than ATTRIBUTION_DAYS)."""
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM ad_postbacks WHERE partner_id = ? AND txn_id = ?",
                            (partner_id, txn_id)).fetchone() is not None:
                return "duplicate"
            click = conn.execute("SELECT user_id, campaign_id, variant, at FROM ad_clicks WHERE click_id = ? "
                                 "AND partner_id = ?", (click_id, partner_id)).fetchone()
            if click is None or click[3] < now - ATTRIBUTION_DAYS * DAY:
                return "unknown_click"
            conn.execute("INSERT INTO ad_postbacks (partner_id, txn_id, click_id, event, at, day, user_id, campaign_id, "
                         "variant, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (partner_id, txn_id, click_id, event, now, day_of(now), click[0], click[1], click[2], source))
        return "accepted"

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
        """Events, distinct users per (campaign, <group>, kind) from `since_day`; group: variant | day | country |
        segment (the viewer class recorded with the event; '' for events from before it was recorded)."""
        column = {"variant": "variant", "day": "day", "country": "COALESCE(country, '')",
                  "segment": "COALESCE(segment, '')"}[group]
        where, args = "day >= ?", [since_day]
        if campaign_id is not None:
            where += " AND campaign_id = ?"
            args.append(campaign_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT campaign_id, {column}, kind, COUNT(*), COUNT(DISTINCT user_id) FROM ad_events WHERE {where} "
                f"GROUP BY campaign_id, {column}, kind ORDER BY campaign_id, {column}", args).fetchall()
        return [{"campaignId": r[0], "key": r[1], "kind": r[2], "events": r[3], "users": r[4]} for r in rows]

    def daily_totals(self, since_day: str, group: str = "variant", campaign_id: int | None = None,
                     until_day: str | None = None) -> list[dict]:
        """Like ad_totals over the daily aggregates (13 months; completed days only). Events only: distinct users
        cannot be added up across days."""
        column = {"variant": "variant", "day": "day", "country": "country", "segment": "segment"}[group]
        where, args = "day >= ?", [since_day]
        if until_day is not None:
            where += " AND day <= ?"
            args.append(until_day)
        if campaign_id is not None:
            where += " AND campaign_id = ?"
            args.append(campaign_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT campaign_id, {column}, kind, SUM(events) FROM ad_daily WHERE {where} "
                f"GROUP BY campaign_id, {column}, kind ORDER BY campaign_id, {column}", args).fetchall()
        return [{"campaignId": r[0], "key": r[1], "kind": r[2], "events": r[3], "users": None} for r in rows]

    def long_totals(self, since_day: str, group: str = "variant", campaign_id: int | None = None) -> list[dict]:
        """Event counts for periods longer than the raw retention: the daily aggregates up to the last rolled day,
        the raw events after it (today, and any day the rollup has not reached yet). Events only."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM telemetry_meta WHERE key = 'rolled_until'").fetchone()
        rolled = row[0] if row else None
        rows = self.daily_totals(since_day, group, campaign_id, until_day=rolled) if rolled else []
        rows += self.ad_totals(max(since_day, _next_day(rolled)) if rolled else since_day, group, campaign_id)
        merged: dict[tuple, dict] = {}
        for r in rows:
            item = merged.setdefault((r["campaignId"], r["key"], r["kind"]), {**r, "events": 0, "users": None})
            item["events"] += r["events"]
        return sorted(merged.values(), key=lambda r: (r["campaignId"], str(r["key"]), r["kind"]))

    def postback_totals(self, since_day: str, campaign_id: int | None = None) -> list[dict]:
        """Partner conversions (postbacks and the partner API hook) per campaign, variant and event name."""
        where, args = "day >= ?", [since_day]
        if campaign_id is not None:
            where += " AND campaign_id = ?"
            args.append(campaign_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT campaign_id, variant, event, COUNT(*), COUNT(DISTINCT user_id) FROM ad_postbacks WHERE {where} "
                "GROUP BY campaign_id, variant, event ORDER BY campaign_id, variant, event", args).fetchall()
        return [{"campaignId": r[0], "variant": r[1], "event": r[2], "count": r[3], "users": r[4]} for r in rows]

    def engaged_clickers(self, since_day: str, campaign_id: int | None = None) -> dict[tuple[int, str], int]:
        """Distinct clickers per (campaign, variant) who also have an impression of that variant from `since_day`:
        the numerator of the unique CTR. A click can outlive its impression (shown before the period, purged, or
        the impression write failed), so raw clickers may exceed reach."""
        where, args = "c.kind = 'click' AND c.day >= ?", [since_day, since_day]  # impression day, click day
        if campaign_id is not None:
            where += " AND c.campaign_id = ?"
            args.append(campaign_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.campaign_id, c.variant, COUNT(DISTINCT c.user_id) FROM ad_events c WHERE EXISTS ("
                "SELECT 1 FROM ad_events i WHERE i.kind = 'impression' AND i.day >= ? AND i.user_id = c.user_id "
                f"AND i.campaign_id = c.campaign_id AND i.variant = c.variant) AND {where} "
                "GROUP BY c.campaign_id, c.variant", args).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

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
                    f"AND user_id IN ({','.join('?' * len(chunk))}) ORDER BY at, id", (campaign_id, since, *chunk)).fetchall()
                out.update({uid: variant for uid, variant in rows})
        return out

    def add_picks(self, user_id: int, placement: str, picks: list[tuple[str, str | None]], until: int) -> None:
        """(event key, market) the server recommended to the user in `placement`; the latest market wins."""
        with self._write() as conn:
            conn.executemany("INSERT INTO offer_picks (user_id, placement, event_key, market, until) VALUES (?, ?, ?, ?, ?) "
                             "ON CONFLICT(user_id, placement, event_key) DO UPDATE SET market = excluded.market, "
                             "until = MAX(until, excluded.until)",
                             [(user_id, placement, e, m, until) for e, m in picks])

    def pick(self, user_id: int, placement: str, event_key: str, now: int) -> tuple[bool, str | None]:
        """(picked, market) of a live recommendation of this match to the user in `placement`."""
        with self._connect() as conn:
            row = conn.execute("SELECT market FROM offer_picks WHERE user_id = ? AND placement = ? AND event_key = ? "
                               "AND until > ?", (user_id, placement, event_key, now)).fetchone()
        return (False, None) if row is None else (True, row[0])

    def rollup(self, now: int) -> int:
        """Rolls every completed UTC day of ad_events not rolled yet into ad_daily (once per day: the days are
        complete, events are stamped with the time they are recorded). Returns the number of days rolled."""
        today = day_of(now)
        with self._write() as conn:
            row = conn.execute("SELECT value FROM telemetry_meta WHERE key = 'rolled_until'").fetchone()
            after = row[0] if row else ""
            days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM ad_events WHERE day > ? AND day < ? ORDER BY day",
                                               (after, today))]
            for day in days:
                conn.execute(
                    "INSERT OR REPLACE INTO ad_daily (day, campaign_id, variant, kind, country, segment, events) "
                    "SELECT day, campaign_id, variant, kind, COALESCE(country, ''), COALESCE(segment, ''), COUNT(*) "
                    "FROM ad_events WHERE day = ? GROUP BY day, campaign_id, variant, kind, COALESCE(country, ''), "
                    "COALESCE(segment, '')", (day,))
            last = days_back(now, 1)
            if last > after:
                conn.execute("INSERT INTO telemetry_meta (key, value) VALUES ('rolled_until', ?) ON CONFLICT(key) "
                             "DO UPDATE SET value = excluded.value", (last,))
        return len(days)

    def maybe_purge(self, now: int) -> None:
        """Retention while the process runs: at most once per UTC day (best effort, called on app traffic)."""
        today = day_of(now)
        if self._purged_day == today or now < self._purge_retry_at:
            return
        # The day counts as done only after a successful purge; a failure (e.g. a lock) retries in an hour,
        # not on every request (each attempt can wait out the busy timeout).
        self._purge_retry_at = now + 3600
        self.rollup(now)  # before the purge: no event leaves without its day in the aggregates
        self.purge(now)
        self._purged_day = today

    def purge(self, now: int) -> None:
        events_before = day_of(now - EVENTS_RETENTION_DAYS * DAY)
        activity_before = day_of(now - ACTIVITY_RETENTION_DAYS * DAY)
        with self._write() as conn:
            conn.execute("DELETE FROM ad_events WHERE day < ?", (events_before,))
            conn.execute("DELETE FROM user_activity WHERE day < ?", (activity_before,))
            conn.execute("DELETE FROM activity_hours WHERE day < ?", (activity_before,))
            # A user's country and platform go with their activity (same retention, counted from the last sign-in).
            conn.execute("DELETE FROM user_edge WHERE updated_at < ?", (now - ACTIVITY_RETENTION_DAYS * DAY,))
            conn.execute("DELETE FROM recent_picks WHERE until <= ?", (now,))
            conn.execute("DELETE FROM offer_picks WHERE until <= ?", (now,))
            conn.execute("DELETE FROM ad_clicks WHERE at < ?", (now - EVENTS_RETENTION_DAYS * DAY,))
            aggregates_before = day_of(now - AGGREGATE_RETENTION_DAYS * DAY)
            conn.execute("DELETE FROM ad_daily WHERE day < ?", (aggregates_before,))
            conn.execute("DELETE FROM ad_postbacks WHERE day < ?", (aggregates_before,))


def _next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=1)).strftime("%Y-%m-%d")


def days_back(now: int, days: int) -> str:
    """The UTC day `days` days before today (today = 0)."""
    return (datetime.fromtimestamp(now, UTC).date() - timedelta(days=days)).strftime("%Y-%m-%d")
