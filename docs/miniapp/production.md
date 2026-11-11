# SporBot Mini App — первый production-релиз (Dokploy)

Подготовлено 2026-11-11. Ничего из этого ещё не выполнено на сервере: каждый шаг с пометкой
**[владелец]** или **[сервер]** — только по отдельному разрешению владельца.

## 0. Что выпускаем

| | |
|---|---|
| Код релиза | `b6cf031` (стенд 2026-11-11) + вход без токена (Ed25519) + превью «Быстрого прогноза» + закрытый список пользователей (`MINIAPP_ALLOWED_USER_IDS`). Кандидат: HEAD `release/miniapp-prod` после этой правки |
| `<TESTED_CODE_SHA>` | коммит, прошедший проверку на стенде (раздел «Ed25519 на стенде») с этим кодом; записывается здесь после неё |
| Ветка выкладки | `release/miniapp-prod` = `<TESTED_CODE_SHA>` + коммиты только с документацией/скриптами вне образа (в т.ч. запись SHA). Код приложения выкладываемого коммита идентичен `<TESTED_CODE_SHA>` |
| Проверка перед push | `git diff --stat <TESTED_CODE_SHA> release/miniapp-prod -- miniapp frontend deploy/miniapp/Dockerfile deploy/miniapp/entrypoint.sh` пуст **и** `git show release/miniapp-prod:miniapp/settings.py \| grep -q MINIAPP_INITDATA_MODE`; SHA выкладки (`git rev-parse release/miniapp-prod`) фиксируется в отчёте о выкладке |
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

### Ed25519 на стенде (до фиксации `<TESTED_CODE_SHA>`)
1. Стенд: код `feat/initdata-ed25519` в режиме `hmac` с `MINIAPP_BOT_ID` тестового бота — вход как раньше,
   плюс замер `auth.ed25519_shadow`.
2. Владелец открывает Mini App тестового бота с iOS, Android, Telegram Desktop, macOS, Web K и Web A.
   Условие: в логе на каждый вход `auth.ed25519_shadow result=ok`, ни одного `missing`/`bad`.
3. Стенд переводится в `ed25519` (без токена, с `MINIAPP_CSRF_SECRET`) не раньше чем через 6 мин после
   шага 1; повторный вход со всех клиентов — `auth.session created=True`, отказов нет.
4. Коммит, прошедший шаги 1–3, — `<TESTED_CODE_SHA>`; `release/miniapp-prod` перематывается на него, затем
   отдельным docs-коммитом SHA вписывается в раздел 0.
Если шаг 2 или 3 не проходит — production в режиме `hmac` с токеном (запасной вариант раздела 2).

## 1. Инфраструктура: что известно и что надо подтвердить

### Состояние на 2026-11-11

| Параметр | Значение | Статус |
|---|---|---|
| Хостинг production | DigitalOcean Droplet + Dokploy (слова владельца, 2026-11-07/08) | предположение |
| Приложения Dokploy | `bot`, `dashboard` из GitHub `umutmost-svg/proqnozai-bot`, ветка `main`, Autodeploy On Push, Preview OFF (слова владельца) | предположение |
| `main` | `8ad4eec`; предок релиза (`git merge-base`) | **подтверждено** |
| Railway | оба проекта удалены (слова владельца) | предположение |
| Число экземпляров бота / второй polling | — | **NOT VERIFIED** (discovery: `getUpdates_conflicts`, число bot-контейнеров) |
| Ресурсы сервера, диски, volumes | — | **NOT VERIFIED** (discovery: host, volumes) |
| Резервные копии `bot.db` | — | **NOT VERIFIED** (discovery: backups) |
| Домены, HTTPS, Traefik, Cloudflare | — | **NOT VERIFIED** (discovery: traefik hosts; Cloudflare — у владельца) |
| SSH-доступ исполнителя к серверу | нет: в `~/.ssh/config` только GitHub; `known_hosts` содержит чужой проект (`167.172.97.220`, не трогать) и неопознанный `164.90.180.208` | **подтверждено** |
| Нагрузка на общий ключ football-data | в минуту замера стенд — 0 запросов, у ключа свободно 103 из 120 → ~16/мин тратит другой клиент (вероятно, production-бот) | **подтверждено** (заголовок провайдера, 2026-11-11 ~14:10 UTC) |
| GitHub: Actions / deployments на `release/miniapp-prod` | 0 / 0 после push | **подтверждено** |

### Где взять недостающее
- Dokploy UI (владелец): Applications → `bot`, `dashboard`: Source/Branch, Autodeploy, Preview, Domains, Volumes,
  Environment (только имена переменных, не значения), Swarm settings; Settings → версия Dokploy, Backups/Destinations.
- Cloudflare (владелец): зона домена, режим SSL/TLS, существующие DNS-записи, Transform Rules, Rate limiting.
- BotFather (владелец): username и числовой id production-бота (`MINIAPP_BOT_ID` = часть токена до `:`), текущая
  Menu Button.
- football-data.org (владелец): аккаунт и тариф для отдельного ключа Mini App.
- Anthropic Console (владелец): отдельный ключ Mini App и лимит расходов (Workspace spend limit).
- Сервер: результат `prod-discovery.sh` (ниже).

Известно со слов владельца (2026-11-07/08), **не проверено чтением сервера**: DigitalOcean Droplet +
Dokploy; приложения `bot` и `dashboard` из GitHub `umutmost-svg/proqnozai-bot`, ветка `main`,
Autodeploy On Push, Preview OFF; Railway удалён. У исполнителя нет доступа к серверу и Dokploy (нет
SSH-ключа сервера, токена Dokploy), поэтому фактическая картина собирается скриптом:

**[владелец] Точная инструкция** (скрипт берётся из GitHub по закреплённому коммиту и сверяется по SHA-256 до
запуска; ничего не устанавливает и не меняет):
```sh
ssh root@<сервер>
cd /root && curl -fsSL -o prod-discovery.sh \
  https://raw.githubusercontent.com/umutmost-svg/proqnozai-bot/c0d7dc0620f179412f98daded045f9984613199f/deploy/miniapp/prod-discovery.sh
echo "9cb46184d1ff142c96b47fe79905831d6d75c1ebe68027533abe9ecf50c33f9d  prod-discovery.sh" | sha256sum -c -   # OK, иначе не запускать
sh prod-discovery.sh > discovery.txt 2>&1
less discovery.txt   # просмотреть перед отправкой: имена контейнеров, домены, счётчики — без секретов и персональных данных
rm prod-discovery.sh
```
Скрипт `sh deploy/miniapp/prod-discovery.sh` — только чтение,
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
| `MINIAPP_INITDATA_MODE` | **`ed25519`** — проверка initData публичным ключом Telegram, токена бота в контейнере нет (условие: замер на стенде дал `ok` на всех клиентах; иначе `hmac` + `MINIAPP_BOT_TOKEN` production-бота) | |
| `MINIAPP_BOT_ID` | публичный числовой id production-бота (часть токена до `:`; не секрет) | |
| `MINIAPP_TELEGRAM_ENV` | `production` | |
| `MINIAPP_CSRF_SECRET` | случайная строка ≥ 32 символов | да |
| `MINIAPP_BOT_TOKEN` | **не задавать** в режиме `ed25519` (запуск остановится с ошибкой) | |
| `MINIAPP_ALLOWED_USER_IDS` | **закрытый запуск:** Telegram id владельца (через запятую — несколько). Вход и любая сессия — только для них, остальным 403 `not_allowed`, их данные не сохраняются. Открытие всем — убрать переменную (Redeploy) | |
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
| 1 | Точный SHA | `<TESTED_CODE_SHA>` после проверки Ed25519 на стенде; ветка выкладки — тот же код + docs | **ждёт стенда** |
| 2 | Образ собирается | `docker build -f deploy/miniapp/Dockerfile --build-arg VITE_DATA_MODE=real .` | ✅ локально (`b6cf031` и `feat/initdata-ed25519`: cryptography 50.0.2 из wheel, проверка хешей) |
| 3 | Контейнер в production-режиме | health 200; без edge-секрета 403; CSP enforce; HSTS; процесс не root (`setpriv`) | ✅ локально |
| 4 | Smoke в контейнере | `docker exec -i -u miniapp <ctr> python - < smoke.py` | ✅ локально: `hmac` 16/16, `ed25519` 11/11 без токена (реальные данные) |
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

`sporbot-miniapp` ниже — имя приложения/сервиса в Dokploy. Dokploy может дать сервису другое имя (суффикс):
перед командами взять фактическое из `docker service ls` и подставить в `SVC=<имя>`; `docker ps -f name=$SVC`.

**Все шаги выполняются без перерыва в работе бота**: бот не пересобирается и не перезапускается; шаги 9–10
меняют только кнопку меню бота в Telegram. Условие остановки на любом шаге — отклонение от ожидаемого;
до шага 10 пользователи Mini App не видят.

1. **[владелец] Разрешения и данные:** домен `<MINIAPP_DOMAIN>`, ключи (football-data — отдельный или
   общий режим; Anthropic для Mini App), `discovery.txt`.
2. **[исполнитель, по разрешению] Push ветки:** проверка из раздела 0 (код = `<TESTED_CODE_SHA>`, Ed25519 в ветке выкладки),
   затем `git push origin release/miniapp-prod`. Перед этим в Dokploy
   убедиться, что у `bot` и `dashboard` branch = `main` (тогда их Autodeploy не сработает).
3. **[владелец/сервер] Скрипты на сервер:** `scp deploy/miniapp/{smoke.py,backup.py,backup-job.sh} <server>:/opt/sporbot/`
   (права 0644, владелец root); сверить `sha256sum` с локальными.
4. **[владелец] Cloudflare** по разделу 2. *Стоп:* домен не резолвится через Cloudflare.
5. **[сервер] Dokploy → Create Application** `sporbot-miniapp` по таблице раздела 2 (Autodeploy OFF,
   Replicas 1, stop-first, volume, domain, env). *Стоп:* свободно < 5 ГБ на диске (сборка ~1,5 ГБ с кешем).
6. **[сервер] Deploy**, затем проверить, что Dokploy применил настройки:
   ```sh
   docker service inspect $SVC --format '{{.Spec.Mode.Replicated.Replicas}} {{.Spec.UpdateConfig.Order}} {{.Spec.UpdateConfig.FailureAction}} {{range .Spec.TaskTemplate.ContainerSpec.Mounts}}{{.Source}}->{{.Target}} {{end}}'
   # ожидается: 1 stop-first rollback sporbot-miniapp-data->/data
   docker ps -f name=$SVC --format '{{.Status}}'      # Up … (healthy)
   docker port $(docker ps -q -f name=$SVC)           # пусто: порт не опубликован на хосте
   ```
   *Стоп/откат:* перезапуски задачи, unhealthy или настройки не совпали → Stop приложения, разбор
   `docker service logs $SVC`.
7. **[сервер] Проверки без Telegram:**
   ```sh
   D=<MINIAPP_DOMAIN>; IP=<DROPLET_IP>; C=$(docker ps -q -f name=$SVC)
   curl -s https://$D/api/health                                                    # {"status":"ok"}
   curl -s -o /dev/null -w '%{http_code}\n' https://$D/                             # 200 через Cloudflare
   curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Edge-Auth: forged' https://$D/   # 200: Cloudflare перезаписал заголовок
   curl -sk -o /dev/null -w '%{http_code}\n' --resolve $D:443:$IP https://$D/       # 403: обход Cloudflare закрыт (-k: у Origin Certificate нет публичной цепочки; проверяется заголовок, не TLS)
   docker exec -i -u miniapp -e SMOKE_EXPECT_ALLOWLIST=1 $C python - < /opt/sporbot/smoke.py   # RESULT PASS: неподписанный вход отклонён, токена нет, список допуска включён, данные идут
   docker exec -i -u miniapp $C python - < /opt/sporbot/backup.py                   # первый бэкап, integrity=ok
   ```
   В режиме `ed25519` smoke не может войти сам (подписать вход может только Telegram): положительная
   проверка входа — шаг 9. В режиме `hmac` нужен `-e SMOKE_USER_ID=<id владельца>`: smoke входит под
   аккаунтом владельца и завершает только свою сессию. *Стоп:* любой FAIL.
8. **[сервер] Ежедневные бэкапы:** скопировать `deploy/miniapp/backup-job.sh` в `/opt/sporbot/` (0700, root),
   один раз задать в нём `SVC`, `OFFSITE_KIND` (`rclone` / `ssh` / `disk`), `OFFSITE` (для `disk` — каталог на
   внешнем диске с файлом-меткой `.sporbot-offsite`, созданным на самом диске) и `NOTIFY` (команда уведомления владельцу); cron root: `17 3 * * * /opt/sporbot/backup-job.sh >> /var/log/sporbot-backup.log 2>&1`.
   Задание: копия обеих БД с `integrity_check` → внешняя копия → хранение 7 дней; любой сбой — код ≠ 0 и
   `NOTIFY`. Первый запуск — вручную, проверить `OK:` в логе и наличие копии во внешнем приёмнике. Один раз —
   пробное восстановление из внешней копии по разделу 7 на тестовом томе.
9. **[владелец] Проверка в Telegram до открытия всем.** Mini App принимает только запуски из
   production-бота (подпись Telegram для его `MINIAPP_BOT_ID`), поэтому тестовый бот не подойдёт: кнопка
   только для владельца ставится production-ботом (токен нужен лишь этой команде на машине владельца, не
   контейнеру Mini App)
   ```sh
   read -rs BOT_TOKEN                        # ввод без эха: токен не попадает в историю и аргументы процессов
   CFG=$(umask 077; mktemp) && trap 'rm -f "$CFG"' EXIT   # уникальный файл 0600, удаляется при выходе
   printf 'url = "https://api.telegram.org/bot%s/setChatMenuButton"\n' "$BOT_TOKEN" > "$CFG"; unset BOT_TOKEN
   curl -s -K "$CFG" -d chat_id=<id владельца> \
     --data-urlencode 'menu_button={"type":"web_app","text":"SporBot","web_app":{"url":"https://<MINIAPP_DOMAIN>"}}'
   rm -f "$CFG"
   ```
   **До установки** сохранить текущее значение: `getChatMenuButton` с `chat_id=<id владельца>` и без `chat_id`
   (ответы `"ok":true` — записать `result`). Проверка после (тот же приём с `-K`): с `chat_id=<id владельца>` — `web_app` с URL Mini
   App; без `chat_id` (кнопка по умолчанию для всех) — прежнее значение. Кнопка ставится только для чата,
   который уже открыт с ботом (иначе `Bad Request: user not found` — проверено на тестовом боте). Откат:
   `setChatMenuButton` с `chat_id=<id владельца>` и **сохранённым до шага значением** (`{"type":"default"}` —
   только если сохранено именно оно).
   (бот не перезапускается). iOS/Android/Desktop/Web: вход, главная, поиск турнира, календарь, матч,
   AI-разбор (1 платный), эмблемы; в Telegram Web — закрыть и открыть Mini App снова, сессия сохраняется.
   **Обязательная положительная проверка входа:** в логах Mini App на каждый клиент `auth.session ...
   created=True` и ни одного `auth.rejected reason=bad_signature|missing_signature|bad_signature_format`
   (перед входом `T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)` — после smoke, чей намеренный отказ не считается; затем
   `docker service logs $SVC --since "$T0" | grep -E 'auth\.(session|rejected)'`). *Стоп:* любой
   такой отказ — неверный `MINIAPP_BOT_ID`/`MINIAPP_TELEGRAM_ENV` или клиент без подписи → выключить кнопку
   владельца, разобрать; запасной вариант — `hmac` с токеном.
10. **Закрытый запуск на этом заканчивается.** Кнопка по умолчанию (для всех) и BotFather не меняются;
    `MINIAPP_ALLOWED_USER_IDS` остаётся. Наблюдение 24 ч (раздел 8).

**Открытие всем — отдельный этап, отдельное разрешение (не 13.11):** после проверки AI 20–22.11 и наблюдения:
убрать `MINIAPP_ALLOWED_USER_IDS` (Redeploy), затем BotFather → Menu Button / Main Mini App = `https://<MINIAPP_DOMAIN>`.

## 7. Откат

| Ситуация | Действие | Бот |
|---|---|---|
| Закрытый запуск (до или после шага 9) | вернуть кнопку владельца: `setChatMenuButton` с `chat_id=<id владельца>` и значением, сохранённым до шага 9 (именно им; `{"type":"default"}` — только если сохранено оно), проверить `"ok":true`; затем Dokploy → Stop `sporbot-miniapp` | не затронут |
| После открытия всем, сбой Mini App | BotFather: вернуть прежнюю Menu Button → Stop приложения | не затронут |
| Массовые проблемы входа | `MINIAPP_LOGINS_ENABLED=false` (Redeploy) и/или `docker exec -u miniapp <ctr> python -m miniapp.admin revoke-all --yes` | не затронут |
| Повреждение данных | процедура «Восстановление» ниже | не затронут |
| Следующие релизы | Deploy предыдущего коммита ветки (Dokploy) — схема только расширяется, старый код работает с новой БД | не затронут |

### Восстановление из бэкапа (обе БД, проверено локально на копии в новом томе)
```sh
S=<stamp>                                   # папка в /data/backups/ (или внешняя копия, скопированная в том)
docker service scale $SVC=0
# `scale` может вернуться раньше остановки: ждать, пока контейнера не станет (иначе стоп, том не трогать)
for i in $(seq 1 60); do [ -z "$(docker ps -q -f name=$SVC)" ] && break; sleep 2; done
[ -z "$(docker ps -q -f name=$SVC)" ] || { echo "STOP: writer still running"; exit 1; }
docker run --rm -v sporbot-miniapp-data:/data --entrypoint sh <образ sporbot-miniapp> -c "
  set -e; cd /data; mkdir -p broken-$S && mv miniapp.db* miniapp.forecasts.db* broken-$S/ 2>/dev/null || true
  cp backups/$S/miniapp.db backups/$S/miniapp.forecasts.db /data/   # без -wal/-shm: копии самодостаточны
  chown miniapp:miniapp /data/miniapp.db /data/miniapp.forecasts.db; chmod 600 /data/miniapp*.db"
# Сессии из копии отзываются ДО запуска сервиса — ни одна не успевает ожить (офлайн, без секретов):
docker run --rm -v sporbot-miniapp-data:/data -e MINIAPP_ENV=development -e MINIAPP_DB_PATH=/data/miniapp.db \
  -e MINIAPP_BOT_TOKEN=000000:offline-maintenance-not-a-token -e MINIAPP_TRUSTED_ORIGINS=http://localhost \
  <образ sporbot-miniapp> admin revoke-all --yes
docker service scale $SVC=1
for i in $(seq 1 90); do [ -n "$(docker ps -q -f name=$SVC -f health=healthy)" ] && break; sleep 2; done
C=$(docker ps -q -f name=$SVC -f health=healthy); [ -n "$C" ] || { echo "STOP: not healthy, see docker service logs"; exit 1; }
docker exec -i -u miniapp $C python - verify /data < /opt/sporbot/backup.py
docker exec -i -u miniapp -e SMOKE_USER_ID=<id владельца> -e SMOKE_USER_EXISTS_OK=1 $C python - < /opt/sporbot/smoke.py
```
Повреждённые файлы остаются в `/data/broken-<stamp>/` до разбора. Пока сервис в 0 реплик, Mini App
недоступна (бот работает).

## 8. Мониторинг

- Uptime-монитор раз в минуту на `https://<MINIAPP_DOMAIN>/api/health` + алерт владельцу.
- Логи Mini App (`docker service logs $SVC`): `event=unhandled_error`, `event=health_db_error`,
  `event=edge_bypass_blocked`, `event=sports_upstream_error ... status=429`, всплески `auth.rate_limited`,
  `auth.rejected reason=not_allowed` (кто-то, кроме допущенных, открыл Mini App), `ai.unavailable path=… reason=…`
  (`timeout`, `upstream_error`, `rate_limited`, `budget_exhausted`), `event=ai_upstream_error`.
- Бэкапы: `/var/log/sporbot-backup.log` и уведомление cron при сбое (шаг 8).
- Логи бота: `football API 429` (общий ключ) — любое появление.
- Квоты: `GET /api/sports/coverage` (под сессией) → `quota.usedLastMinute/usedToday`; расход Anthropic —
  `ai_usage` в БД / консоль Anthropic; потолок `MINIAPP_AI_DAILY_BUDGET`.
- Задержки: AI-разбор холодный ≤ 8 с, тёплый ≤ 3 с (замеры стенда); таймаут сбора данных 20 с.
- Сверка прогнозов: `python -m miniapp.admin forecasts` (read-only) раз в сутки первую неделю.
