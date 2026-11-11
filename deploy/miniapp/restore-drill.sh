#!/bin/sh
# Restore DRILL for the Mini App (on the Mini App server, as root): proves an off-server copy can be restored,
# without touching the live service or its volume.
#   sh restore-drill.sh <stamp> <image> <dir holding the <stamp> folder fetched from OFFSITE> /opt/sporbot/backup.py
# Fetch the copy first (dedicated local dir), e.g.  rclone copy "$OFFSITE/<stamp>" /root/drill-in/<stamp>
#                                         or  rsync -a "$OFFSITE/<stamp>/" /root/drill-in/<stamp>/
# Then: test volume -> copy both DBs in -> integrity_check + row counts -> a temporary container on that volume,
# offline (--network none, no secrets) -> healthy -> everything removed again (also on failure).
# WORK (default /root) holds the temporary copy.
set -eu
[ $# -eq 4 ] || { echo "usage: sh restore-drill.sh <stamp> <image> <source dir> <backup.py>"; exit 2; }
S=$1; IMG=$2; SRC=$3; T=sporbot-restore-test-$$   # unique per run: never someone else's volume
# The stamp goes into a shell command inside the container: only an exact backup name is accepted.
printf '%s\n' "$S" | grep -Eqx '[0-9]{8}T[0-9]{6}Z(-[0-9]+)?' || { echo "STOP: not a backup stamp: $S"; exit 1; }
[ -f "$SRC/$S/miniapp.db" ] && [ -f "$SRC/$S/miniapp.forecasts.db" ] || { echo "STOP: $SRC/$S lacks a database"; exit 1; }
docker volume inspect "$T" >/dev/null 2>&1 && { echo "STOP: $T already exists"; exit 1; }
docker container inspect "$T" >/dev/null 2>&1 && { echo "STOP: container $T already exists"; exit 1; }
# Cleanup removes ONLY what this run created, and says so when it cannot (the drill then fails).
W=""; VOL=""; CTR=""; TOKEN=""
cleanup() {
  rc=$?; trap - EXIT INT TERM HUP
  # Containers by this run's token label: also one whose `docker run` failed after creating it.
  if [ -n "$TOKEN" ]; then
    ids=$(docker ps -aq -f "label=sporbot.drill=$TOKEN") || { echo "CLEANUP FAILED: listing containers"; rc=1; ids=""; }
    for id in $ids; do docker rm -f "$id" >/dev/null 2>&1 || { echo "CLEANUP FAILED: container $id"; rc=1; }; done
  fi
  # The volume only if it carries this run's token (it may have been created before the ownership check failed).
  if [ -n "$VOL" ]; then
    lbl=$(docker volume inspect -f '{{index .Labels "sporbot.drill"}}' "$T" 2>/dev/null) || lbl=""
    if [ "$lbl" = "$TOKEN" ]; then docker volume rm "$T" >/dev/null 2>&1 || { echo "CLEANUP FAILED: volume $T"; rc=1; }
    elif docker volume inspect "$T" >/dev/null 2>&1; then echo "CLEANUP: volume $T is not this run's — left alone"; rc=1; fi
  fi
  [ -n "$W" ] && { rm -rf "$W" || { echo "CLEANUP FAILED: $W"; rc=1; }; }
  [ "$rc" -eq 0 ] && [ -n "${OK:-}" ] && echo "DRILL OK: $S restored into a test volume, integrity ok, container healthy, cleaned up"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP   # dash runs no EXIT trap on a signal: turn it into an exit
W=$(mktemp -d "${WORK:-/root}/sporbot-drill.XXXXXX")
cp -a "$SRC/$S" "$W/$S"
# A volume is ours only if it carries this run's token: `volume create` silently returns an existing one.
TOKEN=$(basename "$W")
VOL=1   # cleanup re-checks the token before removing anything
docker volume create --label "sporbot.drill=$TOKEN" "$T" >/dev/null
[ "$(docker volume inspect -f '{{index .Labels "sporbot.drill"}}' "$T")" = "$TOKEN" ] || { echo "STOP: $T is not this run's volume"; exit 1; }
docker run --rm -v "$T":/data -v "$W":/in:ro --entrypoint sh "$IMG" -c \
  "cp /in/$S/miniapp.db /in/$S/miniapp.forecasts.db /data/ && chown miniapp:miniapp /data /data/miniapp*.db && chmod 700 /data && chmod 600 /data/miniapp*.db"
docker run --rm --user miniapp -v "$T":/data -e MINIAPP_DB_PATH=/data/miniapp.db --entrypoint python -i \
  "$IMG" - verify /data < "$4" || { echo "STOP: restored copy is not usable"; exit 1; }
CTR=$(docker run -d --name "$T" --label "sporbot.drill=$TOKEN" --network none -v "$T":/data -e MINIAPP_ENV=development -e MINIAPP_DB_PATH=/data/miniapp.db \
  -e MINIAPP_BOT_TOKEN=000000:offline-maintenance-not-a-token -e MINIAPP_TRUSTED_ORIGINS=http://localhost "$IMG")   # set only when the container was created
for i in $(seq 1 45); do [ "$(docker inspect -f '{{.State.Health.Status}}' "$CTR")" = healthy ] && break; sleep 2; done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$CTR")" = healthy ] || { echo "STOP: test container not healthy"; docker logs --tail 20 "$CTR"; exit 1; }
OK=1
