#!/bin/sh
# READ-ONLY inventory of the production server before the first Mini App release.
# Run on the Dokploy host as a user that can read Docker (e.g. root):   sh prod-discovery.sh > discovery.txt
# It changes nothing, starts nothing, and never prints environment values, tokens, keys or
# Dokploy credentials: only names, images, mounts, ports, routing hosts and counts.
# Send discovery.txt back; review it first — it lists container names and domains.
set -u

section() { printf '\n===== %s =====\n' "$1"; }

section "host"
uname -srm; uptime; df -h / /var/lib/docker 2>/dev/null; free -m 2>/dev/null | head -2
docker version --format 'docker server {{.Server.Version}}' 2>/dev/null

section "swarm / dokploy services (name, image, replicas, ports)"
docker service ls --format '{{.Name}}\t{{.Image}}\t{{.Replicas}}\t{{.Ports}}' 2>/dev/null

section "service update/rollback order and restart policy (no env)"
for s in $(docker service ls --format '{{.Name}}' 2>/dev/null); do
  docker service inspect "$s" --format \
    '{{.Spec.Name}} update={{with .Spec.UpdateConfig}}{{.Order}}/{{.FailureAction}}{{end}} rollback={{with .Spec.RollbackConfig}}{{.Order}}{{end}} restart={{with .Spec.TaskTemplate.RestartPolicy}}{{.Condition}}{{end}} mounts={{range .Spec.TaskTemplate.ContainerSpec.Mounts}}{{.Type}}:{{.Source}}->{{.Target}}{{if .ReadOnly}}(ro){{end}} {{end}}' 2>/dev/null
done

section "running containers (name, image, status, published ports)"
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

section "container mounts (no env)"
for c in $(docker ps --format '{{.Names}}'); do
  docker inspect "$c" --format '{{.Name}} {{range .Mounts}}{{.Type}}:{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}->{{.Destination}}{{if not .RW}}(ro){{end}} {{end}}'
done

section "docker volumes (name, size)"
docker system df -v 2>/dev/null | sed -n '/VOLUME NAME/,/^$/p'

section "traefik routing: hosts only"
for d in /etc/dokploy/traefik/dynamic /etc/dokploy/traefik; do
  [ -d "$d" ] && grep -rhoE 'Host\(`[^`]+`\)' "$d" 2>/dev/null | sort -u
done

section "dokploy version / applications (names only)"
docker ps --format '{{.Image}}' | grep -i dokploy | sort -u
ls /etc/dokploy/applications 2>/dev/null; ls /etc/dokploy/compose 2>/dev/null

section "backups: crontabs and dokploy backup dirs (names only)"
crontab -l 2>/dev/null | grep -v '^#' | sed 's/[A-Za-z0-9_\-]\{24,\}/<redacted>/g'
ls /etc/cron.d 2>/dev/null
ls -la /etc/dokploy/backups 2>/dev/null | head -20

section "bot: football-data 429s and API errors in the last 7 days (counts only)"
for c in $(docker ps --format '{{.Names}}' | grep -iE 'bot|proqnoz' ); do
  printf '%s: 429=%s football_api_warnings=%s errors=%s\n' "$c" \
    "$(docker logs --since 168h "$c" 2>&1 | grep -c 'football API 429')" \
    "$(docker logs --since 168h "$c" 2>&1 | grep -c 'football API')" \
    "$(docker logs --since 168h "$c" 2>&1 | grep -cE 'ERROR|Traceback')"
done

section "bot: forecasts per hour, busiest hours of the last 7 days (counts only)"
for c in $(docker ps --format '{{.Names}}' | grep -iE 'bot|proqnoz' ); do
  echo "$c:"; docker logs --since 168h --timestamps "$c" 2>&1 | grep -iE 'forecast|прогноз' | cut -c1-13 | sort | uniq -c | sort -rn | head -5
done

section "end"
