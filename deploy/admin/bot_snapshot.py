"""Hourly snapshot of the BOT's user value and top users for the admin panel (docs/ops/admin-server.md).

Runs INSIDE the bot container, streamed on stdin, and prints one JSON object (nothing else):

    docker exec -i <bot container> python - < deploy/admin/bot_snapshot.py

Read-only (SQLite `mode=ro`): the bot's code, database and process are untouched. It outputs aggregates plus the
top users' public Telegram names and counters, and nothing more: no message texts, no forecast texts and no promo
codes. The formulas follow the bot dashboard (`db.db_user_value`, stats top_users) so the figures agree with it.
"""
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime

DB = os.path.join(os.environ.get("BOT_DB_DIR", "/app/data"), "bot.db")
TOP = 20

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
conn.execute("PRAGMA busy_timeout=10000")


def one(sql: str, args: tuple = ()) -> float:
    row = conn.execute(sql, args).fetchone()
    return (row[0] if row else 0) or 0


users = int(one("SELECT COUNT(*) FROM users WHERE is_registered=1"))
forecasts = int(one("SELECT COUNT(*) FROM requests WHERE msg_type='FORECAST'"))
# A user's signed /r/ link can be replayed without limit (partner_links.py): one click per user, partner and day
# counts, so the revenue estimate (clicks x CPA) cannot be inflated by one person (CRM audit B3).
UNIQUE_CLICKS = "SELECT COUNT(*) FROM (SELECT DISTINCT user_id, partner, date(created_at) FROM partner_clicks{where})"
clicks = int(one(UNIQUE_CLICKS.format(where="")))
clickers = int(one("SELECT COUNT(DISTINCT user_id) FROM partner_clicks"))
lifespan = one("SELECT AVG(julianday(last_active) - julianday(joined_at)) FROM users WHERE is_registered=1 "
               "AND last_active != '' AND julianday(last_active) - julianday(joined_at) >= 1")
promo_users = int(one("SELECT COUNT(DISTINCT user_id) FROM promo_claims"))
try:
    cpa = float(os.environ.get("PARTNER_CPA", "0") or 0)
except ValueError:
    cpa = 0.0
per_user = clicks / users if users else 0.0

top = []
for uid, name, username, total, last_active, joined in conn.execute(
        "SELECT user_id, display_name, username, total_requests, last_active, joined_at FROM users "
        "WHERE is_registered=1 ORDER BY total_requests DESC, user_id LIMIT ?", (TOP,)):  # as the bot dashboard
    uf = int(one("SELECT COUNT(*) FROM requests WHERE user_id=? AND msg_type='FORECAST'", (uid,)))
    uc = int(one(UNIQUE_CLICKS.format(where=" WHERE user_id=?"), (uid,)))
    promo = bool(one("SELECT COUNT(*) FROM promo_claims WHERE user_id=?", (uid,)))
    top.append({"userId": int(uid), "name": (name or "")[:64], "username": (username or "")[:64],
                "requests": int(total or 0), "forecasts": uf, "clicks": uc, "promo": promo,
                "lastActive": (last_active or "")[:19], "joinedAt": (joined or "")[:19]})

json.dump({
    "schema": 1,
    "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "value": {
        "users": users,
        "forecastsPerUser": round(forecasts / users, 1) if users else 0.0,
        "clicksPerUser": round(per_user, 2),
        "clickConversion": round(clickers / users * 100) if users else 0,
        "avgLifespanDays": round(lifespan, 1) if lifespan else 0.0,
        "promoConversion": round(promo_users / users * 100) if users else 0,
        "cpa": cpa,
        "revenuePerUser": round(per_user * cpa, 2) if cpa else None,
        "revenueTotal": round(clicks * cpa, 2) if cpa else None,
        "forecasts": forecasts, "clicks": clicks, "clickers": clickers, "promoUsers": promo_users,
    },
    "topUsers": top,
}, sys.stdout, ensure_ascii=False)
