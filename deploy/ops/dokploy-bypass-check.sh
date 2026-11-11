#!/bin/sh
# Hourly guard for the Dokploy panel's Traefik route (docs/ops/dokploy-admin-access.md, step 2a). Root cron:
#   17 * * * * sh /root/dokploy-bypass-check.sh
# OK only when BOTH hold:
#   1) every router rule in dokploy.yml carries ClientIP(`127.0.0.1/32`) — catches Dokploy rewriting the file
#      (Settings -> Web Server domain sets Host(<domain>) without it; the editor may write anything);
#   2) the known default Host is refused from the public address (404).
# Result goes to healthchecks.io (URL in /root/.hc-dokploy-bypass, 0600): ping on OK, /fail otherwise; a missing
# ping (server, cron or network down) alerts after the check's grace time. Prints nothing secret.
set -u
F=${DOKPLOY_ROUTE_FILE:-/etc/dokploy/traefik/dynamic/dokploy.yml}
IP=${PUBLIC_IP:-164.90.180.208}
HC_FILE=${HC_FILE:-/root/.hc-dokploy-bypass}
reason=""
rules=$(grep -E '^[[:space:]]*rule:' "$F" 2>/dev/null) || reason="no rules readable in $F"
if [ -z "$reason" ] && printf '%s\n' "$rules" | grep -vqF 'ClientIP(`127.0.0.1/32`)'; then
  reason="a router rule in $F has no ClientIP restriction"
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Host: dokploy.docker.localhost' "http://$IP/")
[ "$code" = 404 ] || reason="${reason:+$reason; }default host answered $code"
u=$(cat "$HC_FILE" 2>/dev/null) || u=""
if [ -z "$reason" ]; then
  [ -n "$u" ] && curl -fsS -m 10 -o /dev/null "$u"
  exit 0
fi
logger -t dokploy-bypass "OPEN: $reason"
[ -n "$u" ] && curl -fsS -m 10 -o /dev/null "$u/fail"
echo "OPEN: $reason"
exit 1
