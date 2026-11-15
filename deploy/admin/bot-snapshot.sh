#!/bin/sh
# Hourly (root crontab): the bot's value/top snapshot -> the admin's own volume as bot-snapshot.json (0600, uid 10001).
# Reads the bot database read-only inside the bot container; a failed run keeps the previous snapshot.
#   install: install -m 700 bot-snapshot.sh /root/bot-snapshot.sh; install -m 600 bot_snapshot.py /root/bot_snapshot.py
#   crontab: 17 * * * * /usr/bin/flock -n /run/bot-snapshot.lock /root/bot-snapshot.sh >/dev/null 2>&1
set -eu
IDS=$(docker ps -q -f name=proqnozai-proqnozaibot)   # a Docker failure stops here (set -e), never "no bot"
BOT=$(printf '%s\n' "$IDS" | head -1)
[ -n "$BOT" ] || exit 0                      # the bot is retired / not running: the last snapshot stays
DEST=$(docker volume inspect proqnozai-admin-state --format '{{.Mountpoint}}')
TMP=$(mktemp "$DEST/.bot-snapshot.XXXXXX")
trap 'rm -f "$TMP"' EXIT
docker exec -i "$BOT" python - < /root/bot_snapshot.py > "$TMP"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["schema"]==1 and isinstance(d["topUsers"], list)' "$TMP"
chown 10001:10001 "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$DEST/bot-snapshot.json"
trap - EXIT
