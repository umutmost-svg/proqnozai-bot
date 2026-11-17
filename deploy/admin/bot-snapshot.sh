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
# Staged in a root-only directory: the admin's volume belongs to uid 10001, so root never writes, chowns or chmods a
# path there by name (a planted symlink would redirect it to any host file; CRM audit B3).
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
docker exec -i "$BOT" python - < /root/bot_snapshot.py > "$WORK/snapshot.json"
# Validated, then placed by file descriptors: a new file created with O_EXCL|O_NOFOLLOW relative to the opened
# directory, owned and moded through its descriptor, renamed within that directory (a rename replaces the entry
# itself, never what a symlink there points to).
python3 - "$WORK/snapshot.json" "$DEST" <<'PY'
import json, os, secrets, sys
src, dest = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()
d = json.loads(data)
assert d["schema"] == 1 and isinstance(d["topUsers"], list)
dirfd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
name = f".bot-snapshot.{secrets.token_hex(8)}"
fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
with os.fdopen(fd, "wb") as f:  # writes all of it (os.write may write part)
    f.write(data)
    f.flush()
    os.fchown(f.fileno(), 10001, 10001)
    os.fchmod(f.fileno(), 0o600)
    os.fsync(f.fileno())
os.rename(name, "bot-snapshot.json", src_dir_fd=dirfd, dst_dir_fd=dirfd)
os.close(dirfd)
PY
trap - EXIT
rm -rf "$WORK"
