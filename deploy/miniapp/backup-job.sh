#!/bin/sh
# Daily backup job for the Mini App on the Dokploy host (cron as root):
#   17 3 * * * /opt/sporbot/backup-job.sh >> /var/log/sporbot-backup.log 2>&1
# Steps: consistent, integrity-checked copy of BOTH databases inside the container (backup.py, as the app
# user) -> host copy -> copy OFF this server -> keep KEEP_DAYS days in every place. Every step is guarded:
# any failure exits non-zero AND runs NOTIFY, so a broken backup never goes unnoticed.
# Settings (edit once, or pass as environment; none of them is a secret — rclone/SSH credentials are
# configured separately for root):
#   OFFSITE_KIND  rclone | ssh | disk
#   OFFSITE       rclone: "remote:bucket/path"   ssh: "user@host:/path"   disk: "/mnt/backup-disk/sporbot"
#                 (disk: the directory must contain the marker file .sporbot-offsite created once ON the
#                 external disk — if the disk is not mounted, the marker is missing and the job fails
#                 instead of silently copying onto this server)
#   NOTIFY        a command alerting the owner; gets the reason as $1
set -u

SVC="${SVC:-sporbot-miniapp}"
OFFSITE_KIND="${OFFSITE_KIND:-<rclone|ssh|disk>}"
OFFSITE="${OFFSITE:-<destination>}"
NOTIFY="${NOTIFY:-<command that alerts the owner>}"
KEEP_DAYS="${KEEP_DAYS:-7}"
DIR="${SPORBOT_DIR:-/opt/sporbot}"

fail() { echo "$(date -u +%FT%TZ) FAIL: $1"; sh -c "$NOTIFY \"\$1\"" _ "backup failed: $1" || true; exit 1; }

case "$OFFSITE_KIND" in rclone|ssh|disk) ;; *) fail "OFFSITE_KIND is not configured" ;; esac
case "$OFFSITE" in "<"*|"") fail "OFFSITE is not configured" ;; esac

C=$(docker ps -q -f "name=$SVC" -f health=healthy | head -1) || fail "docker ps"
[ -n "$C" ] || fail "no healthy $SVC container"
out=$(docker exec -i -u miniapp "$C" python - < "$DIR/backup.py" 2>&1) || fail "backup.py: $(printf '%s' "$out" | tail -1)"
stamp=$(printf '%s\n' "$out" | sed -n 's#^backup written to /data/backups/##p' | tail -1)
[ -n "$stamp" ] || fail "no backup stamp"

local_copy="$DIR/backups/$stamp"
mkdir -p "$DIR/backups" || fail "mkdir host dir"
chmod 700 "$DIR/backups" || fail "chmod host dir"
docker cp "$C:/data/backups/$stamp" "$local_copy" || fail "docker cp"
chmod -R go-rwx "$local_copy" || fail "chmod host copy"

case "$OFFSITE_KIND" in
  rclone)
    rclone copy "$local_copy" "$OFFSITE/$stamp" || fail "rclone copy"
    rclone delete "$OFFSITE" --min-age "${KEEP_DAYS}d" || fail "rclone retention"
    rclone rmdirs "$OFFSITE" --leave-root || fail "rclone rmdirs" ;;
  ssh)
    host=${OFFSITE%%:*}; path=${OFFSITE#*:}
    rsync -a "$local_copy/" "$OFFSITE/$stamp/" || fail "rsync"
    ssh "$host" "find '$path' -mindepth 1 -maxdepth 1 -type d -mtime +$KEEP_DAYS -exec rm -rf {} +" || fail "ssh retention" ;;
  disk)
    [ -f "$OFFSITE/.sporbot-offsite" ] || fail "external disk not mounted (no $OFFSITE/.sporbot-offsite)"
    cp -a "$local_copy" "$OFFSITE/$stamp" || fail "copy to disk"
    find "$OFFSITE" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + || fail "disk retention" ;;
esac

# Retention here and inside the volume (the app user owns those copies).
find "$DIR/backups" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + || fail "host retention"
docker exec -u miniapp "$C" find /data/backups -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + \
  || fail "volume retention"
echo "$(date -u +%FT%TZ) OK: $stamp copied ($OFFSITE_KIND)"
