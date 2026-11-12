#!/bin/sh
# Server half of the off-server backups (docs/ops/infra-hardening.md, section 3). It is the FORCED COMMAND of a
# dedicated SSH key of the owner's Mac — that key can run nothing else. Line in /root/.ssh/authorized_keys:
#   restrict,command="/opt/proqnozai-ops/backup-export.sh" ssh-ed25519 AAAA... proqnozai-backup-pull
# The Mac asks for one word (SSH_ORIGINAL_COMMAND):
#   check    prerequisites only (containers found, helper files present); prints "ready", copies nothing
#   miniapp  both Mini App databases (deploy/miniapp/backup.py inside the container, as the app user)
#   bot      the bot database (deploy/migration/bot_db_backup.py inside the bot container)
# Each copy is made with the SQLite backup API while the app keeps running, integrity-checked in the container,
# streamed to stdout as a tar (`docker cp ... -`; stdout carries NOTHING else) and REMOVED from the volume again —
# also on failure. A copy left by a killed run is recorded in $STATE and removed at the start of the next run, so
# no plaintext copy stays on the server. Encryption happens on the Mac before anything touches its disk.
# Progress and errors go to stderr. Exit 0 only when the full stream was written and the copy removed.
set -u
umask 077
PATH="$PATH:/usr/local/bin:/usr/bin:/bin"
OPS=${OPS_DIR:-/opt/proqnozai-ops}
MINIAPP=${MINIAPP_NAME:-proqnozai-miniapp}
BOT=${BOT_NAME:-proqnozai-proqnozaibot}
STATE=${STATE_FILE:-/var/lib/proqnozai-ops/export-pending}
LOCK=${LOCK_FILE:-/run/proqnozai-backup-export.lock}
what=${SSH_ORIGINAL_COMMAND:-${1:-}}

log() { echo "export: $*" >&2; }
die() { log "FAIL: $*"; exit 1; }
trap '' PIPE                     # a dropped connection must still run the cleanup below, not kill the shell
trap 'exit 130' INT TERM HUP     # dash runs no EXIT trap on a signal: turn it into an exit

case $what in check|miniapp|bot) ;; *) die "unknown request (expected: check | miniapp | bot)" ;; esac
mkdir -p "$(dirname "$STATE")" || die "state dir"
exec 9>"$LOCK" || die "lock file"
flock -n 9 || die "another export is running"

# Exactly one running container (healthy, for the Mini App). Two bot tasks during a start-first update: refuse.
one() {
  if [ "$2" = healthy ]; then ids=$(docker ps -q -f "name=$1" -f status=running -f health=healthy) || return 1
  else ids=$(docker ps -q -f "name=$1" -f status=running) || return 1; fi
  [ "$(printf '%s\n' "$ids" | grep -c .)" = 1 ] || return 1
  printf '%s\n' "$ids"
}

STAMP_RE='[0-9]{8}T[0-9]{6}Z(-[0-9]+)?'
BOTFILE_RE='/[A-Za-z0-9/_.-]*/bot-backup-[0-9]{8}T[0-9]{6}Z\.db'

# Removes one recorded copy; validated again here because the line may come from the state file.
remove_copy() {  # $1 kind, $2 stamp | absolute path
  case $1 in
    miniapp) printf '%s\n' "$2" | grep -Eqx "$STAMP_RE" || return 1
             c=$(one "$MINIAPP" running) || return 1
             docker exec -u miniapp "$c" rm -rf "/data/backups/$2" ;;
    bot)     printf '%s\n' "$2" | grep -Eqx "$BOTFILE_RE" || return 1
             c=$(one "$BOT" running) || return 1
             docker exec "$c" rm -f "$2" ;;
    *) return 1 ;;
  esac
}

# Leftovers of a killed earlier run first (the file only ever holds lines written below).
if [ -s "$STATE" ]; then
  keep=""
  while read -r k v; do
    [ -n "$k" ] || continue
    if remove_copy "$k" "$v"; then log "removed leftover $k copy $v"
    else log "WARNING: could not remove leftover $k copy $v"; keep="$keep$k $v
"; fi
  done < "$STATE"
  printf '%s' "$keep" > "$STATE" || die "state file"
  [ -z "$keep" ] || die "leftover copies could not be removed (see above)"
fi

KIND=""; VAL=""
cleanup() {
  rc=$?; trap - EXIT
  if [ -n "$VAL" ]; then
    if remove_copy "$KIND" "$VAL"; then
      grep -vFx "$KIND $VAL" "$STATE" > "$STATE.tmp"; mv -f "$STATE.tmp" "$STATE"
    else log "CLEANUP FAILED: $KIND copy $VAL stays recorded in $STATE"; rc=1; fi
  fi
  exit "$rc"
}
trap cleanup EXIT

case $what in
  check)
    [ -f "$OPS/backup.py" ] && [ -f "$OPS/bot_db_backup.py" ] || die "helper files missing in $OPS"
    one "$MINIAPP" healthy >/dev/null || die "no single healthy $MINIAPP container"
    one "$BOT" running >/dev/null || die "no single running $BOT container"
    echo ready ;;
  miniapp)
    C=$(one "$MINIAPP" healthy) || die "no single healthy $MINIAPP container"
    out=$(docker exec -i -u miniapp "$C" python - < "$OPS/backup.py" 2>&1); rc=$?
    stamp=$(printf '%s\n' "$out" | sed -n 's#^backup written to /data/backups/##p' | tail -1)
    if printf '%s\n' "$stamp" | grep -Eqx "$STAMP_RE"; then
      KIND=miniapp; VAL=$stamp; echo "miniapp $stamp" >> "$STATE" || die "state file"
    fi
    [ "$rc" = 0 ] || die "backup.py: $(printf '%s' "$out" | tail -1)"
    [ -n "$VAL" ] || die "no backup stamp in backup.py output"
    printf '%s\n' "$out" | grep -q 'integrity=ok' || die "integrity not ok"
    # The copies are WAL-mode databases; backup.py's read-only verify leaves an EMPTY -wal and a -shm next to them.
    # Only the two self-contained .db files are streamed, after proving no committed page sits in a -wal.
    docker exec -u miniapp "$C" sh -c "cd /data/backups/$stamp && test ! -s miniapp.db-wal && test ! -s miniapp.forecasts.db-wal" \
      || die "a -wal file next to the copy is not empty"
    log "miniapp copy $stamp integrity ok; streaming"
    docker exec -u miniapp "$C" tar -C /data/backups -cf - "$stamp/miniapp.db" "$stamp/miniapp.forecasts.db" || die "tar stream" ;;
  bot)
    C=$(one "$BOT" running) || die "no single running $BOT container"
    out=$(docker exec -i -e MAX_SECONDS=300 "$C" python - < "$OPS/bot_db_backup.py" 2>&1); rc=$?
    path=$(printf '%s\n' "$out" | sed -n 's#^backup written to ##p' | tail -1)
    if printf '%s\n' "$path" | grep -Eqx "$BOTFILE_RE"; then
      KIND=bot; VAL=$path; echo "bot $path" >> "$STATE" || die "state file"
    fi
    [ "$rc" = 0 ] || die "bot_db_backup.py: $(printf '%s' "$out" | tail -1)"
    [ -n "$VAL" ] || die "no backup path in bot_db_backup.py output"
    printf '%s\n' "$out" | grep -qx 'integrity=ok' || die "integrity not ok"
    log "bot copy $(basename "$path") integrity ok; streaming"
    docker cp "$C:$path" - || die "docker cp" ;;
esac
