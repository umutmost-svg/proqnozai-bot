#!/bin/sh
# Every 5 minutes (root cron) on the production server (docs/ops/infra-hardening.md, section 4):
#   */5 * * * * sh /opt/proqnozai-ops/host-monitor.sh >> /var/log/proqnozai-monitor.log 2>&1
# Two healthchecks.io checks, each URL in its own 0600 file (never printed):
#   host            /root/.hc-host            disk use of / <= DISK_MAX_PCT, MemAvailable >= MEM_MIN_MB,
#                                             cloudflared running with >= 1 edge connection, every Swarm service n/n,
#                                             and — once proqnozai-docker-user.service is enabled — the :3000 block
#   miniapp-health  /root/.hc-miniapp-health  GET /api/health through Traefik on loopback (Host APP_HOST) is ok
# All well -> ping (with a short summary as body); otherwise -> /fail with the reasons + syslog tag proqnozai-monitor.
# A dead server, cron or network sends nothing: healthchecks alerts after the grace time.
set -u
PATH="$PATH:/usr/sbin:/sbin:/usr/local/bin:/usr/bin:/bin"
DISK_MAX_PCT=${DISK_MAX_PCT:-85}
MEM_MIN_MB=${MEM_MIN_MB:-400}
APP_HOST=${APP_HOST:-app.proqnozai.com}
CF_CONTAINER=${CF_CONTAINER:-cloudflared}
CF_METRICS=${CF_METRICS:-http://127.0.0.1:20241}
OPS=${OPS_DIR:-/opt/proqnozai-ops}
HC_HOST=${HC_HOST_FILE:-/root/.hc-host}
HC_APP=${HC_APP_FILE:-/root/.hc-miniapp-health}

ping_hc() {  # $1 url file, $2 ok|fail, $3 body
  u=$(cat "$1" 2>/dev/null) || { echo "$(date -u +%FT%TZ) no healthchecks URL in $1"; return 1; }
  case $2 in ok) suffix="";; *) suffix="/fail";; esac
  curl -fsS -m 10 -o /dev/null --data-raw "$3" "$u$suffix" || echo "$(date -u +%FT%TZ) healthchecks ping failed ($1)"
}

report() {  # $1 name, $2 url file, $3 reasons ("" = ok), $4 summary
  if [ -z "$3" ]; then ping_hc "$2" ok "$4"; return 0; fi
  logger -t proqnozai-monitor "$1 FAIL: $3" 2>/dev/null
  echo "$(date -u +%FT%TZ) $1 FAIL: $3"
  ping_hc "$2" fail "$3 | $4"
}

# --- host ---
r=""
add() { r="${r:+$r; }$1"; }
disk=$(df -P / | awk 'NR == 2 { sub("%", "", $5); print $5 }')
case $disk in ''|*[!0-9]*) add "disk: cannot read"; disk=unknown;; *) [ "$disk" -le "$DISK_MAX_PCT" ] || add "disk ${disk}% > ${DISK_MAX_PCT}%";; esac
mem=$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)
case $mem in ''|*[!0-9]*) add "memory: cannot read"; mem=unknown;; *) [ "$mem" -ge "$MEM_MIN_MB" ] || add "MemAvailable ${mem}MB < ${MEM_MIN_MB}MB";; esac
if [ "$(docker inspect -f '{{.State.Running}}' "$CF_CONTAINER" 2>/dev/null)" = true ]; then
  conns=$(curl -s -m 5 "$CF_METRICS/ready" | sed -n 's/.*"readyConnections":\([0-9]*\).*/\1/p')
  [ "${conns:-0}" -ge 1 ] || add "cloudflared has no edge connection"
else
  add "cloudflared container is not running"; conns=0
fi
if svcs=$(docker service ls --format '{{.Name}} {{.Replicas}}' 2>/dev/null); then
  bad=$(printf '%s\n' "$svcs" | awk 'NF { split($2, a, "/"); if (a[1] != a[2] || a[2] + 0 == 0) printf "%s %s, ", $1, $2 }')
  [ -z "$bad" ] || add "services not n/n: ${bad%, }"
else
  add "docker service ls failed"
fi
if systemctl is-enabled --quiet proqnozai-docker-user.service 2>/dev/null; then
  sh "$OPS/docker-user-3000.sh" status >/dev/null 2>&1 || add ":3000 DOCKER-USER block missing"
fi
report host "$HC_HOST" "$r" "disk=${disk}% mem=${mem}MB cf_conns=${conns:-0}"

# --- miniapp-health ---
body=$(curl -s -m 10 -H "Host: $APP_HOST" http://127.0.0.1/api/health 2>/dev/null)
case $body in *'"status":"ok"'*) a="";; *) a="/api/health not ok via Traefik loopback";; esac
report miniapp-health "$HC_APP" "$a" "host=$APP_HOST"
