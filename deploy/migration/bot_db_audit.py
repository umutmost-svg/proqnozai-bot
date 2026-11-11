"""Aggregate-only audit of a bot database COPY (never the live file): what the migration will meet.

    python3 deploy/migration/bot_db_audit.py <copy of bot.db>

Prints counts and distributions only — no user ids, names, usernames or message texts — so the output
can be shared. Opens the file read-only (immutable).
"""
import sqlite3
import sys

if len(sys.argv) != 2:
    sys.exit("usage: bot_db_audit.py <copy of bot.db>")
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro&immutable=1", uri=True)
one = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1")]
print("integrity:", one("PRAGMA integrity_check"))
print("tables:")
for t in tables:
    print(f"  {t}:", one('SELECT count(*) FROM "' + t + '"'))
cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
print("users.columns:", ", ".join(sorted(cols)))
print("users: total", one("SELECT count(*) FROM users"),
      "| registered", one("SELECT count(*) FROM users WHERE is_registered=1"),
      "| blocked", one("SELECT count(*) FROM users WHERE is_blocked=1"),
      "| bad id (<=0 or NULL)", one("SELECT count(*) FROM users WHERE user_id IS NULL OR user_id <= 0"),
      "| duplicate ids", one("SELECT count(*) - count(DISTINCT user_id) FROM users"))
print("users by lang:", dict(c.execute("SELECT coalesce(lang,'<null>'), count(*) FROM users GROUP BY 1 ORDER BY 2 DESC")))
print("active: 7d", one("SELECT count(*) FROM users WHERE last_active >= datetime('now','-7 days')"),
      "| 30d", one("SELECT count(*) FROM users WHERE last_active >= datetime('now','-30 days')"),
      "| empty last_active", one("SELECT count(*) FROM users WHERE coalesce(last_active,'') = ''"))
print("joined_at unparsable:", one("SELECT count(*) FROM users WHERE joined_at IS NULL OR julianday(joined_at) IS NULL"))
if "forecast_history" in tables:
    print("forecast_history: users with history", one("SELECT count(DISTINCT user_id) FROM forecast_history"),
          "| with feedback", one("SELECT count(*) FROM forecast_history WHERE feedback IS NOT NULL"),
          "| last 30d", one("SELECT count(*) FROM forecast_history WHERE created_at >= datetime('now','-30 days')"))
for t in ("live_subscriptions", "odds_alerts", "promo_claims", "partner_clicks"):
    if t in tables:
        print(f"{t}: distinct users", one(f"SELECT count(DISTINCT user_id) FROM {t}"))
if "broadcasts" in tables:
    print("broadcasts by status:", dict(c.execute("SELECT status, count(*) FROM broadcasts GROUP BY 1")))
print("orphans: history without user", one(
    "SELECT count(*) FROM forecast_history f LEFT JOIN users u ON u.user_id=f.user_id WHERE u.user_id IS NULL")
      if "forecast_history" in tables else "-")
