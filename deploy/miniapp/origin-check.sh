#!/bin/sh
# External origin-isolation check for the Mini App server. Run on a machine OUTSIDE Cloudflare and outside both
# servers (the owner's laptop) — a server probing its own public IP says nothing about the cloud firewall.
#   sh origin-check.sh <MINIAPP_DOMAIN> <MINIAPP_SERVER_IPV4> <MINIAPP_SERVER_IPV6 | none>
# "none" only after checking in the DigitalOcean panel that the droplet has no IPv6 address.
# Exit 0: every direct probe was refused/never connected; 1: the origin answered directly (STOP the launch);
# 2: the check itself is not conclusive (bad input, no network, no IPv6 here) — fix and rerun.
set -u
[ $# -eq 3 ] || { echo "usage: sh origin-check.sh <domain> <ipv4> <ipv6|none>"; exit 2; }
D=$1; IP=$2; IP6=$3
printf '%s\n' "$D" | grep -Eqx '([a-z0-9-]+\.)+[a-z]{2,}' || { echo "INCONCLUSIVE: bad domain '$D'"; exit 2; }
printf '%s\n' "$IP" | grep -Eqx '([0-9]{1,3}\.){3}[0-9]{1,3}' || { echo "INCONCLUSIVE: bad IPv4 '$IP'"; exit 2; }
[ "$IP6" = none ] || printf '%s\n' "$IP6" | grep -Eqx '[0-9a-fA-F:]+' || { echo "INCONCLUSIVE: bad IPv6 '$IP6'"; exit 2; }

# Control: the app answers through Cloudflare (HTTP errors such as 525/526 fail here thanks to -f).
curl -fsS -m 15 "https://$D/api/health" 2>/dev/null | grep -q '"status":"ok"' \
  || { echo "INCONCLUSIVE: https://$D/api/health is not OK through Cloudflare (network, DNS or origin TLS)"; exit 2; }

verdict=0
inconclusive() { echo "INCONCLUSIVE: $1"; [ "$verdict" -eq 1 ] || verdict=2; }
# Direct (no proxy) control for one address family and port: without it a refused probe proves nothing.
direct_ok() { curl "$1" -s --noproxy '*' --connect-timeout 6 -m 12 -o /dev/null "$2://www.cloudflare.com/" 2>/dev/null; }
# Closed = curl never completed a TCP connection (time_connect is zero). Any connection — even one that then
# times out, or a 403 — means the firewall lets non-Cloudflare traffic reach the origin.
probe() {
  what=$1; shift
  tc=$(curl -sk --noproxy '*' --connect-timeout 6 -m 12 -o /dev/null -w '%{time_connect}' "$@" 2>/dev/null); rc=$?
  if awk -v t="${tc:-0}" 'BEGIN { exit !(t + 0 == 0) }' && { [ "$rc" -eq 7 ] || [ "$rc" -eq 28 ]; }; then
    echo "closed  $what (exit $rc)"
  else case $rc in
    0|22|28|35|52|56|60) echo "OPEN    $what (exit $rc, connected in ${tc}s) — STOP"; verdict=1;;
    *) inconclusive "$what (exit $rc)";;
  esac; fi
}
for fam in 4 6; do
  if [ "$fam" = 4 ]; then addr=$IP; host=$IP; else [ "$IP6" = none ] && continue; addr="[$IP6]"; host="[$IP6]"; fi
  if direct_ok "-$fam" https; then probe "https IPv$fam" "-$fam" --resolve "$D:443:$addr" "https://$D/"
  else inconclusive "no direct IPv$fam HTTPS from this machine — run from another network"; fi
  if direct_ok "-$fam" http; then probe "http  IPv$fam" "-$fam" "http://$host/"
  else inconclusive "no direct IPv$fam HTTP (port 80) from this machine — run from another network"; fi
done
case $verdict in 0) echo "RESULT: origin isolated";; 1) echo "RESULT: ORIGIN REACHABLE — do not launch";; *) echo "RESULT: inconclusive";; esac
exit "$verdict"
