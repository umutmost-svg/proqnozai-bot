# Архитектура ProqnozAI Bot

Telegram-бот для AI-прогнозов на спортивные события. 7 языков, прогнозы от
Claude (Opus/Haiku), реальные коэффициенты Mostbet, live-трекинг матчей.

## Дерево проекта

```
proqnozai-bot/
│
├── main.py                  # 🚀 Точка входа: сборка app, post_init, background-задачи
├── config.py                # ⚙️  Фундамент: env-переменные, in-memory state, логирование
├── translations.py          # 🌍 i18n: тексты на 7 языках + system-промпты Claude
│
├── db.py                    # 💾 SQLite: пользователи, история, подписки, разговоры
├── security.py              # 🛡️  Rate-limit, анти-спам, блокировки
├── claude_client.py         # 🧠 Anthropic API: прогнозы (Opus), live-советы (Haiku)
├── mostbet.py               # 💰 Mostbet Odds API: матчи, коэффициенты, фильтры виртуалов
├── football_api.py          # ⚽ api-sports.io + football-data.org: форма, H2H, ср. голы
│
├── stats_server.py          # 📊 HTTP-сервер статистики внутри worker-процесса
├── dashboard.py             # 🖥  Flask-дашборд (отдельный web-процесс Railway)
│
├── handlers/                # 🎮 Telegram-хендлеры (по доменам)
│   ├── __init__.py          #     register_handlers() — регистрация всех обработчиков
│   ├── utils.py             #     Клавиатуры (main_menu, lang_kb), форматирование дат
│   ├── registration.py      #     /start, выбор языка, онбординг, профиль, таймзона
│   ├── forecast.py          #     Главный флоу прогноза: спорт→турнир→матч→анализ
│   ├── live.py              #     Live-трекинг: poller, голы, изменения кэфов, daily_push
│   ├── express.py           #     Экспресс-купоны (2–5 матчей)
│   ├── history.py           #     История прогнозов + фидбэк (зашло/не зашло)
│   └── admin.py             #     Админ-панель: статистика, рассылки, поиск юзеров
│
├── tests/                   # 🧪 Офлайн-юнит-тесты (pytest, без сети)
├── test_e2e.py              # 🧪 Ручной интеграционный прогон (реальные API, не для CI)
├── requirements.txt         # 📦 Зависимости рантайма
├── requirements-dev.txt     # 📦 Dev-зависимости (pytest, ruff)
├── Procfile                 # 🚂 Railway: worker (main.py) + web (dashboard.py)
└── README.md
```

## Процессы

| Процесс Railway | Точка входа | Что делает |
|---|---|---|
| `worker` | `main.py` | Telegram-бот (polling/webhook), фоновые задачи, поток `stats_server.py` на `STATS_PORT` |
| `web` | `dashboard.py` | Flask-дашборд (Basic Auth: `DASHBOARD_USER`/`DASHBOARD_TOKEN`) |

Дашборд не читает SQLite: все данные и действия (рассылка, блокировка
пользователей) идут по HTTP к stats-серверу воркера через приватную сеть
Railway (`BOT_API_URL`). Эндпоинты stats-сервера (`/stats`, `/broadcast`,
`/users/search`, `/users/block`) защищены `DASHBOARD_TOKEN`
(`hmac.compare_digest`); `/health` открыт для health-check'ов.

## Слои зависимостей

Импорты идут строго сверху вниз — нижние слои не знают о верхних.

```
┌─────────────────────────────────────────────────────────────┐
│  L5  ТОЧКА ВХОДА                                             │
│      main.py  ──  собирает app, запускает background-tasks   │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  L4  ХЕНДЛЕРЫ (handlers/)                                    │
│      __init__ → registration, forecast, live, express,      │
│                 history, admin                              │
│      utils (клавиатуры) — общий для всех хендлеров          │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  L3  СЕРВИСЫ (внешние интеграции и логика)                   │
│      claude_client   mostbet   football_api   security      │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  L2  ДАННЫЕ                                                  │
│      db.py (SQLite)        translations.py (i18n + промпты)  │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  L1  ФУНДАМЕНТ                                               │
│      config.py — env, in-memory state, logging              │
│      (никаких внутренних зависимостей)                      │
└─────────────────────────────────────────────────────────────┘
```

## Карта зависимостей по модулям

| Модуль                | Зависит от                                                        |
|-----------------------|-------------------------------------------------------------------|
| `config`              | — (только stdlib)                                                  |
| `translations`        | — (чистые данные)                                                  |
| `security`            | `config`                                                          |
| `db`                  | `config`                                                          |
| `mostbet`             | `config`                                                          |
| `football_api`        | `config` (+ `claude_client` лениво для оценки формы)             |
| `claude_client`       | `config`, `db`                                                    |
| `handlers/utils`      | `db`, `translations`                                             |
| `handlers/registration`| `config`, `db`, `translations`, `utils`                         |
| `handlers/forecast`   | `config`, `db`, `translations`, `security`, `claude_client`, `mostbet`, `football_api`, `utils`, `registration` |
| `handlers/live`       | `config`, `db`, `translations`, `football_api`, `mostbet`, `claude_client` |
| `handlers/express`    | `db`, `translations`, `claude_client`, `mostbet`                 |
| `handlers/history`    | `db`, `translations`                                            |
| `handlers/admin`      | `config`, `db`, `translations`, `mostbet`                       |
| `stats_server`        | `db`                                                             |
| `dashboard`           | — (отдельный процесс; только HTTP к stats_server)                |
| `main`                | `config`, `db`, `mostbet`, `handlers`, `translations`, `stats_server` |

## Основной флоу прогноза

```
Пользователь жмёт «⚽ Прогнозы»
        │
        ▼
forecast_menu_start ──► _mostbet_load_matches()        [mostbet]
        │                загружает матчи (кеш 15 мин)
        ▼
fm_sport_cb      выбор вида спорта (lineCategory)
        ▼
fm_league_cb     выбор турнира (lineSubCategory,
        │         сортировка _sorted_leagues: major-турниры сверху)
        ▼
fm_match_cb      выбор матча
        │  ├─ mostbet_get_odds()        реальные кэфы   [mostbet]
        │  └─ fetch_real_data()         форма+H2H+голы  [football_api]
        ▼
_generate_forecast
        │  собирает system_prompt (язык + профиль + данные)
        ▼
claude_forecast()  ──► Claude Opus 4.8                  [claude_client]
        │              + история разговора из db
        ▼
db_save_history()  ──► отправка прогноза пользователю
```

## Фоновые задачи (post_init в main.py)

| Задача                   | Назначение                                              |
|--------------------------|---------------------------------------------------------|
| `poller`                 | Опрос live-матчей, события (голы, карточки)             |
| `daily_push`             | Ежедневная рассылка топ-матчей                          |
| `_preload_mostbet`       | Прогрев кеша матчей Mostbet, обновление каждые 15 мин   |
| `check_odds_changes`     | Отслеживание движения коэффициентов                    |
| `_broadcast_menu_update` | Рассылка обновлённого меню при старте                   |

## Модели Claude

| Задача                          | Модель              | Где                    |
|---------------------------------|---------------------|------------------------|
| Прогноз матча (extended thinking, budget 2500) | `claude-opus-4-8` | `claude_forecast` |
| Оценка формы (нет API-данных)   | `claude-opus-4-8`   | `_sonnet_form_estimate`|
| Экспресс-купоны                 | `claude-haiku-4-5`  | `handlers/express.py`  |
| Сравнение команд (`/compare`)   | `claude-haiku-4-5`  | `handlers/express.py`  |
| Перевод имён команд             | `claude-haiku-4-5`  | `_normalize_names`     |
| Сопоставление с ростером football-data | `claude-haiku-4-5` | `_fd_resolve_ai`  |
| Live-подсказки                  | `claude-haiku-4-5`  | `live_tip`             |

## Внешние интеграции

- **Anthropic API** — генерация прогнозов и вспомогательная обработка
- **Mostbet Odds Checker API** (`mostbet2.com`, IP-whitelist) — матчи и коэффициенты
- **api-sports.io** — форма команд, H2H, статистика (100 req/day free)
- **football-data.org** — резервный источник формы команд
- **Telegram Bot API** — через `python-telegram-bot 21.5`

## In-memory состояние (config.py)

Всё живёт в памяти worker-процесса и теряется при рестарте:

| Структура | Назначение |
|---|---|
| `msg_times`, `violations`, `blocked_until` | Rate-limit и авто-блокировки |
| `reg_step` | Шаг регистрации/онбординга пользователя |
| `live_subs` | Live-подписки `match_id → {uid}` (восстанавливаются из БД при старте) |
| `mostbet_cache` | Кэш списка матчей и коэффициентов Mostbet (TTL 900 с) |
| `last_events`, `ht_sent` | Дедупликация live-событий и HT-уведомлений |
| `_mostbet_lock` | Один конкурентный fetch к Mostbet |

Плюс `football_api._fd_cache` — TTL-кэш football-data (ростер 24ч, форма 6ч).

## Деплой

Railway (`Procfile`: worker + web). Поддержка webhook (`WEBHOOK_URL`) и polling.
```
WEBHOOK_URL установлена  → app.run_webhook()
иначе                    → app.run_polling()
```

SQLite-файл `bot.db` создаётся в `BOT_DB_DIR` — на Railway этот каталог
должен быть volume, иначе данные теряются при редеплое.

## Известные архитектурные риски (зафиксированы, не исправлены)

1. **Rate-limit не покрывает callback-кнопки.** `rate_check` вызывается
   только в `handle_msg` (текст/фото). Прогноз через меню (`fm_mt_*`) и
   экспресс (`expr_*`) — вызовы Opus без лимита частоты.
2. **Персистентность SQLite** зависит от volume на Railway (`BOT_DB_DIR`).
3. **Частичный фид Mostbet кэшируется на полный TTL** (15 мин), если
   пагинация оборвалась по 429/дедлайну — турниры могут «пропасть» до
   следующего обновления.
4. **Двойная инъекция коэффициентов** в меню-флоу: `fm_match_cb` добавляет
   блок кэфов в контент, затем `_generate_forecast` по `parsed_teams`
   находит матч повторно и добавляет блок ещё раз.
5. **`db_log_req` пишет `last_active` в локальном времени** процесса, а
   выборки сравнивают с `date('now')` (UTC) — сегменты активности могут
   съезжать до суток.
6. **Синхронные вызовы SQLite внутри async-хендлеров** — при большой
   нагрузке могут подтормаживать event loop (сейчас запросы короткие, WAL).
7. **In-memory rate-limit/блокировки** сбрасываются при каждом рестарте.
