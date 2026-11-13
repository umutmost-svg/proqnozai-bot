#!/bin/sh
# Restore DRILL from the encrypted off-server archives, on the owner's Mac with Docker (docs/ops/infra-hardening.md,
# section 5). Nothing touches the production server.
#   sh restore-drill-local.sh bot|miniapp [archive.tar.enc]     (default: the newest archive of that kind)
#   sh restore-drill-local.sh sweep                              (only remove leftovers of killed drills)
# Steps: decrypt + verify into a private temp dir (pull_backups.py extract: manifest SHA-256, tar layout,
# integrity_check, expected tables) -> a NEW labelled Docker volume -> the databases copied in by an offline
# container (--network none) -> integrity_check, expected tables and row counts read back from the volume by
# another offline container and compared with the manifest -> for the Mini App with MINIAPP_IMAGE set, the full
# app container on that volume must turn healthy (deploy/miniapp/restore-drill.sh) -> everything removed
# (temp dir, volume, containers), also on failure. Last line: "PASS: ..." (exit 0) or "FAIL: ..." (exit 1).
# Env: BACKUP_DIR (default ~/.proqnozai-backups/auto), PY (python3 for pull_backups.py), CHECK_IMAGE,
#      MINIAPP_IMAGE (optional, e.g. proqnozai-miniapp:<commit> built for the release; linux/amd64 is emulated).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
KIND=${1:-}
BACKUP_DIR=${BACKUP_DIR:-$HOME/.proqnozai-backups/auto}
PY=${PY:-/opt/homebrew/bin/python3}
PULL=${PULL:-$HERE/pull_backups.py}
# Installed copy (~/proqnozai-ops/miniapp/) or the repository layout (deploy/ops -> deploy/miniapp).
M=$HERE/miniapp; [ -f "$M/restore-drill.sh" ] || M=$HERE/../miniapp
MINIAPP_DRILL=${MINIAPP_DRILL:-$M/restore-drill.sh}
BACKUP_PY=${BACKUP_PY:-$M/backup.py}
# Same base image as the Mini App runtime (multi-arch index digest), only used for offline sqlite checks.
CHECK_IMAGE=${CHECK_IMAGE:-python:3.13.18-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc}

fail() { echo "FAIL: $1"; exit 1; }
case $KIND in bot|miniapp|sweep) ;; *) echo "usage: sh restore-drill-local.sh bot|miniapp [archive.tar.enc] | sweep"; exit 2;; esac

# Leftovers of drills killed by SIGKILL / power loss (their trap never ran): every container and volume labelled
# proqnozai.drill=<token> of a drill that is not provably running holds decrypted data of a dead run and is removed.
# "Provably running" = its record ($PENDING/<token>, below) holds a PID that is a live restore-drill-local.sh
# process right now. No record, a dead PID or a PID reused by another program = dead. A leftover that cannot be
# removed = FAIL.
alive() {  # $1 token "drillXXXXXX"
  case $1 in drill[A-Za-z0-9]*) ;; *) return 1;; esac
  pid=$(cat "$PENDING/$1" 2>/dev/null) || return 1
  case $pid in ''|*[!0-9]*) return 1;; esac
  ps -p "$pid" -o command= 2>/dev/null | grep -q 'restore-drill-local\.sh'
}
sweep_orphans() {
  ids=$(docker ps -aq -f label=proqnozai.drill) || return 1
  vols=$(docker volume ls -q -f label=proqnozai.drill) || return 1
  bad=0
  for id in $ids; do
    t=$(docker inspect -f '{{index .Config.Labels "proqnozai.drill"}}' "$id") || { bad=1; continue; }
    alive "$t" || docker rm -f "$id" >/dev/null || { echo "cannot remove orphan drill container $id"; bad=1; }
  done
  for v in $vols; do
    t=$(docker volume inspect -f '{{index .Labels "proqnozai.drill"}}' "$v") || { bad=1; continue; }
    alive "$t" && continue
    if docker volume rm "$v" >/dev/null; then echo "removed orphan drill volume $v"
    else echo "cannot remove orphan drill volume $v"; bad=1; fi
  done
  [ "$bad" = 0 ] || return 1
  # Every dead drill's Docker leftovers are gone now: its durable record can go too.
  for m in "$PENDING"/drill*; do
    [ -f "$m" ] || continue
    alive "$(basename "$m")" || rm -f "$m" || { echo "cannot remove drill record $m"; return 1; }
  done
  return 0
}
# A durable record per drill ($PENDING/<token>, outside .work so no sweep deletes it) exists from before the
# drill creates its first container/volume until its cleanup has verifiably removed them. A record of a dead drill
# means decrypted data may still sit in Docker.
PENDING="$BACKUP_DIR/.drill-pending"
dead_records() {
  for m in "$PENDING"/drill*; do [ -f "$m" ] && ! alive "$(basename "$m")" && echo "$(basename "$m")"; done
}
# `sweep` only removes such leftovers (pull_backups.py runs it on every scheduled pull, so a killed drill's
# decrypted volume cannot wait for the next monthly drill). Docker not running: fine when no dead drill is on
# record; otherwise FAIL (the pull then alerts) until Docker is back and the cleanup is verified.
if [ "$KIND" = sweep ]; then
  if ! { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }; then
    [ -z "$(dead_records)" ] || fail "Docker is not running: leftovers of a killed drill ($(dead_records | tr '\n' ' ')) are not verified removed"
    echo "drill sweep skipped: Docker is not running, no killed drill on record"; exit 0
  fi
  sweep_orphans || fail "leftovers of an earlier drill could not be removed"
  exit 0
fi
A=${2:-$(ls -1 "$BACKUP_DIR/$KIND"/"$KIND"-*.tar.enc 2>/dev/null | sort | tail -1)}
[ -n "$A" ] && [ -f "$A" ] || fail "no $KIND archive found in $BACKUP_DIR/$KIND"
MAN=$(printf '%s\n' "$A" | sed 's/\.tar\.enc$/.manifest.json/')
[ -f "$MAN" ] || fail "no manifest next to $A"
docker info >/dev/null 2>&1 || fail "Docker is not running"

sweep_orphans || fail "leftovers of an earlier drill could not be removed"

W=""; TOKEN=""; VOL=""
cleanup() {
  rc=$?; trap - EXIT INT TERM HUP
  if [ -n "$TOKEN" ]; then
    cf=0
    if ids=$(docker ps -aq -f "label=proqnozai.drill=$TOKEN"); then
      for id in $ids; do docker rm -f "$id" >/dev/null 2>&1 || { echo "CLEANUP FAILED: container $id"; cf=1; }; done
    else echo "CLEANUP FAILED: listing containers"; cf=1; fi
    if vols=$(docker volume ls -q -f "label=proqnozai.drill=$TOKEN"); then
      for v in $vols; do docker volume rm "$v" >/dev/null 2>&1 || { echo "CLEANUP FAILED: volume $v"; cf=1; }; done
    else echo "CLEANUP FAILED: listing volumes"; cf=1; fi
    # The record goes only after a verified removal; otherwise every scheduled pull keeps alerting.
    if [ "$cf" = 0 ]; then rm -f "$PENDING/$TOKEN" || { echo "CLEANUP FAILED: record $PENDING/$TOKEN"; rc=1; }
    else rc=1; fi
  fi
  [ -n "$W" ] && { rm -rf "$W" || { echo "CLEANUP FAILED: $W (holds decrypted data)"; rc=1; }; }
  if [ "$rc" -eq 0 ] && [ -n "${OK:-}" ]; then echo "PASS: $(basename "$A") restored into a fresh volume — $OK; cleaned up"
  elif [ "$rc" -eq 0 ]; then rc=1; echo "FAIL: drill did not complete"; fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# Private work dir under the backup dir (/Users is shared with Docker Desktop by default).
mkdir -p "$BACKUP_DIR/.work" && chmod 700 "$BACKUP_DIR/.work" || fail "work dir"
W=$(mktemp -d "$BACKUP_DIR/.work/drill.XXXXXX") || fail "mktemp"
TOKEN=$(basename "$W" | tr -cd 'A-Za-z0-9')
mkdir -p "$PENDING" && chmod 700 "$PENDING" && echo "$$" > "$PENDING/$TOKEN" || fail "drill record"
"$PY" "$PULL" extract "$A" "$W/d" || fail "archive does not decrypt/verify"
X="$W/d/x"

VOL="proqnozai-drill-$TOKEN"
docker volume create --label "proqnozai.drill=$TOKEN" "$VOL" >/dev/null || fail "volume create"
[ "$(docker volume inspect -f '{{index .Labels "proqnozai.drill"}}' "$VOL")" = "$TOKEN" ] || fail "$VOL is not this run's volume"

# Read back from the volume, offline; prints "name integrity table=count ..." per database, sorted.
CHECK='import sqlite3,sys
for p in sys.argv[1:]:
    c=sqlite3.connect("file:"+p+"?mode=ro",uri=True)
    i=c.execute("PRAGMA integrity_check").fetchone()[0]
    ts=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type=\"table\" AND name NOT LIKE \"sqlite_%\" ORDER BY 1")]
    print(p.rsplit("/",1)[1], i, " ".join(t+"="+str(c.execute("SELECT count(*) FROM \""+t+"\"").fetchone()[0]) for t in ts))'
run_check() {  # $@ = paths inside /data
  docker run --rm --label "proqnozai.drill=$TOKEN" --network none -v "$VOL":/data --entrypoint python \
    "$CHECK_IMAGE" -c "$CHECK" "$@"
}
# The same lines built from the manifest (what was verified right after the pull).
expected() {  # $1 = name mapping: "bot" maps the single file to bot.db
  "$PY" - "$MAN" "$1" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
for name, info in m["files"].items():
    base = "bot.db" if sys.argv[2] == "bot" else name.rsplit("/", 1)[-1]
    print(base, info["integrity"], " ".join(f"{t}={n}" for t, n in sorted(info["tables"].items())))
EOF
}

case $KIND in
  bot)
    f=$(ls -1 "$X"/bot-backup-*.db 2>/dev/null | head -1); [ -n "$f" ] || fail "no bot database in the archive"
    # Streamed over stdin, not bind-mounted: works whatever Docker Desktop shares from the host.
    docker run -i --rm --label "proqnozai.drill=$TOKEN" --network none -v "$VOL":/data --entrypoint sh \
      "$CHECK_IMAGE" -c "cat > /data/bot.db" < "$f" || fail "copy into the volume"
    got=$(run_check /data/bot.db | sort) || fail "check container"
    want=$(expected bot | sort) || fail "manifest"
    [ "$got" = "$want" ] || { echo "restored: $got"; echo "manifest: $want"; fail "restored data differ from the manifest"; }
    echo "$got"
    OK="bot.db integrity ok, row counts equal to the manifest" ;;
  miniapp)
    S=$(ls -1 "$X" | head -1)
    printf '%s\n' "$S" | grep -Eqx '[0-9]{8}T[0-9]{6}Z(-[0-9]+)?' || fail "unexpected archive layout"
    tar -C "$X/$S" -cf - miniapp.db miniapp.forecasts.db \
      | docker run -i --rm --label "proqnozai.drill=$TOKEN" --network none -v "$VOL":/data --entrypoint tar \
        "$CHECK_IMAGE" -xf - -C /data || fail "copy into the volume"
    got=$(run_check /data/miniapp.db /data/miniapp.forecasts.db | sort) || fail "check container"
    want=$(expected miniapp | sort) || fail "manifest"
    [ "$got" = "$want" ] || { echo "restored: $got"; echo "manifest: $want"; fail "restored data differ from the manifest"; }
    echo "$got"
    OK="both databases integrity ok, row counts equal to the manifest"
    if [ -n "${MINIAPP_IMAGE:-}" ]; then
      # The app itself on a restored copy: its own drill (own labelled volume, offline, removed afterwards).
      WORK="$W" DRILL_PARENT="$TOKEN" sh "$MINIAPP_DRILL" "$S" "$MINIAPP_IMAGE" "$X" "$BACKUP_PY" || fail "Mini App container drill"
      OK="$OK; Mini App container healthy on the restored data"
    else
      echo "note: MINIAPP_IMAGE not set — app container start skipped (data-level drill only)"
    fi ;;
esac
