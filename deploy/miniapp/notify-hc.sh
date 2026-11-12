#!/bin/sh
# NOTIFY command for backup-job.sh: signals failure to a healthchecks.io check, the reason as the request body.
#   NOTIFY=/opt/sporbot/notify-hc.sh   (backup-job.sh runs:  $NOTIFY "<reason>")
# URL of the check in HC_URL_FILE (default /root/.hc-miniapp-backup, 0600). Prints nothing secret.
set -u
u=$(cat "${HC_URL_FILE:-/root/.hc-miniapp-backup}" 2>/dev/null) || { echo "notify: no healthchecks URL file" >&2; exit 1; }
curl -fsS -m 10 -o /dev/null --data-raw "${1:-backup failed}" "$u/fail"
