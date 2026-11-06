#!/bin/sh
# Mini App container entrypoint.
#   serve           run the API + static frontend (default)
#   admin <args>    operator CLI (python -m miniapp.admin) as the app user
# Root is used only to fix ownership of the volume, then dropped for good.
set -eu

: "${MINIAPP_DB_PATH:?MINIAPP_DB_PATH is required (a file on the mounted volume)}"
db_dir=$(dirname "$MINIAPP_DB_PATH")

if [ "$(id -u)" = "0" ]; then
  # Without a volume the database lives in the container layer and is lost on
  # every redeploy, silently logging everyone out and forgetting burned launches.
  if [ "${MINIAPP_ALLOW_EPHEMERAL_DB:-false}" != "true" ] && ! mountpoint -q "$db_dir"; then
    echo "refusing to start: $db_dir is not a mounted volume" >&2
    exit 1
  fi
  mkdir -p "$db_dir"
  # Railway mounts volumes owned by root. Files created by a root shell (railway ssh)
  # would lock the app out of its own WAL, so ownership is restored on every start.
  chown -R miniapp:miniapp "$db_dir"
  exec setpriv --reuid=miniapp --regid=miniapp --clear-groups --no-new-privs -- "$0" "$@"
fi

case "${1:-serve}" in
  serve)
    # One worker: the login limiter is per process and SQLite has one writer.
    # --no-proxy-headers and no --forwarded-allow-ips: the app never trusts
    # X-Forwarded-*; per-IP limits live at the edge (docs/miniapp/stand.md).
    exec python -m uvicorn --factory miniapp.asgi:create_app_from_env \
      --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 \
      --no-proxy-headers --no-server-header \
      --limit-concurrency 200 --timeout-keep-alive 5 --timeout-graceful-shutdown 10
    ;;
  admin)
    shift
    exec python -m miniapp.admin "$@"
    ;;
  *)
    echo "usage: miniapp-entrypoint [serve | admin <command>]" >&2
    exit 2
    ;;
esac
