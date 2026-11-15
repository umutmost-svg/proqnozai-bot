"""User value and top users (owner request 15.11): the bot's figures from its hourly snapshot and the Mini App's
from its own telemetry, side by side. Aggregates and public names only; nothing is written.

- Bot: `bot-snapshot.json` in the admin's state directory, written hourly by deploy/admin/bot-snapshot.sh from the
  bot database (read-only). Missing / malformed: "not_collected"; the figures keep their age (`generatedAt`).
- Mini App: users in miniapp.db; activity, forecasts and lifespan from miniapp.telemetry.db (user_activity), partner
  clicks from ad_events. The same formulas as the bot dashboard where an equivalent exists.
Names (top lists) only for roles with the `read_personal` permission; others see ids hidden.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

TOP = 20
MAX_SNAPSHOT_BYTES = 512 * 1024
_VALUE_KEYS = ("users", "forecastsPerUser", "clicksPerUser", "clickConversion", "avgLifespanDays", "promoConversion")


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


# The Mini App's figures share one window: partner clicks are kept 90 days (ads telemetry retention), so activity,
# forecasts and lifespan are counted over the same 90 days (numerator and denominator alike).
WINDOW_DAYS = 90


def read_bot_snapshot(path: Path) -> dict:
    """The snapshot as written by bot_snapshot.py, checked field by field; anything else is "not collected"."""
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return {"source": "unavailable"}
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"source": "not_collected"}
    except (OSError, ValueError):
        return {"source": "unavailable"}
    value, top = (data.get("value"), data.get("topUsers")) if isinstance(data, dict) else (None, None)
    if (not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("generatedAt"), str)
            or not isinstance(value, dict) or not all(_num(value.get(k)) for k in _VALUE_KEYS)
            or not _count(value.get("users"))
            or not isinstance(top, list)):
        return {"source": "unavailable"}
    rows = []
    for r in top[:TOP]:
        if (not isinstance(r, dict) or not _count(r.get("userId"))
                or not all(_count(r.get(k)) for k in ("requests", "forecasts", "clicks"))):
            return {"source": "unavailable"}
        rows.append({"userId": r["userId"], "name": str(r.get("name") or "")[:64],
                     "username": str(r.get("username") or "")[:64], "requests": r["requests"],
                     "forecasts": r["forecasts"], "clicks": r["clicks"],
                     "promo": r.get("promo") is True, "lastActive": str(r.get("lastActive") or "")[:19]})
    # Money figures: a valid number, or null for "no rate"; anything else is a malformed snapshot (never shown as 0).
    if not all(k in value for k in ("cpa", "revenuePerUser", "revenueTotal")):
        return {"source": "unavailable"}  # bot_snapshot.py always writes all three
    cpa = value["cpa"]
    revenue = {k: value[k] for k in ("revenuePerUser", "revenueTotal")}
    if not _num(cpa) or any(v is not None and not _num(v) for v in revenue.values()):
        return {"source": "unavailable"}
    if cpa and any(v is None for v in revenue.values()):
        return {"source": "unavailable"}
    return {"source": "snapshot", "generatedAt": data["generatedAt"][:20],
            "value": {**{k: value[k] for k in _VALUE_KEYS}, "cpa": cpa, **revenue},
            "topUsers": rows}


def miniapp_value(telemetry: Path, total_users: int, now: int) -> dict:
    """The Mini App's figures from its telemetry (read-only), over the last WINDOW_DAYS. Per ACTIVE user (opened the
    Mini App in that window): users carried over from the bot who never opened it would dilute every ratio."""
    if not telemetry.is_file():
        return {"source": "not_collected"}
    # Exactly WINDOW_DAYS UTC dates, today included; nothing dated ahead (clock skew) is counted.
    since = datetime.fromtimestamp(now - (WINDOW_DAYS - 1) * 86400, UTC).strftime("%Y-%m-%d")
    today = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(f"{telemetry.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            active, forecasts = conn.execute(
                "SELECT COUNT(DISTINCT user_id), COALESCE(SUM(quick_single_ok + quick_coupon_ok + ai_created), 0) "
                "FROM user_activity WHERE day >= ? AND day <= ?", (since, today)).fetchone()
            lifespan = conn.execute(
                "SELECT AVG(span) FROM (SELECT julianday(MAX(day)) - julianday(MIN(day)) AS span FROM user_activity "
                "WHERE day >= ? AND day <= ? GROUP BY user_id) WHERE span >= 1", (since, today)).fetchone()[0]
            clicks, clickers = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM ad_events WHERE kind = 'click' AND day >= ? AND day <= ?",
                (since, today)).fetchone()
            top = conn.execute(
                "SELECT user_id, SUM(requests), SUM(quick_single_ok + quick_coupon_ok + ai_created), "
                "COUNT(*), MAX(day) FROM user_activity WHERE day >= ? AND day <= ? GROUP BY user_id "
                "ORDER BY SUM(quick_single_ok + quick_coupon_ok + ai_created) DESC, SUM(requests) DESC, user_id LIMIT ?",
                (since, today, TOP)).fetchall()
            ad_clicks = dict(conn.execute(
                "SELECT user_id, COUNT(*) FROM ad_events WHERE kind = 'click' AND day >= ? AND day <= ? GROUP BY user_id",
                (since, today)).fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return {"source": "unavailable"}
    active = int(active or 0)
    return {"source": "telemetry", "windowDays": WINDOW_DAYS, "totalUsers": total_users, "activeUsers": active,
            "value": {"forecastsPerUser": round(forecasts / active, 1) if active else 0.0,
                      "clicksPerUser": round(clicks / active, 2) if active else 0.0,
                      "clickConversion": round(clickers / active * 100) if active else 0,
                      "avgLifespanDays": round(lifespan, 1) if lifespan else 0.0},
            "topUsers": [{"userId": int(uid), "requests": int(req or 0), "forecasts": int(fc or 0),
                          "activeDays": int(days or 0), "lastDay": str(last or ""), "clicks": int(ad_clicks.get(uid, 0))}
                         for uid, req, fc, days, last in top]}


def register(app: FastAPI, ctx: Any, snapshot_file: Path) -> None:
    @app.get("/api/admin/value")
    async def value(request: Request) -> dict:
        ctx.admin_of(request)
        personal = ctx.can_personal(request)
        bot = read_bot_snapshot(snapshot_file)
        mini = miniapp_value(ctx.telemetry_file, ctx.store.count_users(), ctx.now())
        if mini.get("source") == "telemetry":
            names = {u["id"]: u for u in ctx.store.users_by_ids([r["userId"] for r in mini["topUsers"]], ctx.now())}
            for r in mini["topUsers"]:
                u = names.get(r["userId"], {})
                r["name"] = " ".join(p for p in (u.get("firstName"), u.get("lastName")) if p)
                r["username"] = u.get("username") or ""
        # Without the read_personal permission no identity leaves the API: no id, no name (counters only).
        for block in (bot, mini):
            if not personal and isinstance(block.get("topUsers"), list):
                for r in block["topUsers"]:
                    r["userId"], r["name"], r["username"] = None, "", ""
        return {"bot": bot, "miniapp": mini, "personal": personal}
