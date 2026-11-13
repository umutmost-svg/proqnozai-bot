# Инфраструктура production: закрытие панели, бэкапы, мониторинг, учения — runbook

Подготовлено 2026-11-12 (ветка `feat/infra-hardening`). **На сервере ничего не выполнено**: каждый шаг с
пометкой **[разрешение]** — только по отдельному разрешению владельца; **[владелец]** — делает владелец сам.
Сервер — `proqnozai-prod` 164.90.180.208 (Docker 29.6.1 Swarm, Dokploy v0.29.12, Traefik v3.6.7). Предыстория
и решение по доступу к панели — `docs/ops/dokploy-admin-access.md`; этот документ — исполняемые шаги.

## 0. Факты (проверено 2026-11-12 вечером, только чтение)

| Что | Факт |
|---|---|
| Туннель | контейнер `cloudflared` (`2026.10.5`, host network, `unless-stopped`) работает: `/ready` → 4 соединения с Cloudflare; метрики на `127.0.0.1:20241` |
| `admin.proqnozai.com` | без входа → 302 на `…cloudflareaccess.com` (Access); `app.proqnozai.com` → 200 |
| **:3000 снаружи** | **открыт**: `http://164.90.180.208:3000/` → 200 (с внешней машины) |
| **Обход через :80** | **открыт**: `Host: dokploy.docker.localhost` на `http://164.90.180.208/` → 200. `dokploy.yml` не менялся, SHA-256 `2baa7fdd…84a20` |
| Как опубликован :3000 | сервис `dokploy`, PublishMode `host`: `nat/DOCKER` DNAT `! -i docker_gwbridge --dport 3000 → 172.18.0.3:3000`, плюс `docker-proxy` на `0.0.0.0:3000`/`[::]:3000` |
| Firewall | UFW: deny incoming, разрешены 22/80/443 — но DNAT-трафик идёт через FORWARD, UFW (INPUT) его не видит. `DOCKER-USER` (v4 и v6) **пуст**, стоит первым в FORWARD. Backend Docker — iptables (nf_tables) |
| Интерфейсы | `eth0` 164.90.180.208 + anchor 10.19.0.5; `eth1` 10.114.0.2 (VPC); глобального IPv6 нет |
| Мониторинг/бэкапы | crontab root пуст; `/root/.hc-*` нет; автоматических бэкапов нет. В томе бота лежат **4 незашифрованные копии** `bot-backup-*.db` (ручные, 11–12 ноября) |
| Ресурсы | диск `/` 58 ГБ, занято 21 %; RAM 3,9 ГБ, доступно ~2,2 ГБ; swap нет |

Общий шаг для разделов 1, 2, 4 — **[разрешение] скрипты на сервер** (с машины с репозиторием, из корня, на
коммите этой ветки):

```sh
ssh root@164.90.180.208 'install -d -m 700 /opt/proqnozai-ops'
F="deploy/ops/docker-user-3000.sh deploy/ops/proqnozai-docker-user.service deploy/ops/dokploy-route-lockdown.sh
   deploy/ops/dokploy-bypass-check.sh deploy/ops/host-monitor.sh deploy/ops/proqnozai-ops.cron deploy/ops/backup-export.sh
   deploy/miniapp/backup.py deploy/migration/bot_db_backup.py"
scp $F root@164.90.180.208:/opt/proqnozai-ops/
ssh root@164.90.180.208 'cd /opt/proqnozai-ops && chown root:root * && chmod 600 * && chmod 700 *.sh && sha256sum * | sort -k2'
shasum -a 256 $F | sed 's# [^ ]*/# #' | sort -k2      # суммы обязаны совпасть построчно
```

## 1. Закрыть :3000 снаружи

**Гейт (всё до firewall):** `dokploy-admin-access.md` шаг 0 (webhook/Autodeploy: ничто не ходит на `:3000`
снаружи; применённые DO-firewall записаны) и шаг 4 (Access пропускает только разрешённый e-mail, вход в Dokploy
через `admin.proqnozai.com` работает, WebSocket логов идёт, 2FA Dokploy включена). Резервный путь проверен:
`ssh -L 3000:127.0.0.1:3000 root@164.90.180.208` → `http://localhost:3000` открывается.

Два независимых слоя: правило на хосте (переживает ошибку в облачном firewall и действует для VPC) и
DigitalOcean Cloud Firewall (режет пакеты ещё до сервера, включая окно при загрузке, пока правило хоста не
поставлено).

**1a. [разрешение] Правило хоста `DOCKER-USER`** (`deploy/ops/docker-user-3000.sh`). Почему так: Docker
публикует 3000 через DNAT, такие пакеты идут через FORWARD, а UFW живёт в INPUT; `DOCKER-USER` — цепочка, которую
Docker проверяет первой и сам не трогает. Совпадение — по **исходному** порту назначения (conntrack), поэтому
правило не зависит от IP контейнера Dokploy; входящие только с `eth0` и `eth1`. Loopback `127.0.0.1:3000`
(cloudflared, SSH-туннель) не форвардится — не затрагивается. Проверено локально в изолированных netns: DNAT на
:3000 с «внешнего» интерфейса → сброшен, другой исходный порт к тому же бэкенду → 200, после `remove` → 200.

```sh
sh /opt/proqnozai-ops/docker-user-3000.sh apply      # added … ×4 (iptables/ip6tables × eth0/eth1)
sh /opt/proqnozai-ops/docker-user-3000.sh status     # ok ×4, код 0
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/    # 200 — путь туннеля жив
# постоянство (правила iptables не переживают перезагрузку и рестарт Docker):
install -m 644 /opt/proqnozai-ops/proqnozai-docker-user.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now proqnozai-docker-user.service && systemctl is-active proqnozai-docker-user.service
```

Проверка: **с внешней машины** (ноутбук владельца) `sh deploy/miniapp/origin-check.sh --ports 164.90.180.208 none 3000`
→ `RESULT: isolated`; `https://admin.proqnozai.com` → Access → Dokploy работает; счётчик
`iptables -L DOCKER-USER -v -n` растёт при внешних попытках; бот/дашборд/`/r/` — как до (их порты не задеты).
Откат (мгновенный): `systemctl disable --now proqnozai-docker-user.service; rm /etc/systemd/system/proqnozai-docker-user.service; systemctl daemon-reload; sh /opt/proqnozai-ops/docker-user-3000.sh remove`.
Пока файл юнита установлен, `host-monitor.sh` (раздел 4) каждые 5 минут требует: юнит включён **и** DROP-правила
стоят **первыми** в `DOCKER-USER` (правило, вставленное выше, например ACCEPT, их обошло бы) — иначе тревога `host`.
Повторный `apply` возвращает их наверх без незащищённого момента (сначала новые копии сверху, потом удаление нижних). Удаление файла юнита (только при откате) снимает это требование.

**1b. [владелец, отдельное разрешение] DigitalOcean Cloud Firewall** `proqnozai-prod-fw` — таблица правил,
порядок, гейт «ни один уже применённый firewall не разрешает 3000» и проверки — `dokploy-admin-access.md`
шаги 5–6 (без изменений). Откат: Droplet → Networking → Firewalls → убрать дроплет (действует сразу).

Уже установленные до 1a/1b TCP-соединения к :3000 conntrack может продолжать пропускать — для гарантии
перезапуск задачи Dokploy (перерыв панели ~1 мин, бот не затронут) — отдельное решение.

## 2. Закрыть обход через :80 (маршрут Traefik на панель по заголовку Host)

Подход прежний (`dokploy-admin-access.md` шаг 2a): в правило роутера `dokploy-router-app` добавить
`ClientIP(\`127.0.0.1/32\`)` — панель по этому маршруту отвечает только запросу изнутри контейнера Traefik.
Исполняет `deploy/ops/dokploy-route-lockdown.sh`:
- правит **только** файл с проверенным SHA-256 (`2baa7fdd…84a20`, сгенерирован Dokploy 2026-07-17); любой
  другой → `STOP`, ничего не меняется; результат обязан иметь SHA-256 `d1d1132a…05989`;
- копия → `/root/dokploy.yml.bak-<UTC>`; новый файл пишется рядом и **переименовывается** на место (Traefik не
  читает полфайла, в наблюдаемый каталог не попадает ничего временного);
- проверка до 30 с: изнутри Traefik `200`, с публичного адреса `404`, маршрут Mini App жив (`/api/health` ok —
  file provider не сломан); **не прошла → автоматический откат** на копию и код 1;
- успех → одобренный хеш в `/root/.dokploy-route.sha256` для сторожа.

Проверено локально на `traefik:v3.6.7` с байт-в-байт копией production-файла: исходный → снаружи 200; после
`apply` — loopback 200 / снаружи 404 / Mini App ok; повторный `apply` — только проверка; при упавшем бэкенде
Mini App — автооткат к исходному SHA; изменённый файл → STOP; `rollback` → снова 200.

```sh
# [разрешение]
sh /opt/proqnozai-ops/dokploy-route-lockdown.sh apply
# ожидается: diff в одну строку, "VERIFIED: loopback-in-traefik=200 public=404 miniapp-route=ok", "DONE. Rollback: …"
```
Проверка снаружи (ноутбук): `curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: dokploy.docker.localhost' http://164.90.180.208/` → **404**;
`https://…httpworker…sslip.io/` → 401 как прежде; `https://app.proqnozai.com/api/health` → ok.
Откат: `sh /opt/proqnozai-ops/dokploy-route-lockdown.sh rollback /root/dokploy.yml.bak-<UTC>` (панель снова на :80;
сторож будет сигналить — так и должно быть).
Сторож `dokploy-bypass-check.sh` (почасово, раздел 4) ловит перезапись файла Dokploy (настройки Web Server,
обновление), чужие маршруты на бэкенд панели и метки Traefik на сервисе `dokploy`. После **каждого обновления
Dokploy** — ручной прогон `sh /opt/proqnozai-ops/dokploy-route-lockdown.sh verify`.

## 3. Автоматические внешние бэкапы (бот + Mini App), без новых платных сервисов

**Схема — «тянет Mac владельца»** (сервер не хранит ключей шифрования и никуда не ходит сам):

```
launchd на Mac (05:40 и 17:40 местного) ── ssh, отдельный ключ с forced command ──> backup-export.sh (root)
   │                                                        ├─ miniapp: backup.py в контейнере (SQLite backup API, integrity_check)
   │                                                        └─ bot:     bot_db_backup.py в контейнере бота (онлайн, без остановки)
   │   <── tar-поток по SSH; копия на сервере сразу удаляется (и при сбое; «осиротевшая» — ежечасным `sweep` из cron)
   ├─ openssl enc AES-256-CBC + PBKDF2 (300 000) «на лету», пароль из Keychain по pipe — открытый текст не пишется на диск
   ├─ проверка архива: расшифровка во временный 0700-каталог → SHA-256 потока, состав tar, integrity_check,
   │  обязательные таблицы (бот: users; Mini App: users, sessions / ai_forecasts) → каталог удаляется
   ├─ ~/.proqnozai-backups/auto/{bot,miniapp}/<kind>-<UTC>.tar.enc + .manifest.json (хеши, число строк), 0600
   ├─ зеркало (опц.): та же папка шифротекста, например в iCloud Drive (нужна метка .proqnozai-backup-mirror)
   ├─ хранение: 30 дней по метке времени в имени, но 14 последних — всегда (долгий простой Mac не стирает историю)
   └─ healthchecks.io: пинг «bot-backup» / «miniapp-backup» при успехе, /fail с причиной при сбое
```

RPO — 12 ч (две выгрузки в сутки). Если Mac выключен/спит дольше grace — healthchecks пришлёт тревогу (это и
есть сигнал «внешних копий нет»). Пропущенный во сне запуск launchd выполняет при пробуждении.
Размер: бот ~5 МБ, Mini App — сотни КБ; диск сервера не растёт (копия живёт секунды). Нагрузка на бота: та же
онлайн-копия, что уже делалась вручную (шаги по 256 страниц с паузами, максимум 300 с).

Проверено локально (2026-11-12): `backup-export.sh` в отдельном «сервере» с Docker против настоящего образа
Mini App `b92e863` и контейнера-«бота` → `pull_backups.py pull all` → архивы 0600, расшифровки на диске не
осталось; учения (раздел 5) — PASS для обоих, включая запуск контейнера Mini App на восстановленных данных;
испорченный манифест → FAIL; «осиротевшая» копия прерванного запуска (в т. ч. без напечатанного имени)
убрана следующим запуском; ручные копии, существовавшие до запуска, и сама `bot.db` не трогаются. Найдено и исправлено при этом: копии Mini App —
WAL-базы, рядом остаются пустой `-wal` и `-shm`; выгружаются только два `.db` после проверки, что `-wal` пуст.
Логика Mac-части покрыта офлайн-тестами `deploy/ops/tests/test_pull_backups.py` (12 тестов).

**Владелец предоставляет / делает [владелец]** (на Mac, в своём терминале):
1. Пароль архивов: **сначала** сгенерировать в менеджере паролей (≥ 40 случайных символов) и сохранить там —
   это копия вне Mac, без неё потеря Mac = потеря всех копий. Затем положить в Keychain через **запрос** (пароль
   не попадает в аргументы процессов и историю оболочки; `-w` последним — `security` спросит его дважды):
   ```sh
   security add-generic-password -s proqnozai-auto-backup -a proqnozai -T /usr/bin/security -w
   security find-generic-password -s proqnozai-auto-backup -a proqnozai >/dev/null && echo present   # значение не печатается
   ```
2. Ключ SSH только для бэкапа (без пароля — его использует launchd; ключ умеет только выгрузку, см. п. 3):
   `ssh-keygen -t ed25519 -f ~/.ssh/proqnozai_backup_ed25519 -N '' -C proqnozai-backup-pull` и прислать
   **публичную** часть (`cat ~/.ssh/proqnozai_backup_ed25519.pub`).
3. **[разрешение]** строка в `/root/.ssh/authorized_keys` на сервере (ключ может запускать только выгрузку: без
   shell, tty, проброса портов и агента):
   `restrict,command="/opt/proqnozai-ops/backup-export.sh" ssh-ed25519 AAAA… proqnozai-backup-pull`
   (при постоянном IP владельца — ещё `from="<IP>"` в начало). Проверка с Mac:
   `ssh -i ~/.ssh/proqnozai_backup_ed25519 -o IdentitiesOnly=yes root@164.90.180.208 check` → `ready`;
   `… root@164.90.180.208 'id; ls /'` → `export: FAIL: unknown request` (forced command работает).
4. healthchecks.io (бесплатный аккаунт, раздел 4): проверки `bot-backup` и `miniapp-backup`; их URL — в файлы
   `~/.config/proqnozai-backup/hc-bot-backup` и `…/hc-miniapp-backup` (каталог 0700, файлы 0600):
   `mkdir -p -m 700 ~/.config/proqnozai-backup; (umask 077; pbpaste > ~/.config/proqnozai-backup/hc-bot-backup)`.
5. (опционально) зеркало шифротекста — папка в iCloud Drive (уже есть у владельца, без доплаты при свободном
   месте): `mkdir -p ~/Library/Mobile\ Documents/com~apple~CloudDocs/proqnozai-backups && touch …/.proqnozai-backup-mirror`
   и строка `MIRROR_DIR=…` в конфиге. Не включено — копии только на Mac (тогда это единственное место вне сервера).

**Установка на Mac:**
```sh
install -d -m 700 ~/proqnozai-ops
install -m 600 deploy/ops/pull_backups.py ~/proqnozai-ops/      # копия, не ссылка в рабочую ветку репозитория
install -m 700 deploy/ops/restore-drill-local.sh ~/proqnozai-ops/
install -d -m 700 ~/proqnozai-ops/miniapp && install -m 600 deploy/miniapp/backup.py deploy/miniapp/restore-drill.sh ~/proqnozai-ops/miniapp/
# конфиг (необязателен; значения по умолчанию — в DEFAULTS pull_backups.py):
printf 'KEEP_DAYS=30\nMIN_KEEP=14\n' > ~/.config/proqnozai-backup/config.env && chmod 600 ~/.config/proqnozai-backup/config.env
# первый ручной прогон — оба вида, потом проверка
/opt/homebrew/bin/python3 ~/proqnozai-ops/pull_backups.py pull all          # "bot OK: …", "miniapp OK: …"
/opt/homebrew/bin/python3 ~/proqnozai-ops/pull_backups.py verify "$(ls ~/.proqnozai-backups/auto/bot/*.tar.enc | tail -1)"   # PASS
# расписание
sed "s/OWNER/$(id -un)/g" deploy/ops/com.proqnozai.backup-pull.plist > ~/Library/LaunchAgents/com.proqnozai.backup-pull.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.proqnozai.backup-pull.plist
launchctl kickstart gui/$(id -u)/com.proqnozai.backup-pull && sleep 60 && tail ~/Library/Logs/proqnozai-backup.log
```
При первом обращении launchd к Keychain macOS может спросить разрешение — «Always Allow» (выдаётся
`/usr/bin/security`, благодаря `-T` при создании записи вопроса не будет).
Проверка готовности: в логе `OK` для обоих, в healthchecks обе проверки up, на сервере
`docker exec <miniapp> ls /data/backups` пусто, в томе бота нет новых `bot-backup-*`, `ls /var/lib/proqnozai-ops/` пуст (нет `inflight-*`).
Тест тревоги: `chmod 000 ~/.ssh/proqnozai_backup_ed25519; python3 ~/proqnozai-ops/pull_backups.py pull bot; chmod 600 ~/.ssh/proqnozai_backup_ed25519`
→ healthchecks прислал «bot backup failed: server export failed…».
Откат: `launchctl bootout gui/$(id -u)/com.proqnozai.backup-pull`; удалить строку ключа из `authorized_keys` на сервере.

**Ручные копии на сервере** — только `sh /opt/proqnozai-ops/backup-export.sh manual-bot` (или `manual-miniapp`),
root на сервере; через ключ выгрузки не работает. Под замком выгрузки он сначала доводит до конца уборку
прерванных выгрузок (иначе их запись могла бы принять новую ручную копию за свою и удалить), затем делает копию
и **оставляет** её (путь — в выводе); скопировать с сервера и удалить вручную. Запуск `bot_db_backup.py`/`backup.py`
другим способом — нельзя.

**[разрешение] Уборка старых копий в томе бота** — ровно четырёх файлов, найденных 2026-11-12 (сначала
`docker exec <бот> ls -l /app/data` и сверка списка): `docker exec <бот> rm -f /app/data/bot-backup-20261111T214312Z.db
/app/data/bot-backup-20261111T221942Z.db /app/data/bot-backup-20261112T150519Z.db /app/data/bot-backup-20261112T150531Z.db`
— только после первой успешной автоматической выгрузки и проверки (`verify` PASS). Это 4 незашифрованные копии
с данными пользователей; зашифрованные экземпляры трёх из них уже лежат в `~/.proqnozai-backups/`.

Связь с `release-same-server.md` шаг B8: эта схема закрывает блокер «внешний приёмник бэкапов Mini App» без
B2/Spaces; серверный `backup-job.sh` с `OFFSITE` не нужен.

## 4. Мониторинг и тревоги (healthchecks.io, бесплатный тариф)

**[владелец]** создаёт проверки (Period/Grace), уведомления — e-mail и/или Telegram-интеграция healthchecks;
URL пинга каждой проверки (`https://hc-ping.com/<uuid>`) кладёт в файл 0600 (не в чат и не в репозиторий):

| Проверка | Period | Grace | Кто пингует | Файл с URL |
|---|---|---|---|---|
| `dokploy-bypass` | 1 ч | 1 ч | сервер, cron :17 каждый час | `/root/.hc-dokploy-bypass` |
| `host` | 5 мин | 15 мин | сервер, cron каждые 5 мин | `/root/.hc-host` |
| `miniapp-health` | 5 мин | 15 мин | сервер, cron каждые 5 мин | `/root/.hc-miniapp-health` |
| `bot-backup` | 12 ч | 12 ч | Mac, launchd 2×/сутки | `~/.config/proqnozai-backup/hc-bot-backup` |
| `miniapp-backup` | 12 ч | 12 ч | Mac, launchd 2×/сутки | `~/.config/proqnozai-backup/hc-miniapp-backup` |

Файл на сервере владелец создаёт сам, не показывая URL: `read -rs U; (umask 077; printf '%s' "$U" > /root/.hc-host); unset U`.

`host-monitor.sh` (`host`): диск `/` ≤ 85 %, `MemAvailable` ≥ 400 МБ (swap нет — это порог до OOM), контейнер
`cloudflared` запущен и `/ready` показывает ≥ 1 соединение, все сервисы Swarm `n/n` и ни один из ожидаемых
(`dokploy`, `dokploy-postgres`, бот, дашборд, Mini App — `EXPECT_SERVICES`) не пропал, а после 1a — юнит включён
и DROP-правила первые в `DOCKER-USER`; нет записи `inflight-*` в `/var/lib/proqnozai-ops` старше 90 мин (копию
прерванной выгрузки не смог убрать ежечасный `backup-export.sh sweep` из того же cron-файла). `miniapp-health`: `GET /api/health` через Traefik на loopback (`Host: app.proqnozai.com`).
При сбое — `/fail` с причинами в теле (видны в healthchecks) и запись в syslog (`proqnozai-monitor`); пороги —
переменные `DISK_MAX_PCT`, `MEM_MIN_MB`. Сервер/сеть/cron умерли → пингов нет → тревога по grace.
Снаружи через Cloudflare (DNS, Access, туннель целиком) это не видит — по желанию владельца бесплатный внешний
HTTP-монитор на `https://app.proqnozai.com/api/health` (например UptimeRobot free); `/ready` cloudflared
покрывает основной случай — обрыв туннеля.

**[разрешение] Установка** (после раздела 2 — иначе `dokploy-bypass` сразу сигналит об отсутствии одобренного хеша):
```sh
install -m 644 /opt/proqnozai-ops/proqnozai-ops.cron /etc/cron.d/proqnozai-ops
sh /opt/proqnozai-ops/dokploy-bypass-check.sh; echo $?       # 0, без вывода
sh /opt/proqnozai-ops/host-monitor.sh; echo $?                # без вывода
```
Проверка: в healthchecks все три серверные проверки up в течение 5–60 минут. **Тест тревоги (обязательно):**
`printf 'url = "%s/fail"\n' "$(cat /root/.hc-host)" | curl -fsS -K -` → уведомление пришло →
`printf 'url = "%s"\n' "$(cat /root/.hc-host)" | curl -fsS -K -` → снова up (URL через stdin, не в аргументах `curl`);
то же для двух других файлов. Откат: `rm /etc/cron.d/proqnozai-ops` (и пауза проверок в healthchecks).
Лог `/var/log/proqnozai-monitor.log` пишется только при проблемах.

## 5. Учения по восстановлению

**Ежемесячно и после любых изменений схемы БД** — на Mac (сервер не затрагивается, сеть контейнерам не дана):
```sh
sh ~/proqnozai-ops/restore-drill-local.sh bot          # PASS: … bot.db integrity ok, row counts equal to the manifest; cleaned up
MINIAPP_IMAGE=proqnozai-miniapp:<коммит в проде> sh ~/proqnozai-ops/restore-drill-local.sh miniapp
                                                       # PASS: … Mini App container healthy on the restored data; cleaned up
```
Расшифрованные данные живут только во временных каталогах под `~/.proqnozai-backups/auto/.work/`; при
SIGTERM/SIGHUP они удаляются сразу, после SIGKILL/сбоя питания — следующим `pull`/`verify` (старше 6 ч; рабочий
каталог выгрузки своего вида — в начале каждой выгрузки, ещё до обращения к Keychain). Тома и контейнеры учений,
оставшиеся после SIGKILL (метка `proqnozai.drill`; живыми считаются только учения, чей PID из записи — работающий
`restore-drill-local.sh`), удаляет каждая плановая
выгрузка (`restore-drill-local.sh sweep`, 2×/сутки; не удалось — проверки `bot-backup` и `miniapp-backup` уходят в
`/fail` с причиной) и следующий запуск учений. Каждые учения с начала и до проверенной уборки оставляют запись
`~/.proqnozai-backups/auto/.drill-pending/<токен>`: если учения убиты, а Docker не запущен, выгрузки продолжают
уходить в `/fail`, пока Docker не поднимется и остатки не будут удалены (без записи Docker не нужен — пропуск).

Что делает: свежий архив → расшифровка и проверки (`pull_backups.py extract`) во временный 0700-каталог → **новый**
том Docker с меткой этого запуска → копирование баз в него офлайн-контейнером → `integrity_check`, таблицы и
число строк, прочитанные **из тома**, сравниваются с манифестом → для Mini App при `MINIAPP_IMAGE` — сам
контейнер приложения на этом томе (`deploy/miniapp/restore-drill.sh`: `--network none`, без секретов) должен стать
healthy → удаление каталога, тома и контейнеров (и при сбое; ошибка уборки = FAIL). Последняя строка — `PASS: …`
(код 0) или `FAIL: …` (код 1). Образ Mini App — тот, что собирался для релиза (`docker images proqnozai-miniapp`);
amd64 на Apple Silicon запускается через эмуляцию (медленнее, работает — проверено).
Проверка отдельного архива: `sh ~/proqnozai-ops/restore-drill-local.sh bot ~/.proqnozai-backups/auto/bot/bot-<UTC>.tar.enc`.

Настоящее восстановление в production (решение владельца, с потерей записанного после копии) — по
`droplet-resize.md` «Откат → Данные бота» и `production.md` §7 «Восстановление из бэкапа»; исходный файл —
`pull_backups.py extract <архив> ~/.proqnozai-backups/auto/.work/restore-<дата>` → `x/…` (каталог обязан быть внутри
`.work`: только там остатки убитого запуска убираются автоматически — через 6 ч), затем `scp` на сервер (0600) и
удаление каталога.

## 6. Что нужно от владельца — сводка

| Действие | Кто | Разрешение |
|---|---|---|
| Скрипты в `/opt/proqnozai-ops/` | я | да (запись на сервер) |
| 1a `DOCKER-USER` + systemd-сервис | я | да, после гейта раздела 1 |
| 1b DigitalOcean Cloud Firewall | владелец | да |
| 2 правка `dokploy.yml` | я | да |
| 3 пароль в Keychain + его копия вне Mac; ключ SSH; (опц.) папка iCloud | владелец | — |
| 3 строка ключа в `authorized_keys` | я или владелец | да |
| 3 уборка 4 старых копий в томе бота | я | да |
| 4 аккаунт healthchecks.io, 5 проверок, URL в файлы | владелец | — |
| 4 cron на сервере | я | да |
| 5 учения на Mac | владелец или я на Mac | — |

## 7. Риски и границы

- Бэкапы зависят от Mac владельца: выключен/в поездке дольше ~24 ч — копий нет (тревога придёт). Зеркало в
  iCloud защищает от потери Mac, но не от потери пароля — копия пароля вне Mac обязательна.
- Ключ выгрузки без пароля: у того, кто получил Mac, будут и выгрузки, и сами архивы с паролем в Keychain — тот же
  уровень, что у уже хранящихся там копий. Сервер ключ не открывает (forced command, `restrict`).
- `aes-256-cbc` без встроенной аутентификации: целостность подтверждают SHA-256 в манифесте и проверка
  расшифровки сразу после выгрузки; формат совпадает с уже существующими ручными копиями.
- Правило `DOCKER-USER` ставится после старта Docker (секунды без него при загрузке) — это окно закрывает
  облачный firewall (1b). Обновление Dokploy может переписать `dokploy.yml` — сторож сигналит, затем 2 заново.
- `dokploy-bypass-check` и `host-monitor` ловят **случайные** изменения; администратор с root может обойти любое
  из них — защита от этого — Access, 2FA Dokploy, SSH только по ключу.
- Traefik `api.insecure: true` (дашборд Traefik на внутреннем entrypoint :8080) — порт не опубликован, снаружи
  недоступен; отмечено, не менялось.
- Ручная копия (`backup-export.sh manual-bot|manual-miniapp`), убитая SIGKILL или обрывом SSH на середине, может
  оставить частичную незашифрованную копию в томе, которую сторож не числит своей (Codex P2, принято): команду
  запускает человек и видит результат; при сбое — `ls` тома и удаление вручную (`backup.py` при обычной ошибке и
  SIGTERM убирает копию сам).
