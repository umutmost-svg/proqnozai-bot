#!/bin/sh
# Hourly guard against a public route to the Dokploy panel (docs/ops/dokploy-admin-access.md, step 2a).
# Scope: ACCIDENTAL re-exposure — Dokploy's own Web Server settings (they write only dokploy.yml), upgrades,
# hand edits of dokploy.yml, a copied route file or labels on the dokploy service. Not in scope: a route that a
# Dokploy admin or root deliberately builds elsewhere (an app's domain, another container's labels, a raw service
# IP) — whoever can do that already controls the panel and the server.
#   setup, once, right after the verified edit:   sha256sum /etc/dokploy/traefik/dynamic/dokploy.yml > /root/.dokploy-route.sha256
#   root cron:                                    17 * * * * sh /root/dokploy-bypass-check.sh
# Fails (alert) on ANY of:
#   1) dokploy.yml differs from the approved, verified copy (exact SHA-256) — any rewrite by Dokploy (domain in
#      Settings -> Web Server, the config editor, an upgrade) or by hand needs a human look, whatever it says;
#   2) another file in the Traefik dynamic directory names Dokploy's backend (dokploy-service-app or dokploy:3000);
#   3) the Dokploy Swarm service carries Traefik labels (a route through the Swarm provider);
#   4) the default Host is answered from the public address with anything but 404.
# Result goes to healthchecks.io (URL in /root/.hc-dokploy-bypass, 0600): ping when all is well, /fail otherwise;
# a missing ping (server, cron or network down) alerts after the check's grace time. Prints nothing secret.
set -u
DIR=${TRAEFIK_DYNAMIC_DIR:-/etc/dokploy/traefik/dynamic}
F="$DIR/dokploy.yml"
APPROVED=${APPROVED_SHA_FILE:-/root/.dokploy-route.sha256}
IP=${PUBLIC_IP:-164.90.180.208}
HC_FILE=${HC_FILE:-/root/.hc-dokploy-bypass}
reason=""
add() { reason="${reason:+$reason; }$1"; }

want=$(awk '{print $1}' "$APPROVED" 2>/dev/null)
have=$(sha256sum "$F" 2>/dev/null | awk '{print $1}')
[ -n "$want" ] || add "no approved hash in $APPROVED"
[ -n "$have" ] || add "cannot read $F"
[ -n "$want" ] && [ -n "$have" ] && [ "$want" != "$have" ] && add "dokploy.yml changed since it was approved"

for g in "$DIR"/*.yml "$DIR"/*.yaml "$DIR"/*.toml; do
  [ -f "$g" ] && [ "$g" != "$F" ] || continue
  grep -qiE 'dokploy-service-app|dokploy:3000' "$g" && add "$(basename "$g") names Dokploy's backend"
done

if command -v docker >/dev/null 2>&1; then
  labels=$(docker service inspect dokploy --format '{{json .Spec.Labels}} {{json .Spec.TaskTemplate.ContainerSpec.Labels}}' 2>/dev/null) \
    || add "cannot inspect the dokploy service"
  printf '%s' "${labels:-}" | grep -qi 'traefik\.' && add "dokploy service has Traefik labels"   # labels are case-insensitive
fi

code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Host: dokploy.docker.localhost' "http://$IP/")
[ "$code" = 404 ] || add "default host answered $code"

u=$(cat "$HC_FILE" 2>/dev/null) || u=""
if [ -z "$reason" ]; then
  [ -n "$u" ] && curl -fsS -m 10 -o /dev/null "$u"
  exit 0
fi
logger -t dokploy-bypass "OPEN: $reason" 2>/dev/null
[ -n "$u" ] && curl -fsS -m 10 -o /dev/null "$u/fail"
echo "OPEN: $reason"
exit 1
