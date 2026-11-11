"""Consistent copy of the bot's SQLite database (stdlib only). Two ways to run it:

  * bot RUNNING — inside its container, online:
        docker exec -i <bot container> python - < deploy/migration/bot_db_backup.py
  * bot STOPPED (final copy at cutover) — a throw-away container on the bot's volume:
        docker run --rm -i -v proqnozai-db:<bot's BOT_DB_DIR> -e BOT_DB_DIR=<same> <bot image> \
          python - < deploy/migration/bot_db_backup.py

Uses the SQLite backup API from a read-only connection: safe while the bot keeps writing (WAL included), no
restart. Steps of 256 pages with a pause let the bot's writers in between; a write during the copy restarts
the pass, so the whole run is bounded by MAX_SECONDS (default 300) and then aborts cleanly.
Before copying it checks free space on the volume (database + WAL, twice over, plus 50 MB): a full volume
would stop the bot's own writes. A failed or aborted copy is deleted. The copy (0600, never overwrites)
lands next to the database as bot-backup-<UTC stamp>.db; then integrity_check and row counts are printed —
no row contents. Copy it off the server afterwards and delete it from the volume.
Needs the owner's permission: it is a command against the production database.
"""
import os
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

os.umask(0o077)  # the copy holds Telegram user ids and texts
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "300"))
src = Path(os.environ.get("BOT_DB_DIR", ".")) / "bot.db"
if not src.exists():
    sys.exit(f"STOP: {src} not found (BOT_DB_DIR?)")
need = 2 * sum(p.stat().st_size for p in (src, src.with_name("bot.db-wal")) if p.exists()) + 50 * 2**20
free = shutil.disk_usage(src.parent).free
if free < need:
    sys.exit(f"STOP: {free // 2**20} MB free on the volume, {need // 2**20} MB needed — nothing written")
dst = src.with_name(f"bot-backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db")
if dst.exists():
    sys.exit(f"STOP: {dst.name} already exists")

deadline = time.monotonic() + MAX_SECONDS


def progress(status: int, remaining: int, total: int) -> None:
    if time.monotonic() > deadline:
        raise TimeoutError(f"backup took longer than {MAX_SECONDS}s")


s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
d = sqlite3.connect(dst)
try:
    with d:
        s.backup(d, pages=256, sleep=0.05, progress=progress)
    check = d.execute("PRAGMA integrity_check").fetchone()[0]
    tables = [r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1")]
    counts = {t: d.execute('SELECT count(*) FROM "' + t + '"').fetchone()[0] for t in tables}
except BaseException as exc:
    d.close()
    s.close()
    for p in (dst, dst.with_name(dst.name + "-journal")):
        p.unlink(missing_ok=True)
    sys.exit(f"STOP: copy failed and was removed ({type(exc).__name__}: {exc})")
d.close()
s.close()
print(f"backup written to {dst}")
print(f"integrity={check}")
for t, n in counts.items():
    print(f"  {t}: {n}")
if check != "ok":
    dst.unlink(missing_ok=True)
    sys.exit("STOP: the copy failed integrity_check and was removed")
