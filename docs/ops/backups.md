# Backups and alerts (production, 2026-11-15)

- **Daily database copies on the server.** `deploy/ops/prod-backup.sh` runs from root cron at 03:23 UTC and keeps 7 days in `/root/backups/<stamp>/`. It copies `bot.db`, `miniapp.db`, `miniapp.forecasts.db` and `admin.db`, plus `miniapp.telemetry.db`, `bot-outbox.db` and `bot-crm.db` when they exist. It uses the SQLite online backup API, from read-only connections and while the apps keep running. Each database is read as its owner: `bot.db` on the host as root, the others inside the admin container as uid 10001. Every copy gets an integrity check. Files are 0600 in 0700 directories. Restore: stop the service, put the copy in its volume (`/var/lib/docker/volumes/<volume>/_data/`), fix its owner (bot root, Mini App and admin uid 10001), then start the service.
- **Off-server copies.** DigitalOcean Droplet Backups, the whole disk, enabled by the owner (Droplet → Backups). Restore: Droplet → Backups → Restore, or create a new droplet from the backup.
- **Alerts.** `deploy/ops/notify-telegram.sh "<text>"` messages the owner through the running bot. The token and the owner id are read from the bot container and kept in a root-only `/root/.notify-telegram` (0600), so an alert still goes out when Docker itself is down. It is used by the backup job and by `deploy/ops/dokploy-bypass-check.sh`, which runs hourly and alerts when Dokploy might be reachable without Access. After the bot is retired, switch both to another channel, for example healthchecks.io.

The bypass check refreshes that cache hourly (`notify-telegram.sh --refresh`). Alerts go to healthchecks.io only if `/root/.hc-dokploy-bypass` exists. The Telegram setup has no such file: remove it if one was created earlier.

Install on the host (root):

```sh
for f in notify-telegram.sh prod-backup.sh dokploy-bypass-check.sh; do git show <commit>:deploy/ops/$f | ssh root@164.90.180.208 "install -m 700 /dev/stdin /root/$f"; done
ssh root@164.90.180.208 'rm -f /root/.hc-dokploy-bypass && /root/notify-telegram.sh --refresh && sha256sum /etc/dokploy/traefik/dynamic/dokploy.yml > /root/.dokploy-route.sha256 && (crontab -l | grep -v -e prod-backup -e dokploy-bypass; echo "23 3 * * * /usr/bin/flock -n /run/prod-backup.lock /root/prod-backup.sh >> /var/log/prod-backup.log 2>&1"; echo "47 * * * * sh /root/dokploy-bypass-check.sh >/dev/null 2>&1") | crontab -'
```

## Monitoring (audit wave 2, 2026-11-17)

`deploy/ops/watchdog.sh` runs from root cron every 5 minutes. It messages the owner through `notify-telegram.sh`. Only counts and names go out, never log lines.

- **What it checks:**
  - the Mini App and the admin panel are running and healthy;
  - the bot, the Cloudflare tunnel and Traefik are running;
  - `https://app.proqnozai.com/api/health` answers 200;
  - the disk is not 85% full or more;
  - available memory is at least 250 MB;
  - the Mini App uses less than 90% of its 512 MiB limit;
  - the newest copy in `/root/backups` is at most 26 h old, which catches a backup job that never ran.
- **Log events:** the watchdog counts these patterns in the last 6 minutes of Mini App logs:
  - Python tracebacks;
  - the AI provider refusing the key or the credit (`ai_upstream_error` with status 400/401/403);
  - five or more AI provider errors;
  - `quota_exhausted` from football-data.
- **When it alerts:**
  - **A state problem:** reported once it has lasted 10 minutes, so a stop-first deploy, which waits up to 6 minutes for health, does not trigger it. While it lasts, it is repeated every 6 h. Its recovery is reported with ✅.
  - **A log event:** reported at most once per 6 h, with no recovery message.
- **Dead man's switch (optional):** create a check on healthchecks.io with a period of 5 min and a grace of 15 min. Put its ping URL into `/root/.hc-watchdog` (0600). Every run then pings it, so a dead server or a dead cron also alerts. Without that file, nothing outside watches the server itself.

Install or update (root):

```sh
git show <commit>:deploy/ops/watchdog.sh | ssh root@164.90.180.208 "install -m 700 /dev/stdin /root/watchdog.sh"
ssh root@164.90.180.208 '(crontab -l | grep -v watchdog.sh; echo "*/5 * * * * /usr/bin/flock -n /run/watchdog.lock /root/watchdog.sh >> /var/log/watchdog.log 2>&1") | crontab -'
```

The deploy scripts (`deploy/{miniapp,admin}/dokploy_deploy.py`) gained these guards in the same wave:

- **One run at a time:** each script takes a lock in `/run`.
- **Wait for health:** with `--deploy`, a run waits up to 6 minutes for the new image to report healthy. Otherwise it exits non-zero, and Swarm rolls the update back by itself.
- **Public check (Mini App):** after that, the Mini App script also checks the public health URL.
- **No silent switch-off (Mini App):** the script refuses to switch off a feature the running app has when that feature's file in `/root/miniapp-secrets` is missing. This covers public access, AI, the odds feed, the admin policy and the blocked list. Pass `--allow-off` to switch one off on purpose.
