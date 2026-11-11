#!/bin/sh
# READ-ONLY inventory of the production server before the first Mini App release.
# Run on the Dokploy host as a user that can read Docker (e.g. root):   sh prod-discovery.sh > discovery.txt
#
# It changes nothing, starts or restarts nothing, writes no files (output goes to stdout only; no `sort`,
# no temporary files) and prints no environment values, command lines, log lines, tokens, keys or Dokploy
# credentials: only names, images, mount points, routing hosts, schedules and counts. Every printed value
# passes `scrub` (long token-like strings and e-mail addresses are replaced by <redacted>).
# Load on the live host is kept low: no `docker system df -v` (it walks every filesystem), each bot log is
# read ONCE, bounded by time and line count, at the lowest CPU/IO priority.
# Send discovery.txt back; review it first.
set -u

LOG_TAIL=300000   # at most this many recent log lines per bot container (one pass)
LOW="nice -n 19"
command -v ionice >/dev/null 2>&1 && LOW="ionice -c3 nice -n 19"

section() { printf '\n===== %s =====\n' "$1"; }
# Masks anything that could be a credential or a personal address, whatever field it came from.
# Token-like = 24+ characters of [A-Za-z0-9_+/] mixing letters and digits (API keys, bot tokens, hashes);
# hyphenated names (Dokploy apps, containers) and key=value counters stay readable.
if command -v perl >/dev/null 2>&1; then
  scrub() { perl -pe 's{(?<![A-Za-z0-9_+/])(?=[A-Za-z0-9_+/]*\d)(?=[A-Za-z0-9_+/]*[A-Za-z])[A-Za-z0-9_+/]{24,}={0,2}}{<redacted>}g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<redacted-email>/g'; }
else
  scrub() { sed -E 's/[A-Za-z0-9_+\/]{24,}={0,2}/<redacted>/g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<redacted-email>/g'; }
fi
# Runs a read-only command and says so when it fails, instead of an empty, misleading section.
run() { out=$("$@" 2>&1); rc=$?; printf '%s\n' "$out" | scrub; [ $rc -eq 0 ] || echo "ERROR: '$1' failed (exit $rc)"; }
dedupe() { awk '!seen[$0]++'; }

section "host"
uname -srm; uptime
run df -h /
[ -d /var/lib/docker ] && run df -h /var/lib/docker
run free -m
docker version --format 'docker server {{.Server.Version}}' 2>&1 | scrub
docker info --format 'swarm={{.Swarm.LocalNodeState}} containers={{.Containers}} running={{.ContainersRunning}} images={{.Images}}' 2>&1 | scrub

section "swarm / dokploy services (name, image, replicas, ports)"
run docker service ls --format '{{.Name}}\t{{.Image}}\t{{.Replicas}}\t{{.Ports}}'

section "service update/rollback order, restart policy, mount points (no env)"
for s in $(docker service ls --format '{{.Name}}' 2>/dev/null); do
  docker service inspect "$s" --format \
    '{{.Spec.Name}} update={{with .Spec.UpdateConfig}}{{.Order}}/{{.FailureAction}}{{end}} rollback={{with .Spec.RollbackConfig}}{{.Order}}{{end}} restart={{with .Spec.TaskTemplate.RestartPolicy}}{{.Condition}}{{end}} mounts={{range .Spec.TaskTemplate.ContainerSpec.Mounts}}{{.Type}}:{{.Source}}->{{.Target}}{{if .ReadOnly}}(ro){{end}} {{end}}' 2>&1 | scrub
done

section "running containers (name, image, status, published ports)"
run docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

section "container mounts (no env)"
for c in $(docker ps --format '{{.Names}}'); do
  docker inspect "$c" --format '{{.Name}} {{range .Mounts}}{{.Type}}:{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}->{{.Destination}}{{if not .RW}}(ro){{end}} {{end}}' 2>&1 | scrub
done

section "docker volumes (names only; sizes deliberately not measured)"
run docker volume ls --format '{{.Name}}'

section "traefik routing: hosts only"
for d in /etc/dokploy/traefik/dynamic /etc/dokploy/traefik; do
  [ -d "$d" ] && grep -rhoE 'Host\(`[^`]+`\)' "$d" 2>/dev/null | dedupe | scrub
done

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
for c in $(docker ps --format '{{.Names}}' | grep -iE 'bot|proqnoz'); do
  # One read per container; awk keeps only counters and the top 5 busiest hours (no sort, no temp files).
  $LOW docker logs --since 168h --timestamps --tail "$LOG_TAIL" "$c" 2>&1 | awk -v name="$c" '
    { lines++ }
    /football API 429/ { fd429++ }
    /football API/ { fdwarn++ }
    /ERROR|Traceback/ { errors++ }
    /[Ff]orecast|прогноз/ { hour = substr($1, 1, 13); per[hour]++ }
    /[Cc]onflict: terminated by other getUpdates/ { conflict++ }
    END {
      printf "%s: lines_read=%d football_429=%d football_api_warnings=%d errors=%d getUpdates_conflicts=%d\n", name, lines, fd429, fdwarn, errors, conflict
      for (k = 0; k < 5; k++) { best = ""; max = 0
        for (h in per) if (per[h] > max) { max = per[h]; best = h }
        if (best == "") break
        printf "  busiest hour %s: %d forecast-related lines\n", best, max; delete per[best] }
    }' | scrub
done

section "end"
