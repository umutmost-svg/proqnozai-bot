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
                    "quick_single_ok", "quick_coupon_ok"}
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

    def available(self) -> bool:
        """The file exists and its `user_activity` has every column read here (an older schema is not collected)."""
        try:
            with self._connect() as conn:
                return conn is not None and REQUIRED_COLUMNS <= {
                    r[1] for r in conn.execute("PRAGMA table_info(user_activity)")}
        except sqlite3.Error:
            return False

    def _rows(self, sql: str, ids: list[int] | None, args: tuple = ()) -> list[tuple]:
        """Runs `sql` (with `{ids}` replaced by an IN list, or `1 = 1` for everyone); empty when not collected."""
        try:
            with self._connect() as conn:
                if conn is None:
                    return []
                if ids is None:
                    return conn.execute(sql.format(ids="1 = 1"), args).fetchall()
                out: list[tuple] = []
                for chunk in _chunks(ids):
                    out += conn.execute(sql.format(ids=f"user_id IN ({','.join('?' * len(chunk))})"),
                                        (*args, *chunk)).fetchall()
                return out
        except sqlite3.Error:  # an older file without a table, a lock past the timeout: not collected
            return []

    def activity(self, user_ids: Iterable[int] | None, now: int, window_days: int = 30) -> dict[int, dict]:
        """Per user over the whole retained history: last seen, forecasts (quick single + coupon + AI created),
        AI opened/created, and active days within the last `window_days`."""
        ids = None if user_ids is None else sorted(set(user_ids))
        if ids == []:
            return {}
        since = days_back(now, window_days - 1)
        rows = self._rows(
            "SELECT user_id, MAX(last_at), SUM(quick_single_ok + quick_coupon_ok + ai_created), SUM(ai_created), "
            "SUM(ai_opened), SUM(requests), SUM(day >= ?), MIN(first_at) FROM user_activity WHERE {ids} GROUP BY user_id",
            ids, (since,))
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

    def edge_countries(self, user_ids: Iterable[int] | None) -> dict[int, str]:
        """The latest country code the edge reported for the user (ad events carry it; no IP is ever stored)."""
        ids = None if user_ids is None else sorted(set(user_ids))
        if ids == []:
            return {}
        rows = self._rows(
            "SELECT e.user_id, e.country FROM ad_events e JOIN (SELECT user_id AS uid, MAX(id) AS last FROM ad_events "
            "WHERE {ids} AND country IS NOT NULL AND country != '' GROUP BY user_id) m ON e.id = m.last", ids)
        return {int(uid): str(c).upper()[:2] for uid, c in rows if isinstance(c, str) and len(c) == 2 and c.isalpha()}


def geo(user: dict, edge: dict[int, str]) -> dict:
    """{country, source}: the edge's code when known, else a guess from the interface language, else unknown."""
    if user["id"] in edge:
        return {"country": edge[user["id"]], "source": "edge"}
    guess = LANG_COUNTRY.get(user.get("lang") or "")
    return {"country": guess, "source": "lang" if guess else None}
