# Релиз Mini App на существующем сервере (164.90.180.208) — runbook

Утверждено владельцем 2026-11-12: один сервер (4 ГБ / 2 vCPU), существующий Dokploy и бот `@ProqnozAI_bot`
(id 8727724516), AI-разбор выключен, `/start` и партнёрские ссылки `/r/` продолжают работать, бот не
останавливается. Подготовка и развёртывание без публичного объявления — разрешены; BotFather, порт 3000 и
массовое открытие — отдельные разрешения.

## Архитектура

```
Telegram ──> https://app.proqnozai.com ──> Cloudflare (proxy: X-Edge-Auth, rate limit, cache)
                                               │  Cloudflare Tunnel (только исходящее соединение сервера)
                                   cloudflared (host network) ──> http://127.0.0.1:80  (Traefik, Host app.proqnozai.com)
                                                                        └──> Dokploy-приложение proqnozai-miniapp :8000
https://admin.proqnozai.com ──> Cloudflare Access ──> тот же туннель ──> http://127.0.0.1:3000 (Dokploy)
Бот, дашборд и /r/ — без изменений (Traefik :80/:443, sslip.io).
```

- **TLS** завершает Cloudflare; до сервера — туннель. Сертификатов на сервере для Mini App нет, входящие порты не
  открываются. Прямой запрос на `IP:80` с `Host: app.proqnozai.com` дойдёт до приложения, но без секрета
  `X-Edge-Auth` (его ставит только Cloudflare) получит 403 — кроме `GET /api/health`.
- **Образ** собирается не на сервере: amd64 на машине разработчика (`docker buildx --platform linux/amd64`) из
  коммита ветки `release/miniapp-prod`, переносится `docker save | ssh docker load` и кладётся в **локальный
  registry `127.0.0.1:5000`** на сервере (контейнер `registry:2`, только loopback, свой том). Нужен потому, что
  Dokploy перед выкладкой из образа всегда делает `docker pull` (исходный код v0.29.12, `providers/docker.ts`).
- **Исходящий IP** всех контейнеров — 164.90.180.208 (проверено из контейнера бота). Интеграция с MB не меняется:
  коэффициенты в Mini App в первом запуске **выключены** (`MINIAPP_ODDS_FEED_ENABLED=false`) — их источник —
  экспорт из бота, а он требует новых переменных и перевыкладки бота (отдельное решение, см. «Решения»).
- **Данные:** том `proqnozai-miniapp-data` → `/data` (`miniapp.db` + история прогнозов). БД бота не монтируется.

## Конфигурация приложения в Dokploy (создаётся через API Dokploy с сервера)

| Параметр | Значение |
|---|---|
| Проект / имя | `proqnozai` / `proqnozai-miniapp` |
| Источник | Docker image `127.0.0.1:5000/proqnozai-miniapp:<коммит>` |
| Autodeploy | нет (образ, не Git) |
| Реплики / обновление | 1, `stop-first` (одна SQLite — один писатель) |
| Ресурсы | reservation 256 МБ, limit 512 МБ |
| Том | `proqnozai-miniapp-data` → `/data` |
| Домен | `app.proqnozai.com`, порт 8000, HTTPS в Dokploy **выкл** (TLS — Cloudflare, вход — туннель на :80) |
| Healthcheck | из образа (`/api/health`) |

Переменные (секреты генерируются на сервере и в отчёты не попадают):

| Переменная | Значение |
|---|---|
| `MINIAPP_ENV` | `production` |
| `MINIAPP_INITDATA_MODE` / `MINIAPP_BOT_ID` / `MINIAPP_TELEGRAM_ENV` | `ed25519` / `8727724516` / `production` (токена бота в контейнере нет) |
| `MINIAPP_CSRF_SECRET`, `MINIAPP_EDGE_SECRET` | случайные, 48 символов, файлы `/root/miniapp-secrets/` (0600) |
| `MINIAPP_TRUSTED_ORIGINS` | `https://app.proqnozai.com` |
| `MINIAPP_DB_PATH` | `/data/miniapp.db` |
| `MINIAPP_CSP_MODE` / `MINIAPP_COOKIE_SAMESITE` | `enforce` / `none` |
| `MINIAPP_ALLOWED_USER_IDS` | id владельца (= `ADMIN_ID` бота) — **закрытый режим до проверки клиентов**; убирается при открытии |
| `MINIAPP_BLOCKED_USER_IDS` | пусто (в базе бота заблокированных 0) |
| `MINIAPP_FOOTBALL_DATA_KEY` | **отдельный ключ** (нет — блокер, см. ниже); ключ бота не используется |
| `MINIAPP_FOOTBALL_DATA_LIVE_PLAN` / `PER_MINUTE` / `PER_DAY` / `COMPETITIONS` | по тарифу ключа (Pro: `true`/`100`/`10000`/`all`) |
| `MINIAPP_ANTHROPIC_API_KEY` | **не задаётся** (AI выключен) |
| `MINIAPP_ODDS_FEED_ENABLED` | `false` |
| `MINIAPP_LOGINS_ENABLED` | `true` |

## Порядок

**A. Доступ и защита (`docs/ops/dokploy-admin-access.md`)**
1. [владелец] токен туннеля на сервер → [я] `cloudflared` → [владелец] public hostnames `admin` и `app`.
2. [владелец] healthchecks.io → [я] шаг 2a (порт 80) + сторож.
3. Проверка Access → [владелец, отдельное разрешение] firewall :3000.

**B. Mini App** (независимо от A до шага B5)
1. [я] сборка amd64-образа, smoke в локальном контейнере (production-режим, ed25519, без ключей → ожидаемые
   отказы), перенос на сервер, `registry:2` на `127.0.0.1:5000`, push образа туда.
2. [я] секреты на сервере; [владелец] Dokploy API-ключ на сервер (одна команда).
3. [я] через API Dokploy: проект/приложение/переменные/том/домен/ресурсы/stop-first → Deploy. Проверка: задача
   1/1 healthy, `docker service inspect` = ожидаемым настройкам, бот и прочие сервисы 1/1, `free -m`.
4. [я] smoke внутри контейнера: неподписанный вход отклонён, токена нет, список допуска включён; с ключом
   football-data — данные идут.
5. [владелец] Cloudflare для `app.proqnozai.com`: Transform Rule **Set** `X-Edge-Auth` = значение из
   `/root/miniapp-secrets/edge` (прочитать в своём терминале), rate limit, cache для `/api/crests/*`.
   [я] проверки снаружи: страница 200, `/api/health` ok, прямой `IP:80` с Host app → 403, подделанный
   `X-Edge-Auth` перезаписан.
6. [я] миграция пользователей: копия бота (онлайн) → том Mini App → `migrate_users.py` сухой прогон → `--apply`
   → повтор `to insert 0`.
7. [владелец] меню-кнопка только для своего чата (`setChatMenuButton` с `chat_id` владельца — выполняю я из
   контейнера бота, токен не покидает контейнер) → вход на iOS, Android, Desktop, macOS, Web K, Web A; [я]
   проверяю по логам `auth.session … created=True` и отсутствие `auth.rejected` по подписи.
8. [я] бэкапы Mini App (`backup-job.sh`) + пробное восстановление; мониторинг `/api/health`.

**C. Открытие всем** (отдельное разрешение, после GO): финальная дельта пользователей → убрать
`MINIAPP_ALLOWED_USER_IDS` → Redeploy → smoke → BotFather (Main Mini App + Menu Button) → объявление порциями.

## BotFather — переключение и откат

Сохранено 2026-11-12 (до изменений): кнопка меню по умолчанию `{"type":"commands"}`, Main Mini App **не задан**
(`has_main_web_app: false`), вебхука нет.

- Переключение (после GO): BotFather → `/mybots` → `@ProqnozAI_bot` → Bot Settings → **Configure Mini App** →
  URL `https://app.proqnozai.com` (Main Mini App: кнопка «Открыть» в профиле, ссылка `t.me/ProqnozAI_bot?startapp`);
  кнопка меню для всех — `setChatMenuButton` без `chat_id` с `{"type":"web_app","text":"ProqnozAI","web_app":{"url":"https://app.proqnozai.com"}}`
  (выполняю из контейнера бота; токен не выводится). `/start` и команды бота не меняются.
- Откат: `setChatMenuButton` без `chat_id` с `{"type":"commands"}`; BotFather → Configure Mini App → отключить;
  персональная кнопка владельца — `setChatMenuButton` с его `chat_id` и `{"type":"default"}`.

## Решения и блокеры

- **football-data:** отдельного ключа нет — это блокер данных Mini App. Нужен второй аккаунт football-data.org
  с тарифом не ниже текущего у бота (120 запросов/мин, все турниры). Минимальный вариант — бесплатный ключ
  (10 запросов/мин, 12 турниров: все валидированные лиги есть) с `COMPETITIONS` по списку и `PER_MINUTE=8`.
- **Коэффициенты в Mini App:** требуют включить экспорт из бота (новые переменные бота и его перевыкладка;
  у бота `start-first` — сначала `stop-first`). Рекомендация — после запуска, отдельным окном.
- **Внешние бэкапы Mini App:** нужен приёмник (`OFFSITE`): например Backblaze B2 (бесплатно до 10 ГБ) или
  DigitalOcean Spaces.
