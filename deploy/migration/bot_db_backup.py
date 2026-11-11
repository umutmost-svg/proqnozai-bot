"""Consistent ONLINE copy of the bot's SQLite database, run INSIDE the running bot container (stdlib only).

    docker exec -i <bot container> python - < deploy/migration/bot_db_backup.py

Uses the SQLite backup API from a read-only connection: safe while the bot keeps writing (WAL included), no
restart, no lock held longer than a page step. The copy goes next to the database as
bot-backup-<UTC stamp>.db (0600; never overwrites), then integrity_check and row counts are printed.
Prints no row contents. Copy it off the server afterwards (docker cp + scp) and keep it 0600.
Needs the owner's permission: it is a command in the production container.
"""
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

os.umask(0o077)  # the copy holds Telegram user ids and texts
src = Path(os.environ.get("BOT_DB_DIR", ".")) / "bot.db"
if not src.exists():
    sys.exit(f"STOP: {src} not found (BOT_DB_DIR?)")
dst = src.with_name(f"bot-backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db")
if dst.exists():
    sys.exit(f"STOP: {dst.name} already exists")
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
d = sqlite3.connect(dst)
with d:
    s.backup(d, pages=256, sleep=0.05)  # small steps: the bot's writers wait at most a moment
s.close()
check = d.execute("PRAGMA integrity_check").fetchone()[0]
tables = [r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1")]
counts = {t: d.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
d.close()
print(f"backup written to {dst}")
print(f"integrity={check}")
for t, n in counts.items():
    print(f"  {t}: {n}")
sys.exit(0 if check == "ok" else 1)
