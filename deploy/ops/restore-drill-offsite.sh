#!/bin/sh
# Restore DRILL from the THIRD copy (off-site bucket), on the owner's Mac with Docker (docs/ops/offsite-backup.md).
# Neither the production server nor the local archives are used: the newest off-site archive of the kind is
# downloaded (ciphertext + manifest) into a private dir, then restore-drill-local.sh runs its full drill on that
# file (decrypt, manifest, integrity_check, fresh Docker volume, row counts; Mini App container with MINIAPP_IMAGE).
#   sh restore-drill-offsite.sh bot|miniapp [auto/<kind>/<archive>.tar.enc]
# Env: BACKUP_DIR (default ~/.proqnozai-backups/auto), PY, MINIAPP_IMAGE — as for restore-drill-local.sh.
# Last line: "PASS: ..." (exit 0) or "FAIL: ..." (exit 1).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
KIND=${1:-}
BACKUP_DIR=${BACKUP_DIR:-$HOME/.proqnozai-backups/auto}
PY=${PY:-/opt/homebrew/bin/python3}
OFFSITE=${OFFSITE:-$HERE/offsite_backup.py}
LOCAL_DRILL=${LOCAL_DRILL:-$HERE/restore-drill-local.sh}
export BACKUP_DIR PY

fail() { echo "FAIL: $1"; exit 1; }
case $KIND in bot|miniapp) ;; *) echo "usage: sh restore-drill-offsite.sh bot|miniapp [remote path]"; exit 2;; esac

REL=${2:-$("$PY" "$OFFSITE" list | grep -E "^auto/$KIND/$KIND-[0-9]{8}T[0-9]{6}Z\.tar\.enc\$" | sort | tail -1)}
case $REL in auto/"$KIND"/"$KIND"-*.tar.enc) ;; *) fail "no off-site $KIND archive found (${REL:-empty listing})";; esac

D=""
cleanup() {
  rc=$?; trap - EXIT INT TERM HUP
  # Ciphertext only, but it is a copy nobody tracks: always removed.
  [ -n "$D" ] && { rm -rf "$D" || { echo "CLEANUP FAILED: $D"; rc=1; }; }
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

mkdir -p "$BACKUP_DIR/.work" && chmod 700 "$BACKUP_DIR/.work" || fail "work dir"
D=$(mktemp -d "$BACKUP_DIR/.work/offsite-fetch.XXXXXX") || fail "mktemp"
rmdir "$D" || fail "mktemp"                    # fetch creates it itself (0700) and refuses an existing dir
A=$("$PY" "$OFFSITE" fetch "$REL" "$D" | tail -1)
case $A in "$D"/*.tar.enc) ;; *) fail "download of $REL: $A";; esac
echo "downloaded $REL"
out=$(sh "$LOCAL_DRILL" "$KIND" "$A"); rc=$?
printf '%s\n' "$out"
[ "$rc" -eq 0 ] || fail "drill on the off-site copy failed"
echo "PASS: off-site $REL restored and checked (local archives and server not used)"
