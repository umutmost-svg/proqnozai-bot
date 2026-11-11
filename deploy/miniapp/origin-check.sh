#!/bin/sh
# External isolation check. Run on a machine OUTSIDE Cloudflare and outside the servers (the owner's laptop) —
# a server probing its own public IP says nothing about the cloud firewall.
#   sh origin-check.sh <MINIAPP_DOMAIN> <MINIAPP_SERVER_IPV4> <MINIAPP_SERVER_IPV6 | none>
#       Mini App origin: 443 and 80 must be closed to everyone but Cloudflare.
#   sh origin-check.sh --ports <SERVER_IPV4> <SERVER_IPV6 | none> <port>...
#       Any host and ports, e.g. the production server's Dokploy panel:  --ports <ip> <ipv6|none> 3000
# "none" only after checking in the DigitalOcean panel that the droplet has no IPv6 address.
# Every probe is direct (no proxy) and preceded by a direct control over the same address family and port from
# this machine; "closed" means the TCP connection never completed. Any completed connection — even one that then
# times out, or a 403 — is OPEN.
# Exit 0: everything closed; 1: something answered directly (STOP); 2: not conclusive (bad input, no direct
# path from this machine, no IPv6 here, Cloudflare control failed) — fix and rerun, IPv6 from another network.
set -u
usage() { echo "usage: sh origin-check.sh <domain> <ipv4> <ipv6|none> | --ports <ipv4> <ipv6|none> <port>..."; exit 2; }
MODE=origin
if [ "${1:-}" = --ports ]; then MODE=ports; shift; [ $# -ge 3 ] || usage; else [ $# -eq 3 ] || usage; fi
if [ "$MODE" = origin ]; then D=$1; shift; fi
IP=$1; IP6=$2; shift 2
if [ "$MODE" = origin ]; then
  printf '%s\n' "$D" | grep -Eqx '([a-z0-9-]+\.)+[a-z]{2,}' || { echo "INCONCLUSIVE: bad domain '$D'"; exit 2; }
fi
printf '%s\n' "$IP" | grep -Eqx '([0-9]{1,3}\.){3}[0-9]{1,3}' || { echo "INCONCLUSIVE: bad IPv4 '$IP'"; exit 2; }
[ "$IP6" = none ] || printf '%s\n' "$IP6" | grep -Eqx '[0-9a-fA-F:]+' || { echo "INCONCLUSIVE: bad IPv6 '$IP6'"; exit 2; }
for p in "$@"; do case $p in ''|*[!0-9]*) echo "INCONCLUSIVE: bad port '$p'"; exit 2;; esac; done

if [ "$MODE" = origin ]; then
  # Control: the app answers through Cloudflare (HTTP errors such as 525/526 fail here thanks to -f).
  curl -fsS -m 15 "https://$D/api/health" 2>/dev/null | grep -q '"status":"ok"' \
    || { echo "INCONCLUSIVE: https://$D/api/health is not OK through Cloudflare (network, DNS or origin TLS)"; exit 2; }
fi

verdict=0
inconclusive() { echo "INCONCLUSIVE: $1"; [ "$verdict" -eq 1 ] || verdict=2; }
# Direct control: this machine can open a TCP connection over this family to this port somewhere on the internet
# (443/80: Cloudflare; any other port: portquiz.net, which listens on every port).
direct_ok() {
  case $2 in 443) u=https://www.cloudflare.com/;; 80) u=http://www.cloudflare.com/;; *) u=http://portquiz.net:$2/;; esac
  tc=$(curl "-$1" -s --noproxy '*' --connect-timeout 6 -m 12 -o /dev/null -w '%{time_connect}' "$u" 2>/dev/null)
  awk -v t="${tc:-0}" 'BEGIN { exit !(t + 0 > 0) }'
}
probe() {
  what=$1; shift
  tc=$(curl -sk --noproxy '*' --connect-timeout 6 -m 12 -o /dev/null -w '%{time_connect}' "$@" 2>/dev/null); rc=$?
  if awk -v t="${tc:-0}" 'BEGIN { exit !(t + 0 == 0) }' && { [ "$rc" -eq 7 ] || [ "$rc" -eq 28 ]; }; then
    echo "closed  $what (exit $rc)"
  elif ! awk -v t="${tc:-0}" 'BEGIN { exit !(t + 0 == 0) }'; then
    echo "OPEN    $what (exit $rc, connected in ${tc}s) — STOP"; verdict=1
  else inconclusive "$what (exit $rc)"; fi
}
[ "$MODE" = origin ] && set -- 443 80
for fam in 4 6; do
  if [ "$fam" = 4 ]; then host=$IP; else [ "$IP6" = none ] && continue; host="[$IP6]"; fi
  for port in "$@"; do
    if ! direct_ok "$fam" "$port"; then inconclusive "no direct IPv$fam path to port $port from this machine"; continue; fi
    if [ "$MODE" = origin ] && [ "$port" = 443 ]; then probe "https IPv$fam :443" "-$fam" --resolve "$D:443:$host" "https://$D/"
    else probe "tcp   IPv$fam :$port" "-$fam" "http://$host:$port/"; fi
  done
done
case $verdict in 0) echo "RESULT: isolated";; 1) echo "RESULT: REACHABLE — do not launch";; *) echo "RESULT: inconclusive";; esac
exit "$verdict"
