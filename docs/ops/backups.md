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
