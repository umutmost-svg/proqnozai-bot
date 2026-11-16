#!/bin/sh
# Production watchdog (audit wave 2, docs/ops/backups.md "Monitoring"). Root cron, every 5 minutes:
#   */5 * * * * /usr/bin/flock -n /run/watchdog.lock /root/watchdog.sh >> /var/log/watchdog.log 2>&1
# Checks: the services run (Mini App and admin healthy; bot, tunnel, Traefik up), the public Mini App answers, disk,
# memory, the Mini App's memory against its 512 MiB limit, the age of the newest database backup, and the last
# minutes of Mini App logs (Python errors, the AI provider refusing the key or credit, the football-data quota).
# Alerts go to the owner through notify-telegram.sh:
#   - a state problem is reported once it has lasted GRACE_S (10 min: a stop-first deploy waits up to 6 min for health),
#     repeated every REMIND_S while it lasts, and its recovery is reported;
#   - a log event is reported once per REMIND_S (no recovery message: logs only say something happened).
# Optional dead man's switch: when /root/.hc-watchdog (0600) holds a healthchecks.io URL, every run pings it, so a dead
# server or cron alerts from outside. Only counts and names are ever sent — never log lines (they may hold user data).
set -u
NOTIFY=${NOTIFY:-/root/notify-telegram.sh}
STATE=${WATCHDOG_STATE:-/root/.watchdog-state}
BACKUPS=${BACKUP_ROOT:-/root/backups}
URL=${PUBLIC_HEALTH_URL:-https://app.proqnozai.com/api/health}
HC_FILE=${HC_FILE:-/root/.hc-watchdog}
REMIND_S=${REMIND_S:-21600}
GRACE_S=${GRACE_S:-600}
DISK_MAX=${DISK_MAX:-85}               # % of / used
MEM_MIN_MB=${MEM_MIN_MB:-250}          # MemAvailable
APP_MEM_MAX=${APP_MEM_MAX:-90}         # % of the Mini App's memory limit
BACKUP_MAX_AGE_S=${BACKUP_MAX_AGE_S:-93600}   # 26 h: the daily job plus slack
LOG_WINDOW=${LOG_WINDOW:-6m}
now=$(date -u +%s)

TAB=$(printf '\t')   # field separator of the problem list and the state file (no literal tabs in this file)
problems=""   # lines: key<TAB>message
add() { problems="$problems$1${TAB}$2
"; }

# 1) services. Names are Swarm/compose name prefixes; "healthy" ones must report (healthy).
for spec in "proqnozai-miniapp:healthy:Mini App" "proqnozai-admin:healthy:admin panel" \
            "proqnozai-proqnozaibot:up:bot" "cloudflared:up:Cloudflare tunnel" "dokploy-traefik:up:Traefik"; do
  name=${spec%%:*}; rest=${spec#*:}; need=${rest%%:*}; label=${rest#*:}
  status=$(docker ps -f "name=$name" --format '{{.Status}}' 2>/dev/null | head -1)
  case "$need:$status" in
    *:) add "svc_$name" "$label is not running" ;;
    healthy:*"(healthy)"*) ;;
    healthy:*) add "svc_$name" "$label is not healthy ($status)" ;;
    up:Up*) ;;
    *) add "svc_$name" "$label: $status" ;;
  esac
done

# 2) the public address, through Cloudflare and Traefik (Cloudflare refuses some default User-Agents).
code=$(curl -s -o /dev/null -m 20 -A proqnozai-watchdog -w '%{http_code}' "$URL" 2>/dev/null)
[ "$code" = 200 ] || add public "app.proqnozai.com health answers ${code:-nothing} (not 200)"

# 3) host resources
used=$(df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}')
[ -n "$used" ] && [ "$used" -ge "$DISK_MAX" ] && add disk "disk / is ${used}% full"
avail=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
[ -n "$avail" ] && [ "$avail" -lt "$MEM_MIN_MB" ] && add memory "only ${avail} MB of memory available"
APP=$(docker ps -q -f name=proqnozai-miniapp 2>/dev/null | head -1)
if [ -n "$APP" ]; then
  perc=$(docker stats --no-stream --format '{{.MemPerc}}' "$APP" 2>/dev/null | tr -d '%' | cut -d. -f1)
  [ -n "$perc" ] && [ "$perc" -ge "$APP_MEM_MAX" ] && add app_memory "Mini App uses ${perc}% of its memory limit"
fi

# 4) the newest backup (prod-backup.sh alerts on its own failures; this catches a job that never ran)
# The newest COMPLETE copy: prod-backup.sh logs "OK: <stamp>" only after every database was copied and checked.
newest=$(grep -o ' OK: [0-9]\{8\}T[0-9]\{6\}Z' "${BACKUP_LOG:-/var/log/prod-backup.log}" 2>/dev/null | tail -1 | cut -c6-)
[ -n "$newest" ] && [ -d "$BACKUPS/$newest" ] || newest=""
if [ -z "$newest" ]; then
  add backup "no complete database backup in $BACKUPS (prod-backup log)"
else
  t=$(date -u -d "$(printf '%s' "$newest" | sed -E 's/(....)(..)(..)T(..)(..)(..)Z/\1-\2-\3 \4:\5:\6/')" +%s 2>/dev/null)
  [ -n "$t" ] && [ $((now - t)) -gt "$BACKUP_MAX_AGE_S" ] && add backup "newest database backup is $(((now - t) / 3600)) h old"
fi

# 5) Mini App logs of the last minutes: counted by pattern, never forwarded.
if [ -n "$APP" ]; then
  logs=$(docker logs --since "$LOG_WINDOW" "$APP" 2>&1)
  n=$(printf '%s\n' "$logs" | grep -c '^Traceback')
  [ "$n" -gt 0 ] && add log_errors "Mini App: $n Python error(s) in the last $LOG_WINDOW (docker logs)"
  n=$(printf '%s\n' "$logs" | grep -Ec 'event=ai_upstream_error status=(400|401|403)')
  [ "$n" -gt 0 ] && add log_ai_key "AI provider refused the key or credit $n time(s) (status 400/401/403): AI analyses fail"
  n=$(printf '%s\n' "$logs" | grep -c 'event=ai_upstream_error')
  [ "$n" -ge 5 ] && add log_ai_errors "AI provider errors: $n in the last $LOG_WINDOW"
  n=$(printf '%s\n' "$logs" | grep -c 'quota_exhausted')
  [ "$n" -gt 0 ] && add log_quota "football-data daily quota exhausted (match data stops until 00:00 UTC)"
fi

# 6) compare with the state: key<TAB>first seen<TAB>last alerted<TAB>message
[ -f "$STATE" ] || (umask 077; : > "$STATE")
# Two versions of the new state: `sent` if the alert goes out, `unsent` (nothing marked alerted, resolved problems
# kept) if it does not — so a failed send is retried on the next run instead of being recorded as delivered.
sent=""; unsent=""; out=""
seen() { printf '%s' "$problems" | awk -F"$TAB" -v k="$1" '$1 == k {found = 1} END {exit !found}'; }
while IFS="$TAB" read -r key msg; do
  [ -n "$key" ] || continue
  prev=$(awk -F"$TAB" -v k="$key" '$1 == k {print $2 FS $3}' "$STATE")
  first=${prev%%"$TAB"*}; alerted=${prev#*"$TAB"}
  [ -n "$prev" ] || { first=$now; alerted=0; }
  case "$key" in
    log_*) due=$([ $((now - alerted)) -ge "$REMIND_S" ] && echo 1) ;;
    *) due=$({ [ "$alerted" = 0 ] && [ $((now - first)) -ge "$GRACE_S" ]; } || { [ "$alerted" != 0 ] && [ $((now - alerted)) -ge "$REMIND_S" ]; } && echo 1) ;;
  esac
  unsent="$unsent$key${TAB}$first${TAB}$alerted${TAB}$msg
"
  if [ -n "$due" ]; then out="$out• $msg
"; alerted=$now; fi
  sent="$sent$key${TAB}$first${TAB}$alerted${TAB}$msg
"
done <<EOF
$problems
EOF
# Resolved: state problems that were alerted and are gone. Log keys stay until their quiet period ends.
while IFS="$TAB" read -r key first alerted msg; do
  [ -n "$key" ] || continue
  seen "$key" && continue
  case "$key" in
    log_*) [ $((now - alerted)) -lt "$REMIND_S" ] && { line="$key${TAB}$first${TAB}$alerted${TAB}$msg
"; sent="$sent$line"; unsent="$unsent$line"; } ;;
    *) [ "$alerted" != 0 ] && { out="$out• ✅ resolved: $msg
"; unsent="$unsent$key${TAB}$first${TAB}$alerted${TAB}$msg
"; } ;;
  esac
done < "$STATE"
new_state=$sent
if [ -n "$out" ] && ! "$NOTIFY" "watchdog
$out"; then
  echo "$(date -u +%FT%TZ) notify failed"
  new_state=$unsent
fi
(umask 077; printf '%s' "$new_state" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE")
echo "$(date -u +%FT%TZ) problems=$(printf '%s' "$problems" | grep -c .) alerted=$(printf '%s' "$out" | grep -c .)"
if [ -f "$HC_FILE" ]; then
  hc=$(cat "$HC_FILE")
  curl -fsS -m 10 -o /dev/null "$hc" 2>/dev/null || echo "$(date -u +%FT%TZ) healthchecks ping failed"
fi
exit 0
