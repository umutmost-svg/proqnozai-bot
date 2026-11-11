# SporBot Mini App — первый production-релиз (Dokploy)

Подготовлено 2026-11-11. Ничего из этого ещё не выполнено на сервере: каждый шаг с пометкой
**[владелец]** или **[сервер]** — только по отдельному разрешению владельца.

## 0. Что выпускаем

| | |
|---|---|
| Код релиза | `b6cf031` (`rc/stand-2026-11-11`), проверен на стенде 2026-11-11 |
| Ветка выкладки | `release/miniapp-prod` = `b6cf031` + этот документ и скрипты `deploy/miniapp/{prod-discovery.sh,smoke.py,backup.py,ai_matchday_check.py}`. Код приложения, Dockerfile и entrypoint идентичны `b6cf031` (`git diff b6cf031 release/miniapp-prod -- miniapp frontend deploy/miniapp/Dockerfile deploy/miniapp/entrypoint.sh` пуст) |
| Что не выпускаем | `main` (бот), админку (`feat/admin-pilot`), экспорт коэффициентов бота в Mini App |

**Почему не через `main`.** Приложения Dokploy `bot` и `dashboard` собираются из `main` с
Autodeploy On Push. RC содержит и изменения бота (`main.py` + `mostbet_feed.py`: экспорт
коэффициентов, выключен без переменных окружения). Merge в `main` перезапустил бы действующего бота.
Mini App выпускается отдельным приложением Dokploy из `release/miniapp-prod`; бот не
пересобирается и не перезапускается. Merge в `main` — отдельным решением после релиза.

**Как пользователи попадут в Mini App.** Действующий бот сейчас не ссылается на Mini App (в коде
`main` нет `web_app`/`MenuButton`). Вход появится, когда владелец в BotFather задаст production-боту
Menu Button / Main Mini App с URL Mini App. Это меняет только настройку бота в Telegram, без
перезапуска и без изменения кода; откат — вернуть прежнюю кнопку.

## 1. Инфраструктура: что известно и что надо подтвердить

Известно со слов владельца (2026-11-07/08), **не проверено чтением сервера**: DigitalOcean Droplet +
Dokploy; приложения `bot` и `dashboard` из GitHub `umutmost-svg/proqnozai-bot`, ветка `main`,
Autodeploy On Push, Preview OFF; Railway удалён. У исполнителя нет доступа к серверу и Dokploy (нет
SSH-ключа сервера, токена Dokploy), поэтому фактическая картина собирается скриптом:

**[владелец]** на сервере: `sh deploy/miniapp/prod-discovery.sh > discovery.txt` — только чтение,
без значений переменных и секретов; выводит сервисы, образы, тома, порядок обновления Swarm, хосты
Traefik, расписания бэкапов и счётчики 429 football-data в логах бота за 7 дней. Результат нужен для
пунктов 2 (свободное место, имя сети Traefik, существующие тома/хосты) и 4 (реальная нагрузка бота).

## 2. Конфигурация Dokploy

### Приложение `sporbot-miniapp` (Application)

| Настройка | Значение | Почему |
|---|---|---|
| Source | GitHub `umutmost-svg/proqnozai-bot`, branch `release/miniapp-prod` | не `main`: бот не трогается |
| **Autodeploy** | **OFF** | выкладка только кнопкой Deploy по runbook |
| Build type | Dockerfile, path `deploy/miniapp/Dockerfile`, context `.` | образ проверен локально |
| Build args | `VITE_DATA_MODE=real` | без него сборка падает (по задумке) |
| Replicas | **1** | SQLite — один писатель; лимитер входов в памяти |
| Swarm UpdateConfig | `Parallelism 1`, **`Order stop-first`**, `FailureAction rollback` | два контейнера никогда не пишут в одну БД одновременно |
| Health check | из образа (`HEALTHCHECK` → `GET /api/health`, проверяет чтение БД) | Swarm не считает задачу живой без БД |
| Restart policy | on-failure | |
| Volume | named volume `sporbot-miniapp-data` → `/data` | entrypoint не стартует без смонтированного тома |
| Domain | `<MINIAPP_DOMAIN>` (поддомен домена владельца), container port `8000`, HTTPS (Let's Encrypt или Cloudflare Origin Certificate) | один origin: страница + `/api` |
| Ports | не публиковать на хост (только через Traefik) | нет обхода Cloudflare по IP:порт |

### Cloudflare (домен Mini App)
- DNS `<MINIAPP_DOMAIN>` → IP Droplet, **Proxied**; SSL/TLS **Full (strict)**.
- Transform Rule → Modify Request Header → **Set** (не Add) `X-Edge-Auth` = `<MINIAPP_EDGE_SECRET>` для
  хоста `<MINIAPP_DOMAIN>`: значение, присланное клиентом, всегда перезаписывается.
- Rate limiting: `POST /api/auth/session` — 20/мин по IP; весь `/api/*` — 300/мин по IP (защита SQLite и
  квоты от массовых GET под сессией).
- WAF: для хоста только методы `GET, HEAD, POST`.
- Cache Rule: `/api/crests/*` — Eligible for cache, «Respect origin» (`public, max-age=86400`, флаг-заменитель —
  3600): поток картинок не доходит до приложения.
- Приложение без верного `X-Edge-Auth` отвечает 403 (кроме `GET /api/health`), лог `event=edge_bypass_blocked`.

### Будущая админка (не в этом релизе)
Отдельное приложение Dokploy из той же ветки/репозитория, свой Dockerfile (будет добавлен с D2), тот же
volume `sporbot-miniapp-data` **только на чтение** (`/data:ro`) плюс свой volume для `admin.db`, без
публичного домена (доступ по SSH-туннелю/Cloudflare Access). Архитектура A2 этому соответствует:
админка читает `miniapp.db` и историю прогнозов read-only и держит свои данные отдельно.

### Переменные окружения (Dokploy → Environment; секреты только там)

| Переменная | Значение | Новый секрет? |
|---|---|---|
| `MINIAPP_ENV` | `production` | |
| `MINIAPP_BOT_TOKEN` | токен **production-бота** (тот, из которого открывается Mini App; нужен для проверки подписи initData) | значение уже есть у бота; в новое приложение — **[владелец]**. Токен оказывается во втором, публичном контейнере: при компрометации — ротация в BotFather (`/revoke`), затем новое значение в оба приложения Dokploy (бот перезапустится — это плановый короткий простой) |
| `MINIAPP_DB_PATH` | `/data/miniapp.db` | |
| `MINIAPP_TRUSTED_ORIGINS` | `https://<MINIAPP_DOMAIN>` | |
| `MINIAPP_EDGE_SECRET` | случайная строка ≥ 32 символов, та же в Transform Rule | да |
| `MINIAPP_CSP_MODE` | `enforce` (как на стенде) | |
| `MINIAPP_COOKIE_SAMESITE` | `none` (Telegram Web открывает Mini App во фрейме; как на стенде) | |
| `MINIAPP_FOOTBALL_DATA_KEY` | см. раздел 3 | да |
| `MINIAPP_FOOTBALL_DATA_LIVE_PLAN` | `true` | |
| `MINIAPP_FOOTBALL_DATA_PER_MINUTE` | `100` при отдельном ключе; `30` в режиме общего ключа (раздел 3) | |
| `MINIAPP_FOOTBALL_DATA_PER_DAY` | `10000` | |
| `MINIAPP_FOOTBALL_DATA_COMPETITIONS` | `all` | |
| `MINIAPP_ANTHROPIC_API_KEY` | ключ Anthropic **только для Mini App** (не `ANTHROPIC_API_KEY` бота) | да |
| `MINIAPP_AI_PER_USER_DAILY` / `MINIAPP_AI_DAILY_BUDGET` | `10` / `200` (≈ $0,007 за анализ → до ~$1,5 в сутки) | |
| `MINIAPP_ODDS_FEED_ENABLED` | `false` (экспорт коэффициентов бота в этот релиз не входит) | |
| `MINIAPP_LOGINS_ENABLED` | `true` (аварийно — `false`) | |

`MINIAPP_STATIC_DIR` и `PORT` заданы в образе. Значения не коммитятся; в репозитории только
плейсхолдеры (`miniapp/.env.example`).

## 3. Ключ football-data.org

**Приоритет — отдельный ключ Mini App.** У football-data.org один API-ключ на аккаунт; второй ключ —
это второй аккаунт с подпиской (**[владелец]**: купить/завести и проверить условия провайдера). С
отдельным ключом квоты бота и Mini App не пересекаются; `PER_MINUTE=100`.

**Если отдельного ключа нет — общий ключ Pro (120 запросов/мин на ключ): только как осознанно принятый
владельцем риск.** Гарантии «боту всегда останется N» общий ключ не даёт:

- потолок Mini App (`MINIAPP_FOOTBALL_DATA_PER_MINUTE`) считается в памяти процесса и обнуляется при
  перезапуске/выкладке — в минуту рестарта Mini App может отправить до двух своих потолков;
- бот на один «холодный» прогноз делает до 3 запросов, каждый с повторами при 429/5xx (до 3 попыток), а
  после исчерпания повторов прогноз строится без данных football-data — это и есть деградация бота;
- общий счётчик провайдера (`X-Requests-Available-Minute`) Mini App учитывает только при `0`.

| | Запросов в минуту |
|---|---|
| Лимит ключа (Pro) | 120 (заголовок провайдера: `availableThisMinute` 119) |
| Потолок Mini App в режиме общего ключа | **30** (`MINIAPP_FOOTBALL_DATA_PER_MINUTE=30`); в минуту рестарта — до 60 |
| Остаётся боту в худшем случае | ≥ 60 |
| Бот, худший случай на прогноз | 3 запроса × 3 попытки = 9 → 429 у бота возможен при > 6 одновременных «холодных» прогнозах в минуту в минуту рестарта Mini App, иначе при > 10 |
| Фоновый прогрев Mini App при 30/мин | только календари, без прошлых сезонов (`search_pacing` отключает их при малом минутном лимите), шаг 8 с, первый проход ~20 мин; прошлый сезон грузится при первом AI-анализе лиги (параллельно с календарём, 5–8 с) |

Строгую гарантию дал бы только общий лимитер бота и Mini App (изменение кода обоих) — в этот релиз не
входит. **Условия режима общего ключа:** `discovery.txt` показывает 0 строк `football API 429` у бота за
7 дней; алерт на любое `football API 429` в логах бота; при первом же 429 — `MINIAPP_FOOTBALL_DATA_PER_MINUTE=15`
(Redeploy Mini App, бот не затрагивается) и решение об отдельном ключе.

## 4. Три P2 из техдолга — оценка для production

| P2 | Риск для production | Решение |
|---|---|---|
| Общий дневной потолок фоновой работы | При отдельном ключе (100/мин) первый проход ~14 мин, при общем (30/мин) ~20 мин и без прошлых сезонов; часть календарей/сезонов дольше холодные → AI-анализ таких лиг идёт по холодному пути (5–8 с, в пределах 20 с; прошлый сезон грузится параллельно). Корректность не страдает | оставить в техдолге |
| Settlement ждёт проход прогрева | Сверка — внутренняя статистика качества, пользователю не показывается; результаты приходят с задержкой до ~20 мин, не теряются (календари прогрева сверяются через слушателя) | оставить в техдолге; проверка на стенде 12–13.11 |
| Временные сбои хоста эмблем | Косметика: 0–2 эмблемы из 109 не видны до часа; вместо картинки — инициалы | оставить в техдолге |

## 5. Production preflight

| # | Проверка | Как | Статус |
|---|---|---|---|
| 1 | Точный SHA | код `b6cf031`; ветка `release/miniapp-prod` (только docs/скрипты сверху) | готово локально |
| 2 | Образ собирается | `docker build -f deploy/miniapp/Dockerfile --build-arg VITE_DATA_MODE=real .` из `b6cf031` | ✅ локально |
| 3 | Контейнер в production-режиме | health 200; без edge-секрета 403; CSP enforce; HSTS; процесс не root (`setpriv`) | ✅ локально |
| 4 | Smoke в контейнере | `docker exec -i -u miniapp -e SMOKE_USER_ID=… <ctr> python - < smoke.py` | ✅ локально (реальные данные, 117 турниров, 109/109 эмблем) |
| 5 | Совместимость БД | у production нет БД Mini App: новый volume, схема создаётся при старте (`CREATE TABLE IF NOT EXISTS`); `bot.db` бота не используется и не монтируется | ✅ по коду |
| 6 | Бэкап и пробное восстановление | `deploy/miniapp/backup.py` от пользователя `miniapp` (обе БД, SQLite backup API, 0600/0700, integrity_check); копия восстановлена в новый volume, новый контейнер здоров, данные совпали | ✅ локально; на сервере — шаги 7–8 и раздел 7 |
| 7 | Права доступа | секреты только в Dokploy env; порт не публикуется; edge-секрет; volume владелец `miniapp` | на сервере — шаг runbook |
| 8 | Health check | `/api/health` (чтение БД), Swarm healthcheck из образа | ✅ в образе |
| 9 | Внешние API | football-data (ключ, план, остаток минуты), Anthropic (ключ Mini App), Telegram (подпись initData) | на сервере — шаг runbook |
| 10 | Откат | первый релиз: остановить приложение + вернуть кнопку бота; бот не затронут | план ниже |
| 11 | Нагрузка football-data | `discovery.txt`: 429 у бота за 7 дней | **ждёт владельца** |
| 12 | Сверка прогнозов на стенде | первые матчи 12.11; проверка 13.11 ~02:07 (Баку) | **ждёт даты** |
| 13 | AI-рекомендации в день матча | `docs/miniapp/ai-matchday-check.md`, 20–22.11 | **проверка качества, не preflight**: выпуск её не ждёт, если владелец так решит |

## 6. Runbook выкладки

**Все шаги выполняются без перерыва в работе бота**: бот не пересобирается и не перезапускается; шаги 9–10
меняют только кнопку меню бота в Telegram. Условие остановки на любом шаге — отклонение от ожидаемого;
до шага 10 пользователи Mini App не видят.

1. **[владелец] Разрешения и данные:** домен `<MINIAPP_DOMAIN>`, ключи (football-data — отдельный или
   общий режим; Anthropic для Mini App), `discovery.txt`.
2. **[исполнитель, по разрешению] Push ветки:** `git push origin release/miniapp-prod`. Перед этим в Dokploy
   убедиться, что у `bot` и `dashboard` branch = `main` (тогда их Autodeploy не сработает).
3. **[владелец/сервер] Скрипты на сервер:** `scp deploy/miniapp/{smoke.py,backup.py} <server>:/opt/sporbot/`
   (права 0644, владелец root); сверить `sha256sum` с локальными.
4. **[владелец] Cloudflare** по разделу 2. *Стоп:* домен не резолвится через Cloudflare.
5. **[сервер] Dokploy → Create Application** `sporbot-miniapp` по таблице раздела 2 (Autodeploy OFF,
   Replicas 1, stop-first, volume, domain, env). *Стоп:* свободно < 5 ГБ на диске (сборка ~1,5 ГБ с кешем).
6. **[сервер] Deploy**, затем проверить, что Dokploy применил настройки:
   ```sh
   docker service inspect sporbot-miniapp --format '{{.Spec.Mode.Replicated.Replicas}} {{.Spec.UpdateConfig.Order}} {{.Spec.UpdateConfig.FailureAction}} {{range .Spec.TaskTemplate.ContainerSpec.Mounts}}{{.Source}}->{{.Target}} {{end}}'
   # ожидается: 1 stop-first rollback sporbot-miniapp-data->/data
   docker ps -f name=sporbot-miniapp --format '{{.Status}}'      # Up … (healthy)
   docker port $(docker ps -q -f name=sporbot-miniapp)           # пусто: порт не опубликован на хосте
   ```
   *Стоп/откат:* перезапуски задачи, unhealthy или настройки не совпали → Stop приложения, разбор
   `docker service logs sporbot-miniapp`.
7. **[сервер] Проверки без Telegram:**
   ```sh
   D=<MINIAPP_DOMAIN>; IP=<DROPLET_IP>; C=$(docker ps -q -f name=sporbot-miniapp)
   curl -s https://$D/api/health                                                    # {"status":"ok"}
   curl -s -o /dev/null -w '%{http_code}\n' https://$D/                             # 200 через Cloudflare
   curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Edge-Auth: forged' https://$D/   # 200: Cloudflare перезаписал заголовок
   curl -sk -o /dev/null -w '%{http_code}\n' --resolve $D:443:$IP https://$D/       # 403: обход Cloudflare закрыт (-k: у Origin Certificate нет публичной цепочки; проверяется заголовок, не TLS)
   docker exec -i -u miniapp -e SMOKE_USER_ID=<Telegram id владельца> $C python - < /opt/sporbot/smoke.py   # RESULT PASS
   docker exec -i -u miniapp $C python - < /opt/sporbot/backup.py                   # первый бэкап, integrity=ok
   ```
   Smoke входит под аккаунтом владельца (его строка `users` создаётся/обновляется) и завершает только свою
   сессию. *Стоп:* любой FAIL.
8. **[сервер] Ежедневные бэкапы** (cron root на хосте, 03:17):
   `C=$(docker ps -q -f name=sporbot-miniapp) && docker exec -i -u miniapp $C python - < /opt/sporbot/backup.py`
   плюс копия последней папки с сервера (`docker cp $C:/data/backups/<stamp> …` → внешний диск/S3) и удаление
   копий старше 7 дней. Один раз — пробное восстановление из внешней копии по разделу 7 на тестовом томе.
9. **[владелец] Проверка в Telegram до открытия всем.** Подпись initData проверяется токеном
   production-бота, поэтому тестовый бот не подойдёт: кнопка только для владельца
   ```sh
   read -rs BOT_TOKEN                        # ввод без эха: токен не попадает в историю и аргументы процессов
   printf 'url = "https://api.telegram.org/bot%s/setChatMenuButton"\n' "$BOT_TOKEN" > /dev/shm/tg.cfg; unset BOT_TOKEN
   curl -s -K /dev/shm/tg.cfg -d chat_id=<id владельца> \
     --data-urlencode 'menu_button={"type":"web_app","text":"SporBot","web_app":{"url":"https://<MINIAPP_DOMAIN>"}}'
   rm -f /dev/shm/tg.cfg                     # (на macOS вместо /dev/shm — mktemp, затем rm)
   ```
   (бот не перезапускается). iOS/Android/Desktop/Web: вход, главная, поиск турнира, календарь, матч,
   AI-разбор (1 платный), эмблемы; в Telegram Web — закрыть и открыть Mini App снова, сессия сохраняется.
10. **[владелец] Открыть пользователям:** BotFather → production-бот → Menu Button / Main Mini App =
    `https://<MINIAPP_DOMAIN>`. Бот не перезапускается.
11. **Наблюдение 24 ч** (раздел 8).

## 7. Откат

| Ситуация | Действие | Бот |
|---|---|---|
| До шага 10 | Dokploy → Stop/Delete `sporbot-miniapp` | не затронут |
| После шага 10, сбой Mini App | BotFather: убрать/вернуть прежнюю Menu Button → Stop приложения | не затронут |
| Массовые проблемы входа | `MINIAPP_LOGINS_ENABLED=false` (Redeploy) и/или `docker exec -u miniapp <ctr> python -m miniapp.admin revoke-all --yes` | не затронут |
| Повреждение данных | процедура «Восстановление» ниже | не затронут |
| Следующие релизы | Deploy предыдущего коммита ветки (Dokploy) — схема только расширяется, старый код работает с новой БД | не затронут |

### Восстановление из бэкапа (обе БД, проверено локально на копии в новом томе)
```sh
S=<stamp>                                   # папка в /data/backups/ (или внешняя копия, скопированная в том)
docker service scale sporbot-miniapp=0
# `scale` может вернуться раньше остановки: ждать, пока контейнера не станет (иначе стоп, том не трогать)
for i in $(seq 1 60); do [ -z "$(docker ps -q -f name=sporbot-miniapp)" ] && break; sleep 2; done
[ -z "$(docker ps -q -f name=sporbot-miniapp)" ] || { echo "STOP: writer still running"; exit 1; }
docker run --rm -v sporbot-miniapp-data:/data --entrypoint sh <образ sporbot-miniapp> -c "
  set -e; cd /data; mkdir -p broken-$S && mv miniapp.db* miniapp.forecasts.db* broken-$S/ 2>/dev/null || true
  cp backups/$S/miniapp.db backups/$S/miniapp.forecasts.db /data/   # без -wal/-shm: копии самодостаточны
  chown miniapp:miniapp /data/miniapp.db /data/miniapp.forecasts.db; chmod 600 /data/miniapp*.db"
# Сессии из копии отзываются ДО запуска сервиса — ни одна не успевает ожить (офлайн, без секретов):
docker run --rm -v sporbot-miniapp-data:/data -e MINIAPP_ENV=development -e MINIAPP_DB_PATH=/data/miniapp.db \
  -e MINIAPP_BOT_TOKEN=000000:offline-maintenance-not-a-token -e MINIAPP_TRUSTED_ORIGINS=http://localhost \
  <образ sporbot-miniapp> admin revoke-all --yes
docker service scale sporbot-miniapp=1
for i in $(seq 1 90); do [ -n "$(docker ps -q -f name=sporbot-miniapp -f health=healthy)" ] && break; sleep 2; done
C=$(docker ps -q -f name=sporbot-miniapp -f health=healthy); [ -n "$C" ] || { echo "STOP: not healthy, see docker service logs"; exit 1; }
docker exec -i -u miniapp $C python - verify /data < /opt/sporbot/backup.py
docker exec -i -u miniapp -e SMOKE_USER_ID=<id владельца> -e SMOKE_USER_EXISTS_OK=1 $C python - < /opt/sporbot/smoke.py
```
Повреждённые файлы остаются в `/data/broken-<stamp>/` до разбора. Пока сервис в 0 реплик, Mini App
недоступна (бот работает).

## 8. Мониторинг

- Uptime-монитор раз в минуту на `https://<MINIAPP_DOMAIN>/api/health` + алерт владельцу.
- Логи Mini App (`docker service logs sporbot-miniapp`): `event=unhandled_error`, `event=health_db_error`,
  `event=edge_bypass_blocked`, `sports_upstream_error ... status=429`, всплески `auth.rate_limited`,
  `ai.` ошибки (`timeout`, `upstream_error`).
- Логи бота: `football API 429` (общий ключ) — любое появление.
- Квоты: `GET /api/sports/coverage` (под сессией) → `quota.usedLastMinute/usedToday`; расход Anthropic —
  `ai_usage` в БД / консоль Anthropic; потолок `MINIAPP_AI_DAILY_BUDGET`.
- Задержки: AI-разбор холодный ≤ 8 с, тёплый ≤ 3 с (замеры стенда); таймаут сбора данных 20 с.
- Сверка прогнозов: `python -m miniapp.admin forecasts` (read-only) раз в сутки первую неделю.
