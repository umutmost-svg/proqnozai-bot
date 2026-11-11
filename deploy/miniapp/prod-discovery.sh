#!/bin/sh
# READ-ONLY inventory of the production server before the first Mini App release.
# Run on the Dokploy host as a user that can read Docker (e.g. root):   sh prod-discovery.sh > discovery.txt
#
# It changes nothing, starts or restarts nothing and writes no files (stdout only; no `sort`, no temp files).
# It never prints environment values, command lines, log lines or crontab commands. It DOES print
# operator-chosen names: Docker service/container/image names, Traefik Host() domains, Dokploy app and cron
# file names; bind-mount host paths only at or under known system roots (others become <host path>). Everything printed
# passes `scrub` (token-like strings and e-mail addresses -> <redacted>), which cannot recognise every
# possible secret hidden in a name: READ discovery.txt BEFORE SENDING IT and blank anything sensitive.
# Load on the live host is bounded: no `docker system df -v`; logs of at most MAX_LOG_CONTAINERS containers,
# each read once with --since/--tail and a hard timeout, at the lowest CPU/IO priority of the client.
set -u

LOG_TAIL=100000          # at most this many recent log lines per bot container (one pass)
LOG_TIMEOUT=90           # seconds per container read, hard stop
MAX_LOG_CONTAINERS=5
LOW="nice -n 19"
command -v ionice >/dev/null 2>&1 && LOW="ionice -c3 nice -n 19"
STOP=""
command -v timeout >/dev/null 2>&1 && STOP="timeout $LOG_TIMEOUT"

section() { printf '\n===== %s =====\n' "$1"; }
# Masks anything that could be a credential or a personal address, whatever field it came from.
# Token-like = 24+ characters of [A-Za-z0-9_+/] mixing letters and digits (API keys, bot tokens, hashes);
# hyphenated names (Dokploy apps, containers) and key=value counters stay readable.
if command -v perl >/dev/null 2>&1; then
  scrub() { perl -pe 's{(?<![A-Za-z0-9_+/])(?=[A-Za-z0-9_+/]*\d)(?=[A-Za-z0-9_+/]*[A-Za-z])[A-Za-z0-9_+/]{24,}={0,2}}{<redacted>}g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<redacted-email>/g'; }
else
  scrub() { sed -E 's/[A-Za-z0-9_+\/]{24,}={0,2}/<redacted>/g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<redacted-email>/g'; }
fi
# One mount per line: type<TAB>"target"<TAB>ro<TAB>"source", target and source Go-quoted (%q escapes tabs and
# newlines, so a path can neither split a field nor forge a line). A bind source is shown only when it equals
# or lies under a known system root ("/" boundary); anything else is <host path>.
mounts() { awk -F'\t' 'NF == 4 { src = $4; gsub(/^"|"$/, "", src)
    if ($1 == "bind" && src !~ /^\/(etc\/dokploy|var\/lib\/docker|etc\/letsencrypt)(\/|$)/ && src !~ /^\/(var\/)?run\/docker\.sock$/) src = "<host path>"
    printf "  %s:%s->%s%s\n", $1, src, $2, ($3 == "true" ? "(ro)" : "") ; next }
  NF > 0 { print "  <unreadable mount record>" }'; }
# Runs a read-only command and says so when it fails, instead of an empty, misleading section.
run() { out=$("$@" 2>&1); rc=$?; printf '%s\n' "$out" | scrub; [ $rc -eq 0 ] || echo "ERROR: '$1' failed (exit $rc)"; }
dedupe() { awk '!seen[$0]++'; }

section "host"
{ uname -srm; uptime; } | scrub
run df -h /
[ -d /var/lib/docker ] && run df -h /var/lib/docker
run free -m
docker version --format 'docker server {{.Server.Version}}' 2>&1 | scrub
docker info --format 'swarm={{.Swarm.LocalNodeState}} containers={{.Containers}} running={{.ContainersRunning}} images={{.Images}}' 2>&1 | scrub

section "swarm / dokploy services (name, image, replicas, ports)"
run docker service ls --format '{{.Name}}\t{{.Image}}\t{{.Replicas}}\t{{.Ports}}'

section "service update/rollback order, restart policy, mount points (no env)"
for svc in $(docker service ls --format '{{.Name}}' 2>/dev/null); do
  docker service inspect "$svc" --format \
    '{{.Spec.Name}} update={{with .Spec.UpdateConfig}}{{.Order}}/{{.FailureAction}}{{end}} rollback={{with .Spec.RollbackConfig}}{{.Order}}{{end}} restart={{with .Spec.TaskTemplate.RestartPolicy}}{{.Condition}}{{end}}' 2>&1 | scrub
  docker service inspect "$svc" --format \
    '{{range .Spec.TaskTemplate.ContainerSpec.Mounts}}{{.Type}}{{"\t"}}{{printf "%q" .Target}}{{"\t"}}{{.ReadOnly}}{{"\t"}}{{printf "%q" .Source}}{{"\n"}}{{end}}' 2>&1 | mounts | scrub
done

section "running containers (name, image, status, published ports)"
run docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

section "container mounts (no env)"
for c in $(docker ps --format '{{.Names}}'); do
  echo "$c" | scrub
  docker inspect "$c" --format \
    '{{range .Mounts}}{{.Type}}{{"\t"}}{{printf "%q" .Destination}}{{"\t"}}{{if .RW}}false{{else}}true{{end}}{{"\t"}}{{if .Name}}{{printf "%q" .Name}}{{else}}{{printf "%q" .Source}}{{end}}{{"\n"}}{{end}}' 2>&1 | mounts | scrub
done

section "docker volumes (names only; sizes deliberately not measured)"
run docker volume ls --format '{{.Name}}'

section "traefik routing: hosts only"
# Only small config files, two levels deep: no unbounded recursive read.
[ -d /etc/dokploy/traefik ] && find /etc/dokploy/traefik -maxdepth 2 -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.toml' \) -size -1024k \
  -exec grep -hoE 'Host\(`[^`]+`\)' {} + 2>/dev/null | dedupe | scrub

section "dokploy version / applications (names only)"
docker ps --format '{{.Image}}' | grep -i dokploy | dedupe | scrub
for d in /etc/dokploy/applications /etc/dokploy/compose; do [ -d "$d" ] && ls "$d" 2>&1 | scrub; done

section "backups: schedules only (no command text: it may carry credentials)"
# A line is shown only as its five schedule fields when they parse as a schedule (minute and hour start
# with a digit or "*"); @macros show only the macro name; anything else (variable assignments, odd lines)
# is counted, never printed. The command itself is reduced to a backup-like flag.
crontab -l 2>/dev/null | awk '
  /^[[:space:]]*(#|$)/ { next }
  { b = ($0 ~ /backup|dump|rsync|s3|restic|borg/) ? "backup-like" : "other" }
  $1 ~ /^@(reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)$/ { print $1, b; next }
  $1 ~ /^[0-9*][0-9*\/,-]*$/ && $2 ~ /^[0-9*][0-9*\/,-]*$/ && $3 ~ /^[0-9*][0-9*\/,-]*$/ &&
  $4 ~ /^([0-9*][0-9*\/,-]*|[A-Za-z][A-Za-z][A-Za-z])$/ && $5 ~ /^([0-9*][0-9*\/,-]*|[A-Za-z][A-Za-z][A-Za-z](-[A-Za-z][A-Za-z][A-Za-z])?)$/ {
    print $1, $2, $3, $4, $5, b; next }
  { hidden++ }
  END { if (hidden) print hidden " other line(s) hidden (assignments or non-schedule text)" }'
for f in /etc/cron.d/*; do [ -f "$f" ] && printf '%s: %s entries\n' "$(basename "$f" | scrub)" "$(grep -cvE '^\s*(#|$)' "$f")"; done 2>/dev/null
[ -d /etc/dokploy/backups ] && ls /etc/dokploy/backups 2>&1 | head -20 | scrub

section "bot containers: one bounded pass over 7 days of logs (counts only, never a log line)"
[ -n "$STOP" ] || echo "note: no 'timeout' command here: log reads have no hard time limit"
n=0
for c in $(docker ps --format '{{.Names}}' | grep -iE 'bot|proqnoz'); do
  n=$((n + 1)); [ "$n" -gt "$MAX_LOG_CONTAINERS" ] && { echo "more bot-like containers: not read (limit $MAX_LOG_CONTAINERS)"; break; }
  # One read per container, hard-stopped; the exit status travels as a marker so a failed read is reported,
  # never counted as an empty log. awk keeps counters and the top 5 hours only (no sort, no temp files).
  { $LOW $STOP docker logs --since 168h --timestamps --tail "$LOG_TAIL" "$c" 2>&1; echo "__READ_RC__=$?"; } | awk -v name="$c" '
    /^__READ_RC__=/ { rc = substr($0, 13); next }
    { lines++ }
    /football API 429/ { fd429++ }
    /football API/ { fdwarn++ }
    /ERROR|Traceback/ { errors++ }
    /[Ff]orecast|прогноз/ { hour = substr($1, 1, 13); if (hour ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}$/) per[hour]++ }
    /[Cc]onflict: terminated by other getUpdates/ { conflict++ }
    END {
      if (rc != 0) { printf "%s: ERROR log read failed or timed out (exit %s); counts below are partial\n", name, rc }
      printf "%s: lines_read=%d football_429=%d football_api_warnings=%d errors=%d getUpdates_conflicts=%d\n", name, lines, fd429, fdwarn, errors, conflict
      for (k = 0; k < 5; k++) { best = ""; max = 0
        for (h in per) if (per[h] > max) { max = per[h]; best = h }
        if (best == "") break
        printf "  busiest hour %s: %d forecast-related lines\n", best, max; delete per[best] }
    }' | scrub
done

section "end"
