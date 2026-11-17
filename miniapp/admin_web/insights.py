"""Per-user activity and GEO for the admin's users list and card, read from data that already exists.

Sources (no new collection, no external services):
- `miniapp.telemetry.db` (written by the Mini App only when MINIAPP_ADS_ENABLED is on; opened here READ-ONLY):
  `user_activity` rows per user and UTC day, and the edge's two-letter country code on `ad_events`;
- the Mini App's `users.lang` for a country GUESS from the interface language, only where a language points to
  one country (az -> AZ, tr -> TR, uz -> UZ); every guess is labelled `source = "lang"`. Portuguese is not
  guessed: the Mini App maps every Portuguese locale to pt-BR.

A missing or unreadable telemetry file means "not collected": every reader answers empty, never an error, so
the panel keeps working when the Mini App runs without telemetry.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

DAY = 86400
# Short waits: these are display reads in request handlers; a locked file shows "—" instead of stalling the page.
READ_TIMEOUT_S = 1
REQUIRED_COLUMNS = {"day", "user_id", "first_at", "last_at", "requests", "ai_opened", "ai_created",
                    "quick_single_ok", "quick_coupon_ok", "quick_failed"}
# Interface languages that point to one country; ru/en/es/pt are spoken in too many to guess.
LANG_COUNTRY = {"az": "AZ", "tr": "TR", "uz": "UZ"}
_CHUNK = 500


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def days_back(now: int, days: int) -> str:
    return (datetime.fromtimestamp(now, UTC).date() - timedelta(days=days)).strftime("%Y-%m-%d")


def _chunks(ids: list[int]) -> Iterator[list[int]]:
    for start in range(0, len(ids), _CHUNK):
        yield ids[start:start + _CHUNK]


# Segment windows, both inside what telemetry keeps (activity 400 days, ad events 90 days).
OPENED_WINDOW_DAYS = 365
AD_CLICK_WINDOW_DAYS = 60


class UserInsights:
    def __init__(self, telemetry_file: Path) -> None:
        self.path = telemetry_file

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection | None]:
        if not self.path.is_file():
            yield None
            return
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT_S)
        except sqlite3.Error:
            yield None
            return
        try:
            conn.execute(f"PRAGMA busy_timeout={READ_TIMEOUT_S * 1000}")
            yield conn
        finally:
            conn.close()

    def available(self, strict: bool = False) -> bool:
        """The file exists and its `user_activity` has every column read here (an older schema is not collected).
        `strict`: an existing file that cannot be opened or read raises instead of reading as "not collected"."""
        try:
            with self._connect() as conn:
                if conn is None:
                    if strict and self.path.is_file():
                        raise sqlite3.OperationalError("telemetry file cannot be opened")
                    return False
                return REQUIRED_COLUMNS <= {r[1] for r in conn.execute("PRAGMA table_info(user_activity)")}
        except sqlite3.Error:
            if strict:
                raise
            return False

    def _rows(self, sql: str, ids: list[int] | None, args: tuple = (), strict: bool = False) -> list[tuple]:
        """Runs `sql` (with `{ids}` replaced by an IN list, or `1 = 1` for everyone); empty when not collected.
        `strict`: a read error is raised (aggregates must never show a failed read as zero activity)."""
        try:
            with self._connect() as conn:
                if conn is None:
                    if strict:
                        raise sqlite3.OperationalError("telemetry file cannot be opened")
                    return []
                if ids is None:
                    return conn.execute(sql.format(ids="1 = 1"), args).fetchall()
                out: list[tuple] = []
                for chunk in _chunks(ids):
                    out += conn.execute(sql.format(ids=f"user_id IN ({','.join('?' * len(chunk))})"),
                                        (*args, *chunk)).fetchall()
                return out
        except sqlite3.Error:  # an older file without a table, a lock past the timeout: not collected
            if strict:
                raise
            return []

    def activity(self, user_ids: Iterable[int] | None, now: int, window_days: int = 30,
                 strict: bool = False) -> dict[int, dict]:
        """Per user over the whole retained history: last seen, forecasts (quick single + coupon + AI created),
        AI opened/created, and active days within the last `window_days`. `strict`: a failed read raises."""
        ids = None if user_ids is None else sorted(set(user_ids))
        if ids == []:
            return {}
        since = days_back(now, window_days - 1)
        rows = self._rows(
            "SELECT user_id, MAX(last_at), SUM(quick_single_ok + quick_coupon_ok + ai_created), SUM(ai_created), "
            "SUM(ai_opened), SUM(requests), SUM(day >= ?), MIN(first_at) FROM user_activity WHERE {ids} GROUP BY user_id",
            ids, (since,), strict=strict)
        return {int(r[0]): {"lastSeenAt": r[1], "forecasts": int(r[2] or 0), "aiCreated": int(r[3] or 0),
                            "aiOpened": int(r[4] or 0), "requests": int(r[5] or 0), "activeDays": int(r[6] or 0),
                            "firstSeenAt": r[7]} for r in rows}

    def daily(self, user_id: int, now: int, days: int = 30) -> list[dict]:
        """One user's activity per UTC day for the card (missing days are zero)."""
        since = days_back(now, days - 1)
        rows: dict[str, tuple] = {}
        try:
            with self._connect() as conn:
                if conn is not None:
                    rows = {r[0]: r for r in conn.execute(
                        "SELECT day, requests, quick_single_ok + quick_coupon_ok + ai_created, ai_opened "
                        "FROM user_activity WHERE user_id = ? AND day >= ?", (user_id, since))}
        except sqlite3.Error:
            rows = {}
        start = datetime.fromtimestamp(now, UTC).date() - timedelta(days=days - 1)
        out = []
        for offset in range(days):
            day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
            r = rows.get(day)
            out.append({"day": day, "requests": int(r[1]) if r else 0, "forecasts": int(r[2]) if r else 0,
                        "aiOpened": int(r[3]) if r else 0})
        return out

    def week_offsets(self, members: list[tuple[int, str]], until_day: str, strict: bool = False) -> dict[int, set[int]]:
        """For each (user, registration day): the week numbers k (days 7k..7k+6 after registration) in which the
        user was active, up to `until_day`. Aggregated in SQLite over a TEMP table of the members: at most
        users x weeks rows come back, never the raw user-days."""
        if not members:
            return {}
        try:
            with self._connect() as conn:
                if conn is None:
                    if strict:
                        raise sqlite3.OperationalError("telemetry file cannot be opened")
                    return {}
                conn.execute("CREATE TEMP TABLE members (user_id INTEGER PRIMARY KEY, reg TEXT NOT NULL)")
                conn.executemany("INSERT OR REPLACE INTO temp.members (user_id, reg) VALUES (?, ?)", members)
                rows = conn.execute(
                    "SELECT a.user_id, CAST((julianday(a.day) - julianday(m.reg)) / 7 AS INTEGER) AS k "
                    # CROSS JOIN fixes the loop order: per member, an index lookup on user_activity(user_id, day).
                    "FROM temp.members m CROSS JOIN user_activity a ON a.user_id = m.user_id "
                    "WHERE a.day >= m.reg AND a.day <= ? GROUP BY a.user_id, k", (until_day,)).fetchall()
        except sqlite3.Error:
            if strict:
                raise
            return {}
        out: dict[int, set[int]] = {}
        for uid, k in rows:
            out.setdefault(int(uid), set()).add(int(k))
        return out

    def edge_overview(self, now: int, days: int) -> dict:
        """Users active in the period by sign-in country and Telegram platform, and activity by weekday x hour in
        Baku time (UTC+4, no DST) — distinct users per hour (D1 collection; no IP, no device data)."""
        # The rolling period (the last `days` x 24 h, as every admin period), to the hour the data has.
        start, end = datetime.fromtimestamp(now - days * 86400, UTC), datetime.fromtimestamp(now, UTC)
        # Bounded on both sides: a row dated ahead (clock skew between the Mini App and the admin) is not counted.
        in_period = "(day > ? OR (day = ? AND hour >= ?)) AND (day < ? OR (day = ? AND hour <= ?))"
        args = (start.strftime("%Y-%m-%d"), start.strftime("%Y-%m-%d"), start.hour,
                end.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), end.hour)
        try:
            with self._connect() as conn:
                if conn is None:
                    return {"source": "not_collected"}
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if not {"user_edge", "activity_hours"} <= tables:
                    return {"source": "not_collected"}
                active = f"SELECT DISTINCT user_id FROM activity_hours WHERE {in_period}"
                geo = conn.execute(f"SELECT e.country, COUNT(*) FROM ({active}) a JOIN user_edge e USING (user_id) "
                                   "WHERE e.country IS NOT NULL GROUP BY e.country ORDER BY 2 DESC, 1", args).fetchall()
                platforms = conn.execute(f"SELECT e.platform, COUNT(*) FROM ({active}) a JOIN user_edge e USING (user_id) "
                                         "WHERE e.platform IS NOT NULL GROUP BY e.platform ORDER BY 2 DESC, 1",
                                         args).fetchall()
                hours = conn.execute(f"SELECT day, hour, COUNT(*) FROM activity_hours WHERE {in_period} GROUP BY day, hour",
                                     args).fetchall()
                users = conn.execute(f"SELECT COUNT(*) FROM ({active})", args).fetchone()[0]
        except sqlite3.Error:
            return {"source": "unavailable"}
        heat = [[0] * 24 for _ in range(7)]
        for day, hour, count in hours:
            local = datetime.strptime(str(day), "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(hours=int(hour) + 4)
            heat[local.weekday()][local.hour] += int(count)
        return {"source": "telemetry", "activeUsers": int(users),
                "geo": [{"code": str(c).upper()[:2], "count": int(n)} for c, n in geo if isinstance(c, str) and len(c) == 2],
                "platforms": [{"name": str(pl)[:32], "count": int(n)} for pl, n in platforms],
                "activityHeat": per_slot_average(heat, now - days * 86400, now)}

    def segment_extras(self, user_ids: list[int], now: int, parts: frozenset[str]) -> dict[int, dict] | None:
        """Per user, for segment rules, only the `parts` asked for: "activity" (forecasts, AI analyses and active days
        over the last 30 days, and "opened" = any activity in OPENED_WINDOW_DAYS), "edge" (sign-in country and
        platform), "ads" (ad clicks in AD_CLICK_WINDOW_DAYS). Users without data are absent. None when telemetry is not collected or cannot be read: a segment
        must never read a failure as "did not" (strict reads)."""
        try:
            if not self.available(strict=True):
                return None
            out: dict[int, dict] = {}
            if "activity" in parts:
                for uid, fc, ai, days in self._rows(
                        "SELECT user_id, SUM(quick_single_ok + quick_coupon_ok + ai_created), SUM(ai_created), COUNT(*) "
                        "FROM user_activity WHERE day >= ? AND day <= ? AND {ids} GROUP BY user_id", user_ids,
                        (days_back(now, 29), days_back(now, 0)), strict=True):
                    out[int(uid)] = {"forecasts": int(fc or 0), "aiCreated": int(ai or 0), "activeDays": int(days or 0)}
                # Windows inside retention (activity 400 d, ad events 90 d): "no" within them is a real "no".
                for (uid,) in self._rows("SELECT DISTINCT user_id FROM user_activity WHERE day >= ? AND day <= ? AND {ids}",
                                         user_ids, (days_back(now, OPENED_WINDOW_DAYS - 1), days_back(now, 0)),
                                         strict=True):
                    out.setdefault(int(uid), {})["opened"] = True
            if "edge" in parts:
                for uid, country, platform in self._rows("SELECT user_id, country, platform FROM user_edge WHERE {ids}",
                                                         user_ids, strict=True):
                    out.setdefault(int(uid), {}).update(country=country, platform=platform)
            if "ads" in parts:
                for uid, n in self._rows("SELECT user_id, COUNT(*) FROM ad_events WHERE kind = 'click' AND day >= ? "
                                         "AND day <= ? AND {ids} GROUP BY user_id", user_ids,
                                         (days_back(now, AD_CLICK_WINDOW_DAYS - 1), days_back(now, 0)), strict=True):
                    out.setdefault(int(uid), {})["adClicks"] = int(n)
            return out
        except sqlite3.Error:
            return None

    def edge_countries(self, user_ids: Iterable[int] | None) -> dict[int, str]:
        """The latest country code the edge reported for the user: the sign-in record (D1), else the latest ad event
        (both carry the edge's two-letter code; no IP is ever stored)."""
        ids = None if user_ids is None else sorted(set(user_ids))
        if ids == []:
            return {}
        signed = self._rows("SELECT user_id, country FROM user_edge WHERE {ids} AND country IS NOT NULL", ids)
        found = {int(uid): str(c).upper()[:2] for uid, c in signed if isinstance(c, str) and len(c) == 2 and c.isalpha()}
        rows = self._rows(
            "SELECT e.user_id, e.country FROM ad_events e JOIN (SELECT user_id AS uid, MAX(id) AS last FROM ad_events "
            "WHERE {ids} AND country IS NOT NULL AND country != '' GROUP BY user_id) m ON e.id = m.last", ids)
        from_ads = {int(uid): str(c).upper()[:2] for uid, c in rows if isinstance(c, str) and len(c) == 2 and c.isalpha()}
        return {**from_ads, **found}


def geo(user: dict, edge: dict[int, str]) -> dict:
    """{country, source}: the edge's code when known, else a guess from the interface language, else unknown."""
    if user["id"] in edge:
        return {"country": edge[user["id"]], "source": "edge"}
    guess = LANG_COUNTRY.get(user.get("lang") or "")
    return {"country": guess, "source": "lang" if guess else None}


COHORT_WEEKS = 8


def cohort_since(now: int) -> int:
    """Start (UTC midnight, a Monday) of the oldest cohort week."""
    today = datetime.fromtimestamp(now, UTC).date()
    first = today - timedelta(days=today.weekday()) - timedelta(weeks=COHORT_WEEKS - 1)
    return int(datetime(first.year, first.month, first.day, tzinfo=UTC).timestamp())


def product(ins: UserInsights, now: int, days: int, users: list[dict]) -> dict:
    """Product analytics over the telemetry (aggregates only): DAU per UTC day with WAU/MAU (unique users, not sums),
    forecasts requested and AI usage per day, and weekly registration cohorts with their week-N retention (share of
    the cohort active in days [7N, 7N + 7) after their own registration, among members whose week N is over; None when
    no member's week N is over). `users`: id and createdAt of everyone registered since cohort_since(now)."""
    try:
        if not ins.available(strict=True):  # no file, or an older schema
            return {"source": "not_collected"}
        return _product(ins, now, days, users)
    except sqlite3.Error:  # collected, but this read failed (a lock past the short timeout): never shown as zeros
        return {"source": "unavailable"}


def _product(ins: UserInsights, now: int, days: int, users: list[dict]) -> dict:
    first_day = days_back(now, days - 1)
    rows = ins._rows(
        "SELECT day, COUNT(DISTINCT user_id), SUM(quick_single_ok), SUM(quick_coupon_ok), SUM(ai_created), "
        "SUM(ai_opened), SUM(quick_failed) FROM user_activity WHERE {ids} AND day >= ? AND day <= ? GROUP BY day", None,
        (first_day, day_of(now)), strict=True)
    by_day = {r[0]: r for r in rows}
    start = datetime.fromtimestamp(now, UTC).date() - timedelta(days=days - 1)
    series = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        r = by_day.get(day)
        series.append({"day": day, "dau": int(r[1]) if r else 0, "quick": int(r[2] or 0) if r else 0,
                       "coupons": int(r[3] or 0) if r else 0, "aiCreated": int(r[4] or 0) if r else 0,
                       "aiOpened": int(r[5] or 0) if r else 0, "quickFailed": int(r[6] or 0) if r else 0})

    def unique_since(back: int) -> int:
        got = ins._rows("SELECT COUNT(DISTINCT user_id) FROM user_activity WHERE {ids} AND day >= ? AND day <= ?", None,
                        (days_back(now, back - 1), day_of(now)), strict=True)
        return int(got[0][0]) if got else 0

    # Cohorts: users registered in the last COHORT_WEEKS weeks (Monday-based UTC weeks).
    today = datetime.fromtimestamp(now, UTC).date()
    first_monday = today - timedelta(days=today.weekday()) - timedelta(weeks=COHORT_WEEKS - 1)
    cohort_users = [u for u in users if datetime.fromtimestamp(u["createdAt"], UTC).date() >= first_monday]
    regs = {u["id"]: datetime.fromtimestamp(u["createdAt"], UTC).date() for u in cohort_users}
    weeks_active = ins.week_offsets([(uid, reg.strftime("%Y-%m-%d")) for uid, reg in regs.items()],
                                    today.strftime("%Y-%m-%d"), strict=True)
    cohorts = []
    for w in range(COHORT_WEEKS):
        week_start = first_monday + timedelta(weeks=w)
        members = [uid for uid, reg in regs.items() if week_start <= reg < week_start + timedelta(weeks=1)]
        retention: list[float | None] = []
        for k in range(COHORT_WEEKS):
            # Only members whose week k (days 7k..7k+6 after their own registration) is over before today count;
            # none such: the cell is empty, never a zero.
            eligible = [uid for uid in members if regs[uid] + timedelta(days=7 * k + 7) <= today]
            hit = sum(1 for uid in eligible if k in weeks_active.get(uid, ()))
            retention.append(round(hit / len(eligible), 4) if eligible else None)
        cohorts.append({"week": week_start.strftime("%Y-%m-%d"), "size": len(members), "retention": retention})
    totals = {k: sum(d[k] for d in series) for k in ("quick", "coupons", "aiCreated", "aiOpened", "quickFailed")}
    return {"source": "telemetry", "series": series, "wau": unique_since(7), "mau": unique_since(30),
            "dau": series[-1]["dau"], # Delivered forecasts (successful quick picks, coupons, delivered AI analyses): failed attempts are not
            # recorded by the Mini App, so this is not a count of requests.
            "totals": {**totals, "forecasts": totals["quick"] + totals["coupons"] + totals["aiCreated"]},
            "cohorts": cohorts}


def slot_occurrences(start: int, end: int, offset_h: int = 4) -> list[list[int]]:
    """How many times each weekday x hour (Baku time, UTC+4 without DST) occurs in [start, end] (whole hours touched)."""
    occ = [[0] * 24 for _ in range(7)]
    t = start - start % 3600
    while t <= end:
        local = datetime.fromtimestamp(t + offset_h * 3600, UTC)
        occ[local.weekday()][local.hour] += 1
        t += 3600
    return occ


def per_slot_average(heat: list[list[int]], start: int, end: int) -> list[list[float]]:
    """Sums per weekday x hour divided by how often that slot occurs in the period: with a 30-day window two weekdays
    occur 5 times and the others 4, so raw sums favoured them by 25% (CRM audit B2)."""
    occ = slot_occurrences(start, end)
    return [[round(heat[d][h] / occ[d][h], 4) if occ[d][h] else 0 for h in range(24)] for d in range(7)]
