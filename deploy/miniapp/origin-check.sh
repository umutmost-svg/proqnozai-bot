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
# Closed = curl never completed a TCP connection (time_connect 0). Any connection — even one that then
# times out, or a 403 — means the firewall lets non-Cloudflare traffic reach the origin. No proxies.
probe() {
  what=$1; shift
  tc=$(curl -sk --noproxy '*' --connect-timeout 6 -m 12 -o /dev/null -w '%{time_connect}' "$@" 2>/dev/null); rc=$?
  case "$rc:$tc" in
    7:0*|28:0.000000|28:0) echo "closed  $what (exit $rc)";;
    *) case $rc in
         0|22|28|35|52|56|60) echo "OPEN    $what (exit $rc, connected in ${tc}s) — STOP"; verdict=1;;
         *) echo "INCONCLUSIVE $what (exit $rc)"; [ "$verdict" -eq 1 ] || verdict=2;;
       esac;;
  esac
}
probe "https $IP"  --resolve "$D:443:$IP" "https://$D/"
probe "http  $IP"  "http://$IP/"
if [ "$IP6" != none ]; then
  if curl -6 -fs --noproxy '*' -m 10 -o /dev/null https://www.cloudflare.com/ 2>/dev/null; then
    probe "https [$IP6]" -6 --resolve "$D:443:[$IP6]" "https://$D/"
  else
    echo "INCONCLUSIVE: this machine has no working IPv6 — run the IPv6 probe from another network"
    [ "$verdict" -eq 1 ] || verdict=2
  fi
fi
case $verdict in 0) echo "RESULT: origin isolated";; 1) echo "RESULT: ORIGIN REACHABLE — do not launch";; *) echo "RESULT: inconclusive";; esac
exit "$verdict"
