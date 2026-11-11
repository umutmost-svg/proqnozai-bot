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

# Only this job's copies are ever deleted: directories named EXACTLY like a backup stamp
# (YYYYMMDDTHHMMSSZ or YYYYMMDDTHHMMSSZ-n), older than KEEP_DAYS judged by the UTC time in the name itself
# (the same rule for every destination, independent of file times).
CUTOFF=""
expired() { while read -r n; do
    printf '%s\n' "$n" | grep -Eq '^[0-9]{8}T[0-9]{6}Z(-[0-9]+)?$' || continue
    [ "$(printf '%.16s' "$n")" \< "$CUTOFF" ] && printf '%s\n' "$n"
  done; }
fail() { echo "$(date -u +%FT%TZ) FAIL: $1"; sh -c "$NOTIFY \"\$1\"" _ "backup failed: $1" || true; exit 1; }

case "$OFFSITE_KIND" in rclone|ssh|disk) ;; *) fail "OFFSITE_KIND is not configured" ;; esac
case "$KEEP_DAYS" in ''|*[!0-9]*) fail "KEEP_DAYS must be a whole number of days" ;; esac
[ "$KEEP_DAYS" -ge 1 ] || fail "KEEP_DAYS must be at least 1"
now=$(date -u +%s) || fail "date"
CUTOFF=$(date -u -d "@$((now - KEEP_DAYS * 86400))" +%Y%m%dT%H%M%SZ 2>/dev/null \
  || date -u -r "$((now - KEEP_DAYS * 86400))" +%Y%m%dT%H%M%SZ 2>/dev/null) || fail "cutoff date"
printf '%s\n' "$CUTOFF" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$' || fail "cutoff date"
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
    names=$(rclone lsf --dirs-only "$OFFSITE") || fail "rclone list"
    for n in $(printf '%s\n' "$names" | sed 's#/$##' | expired); do rclone purge "$OFFSITE/$n" || fail "rclone retention"; done ;;
  ssh)
    host=${OFFSITE%%:*}; path=${OFFSITE#*:}
    rsync -a "$local_copy/" "$OFFSITE/$stamp/" || fail "rsync"
    names=$(ssh "$host" "ls -1 '$path'") || fail "ssh list"
    for n in $(printf '%s\n' "$names" | expired); do ssh "$host" "rm -rf '$path/$n'" || fail "ssh retention"; done ;;
  disk)
    [ -f "$OFFSITE/.sporbot-offsite" ] || fail "external disk not mounted (no $OFFSITE/.sporbot-offsite)"
    cp -a "$local_copy" "$OFFSITE/$stamp" || fail "copy to disk"
    names=$(ls -1 "$OFFSITE") || fail "disk list"
    for n in $(printf '%s\n' "$names" | expired); do rm -rf "${OFFSITE:?}/$n" || fail "disk retention"; done ;;
esac

# Retention here and inside the volume (the app user owns those copies).
names=$(ls -1 "$DIR/backups") || fail "host list"
for n in $(printf '%s\n' "$names" | expired); do rm -rf "$DIR/backups/$n" || fail "host retention"; done
names=$(docker exec -u miniapp "$C" ls -1 /data/backups) || fail "volume list"
for n in $(printf '%s\n' "$names" | expired); do docker exec -u miniapp "$C" rm -rf "/data/backups/$n" || fail "volume retention"; done
echo "$(date -u +%FT%TZ) OK: $stamp copied ($OFFSITE_KIND)"
