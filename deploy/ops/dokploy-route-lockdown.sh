#!/bin/sh
# Closes the second public path to the Dokploy panel: Traefik :80 with `Host: dokploy.docker.localhost`
# (docs/ops/dokploy-admin-access.md step 2a; docs/ops/infra-hardening.md section 2). Run as root on the server.
#   sh dokploy-route-lockdown.sh apply             back up dokploy.yml, add ClientIP(`127.0.0.1/32`), verify,
#                                                  record the approved hash; on a failed check: restore + exit 1
#   sh dokploy-route-lockdown.sh verify            loopback-in-Traefik 200, public address 404, Mini App route alive
#   sh dokploy-route-lockdown.sh rollback <copy>   put a saved copy back (the panel is public on :80 again)
# Only the exact file Dokploy v0.29.12 generated (audited SHA-256) is edited; anything else is a STOP for a human.
# The new file is written next to the backup and renamed into place (atomic: Traefik never reads half a file;
# nothing temporary ever lands in the watched dynamic directory).
set -u
F=${DOKPLOY_ROUTE_FILE:-/etc/dokploy/traefik/dynamic/dokploy.yml}
IP=${PUBLIC_IP:-164.90.180.208}
TRAEFIK=${TRAEFIK_CONTAINER:-dokploy-traefik}
APP_HOST=${APP_HOST:-app.proqnozai.com}
LOCAL_HTTP=${LOCAL_HTTP:-http://127.0.0.1}   # Traefik :80 as the server itself reaches it
APPROVED=${APPROVED_SHA_FILE:-/root/.dokploy-route.sha256}
BACKUP_DIR=${BACKUP_DIR:-/root}
# dokploy.yml exactly as generated on 2026-07-17 and audited read-only on 2026-11-12.
ORIG_SHA=${ORIG_SHA:-2baa7fdd500e23a58723899db9eb35f6fc468f4bf7cb55557337ff2b69e84a20}
# The same file after exactly this one-line change (checked locally against traefik:v3.6.7 on a byte-exact copy).
APPLIED_SHA=${APPLIED_SHA:-d1d1132a8c933c2eda93012d0ea581be940e504fc7af52d3d8f354dc65005989}
OLD='rule: Host(`dokploy.docker.localhost`) && PathPrefix(`/`)'
NEW='rule: Host(`dokploy.docker.localhost`) && PathPrefix(`/`) && ClientIP(`127.0.0.1/32`)'
PATH="$PATH:/usr/local/bin:/usr/bin:/bin"
umask 077

sha() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

# One round of the three checks; prints the observed values. 0 = locked down and the file provider is alive.
check_once() {
  inner=$(docker exec "$TRAEFIK" wget -q -S -O /dev/null --header 'Host: dokploy.docker.localhost' http://127.0.0.1/ 2>&1 \
    | grep -m1 -oE 'HTTP/1\.1 [0-9]+' | awk '{print $2}')
  outer=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Host: dokploy.docker.localhost' "http://$IP/")
  app=$(curl -s -m 10 -H "Host: $APP_HOST" "$LOCAL_HTTP/api/health" 2>/dev/null)
  case $app in *'"status":"ok"'*) app=ok;; *) app=down;; esac
  echo "loopback-in-traefik=${inner:-none} public=${outer:-none} miniapp-route=$app"
  [ "$inner" = 200 ] && [ "$outer" = 404 ] && [ "$app" = ok ]
}

# Traefik reloads the watched directory within seconds; give it up to ~30 s.
verify() {
  i=0
  while [ $i -lt 10 ]; do
    if res=$(check_once); then echo "VERIFIED: $res"; return 0; fi
    i=$((i + 1)); sleep 3
  done
  echo "NOT VERIFIED: $res"
  echo "  expected loopback-in-traefik=200 (rule loaded and the panel works for the server itself),"
  echo "  public=404 (closed for everyone else), miniapp-route=ok (the file provider still works)"
  return 1
}

# The watcher (dokploy-bypass-check.sh) compares against this file: success is reported only once it is written.
record_approved() {
  sha256sum "$F" > "$APPROVED.tmp" && mv -f "$APPROVED.tmp" "$APPROVED" \
    && [ "$(awk '{print $1}' "$APPROVED")" = "$(sha "$F")" ] \
    && { echo "approved hash recorded in $APPROVED"; return 0; }
  echo "FAILED to record the approved hash in $APPROVED"; return 1
}

put_in_place() {  # $1 = prepared file in BACKUP_DIR; rename into place keeping 0644 root:root
  chmod 644 "$1" && chown root:root "$1" && mv -f "$1" "$F"
}

[ "$(id -u)" = 0 ] || { echo "STOP: run as root"; exit 1; }
case ${1:-} in
  verify) verify; exit $? ;;
  apply)
    [ -f "$F" ] || { echo "STOP: $F not found"; exit 1; }
    have=$(sha "$F")
    if [ "$have" = "$APPLIED_SHA" ]; then
      echo "already applied (sha256 $have) — verifying only"
      verify || exit 1
      record_approved || exit 1
      exit 0
    fi
    [ "$have" = "$ORIG_SHA" ] || { echo "STOP: $F differs from the audited original (sha256 $have) — needs a human look, nothing changed"; exit 1; }
    [ "$(grep -Fxc "      $OLD" "$F")" = 1 ] || { echo "STOP: the expected rule line is not there exactly once"; exit 1; }
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    bak="$BACKUP_DIR/dokploy.yml.bak-$stamp"; new="$BACKUP_DIR/dokploy.yml.new-$stamp"
    cp -p "$F" "$bak" || { echo "STOP: cannot back up $F"; exit 1; }
    awk -v old="      $OLD" -v new="      $NEW" '$0 == old { print new; next } { print }' "$F" > "$new" \
      || { rm -f "$new"; echo "STOP: cannot prepare the new file"; exit 1; }
    [ "$(sha "$new")" = "$APPLIED_SHA" ] \
      || { rm -f "$new"; echo "STOP: prepared file is not the expected one-line change"; exit 1; }
    echo "backup: $bak"
    diff "$F" "$new"
    put_in_place "$new" || { echo "STOP: could not put the new file in place; original untouched? check $F against $bak"; exit 1; }
    if verify; then
      record_approved || { echo "the route IS locked down; record the hash by hand: sha256sum $F > $APPROVED"; exit 1; }
      echo "DONE. Rollback: sh $0 rollback $bak"
      exit 0
    fi
    echo "ROLLING BACK to $bak"
    cp -p "$bak" "$BACKUP_DIR/dokploy.yml.restore-$stamp" && put_in_place "$BACKUP_DIR/dokploy.yml.restore-$stamp" \
      && [ "$(sha "$F")" = "$ORIG_SHA" ] && echo "rolled back: original file in place" || echo "ROLLBACK FAILED: restore $bak to $F by hand"
    exit 1 ;;
  rollback)
    bak=${2:-}
    [ -f "$bak" ] || { echo "usage: sh dokploy-route-lockdown.sh rollback <backup copy>"; exit 2; }
    tmp="$BACKUP_DIR/dokploy.yml.restore-$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$bak" "$tmp" && put_in_place "$tmp" || { echo "FAILED: $F unchanged?"; exit 1; }
    echo "restored $bak -> $F (sha256 $(sha "$F")). The panel answers on :80 again; dokploy-bypass-check will alert."
    exit 0 ;;
  *) echo "usage: sh dokploy-route-lockdown.sh apply | verify | rollback <copy>"; exit 2 ;;
esac
