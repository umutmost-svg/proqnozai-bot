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
    # --no-access-log: uvicorn logs the path WITH its query string, so a request that
    # puts init_data or a token in the URL would land in the logs; the app writes
    # its own structured events (auth.*, request_id) without request data.
    # --timeout-keep-alive 95: longer than the proxy's idle timeout to the backend (Traefik's default
    # idleConnTimeout is 90 s); a shorter one lets the app close a connection the proxy is about to
    # reuse, which the user sees as a sporadic 502.
    # --limit-concurrency 1000: uvicorn counts OPEN connections, idle keep-alive ones included; Traefik
    # keeps up to 200 idle connections per backend by default, so the limit must sit well above that
    # (a limit at 200 would answer 503 to new requests while nothing is running).
    exec python -m uvicorn --factory miniapp.asgi:create_app_from_env \
      --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 \
      --no-proxy-headers --no-server-header --no-access-log \
      --limit-concurrency 1000 --timeout-keep-alive 95 --timeout-graceful-shutdown 10
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
