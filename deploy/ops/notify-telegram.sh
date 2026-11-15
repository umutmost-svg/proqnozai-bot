#!/bin/sh
# Alert the owner in Telegram through the running bot (no service to register):  notify-telegram.sh "<text>"
#   notify-telegram.sh --refresh   only refreshes the root-only cache (called hourly by dokploy-bypass-check.sh)
# The bot token and the owner's id are read from the running bot container (and kept in a root-only 0600 file so an
# alert still goes out when Docker itself is down); never logged or put on a command line (curl reads them from stdin). When the bot is retired, switch alerts to another channel.
set -u
TEXT="${1:-alert}"
CACHE=/root/.notify-telegram   # root-only copy (0600) of the bot's token and owner id: alerts work with Docker down
TOKEN=""; CHAT=""
BOT=$(docker ps -q -f name=proqnozai-proqnozaibot 2>/dev/null | head -1)
if [ -n "$BOT" ]; then
  TOKEN=$(docker exec "$BOT" printenv TELEGRAM_TOKEN 2>/dev/null)
  CHAT=$(docker exec "$BOT" printenv ADMIN_ID 2>/dev/null)
fi
case "$CHAT" in ''|*[!0-9]*) TOKEN=""; CHAT="" ;; esac
if [ -n "$TOKEN" ]; then
  (umask 077; printf '%s\n%s\n' "$TOKEN" "$CHAT" > "$CACHE.tmp" && mv "$CACHE.tmp" "$CACHE") || true
elif [ -f "$CACHE" ]; then
  TOKEN=$(sed -n 1p "$CACHE"); CHAT=$(sed -n 2p "$CACHE")   # Docker or the bot is down: the last known values
fi
case "$CHAT" in ''|*[!0-9]*) echo "notify: no owner id" >&2; exit 1 ;; esac
[ -n "$TOKEN" ] || { echo "notify: no bot token" >&2; exit 1; }
[ "$TEXT" = "--refresh" ] && exit 0   # only refresh the cache (the hourly check calls this), send nothing
# Token and chat id only in curl's config on stdin (never argv); the alert text is not secret.
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\ndata-urlencode = "chat_id=%s"\n' "$TOKEN" "$CHAT" \
  | curl -fsS -m 15 -o /dev/null -K - --data-urlencode "text=⚠️ ProqnozAI server: $TEXT" \
  || { echo "notify: send failed" >&2; exit 1; }
