#!/bin/sh
# shellcheck disable=SC2046  # $(rule ...) is split into iptables arguments on purpose
# Host-level block of Dokploy's published port 3000 from outside (docs/ops/infra-hardening.md, section 1).
# Why not UFW: Docker publishes 3000 by DNAT in nat/PREROUTING, so outside packets go through FORWARD, never INPUT
# (UFW lives in INPUT). DOCKER-USER is the FORWARD chain Docker evaluates first and never edits itself.
# The match is the ORIGINAL destination port (conntrack), so the rule stays valid when Dokploy's task gets a new
# container IP. Loopback 127.0.0.1:3000 (cloudflared, an SSH tunnel) is never forwarded: it is not affected.
# Replies to connections the containers open to some remote :3000 are REPLY-direction: not affected either.
#   sh docker-user-3000.sh apply     insert the DROP rules (idempotent)
#   sh docker-user-3000.sh remove    delete them (rollback)
#   sh docker-user-3000.sh status    exit 0 only when DOCKER-USER is first in FORWARD and its FIRST rules are exactly
#                                    these DROPs (a rule inserted above them, e.g. an ACCEPT, would bypass them)
# Survives reboots through proqnozai-docker-user.service (same directory). Run as root.
set -u
PORT=${PORT:-3000}
IFACES=${BLOCK_IFACES:-eth0 eth1}   # eth0: public IPv4 + anchor IP; eth1: DigitalOcean VPC
TAG="proqnozai-block-$PORT"
PATH="$PATH:/usr/sbin:/sbin:/usr/bin:/bin"

case $PORT in ''|*[!0-9]*) echo "bad PORT '$PORT'"; exit 2;; esac
for i in $IFACES; do
  printf '%s\n' "$i" | grep -Eqx '[A-Za-z0-9_.-]{1,15}' || { echo "bad interface name '$i'"; exit 2; }
done

# Word splitting of the rule text is intended: no element contains a space.
rule() { echo "-i $1 -p tcp -m conntrack --ctorigdstport $PORT --ctdir ORIGINAL -m comment --comment $TAG -j DROP"; }

N=$(printf '%s\n' $IFACES | grep -c .)
tagged() { grep -Ec -- "--comment \"?$TAG\"?( |\$)"; }
# 0 when the first N rules of DOCKER-USER are tagged DROPs, no tagged rule sits lower, and every interface has its
# rule: nothing can be evaluated before them.
on_top() {  # $1 iptables|ip6tables
  rules=$($1 -S DOCKER-USER 2>/dev/null | grep -v '^-N ') || return 1
  [ "$(printf '%s\n' "$rules" | head -n "$N" | grep -- '-j DROP' | tagged)" = "$N" ] || return 1
  [ "$(printf '%s\n' "$rules" | tagged)" = "$N" ] || return 1
  for i in $IFACES; do $1 -C DOCKER-USER $(rule "$i") 2>/dev/null || return 1; done
}

# The DROPs only act if FORWARD sends packets to DOCKER-USER first (Docker sets this jump; it is not ours to edit).
jump_first() { [ "$($1 -S FORWARD 2>/dev/null | sed -n 2p)" = "-A FORWARD -j DOCKER-USER" ]; }

rc=0
case ${1:-} in
  apply)
    for bin in iptables ip6tables; do
      # Docker creates the chain at start; creating it first is harmless (Docker then uses the existing chain).
      $bin -n -L DOCKER-USER >/dev/null 2>&1 || $bin -N DOCKER-USER || { echo "FAIL: $bin cannot create DOCKER-USER"; rc=1; continue; }
      jump_first "$bin" || { echo "FAIL: $bin: DOCKER-USER is not the first FORWARD rule — the block would not apply"; rc=1; }
      if on_top "$bin"; then echo "present  $bin :$PORT ($IFACES)"; continue; fi
      # New copies go to the top first (no unprotected moment), then every lower tagged copy is deleted by number,
      # highest first so the remaining numbers stay valid.
      for i in $IFACES; do $bin -I DOCKER-USER 1 $(rule "$i") || { echo "FAIL: $bin insert $i:$PORT"; rc=1; }; done
      for num in $($bin -S DOCKER-USER | grep -v '^-N ' | grep -En -- "--comment \"?$TAG\"?( |\$)" \
                   | awk -F: -v n="$N" '$1 > n { print $1 }' | sort -rn); do
        $bin -D DOCKER-USER "$num" || { echo "FAIL: $bin delete old rule $num"; rc=1; }
      done
      if on_top "$bin"; then echo "added    $bin :$PORT ($IFACES), first in DOCKER-USER"
      else echo "FAIL: $bin rules are not first in DOCKER-USER"; rc=1; fi
    done ;;
  remove)
    for bin in iptables ip6tables; do
      for i in $IFACES; do
        n=0
        while $bin -C DOCKER-USER $(rule "$i") 2>/dev/null && [ $n -lt 10 ]; do
          $bin -D DOCKER-USER $(rule "$i") || { echo "FAIL: $bin delete $i:$PORT"; rc=1; break; }
          n=$((n + 1))
        done
        echo "removed  $bin $i:$PORT ($n)"
      done
    done ;;
  status)
    for bin in iptables ip6tables; do
      jump_first "$bin" || { echo "MISSING  $bin: DOCKER-USER is not the first FORWARD rule"; rc=1; }
      if on_top "$bin"; then echo "ok       $bin :$PORT ($IFACES)"
      else echo "MISSING  $bin: the :$PORT DROPs are absent or not the first rules of DOCKER-USER"; rc=1; fi
    done ;;
  *) echo "usage: sh docker-user-3000.sh apply|remove|status"; exit 2 ;;
esac
exit $rc
