#!/bin/sh
# Admin container entrypoint (server mode).
#   serve                  the admin API + static frontend (default)
#   create-owner <email>   the first owner, interactively (docker exec -it <container> admin-entrypoint create-owner ...)
#   unlock <email>         end a 2FA lock early
#   reset-credentials <email>  lost password or 2FA device: new password (asked twice) + new 2FA secret shown once
# Root only to check the volumes and hand the admin's own state volume to the app user; then dropped for good.
set -eu

: "${ADMIN_DATA_DIR:?ADMIN_DATA_DIR is required (the Mini App's data volume)}"
: "${ADMIN_STATE_DIR:?ADMIN_STATE_DIR is required (the admin's own volume)}"

if [ "$(id -u)" = "0" ]; then
  for dir in "$ADMIN_DATA_DIR" "$ADMIN_STATE_DIR"; do
    if ! mountpoint -q "$dir"; then
      echo "refusing to start: $dir is not a mounted volume" >&2
      exit 1
    fi
  done
  chown miniapp:miniapp "$ADMIN_STATE_DIR"
  chmod 0700 "$ADMIN_STATE_DIR"
  exec setpriv --reuid=miniapp --regid=miniapp --clear-groups --no-new-privs -- "$0" "$@"
fi

case "${1:-serve}" in
  serve) exec python -m miniapp.admin_web serve ;;
  create-owner) shift; exec python -m miniapp.admin_web create-owner "$@" ;;
  unlock) shift; exec python -m miniapp.admin_web unlock "$@" ;;
  reset-credentials) shift; exec python -m miniapp.admin_web reset-credentials "$@" ;;
  *) echo "usage: serve | create-owner <email> | unlock <email> | reset-credentials <email>" >&2; exit 2 ;;
esac
