#!/bin/sh
# Server half of the off-server backups (docs/ops/infra-hardening.md, section 3). It is the FORCED COMMAND of a
# dedicated SSH key of the owner's Mac — that key can run nothing else. Line in /root/.ssh/authorized_keys:
#   restrict,command="/opt/proqnozai-ops/backup-export.sh" ssh-ed25519 AAAA... proqnozai-backup-pull
# The Mac asks for one word (SSH_ORIGINAL_COMMAND):
#   check    prerequisites only (containers found, helper files present, no leftovers); prints "ready"
#   miniapp  both Mini App databases (deploy/miniapp/backup.py inside the container, as the app user)
#   bot      the bot database (deploy/migration/bot_db_backup.py inside the bot container)
# Each copy is made with the SQLite backup API while the app keeps running, integrity-checked in the container,
# streamed to stdout as a tar (stdout carries NOTHING else) and REMOVED from the volume again — also on failure.
# Leftovers of a killed run: BEFORE a helper runs, the names already present in its backup place are saved in
# $STATE_DIR/inflight-<kind>. Whatever appears there afterwards and matches the helper's naming is this job's
# copy (also a half-written one whose name was never printed) and is removed — at the end of this run, or at the
# start of the next one if this run was killed. Pre-existing names (e.g. manual copies) are never touched, and
# nothing outside the exact copy naming (never bot.db or miniapp.db) can match.
# Encryption happens on the Mac before anything touches its disk. Progress and errors go to stderr.
set -u
umask 077
PATH="$PATH:/usr/local/bin:/usr/bin:/bin"
OPS=${OPS_DIR:-/opt/proqnozai-ops}
MINIAPP=${MINIAPP_NAME:-proqnozai-miniapp}
BOT=${BOT_NAME:-proqnozai-proqnozaibot}
STATE_DIR=${STATE_DIR:-/var/lib/proqnozai-ops}
LOCK=${LOCK_FILE:-/run/proqnozai-backup-export.lock}
what=${SSH_ORIGINAL_COMMAND:-${1:-}}

log() { echo "export: $*" >&2; }
die() { log "FAIL: $*"; exit 1; }
trap '' PIPE                     # a dropped connection must still run the cleanup below, not kill the shell
trap 'exit 130' INT TERM HUP     # dash runs no EXIT trap on a signal: turn it into an exit

case $what in check|miniapp|bot) ;; *) die "unknown request (expected: check | miniapp | bot)" ;; esac
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR" || die "state dir"
exec 9>"$LOCK" || die "lock file"
flock -n 9 || die "another export is running"

# Exactly one running container (healthy, for the Mini App). Two bot tasks during a start-first update: refuse.
one() {
  if [ "$2" = healthy ]; then ids=$(docker ps -q -f "name=$1" -f status=running -f health=healthy) || return 1
  else ids=$(docker ps -q -f "name=$1" -f status=running) || return 1; fi
  [ "$(printf '%s\n' "$ids" | grep -c .)" = 1 ] || return 1
  printf '%s\n' "$ids"
}

MINIAPP_RE='[0-9]{8}T[0-9]{6}Z(-[0-9]+)?'                            # backup.py: /data/backups/<stamp>/
BOT_RE='bot-backup-[0-9]{8}T[0-9]{6}Z\.db(-journal|-wal|-shm)?'      # bot_db_backup.py: <BOT_DB_DIR>/bot-backup-<stamp>.db

# Where a kind's copies appear (inside its container) and how they are listed / removed.
bot_dir() {  # $1 container
  d=$(docker exec "$1" printenv BOT_DB_DIR) || return 1
  printf '%s\n' "$d" | grep -Eqx '/[A-Za-z0-9/_.-]+' || return 1
  printf '%s\n' "$d"
}
list_copies() {  # $1 kind, $2 container -> names (one per line) matching the kind's copy naming
  case $1 in
    miniapp) out=$(docker exec -u miniapp "$2" sh -c 'mkdir -p /data/backups && ls -1A /data/backups') || return 1
             printf '%s\n' "$out" | grep -Ex "$MINIAPP_RE" || true ;;
    bot)     d=$(bot_dir "$2") || return 1
             out=$(docker exec "$2" ls -1A "$d") || return 1
             printf '%s\n' "$out" | grep -Ex "$BOT_RE" || true ;;
  esac
}
remove_copy() {  # $1 kind, $2 container, $3 name (validated again)
  case $1 in
    miniapp) printf '%s\n' "$3" | grep -Eqx "$MINIAPP_RE" || return 1
             docker exec -u miniapp "$2" rm -rf "/data/backups/$3" ;;
    bot)     printf '%s\n' "$3" | grep -Eqx "$BOT_RE" || return 1
             d=$(bot_dir "$2") || return 1
             docker exec "$2" rm -f "$d/$3" ;;
  esac
}
container_of() {  # $1 kind
  case $1 in miniapp) one "$MINIAPP" running;; bot) one "$BOT" running;; esac
}

# Removes every copy of $1 that is not in its inflight snapshot, then the snapshot. 0 = nothing left.
settle() {
  f="$STATE_DIR/inflight-$1"
  [ -f "$f" ] || return 0
  c=$(container_of "$1") || { log "cannot reach the $1 container to remove leftovers"; return 1; }
  now=$(list_copies "$1" "$c") || { log "cannot list $1 copies"; return 1; }
  ok=0
  for n in $now; do
    grep -Fqx "$n" "$f" && continue
    if remove_copy "$1" "$c" "$n"; then log "removed $1 copy $n"; else log "CLEANUP FAILED: $1 copy $n"; ok=1; fi
  done
  [ "$ok" = 0 ] && rm -f "$f"
  return "$ok"
}

for k in miniapp bot; do settle "$k" || die "leftover $k copies could not be removed (see above)"; done

KIND=""
cleanup() {
  rc=$?; trap - EXIT
  if [ -n "$KIND" ]; then settle "$KIND" || { log "CLEANUP FAILED: $KIND copy stays recorded in $STATE_DIR"; rc=1; }; fi
  exit "$rc"
}
trap cleanup EXIT

begin() {  # $1 kind, $2 container: snapshot existing names BEFORE the helper creates anything
  snap=$(list_copies "$1" "$2") || die "cannot list existing $1 copies"
  printf '%s\n' "$snap" > "$STATE_DIR/inflight-$1.tmp" && mv -f "$STATE_DIR/inflight-$1.tmp" "$STATE_DIR/inflight-$1" \
    || die "state file"
  KIND=$1
}

case $what in
  check)
    [ -f "$OPS/backup.py" ] && [ -f "$OPS/bot_db_backup.py" ] || die "helper files missing in $OPS"
    one "$MINIAPP" healthy >/dev/null || die "no single healthy $MINIAPP container"
    c=$(one "$BOT" running) || die "no single running $BOT container"
    bot_dir "$c" >/dev/null || die "bot container has no usable BOT_DB_DIR"
    echo ready ;;
  miniapp)
    C=$(one "$MINIAPP" healthy) || die "no single healthy $MINIAPP container"
    begin miniapp "$C"
    out=$(docker exec -i -u miniapp "$C" python - < "$OPS/backup.py" 2>&1) || die "backup.py: $(printf '%s' "$out" | tail -1)"
    stamp=$(printf '%s\n' "$out" | sed -n 's#^backup written to /data/backups/##p' | tail -1)
    printf '%s\n' "$stamp" | grep -Eqx "$MINIAPP_RE" || die "no backup stamp in backup.py output"
    printf '%s\n' "$out" | grep -q 'integrity=ok' || die "integrity not ok"
    # The copies are WAL-mode databases; backup.py's read-only verify leaves an EMPTY -wal and a -shm next to them.
    # Only the two self-contained .db files are streamed, after proving no committed page sits in a -wal.
    docker exec -u miniapp "$C" sh -c "cd /data/backups/$stamp && test ! -s miniapp.db-wal && test ! -s miniapp.forecasts.db-wal" \
      || die "a -wal file next to the copy is not empty"
    log "miniapp copy $stamp integrity ok; streaming"
    docker exec -u miniapp "$C" tar -C /data/backups -cf - "$stamp/miniapp.db" "$stamp/miniapp.forecasts.db" || die "tar stream" ;;
  bot)
    C=$(one "$BOT" running) || die "no single running $BOT container"
    D=$(bot_dir "$C") || die "bot container has no usable BOT_DB_DIR"
    begin bot "$C"
    out=$(docker exec -i -e MAX_SECONDS=300 "$C" python - < "$OPS/bot_db_backup.py" 2>&1) \
      || die "bot_db_backup.py: $(printf '%s' "$out" | tail -1)"
    name=$(printf '%s\n' "$out" | sed -n "s#^backup written to $D/##p" | tail -1)
    printf '%s\n' "$name" | grep -Eqx 'bot-backup-[0-9]{8}T[0-9]{6}Z\.db' || die "no backup path in bot_db_backup.py output"
    printf '%s\n' "$out" | grep -qx 'integrity=ok' || die "integrity not ok"
    log "bot copy $name integrity ok; streaming"
    docker cp "$C:$D/$name" - || die "docker cp" ;;
esac
