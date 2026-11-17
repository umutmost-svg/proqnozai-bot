# Admin panel on admin.proqnozai.com (server mode)

Written 2026-11-15. The local pilot (`docs/miniapp/admin-pilot.md`) is unchanged; this describes `ADMIN_WEB_MODE=server`.

## Request path

```
browser ─HTTPS─> Cloudflare Access (owner's e-mail only)
                 └─ Transform Rule: sets X-Admin-Edge-Auth = <edge secret> on admin.proqnozai.com
                    └─ Tunnel proqnozai-admin (Protect with Access: JWT checked by cloudflared)
                       └─ 127.0.0.1:80 Traefik ── Host admin.proqnozai.com ──> proqnozai-admin:8795
                          └─ admin: edge secret (constant time) → Host → Origin/CSRF → session (password + TOTP)
```

There are four layers. A request arriving another way, such as the server's public :80 with a forged `Host`, carries no edge secret and gets 403 before anything else is checked.

## Process

- Image: `deploy/admin/Dockerfile`, with the same pinned bases and hash-checked lock as the Mini App. Entrypoint: `deploy/admin/entrypoint.sh`.
- The container runs as uid 10001 (the Mini App's uid), so it can read the Mini App's SQLite files.
- Volumes:
  - `proqnozai-miniapp-data` → `/data`: the Mini App's data. The admin reads `miniapp.db` and `miniapp.forecasts.db`, and also writes to `miniapp.db` (session revocation, credits, MB IDs). It writes its published files here: `access-policy.json`, `ads-policy.json` and `bot-outbox.db`.
  - `proqnozai-admin-state` → `/state`: the admin's own data (`admin.db` with accounts, sessions and TOTP secrets; the signing key is the separate file `ads-signing.key`, backed up with `admin.db`). Mode 0700. **Never mounted into the Mini App.** The Mini App's entrypoint runs `chown -R` on its whole volume, so the admin's secrets cannot live there.
- Environment: `ADMIN_WEB_MODE=server`, `ADMIN_DATA_DIR=/data`, `ADMIN_STATE_DIR=/state`, `ADMIN_PUBLIC_HOST=admin.proqnozai.com` and `ADMIN_EDGE_SECRET`. The last comes from `/root/admin-secrets/edge`, is at least 32 characters, and is never printed. `ADMIN_BOT_USERNAME=ProqnozAI_bot` (public) gives full referral links; with no `ADMIN_MINIAPP_SHORT_NAME`, Mini App links use the bot's main Mini App (`t.me/ProqnozAI_bot?startapp=ref_…`), which must be set in BotFather.
- The server mode refuses:
  - demo data (`?demo=1` is ignored);
  - the `init` and `code` CLI commands (a printed 2FA code would bypass the 2FA);
  - a pilot directory;
  - a state directory inside the Mini App's volume.
- Cookies: `__Host-sporbot_admin_session` and `__Secure-sporbot_admin_pre`, both Secure, HttpOnly and SameSite=Strict. HSTS is sent.
- Deploy: `deploy/admin/dokploy_deploy.py <tag> --deploy`. The image goes in the loopback registry as `127.0.0.1:5000/proqnozai-admin:<tag>`, with no host ports and no autodeploy.

## Owner steps

1. **Edge secret.** In your own terminal, not in a chat:
   ```sh
   ssh root@164.90.180.208 'install -d -m 700 /root/admin-secrets && (umask 077; openssl rand -hex 32 > /root/admin-secrets/edge) && cat /root/admin-secrets/edge'
   ```
2. **Cloudflare → proqnozai.com → Rules → Transform Rules → Modify Request Header → Create.** Name `admin-edge-auth`. Condition: *Hostname equals* `admin.proqnozai.com`. Action: *Set static* header `X-Admin-Edge-Auth` with the value from step 1.
3. **Tunnel `proqnozai-admin` → Published application routes → Add.** `admin.proqnozai.com`, path empty, `HTTP` → `127.0.0.1:80`. **Protect with Access** on, with Team name `patient-mode-acdb` and the AUD of the `admin.proqnozai.com` Access application.
4. **Access application** `admin.proqnozai.com`: Self-hosted on the whole host, with a single policy *Allow → Emails →* the owner's e-mail.
   Every colleague invited to the panel is added to the same policy (*Emails*), otherwise Access stops them before the panel.

### Colleagues (16.11)

- **Settings → Administrators → Add a colleague** (owner, reason + 2FA) creates the account and shows a one-time link
  `https://admin.proqnozai.com/activate#<token>`, valid 72 hours. It is shown once; **New link** revokes the old one.
  The token stays in the URL fragment (never sent to a server or logged) and only its SHA-256 is stored.
- On the link the colleague sets a password (12+ characters) and adds the 2FA key to an authenticator app; the account
  becomes active after a valid code. Five wrong codes burn the link.
- Role **promoter** (promotion manager): analytics aggregates (overview, analytics, value, product) and only the
  referral links they created — create, archive, stats, label summary. Every other API route is refused by the request
  guard (default deny); no personal data.
5. **First owner**, interactive in your own SSH session:
   ```sh
   ssh -t root@164.90.180.208 'docker exec -it $(docker ps -q -f name=proqnozai-admin | head -1) admin-entrypoint create-owner <your e-mail>'
   ```
   The password is asked twice (at least 14 characters). The 2FA secret and the `otpauth://` link are shown once. Add them to an authenticator app straight away and never paste them into a chat.
6. Open `https://admin.proqnozai.com` and sign in through Access, then with the e-mail, password and 6-digit code.

### Recovery from the server shell

- A colleague lost the password or the 2FA phone: the owner presses «Сбросить вход» in Settings → Administrators and
  sends the new one-time link.
- The owner (or the only owner) lost the password or the 2FA phone, interactive like `create-owner` (new password asked
  twice, the new 2FA secret shown once, every session of the account ends):
  ```sh
  ssh -t root@164.90.180.208 'docker exec -it $(docker ps -q -f name=proqnozai-admin | head -1) admin-entrypoint reset-credentials <e-mail>'
  ```
- An account locked after wrong 2FA codes (15 min, doubling for each further lock within 24 h, up to 24 h):
  ```sh
  ssh root@164.90.180.208 'docker exec $(docker ps -q -f name=proqnozai-admin | head -1) admin-entrypoint unlock <e-mail>'
  ```

## Bot snapshot (value and top users)

Install it once on the host, after the admin is deployed. It must be reinstalled when the scripts change.

```sh
git show <commit>:deploy/admin/bot-snapshot.sh | ssh root@164.90.180.208 'install -m 700 /dev/stdin /root/bot-snapshot.sh'
git show <commit>:deploy/admin/bot_snapshot.py | ssh root@164.90.180.208 'install -m 600 /dev/stdin /root/bot_snapshot.py'
ssh root@164.90.180.208 '/root/bot-snapshot.sh && (crontab -l 2>/dev/null | grep -v bot-snapshot; echo "17 * * * * /usr/bin/flock -n /run/bot-snapshot.lock /root/bot-snapshot.sh >/dev/null 2>&1") | crontab -'
```

The snapshot reads the bot database read-only, inside the bot container, and writes `bot-snapshot.json` (0600, uid 10001) to the admin's volume. While the bot is stopped, the last snapshot stays.

## Checks

- Without Access: 302 to `patient-mode-acdb.cloudflareaccess.com`.
- `curl -H 'Host: admin.proqnozai.com' http://164.90.180.208/api/admin/health` should give **403** (no edge secret).
- From inside the server, with the secret: `/api/admin/health` should give 200.
- Sign-in requires both factors. The session cookie is `__Host-` and Secure.

## Rollback

- Turn the admin off: Dokploy → `proqnozai-admin` → Stop. The tunnel route can stay, since the origin then answers 404.
- Remove it entirely: delete the application and the `proqnozai-admin-state` volume (this deletes the admin accounts), the tunnel route, the Transform Rule and `/root/admin-secrets/edge`.
- The Mini App is unaffected: its volume is only mounted, never changed by the deploy.

## Known limits

- D1 collection (sign-in country from CF-IPCountry, Telegram platform, active hours) shares the Mini App's telemetry switch: it runs only with `MINIAPP_ADS_ENABLED=true`, which `deploy/miniapp/dokploy_deploy.py` turns on together with the admin's policy key. No IP and no device data are stored. The country and platform expire with the user's activity after 400 days.

- TOTP secrets are stored in plain form in `admin.db`. They are protected by the separate volume (0700, never mounted into the Mini App) and by the server's own access. Encrypting them at rest would need a key kept outside that volume; this is deferred as a documented P2.
- There is no access log (`access_log=False`): URLs with their queries never reach the container log.

The prod Mini App runs the `feat/odds-catalog` line. Admin features that need the Mini App side of this branch are therefore not effective until that is merged. These are telemetry, user blocking through `access-policy.json`, ads, and bot sending through the outbox. The panel shows them as not collected or unavailable and never as zero.
