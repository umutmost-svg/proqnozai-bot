"""Consistent online backup of BOTH Mini App databases (users/sessions and the forecast history), run
INSIDE the container (stdlib only):

    docker exec -i -u miniapp <container> python - < deploy/miniapp/backup.py            # -> /data/backups/<UTC stamp>/
    docker exec -i -u miniapp <container> python - verify /data/backups/<stamp> < deploy/miniapp/backup.py

Uses the SQLite backup API (safe while the app writes; WAL included). Never overwrites a file. `verify`
opens each copy read-only, runs integrity_check and prints table row counts: a restore drill without
touching the live files. Copies stay on the same volume: copy them off the server as well.
"""
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

os.umask(0o077)  # the copies hold Telegram user ids: owner-only, like the live files
db = Path(os.environ["MINIAPP_DB_PATH"])
files = [db, db.with_name(f"{db.stem}.forecasts.db")]


def verify(directory: Path) -> bool:
    ok = True
    for f in files:
        copy = directory / f.name
        if not copy.exists():
            print(f"{copy.name}: missing"); ok = False; continue
        c = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        check = c.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        c.close()
        print(f"{copy.name}: integrity={check} {counts}")
        ok &= check == "ok"
    return ok


if len(sys.argv) > 2 and sys.argv[1] == "verify":
    sys.exit(0 if verify(Path(sys.argv[2])) else 1)

target = db.parent / "backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
target.mkdir(parents=True, exist_ok=False)
for f in files:
    if not f.exists():
        print(f"{f.name}: not created yet (skipped)"); continue
    src, dst = sqlite3.connect(f"file:{f}?mode=ro", uri=True), sqlite3.connect(target / f.name)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
print(f"backup written to {target}")
sys.exit(0 if verify(target) else 1)
