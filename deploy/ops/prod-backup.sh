#!/bin/sh
# Daily consistent copies of every production SQLite database, on this server (root cron, docs/ops/backups.md):
#   23 3 * * * /usr/bin/flock -n /run/prod-backup.lock /root/prod-backup.sh >> /var/log/prod-backup.log 2>&1
# SQLite's online backup API from a READ-ONLY connection (safe while the apps write; WAL included), integrity check
# of every copy, kept KEEP_DAYS days in /root/backups/<UTC stamp>/ (0700 / 0600). Any failure alerts the owner
# (notify-telegram.sh) and exits non-zero. Off-server copies: DigitalOcean Droplet Backups of the whole disk.
set -u
KEEP_DAYS="${KEEP_DAYS:-7}"
ROOT="${BACKUP_ROOT:-/root/backups}"
VOL=/var/lib/docker/volumes
NOTIFY="${NOTIFY:-/root/notify-telegram.sh}"
fail() { echo "$(date -u +%FT%TZ) FAIL: $1"; "$NOTIFY" "backup failed: $1" || true; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$ROOT/$stamp"
umask 077
# A dedicated directory: created with its marker on the first run; retention runs only where the marker is.
if [ ! -d "$ROOT" ]; then
  mkdir -p "$ROOT" && touch "$ROOT/.prod-backup" || fail "create $ROOT"
fi
[ -f "$ROOT/.prod-backup" ] || fail "$ROOT is not this job's directory (no .prod-backup marker)"
mkdir "$dest" || fail "mkdir $dest"
chmod 700 "$ROOT" "$dest" || fail "chmod"

# The bot's database: read on the host as root (its owner; the bot runs as root). The Mini App's and the admin's:
# read INSIDE the admin container as their owner (uid 10001), which mounts both volumes (/data, /state) — the host
# path under /var/lib/docker is root-only, and a read-only SQLite connection may still create -wal/-shm sidecars,
# which must stay app-owned. Copies leave the container through docker cp and are removed there.
copy_py='
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst, pages=256, sleep=0.05)
src.close()
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
if ok != "ok":
    sys.exit(f"integrity {ok}")
'
store() { chown root:root "$1" && chmod 600 "$1" || fail "store $(basename "$1")"; }

bot_src="$VOL/proqnozai-db/_data/bot.db"
[ -f "$bot_src" ] || fail "missing $bot_src"
python3 -c "$copy_py" "$bot_src" "$dest/bot.db" || fail "copy bot.db"
store "$dest/bot.db"

ADMIN=$(docker ps -q -f name=proqnozai-admin -f health=healthy | head -1) || fail "docker ps"
[ -n "$ADMIN" ] || fail "no healthy admin container (it reads the Mini App and admin databases)"
tmp="/tmp/prod-backup-$stamp"
docker exec -u 10001 "$ADMIN" sh -c "umask 077 && mkdir $tmp" || fail "container scratch"
# name:path in the admin container. Required ones fail the job when missing; optional ones are copied when present.
for spec in "miniapp.db:/data/miniapp.db:required" "miniapp.forecasts.db:/data/miniapp.forecasts.db:required" \
            "miniapp.telemetry.db:/data/miniapp.telemetry.db:optional" "bot-outbox.db:/data/bot-outbox.db:optional" \
            "bot-crm.db:/data/bot-crm.db:optional" "admin.db:/state/admin.db:required"; do
  name=${spec%%:*}; rest=${spec#*:}; path=${rest%%:*}; need=${rest#*:}
  if ! docker exec -u 10001 "$ADMIN" test -f "$path"; then
    [ "$need" = required ] && fail "missing $path"
    continue
  fi
  docker exec -i -u 10001 "$ADMIN" python - "$path" "$tmp/$name" <<EOF2 || fail "copy $name"
$copy_py
EOF2
  docker cp "$ADMIN:$tmp/$name" "$dest/$name" || fail "docker cp $name"
  store "$dest/$name"
done
docker exec -u 10001 "$ADMIN" rm -rf "$tmp" || fail "container cleanup"

# Retention: only directories named exactly like a stamp, older than KEEP_DAYS by the time in the name.
cutoff=$(date -u -d "@$(( $(date -u +%s) - KEEP_DAYS * 86400 ))" +%Y%m%dT%H%M%SZ) || fail "cutoff"
for d in "$ROOT"/*; do
  n=$(basename "$d")
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  printf '%s\n' "$n" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$' || continue
  [ "$n" \< "$cutoff" ] && { rm -rf "${ROOT:?}/$n" || fail "retention $n"; }
done
echo "$(date -u +%FT%TZ) OK: $stamp ($(du -sh "$dest" | cut -f1))"
