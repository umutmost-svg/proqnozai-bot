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
- Transform Rule (Modify Request Header) для хоста: добавить `X-Edge-Auth: <MINIAPP_EDGE_SECRET>`.
- Rate limiting rule: `POST /api/auth/session` по IP (например, 20/мин), WAF — только `GET, HEAD, POST`.
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
| `MINIAPP_BOT_TOKEN` | токен **production-бота** (тот, из которого открывается Mini App; нужен для проверки подписи initData) | значение уже есть у бота; в новое приложение — **[владелец]** |
| `MINIAPP_DB_PATH` | `/data/miniapp.db` | |
| `MINIAPP_TRUSTED_ORIGINS` | `https://<MINIAPP_DOMAIN>` | |
| `MINIAPP_EDGE_SECRET` | случайная строка ≥ 32 символов, та же в Transform Rule | да |
| `MINIAPP_CSP_MODE` | `enforce` (как на стенде) | |
| `MINIAPP_COOKIE_SAMESITE` | `none` (Telegram Web открывает Mini App во фрейме; как на стенде) | |
| `MINIAPP_FOOTBALL_DATA_KEY` | см. раздел 3 | да |
| `MINIAPP_FOOTBALL_DATA_LIVE_PLAN` | `true` | |
| `MINIAPP_FOOTBALL_DATA_PER_MINUTE` | `60` при общем ключе с ботом, `100` при отдельном ключе | |
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

**Если отдельного ключа нет — общий ключ Pro (120 запросов/мин на ключ), с ограничением Mini App:**

| | Запросов в минуту |
|---|---|
| Лимит ключа (Pro) | 120 (подтверждено заголовком провайдера: `availableThisMinute` 119) |
| Потолок Mini App (клиентский, жёсткий) | **60** (`MINIAPP_FOOTBALL_DATA_PER_MINUTE=60`) |
| Гарантированно остаётся боту | **≥ 60** |
| Бот: запросов на один прогноз | ≤ 3 (состав турнира — кеш 24 ч; форма двух команд — кеш 6 ч), только 10 бесплатных турниров |
| Бот: чтобы занять 60/мин | > 20 «холодных» прогнозов разных пар команд в минуту |
| Фоновый прогрев Mini App при 60/мин | шаг 8 с, ≤ 15/мин (календарь + прошлый сезон), первый проход ~20 мин, затем обновление раз в ~71 мин; резерв минуты для пользователей Mini App — 3 запроса |
| Сутки (Mini App) | ~2 400 календарей + 117 прошлых сезонов + таблицы ≈ 3 500 из своего потолка 10 000 |

Поведение у границы: Mini App читает заголовок провайдера `X-Requests-Available-Minute` (общий для
ключа) и при `0` сама уходит в паузу до сброса минуты — то есть уступает боту первой; на 429 — пауза по
`Retry-After`, фон останавливается. Бот на 429 ждёт `Retry-After` (≤ 30 с) и повторяет до 3 раз —
прогноз медленнее, но не ломается.

**Условие запуска в режиме общего ключа:** `discovery.txt` показывает 0 (или единицы) строк
`football API 429` у бота за 7 дней. **Мониторинг после запуска:** любое `football API 429` в логах
бота или `sports_upstream_error ... status=429` в логах Mini App → снизить
`MINIAPP_FOOTBALL_DATA_PER_MINUTE` до 40 (Redeploy, бот не затрагивается).

## 4. Три P2 из техдолга — оценка для production

| P2 | Риск для production | Решение |
|---|---|---|
| Общий дневной потолок фоновой работы | При 60/мин первый проход прогрева ~20 мин; часть календарей/сезонов дольше холодные → AI-анализ таких лиг идёт по холодному пути (5–8 с, в пределах 20 с; прошлый сезон грузится параллельно). Корректность не страдает | оставить в техдолге |
| Settlement ждёт проход прогрева | Сверка — внутренняя статистика качества, пользователю не показывается; результаты приходят с задержкой до ~20 мин, не теряются (календари прогрева сверяются через слушателя) | оставить в техдолге; проверка на стенде 12–13.11 |
| Временные сбои хоста эмблем | Косметика: 0–2 эмблемы из 109 не видны до часа; вместо картинки — инициалы | оставить в техдолге |

## 5. Production preflight

| # | Проверка | Как | Статус |
|---|---|---|---|
| 1 | Точный SHA | код `b6cf031`; ветка `release/miniapp-prod` (только docs/скрипты сверху) | готово локально |
| 2 | Образ собирается | `docker build -f deploy/miniapp/Dockerfile --build-arg VITE_DATA_MODE=real .` из `b6cf031` | ✅ локально |
| 3 | Контейнер в production-режиме | health 200; без edge-секрета 403; CSP enforce; HSTS; процесс не root (`setpriv`) | ✅ локально |
| 4 | Smoke в контейнере | `docker exec -i <ctr> python - < deploy/miniapp/smoke.py` | ✅ 16/16 локально (реальные данные, 117 турниров, 109/109 эмблем) |
| 5 | Совместимость БД | у production нет БД Mini App: новый volume, схема создаётся при старте (`CREATE TABLE IF NOT EXISTS`); `bot.db` бота не используется и не монтируется | ✅ по коду |
| 6 | Бэкап и пробное восстановление | `deploy/miniapp/backup.py` (обе БД, SQLite backup API, 0600, проверка целостности); копия восстановлена в новый volume, новый контейнер здоров, данные совпали | ✅ локально |
| 7 | Права доступа | секреты только в Dokploy env; порт не публикуется; edge-секрет; volume владелец `miniapp` | на сервере — шаг runbook |
| 8 | Health check | `/api/health` (чтение БД), Swarm healthcheck из образа | ✅ в образе |
| 9 | Внешние API | football-data (ключ, план, остаток минуты), Anthropic (ключ Mini App), Telegram (подпись initData) | на сервере — шаг runbook |
| 10 | Откат | первый релиз: остановить приложение + вернуть кнопку бота; бот не затронут | план ниже |
| 11 | Нагрузка football-data | `discovery.txt`: 429 у бота за 7 дней | **ждёт владельца** |
| 12 | Сверка прогнозов на стенде | первые матчи 12.11; проверка 13.11 ~02:07 (Баку) | **ждёт даты** |

## 6. Runbook выкладки

Всё, кроме шага 9, выполняется **без перерыва в работе бота**: бот не пересобирается, не
перезапускается и не меняет конфигурацию. Условие остановки на любом шаге — отклонение от ожидаемого;
до шага 9 пользователи Mini App не видят.

1. **[владелец] Разрешения и данные:** домен `<MINIAPP_DOMAIN>`, ключи (football-data — отдельный или
   общий режим; Anthropic для Mini App), `discovery.txt`.
2. **[исполнитель, по разрешению] Push ветки:** `git push origin release/miniapp-prod` (бот следит только
   за `main`: Autodeploy бота не сработает — проверить в Dokploy, что у `bot`/`dashboard` branch=`main`).
3. **[владелец/сервер] Cloudflare:** DNS proxied, SSL Full (strict), Transform Rule `X-Edge-Auth`,
   rate limit `/api/auth/session`. *Стоп:* домен не резолвится через Cloudflare.
4. **[сервер] Dokploy → Create Application** `sporbot-miniapp` по таблице раздела 2 (Autodeploy OFF,
   Replicas 1, stop-first, volume, domain, env). *Стоп:* нет свободного места > 5 ГБ на диске (сборка
   образа ~1,5 ГБ с кешем).
5. **[сервер] Deploy.** Ждать `Running` и healthy. *Стоп/откат:* задача перезапускается или unhealthy →
   Stop приложения, разбор логов (`docker service logs sporbot-miniapp`).
6. **[сервер] Проверки без Telegram:**
   ```sh
   curl -s https://<MINIAPP_DOMAIN>/api/health                # {"status":"ok"}
   curl -s -o /dev/null -w '%{http_code}\n' https://<MINIAPP_DOMAIN>/   # 200 (через Cloudflare)
   curl -s -o /dev/null -w '%{http_code}\n' --resolve <MINIAPP_DOMAIN>:443:<DROPLET_IP> https://<MINIAPP_DOMAIN>/  # 403: обход Cloudflare закрыт
   C=$(docker ps -q -f name=sporbot-miniapp); docker exec -i $C python - < deploy/miniapp/smoke.py   # RESULT PASS
   docker exec -i -u miniapp $C python - < deploy/miniapp/backup.py                                  # первый бэкап, integrity=ok
   ```
   *Стоп:* любой FAIL в smoke.
7. **[сервер] Бэкапы:** cron на хосте раз в сутки `docker exec -i -u miniapp $(docker ps -q -f name=sporbot-miniapp) python - < /opt/sporbot/backup.py`
   + копирование `/data/backups` с сервера (или Dokploy Volume Backups → S3, если настроен
   destination); хранить 7 дней.
8. **[владелец] Проверка в Telegram до открытия:** временно тестовый бот (или Menu Button только для
   себя через `setChatMenuButton` с `chat_id` владельца) → открыть Mini App в iOS/Android/Desktop/Web:
   вход, главная, поиск турнира, календарь, матч, AI-разбор (1 платный), эмблемы.
9. **[владелец] Открыть пользователям:** BotFather → production-бот → Menu Button / Main Mini App =
   `https://<MINIAPP_DOMAIN>`. Бот не перезапускается.
10. **Наблюдение 24 ч** (раздел 8).

## 7. Откат

| Ситуация | Действие | Бот |
|---|---|---|
| До шага 9 | Dokploy → Stop/Delete `sporbot-miniapp` | не затронут |
| После шага 9, сбой Mini App | BotFather: убрать/вернуть прежнюю Menu Button → Stop приложения | не затронут |
| Массовые проблемы входа | `MINIAPP_LOGINS_ENABLED=false` (Redeploy) и/или `docker exec -u miniapp <ctr> python -m miniapp.admin revoke-all --yes` | не затронут |
| Повреждение данных | Stop → восстановить файлы из `/data/backups/<stamp>` в volume (проверено) → Start → `admin revoke-all --yes` | не затронут |
| Следующие релизы | Deploy предыдущего коммита ветки (Dokploy) — схема только расширяется, старый код работает с новой БД | не затронут |

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
