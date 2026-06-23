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
├── claude_client.py         # 🧠 Anthropic API: прогнозы (Opus), парсинг/live (Haiku)
├── mostbet.py               # 💰 Mostbet Odds API: матчи, коэффициенты, нормализация турниров
├── football_api.py          # ⚽ api-sports.io + football-data.org: форма, H2H, ср. голы
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
├── test_e2e.py              # 🧪 E2E-тесты
├── requirements.txt         # 📦 Зависимости
├── Procfile                 # 🚂 Railway worker
└── README.md
```

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
| `main`                | `config`, `db`, `mostbet`, `handlers`, `translations`           |

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
fm_league_cb     выбор турнира (lineSubCategory)
        │         normalize_tournament_ai()             [mostbet → Haiku]
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
| Прогноз матча                   | `claude-opus-4-8`   | `claude_forecast`      |
| Оценка формы (нет API-данных)   | `claude-opus-4-8`   | `_sonnet_form_estimate`|
| Парсинг запроса (имена команд)  | `claude-haiku-4-5`  | `parse_match_query`    |
| Перевод имён команд             | `claude-haiku-4-5`  | `_normalize_names`     |
| Нормализация турниров           | `claude-haiku-4-5`  | `normalize_tournament_ai` |
| Live-подсказки                  | `claude-haiku-4-5`  | `live_tip`             |

## Внешние интеграции

- **Anthropic API** — генерация прогнозов и вспомогательная обработка
- **Mostbet Odds Checker API** (`mostbet2.com`, IP-whitelist) — матчи и коэффициенты
- **api-sports.io** — форма команд, H2H, статистика (100 req/day free)
- **football-data.org** — резервный источник формы команд
- **Telegram Bot API** — через `python-telegram-bot 21.5`

## Деплой

Railway (worker-режим, `Procfile`). Поддержка webhook (`WEBHOOK_URL`) и polling.
```
WEBHOOK_URL установлена  → app.run_webhook()
иначе                    → app.run_polling()
```
