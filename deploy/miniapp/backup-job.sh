#!/bin/sh
# Daily backup job for the Mini App on the Dokploy host (cron as root):
#   17 3 * * * /opt/sporbot/backup-job.sh >> /var/log/sporbot-backup.log 2>&1
# Steps: consistent, integrity-checked copy of BOTH databases inside the container (backup.py, as the app
# user) -> copy OFF this server -> keep KEEP_DAYS days in both places. Any failure exits non-zero AND runs
# NOTIFY, so a broken backup never goes unnoticed. Edit the four settings below once (no secrets in them:
# OFFSITE uses a pre-configured rclone remote / SSH key, NOTIFY a pre-configured command).
set -eu

SVC="${SVC:-sporbot-miniapp}"                       # Dokploy service name: `docker service ls`
OFFSITE="${OFFSITE:-<rclone-remote:bucket/sporbot>}"    # off-server: rclone remote, user@host:/path (rsync/SSH) or a mounted external disk path
NOTIFY="${NOTIFY:-<command that alerts the owner>}"    # e.g. a curl to the owner's monitoring channel; receives $1 = reason
KEEP_DAYS=7
DIR="${SPORBOT_DIR:-/opt/sporbot}"

fail() { echo "$(date -u +%FT%TZ) FAIL: $1"; sh -c "$NOTIFY \"\$1\"" _ "backup failed: $1" || true; exit 1; }

C=$(docker ps -q -f "name=$SVC" -f health=healthy | head -1)
[ -n "$C" ] || fail "no healthy $SVC container"
out=$(docker exec -i -u miniapp "$C" python - < "$DIR/backup.py" 2>&1) || fail "backup.py: $(printf '%s' "$out" | tail -1)"
stamp=$(printf '%s\n' "$out" | sed -n 's#^backup written to /data/backups/##p' | tail -1)
[ -n "$stamp" ] || fail "no backup stamp"

local_copy="$DIR/backups/$stamp"
mkdir -p "$DIR/backups" && chmod 700 "$DIR/backups"
docker cp "$C:/data/backups/$stamp" "$local_copy" || fail "docker cp"
chmod -R go-rwx "$local_copy"
case "$OFFSITE" in
  "<"*) fail "OFFSITE is not configured" ;;
  /*) [ -d "$OFFSITE" ] || fail "OFFSITE directory missing (external disk not mounted?)"
      cp -a "$local_copy" "$OFFSITE/$stamp" || fail "copy to $OFFSITE" ;;
  *:*/*) if command -v rclone >/dev/null 2>&1; then rclone copy "$local_copy" "$OFFSITE/$stamp" || fail "rclone copy"
         else rsync -a "$local_copy/" "$OFFSITE/$stamp/" || fail "rsync"; fi ;;
  *) fail "OFFSITE format not recognised" ;;
esac

# Retention: host copies here and inside the volume (the app user owns those).
[ "${OFFSITE#/}" != "$OFFSITE" ] && find "$OFFSITE" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
find "$DIR/backups" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + || fail "host retention"
docker exec -u miniapp "$C" find /data/backups -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + \
  || fail "volume retention"
echo "$(date -u +%FT%TZ) OK: $stamp copied to $OFFSITE"
