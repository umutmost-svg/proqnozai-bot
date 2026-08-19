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
├── db.py                    # 💾 SQLite: пользователи, история, подписки, разговоры,
│                            #     db_match_demand() — агрегат спроса для Priority Engine
├── security.py              # 🛡️  Rate-limit, анти-спам, блокировки
├── claude_client.py         # 🧠 Anthropic API: прогнозы (Opus), live-советы (Haiku)
├── mostbet.py               # 💰 Mostbet Odds API: матчи, коэффициенты, фильтры виртуалов
├── football_api.py          # ⚽ api-sports.io + football-data.org: форма, H2H, ср. голы
├── enrichment.py            # ✅ Верифицированное обогащение футбола (HIGH-confidence)
├── match_validation.py      # 🔎 Детерминированное сопоставление матчей между источниками
├── metrics.py               # 📐 Детерминированные вычисления (ср. голы и т.д.)
├── provenance.py            # 🏷️  Маркировка происхождения данных (реальные/оценочные)
├── priority_config.py       # 🎯 Match Priority Engine: тиры турниров/команд, дерби,
│                            #     паттерны стадий — статические конфиг-константы
├── priority_engine.py       # 🎯 Match Priority Engine: compute_priority() — детерми-
│                            #     нированный priority_score (0-100) для ранжирования
├── event_list.py            # 📋 Нормализация фида Mostbet, разделение турнир/стадия,
│                            #     фильтры по дню/стране, приоритетная сортировка,
│                            #     пагинация (paginate()) — чистый, offline-тестируемый
│
├── broadcast.py             # 📢 Рассылки: валидация HTML/кнопок, отправка, планировщик
│                            #     отложенных (таблица `broadcasts`, переживает рестарт)
├── stats_server.py          # 📊 HTTP-сервер статистики внутри worker-процесса
├── dashboard.py             # 🖥  Flask-дашборд (отдельный web-процесс Railway)
│
├── handlers/                # 🎮 Telegram-хендлеры (по доменам)
│   ├── __init__.py          #     register_handlers() — регистрация всех обработчиков
│   ├── utils.py             #     Клавиатуры, cb_guard (дорогие колбэки) / nav_guard
│   │                        #     (навигация меню) — rate-limit на ВСЕ callback-кнопки
│   ├── registration.py      #     /start, выбор языка, онбординг, профиль, таймзона
│   ├── forecast.py          #     Флоу прогноза: спорт→день→страна→турнир→матч→анализ,
│   │                        #     постраничный вывод (без жёсткой обрезки)
│   ├── live.py              #     Live-трекинг: poller, голы, изменения кэфов, daily_push
│   ├── compare.py           #     Сравнение двух команд (/compare)
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
`/broadcast/status`, `/broadcast/list`, `/broadcast/cancel`, `/segment/size`,
`/users/search`, `/users/block`) защищены `DASHBOARD_TOKEN`
(`hmac.compare_digest`); `/health` открыт для health-check'ов.
`/stats` отдаёт, помимо счётчиков, серию роста (`growth` — новые за день плюс
накопительный итог с базой до окна), активность по часам в МСК (`hourly`),
выбор видов спорта из онбординга (`sports`) и прокси-LTV (`value`: срок жизни,
прогнозы и клики на пользователя; деньги — только если задан `PARTNER_CPA`,
и это прогноз по ставке, а не фактическая выручка).

Ответ `/stats` кешируется на `STATS_TTL` (20 c): дашборд опрашивает его раз в
45 c из каждой открытой вкладки, а сбор — это ~30 агрегатов по SQLite.

### Рассылки
Каждая рассылка (и мгновенная, и отложенная) — строка в таблице `broadcasts`.
Дашборд только валидирует и ставит в очередь; отправляет воркер. Отложенные
подхватывает `broadcast.scheduler` (тик 30 c), поэтому запланированная рассылка
переживает редеплой. Забор задания — атомарный `UPDATE ... WHERE status='pending' AND NOT EXISTS
(... status='running')`: и одну рассылку нельзя забрать дважды, и две кампании
не пойдут параллельно (stats-сервер многопоточный, поэтому проверка по
in-memory флагу этого не гарантирует). Проигравший claim остаётся `pending` —
планировщик запустит его после текущей. Строки, оставшиеся `running` после
падения процесса, освобождаются при старте планировщика. Время админ вводит в МСК,
в БД хранится UTC. Текст — HTML-подмножество Telegram (`<b> <i> <u> <s> <a href>
<code> <pre> <blockquote> <tg-spoiler>`), разметка и inline-кнопки проверяются
до постановки в очередь: иначе Telegram отклонил бы всю кампанию целиком.

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
│      __init__ → registration, forecast, live, compare,      │
│                 history, admin                              │
│      utils (клавиатуры) — общий для всех хендлеров          │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  L3  СЕРВИСЫ (внешние интеграции и логика)                   │
│      claude_client   mostbet   football_api   security      │
│      enrichment   match_validation   metrics   provenance    │
│      priority_engine   event_list (priority_config — leaf,   │
│      как config/translations, без внутренних зависимостей)   │
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
| `priority_config`     | — (чистые данные/константы, leaf-модуль)                          |
| `security`            | `config`                                                          |
| `db`                  | `config`, `priority_config` (нормализация ключа спроса)           |
| `mostbet`             | `config`                                                          |
| `football_api`        | `config` (+ `claude_client` лениво для оценки формы)             |
| `enrichment`          | `config`, `match_validation`, `metrics`, `provenance`             |
| `match_validation`    | — (чистая логика, без внутренних зависимостей)                    |
| `priority_engine`     | `priority_config`                                                 |
| `event_list`          | `config`, `mostbet`, `priority_config`, `priority_engine`         |
| `claude_client`       | `config`, `db`                                                    |
| `handlers/utils`      | `config`, `db`, `security`, `translations` (`cb_guard`/`nav_guard`) |
| `handlers/registration`| `config`, `db`, `translations`, `utils`                         |
| `handlers/forecast`   | `config`, `db`, `translations`, `security`, `claude_client`, `mostbet`, `football_api`, `enrichment`, `match_validation`, `event_list`, `utils`, `registration` |
| `handlers/live`       | `config`, `db`, `translations`, `football_api`, `mostbet`, `claude_client` |
| `handlers/compare`    | `db`, `translations`, `claude_client`                            |
| `handlers/history`    | `db`, `translations`                                            |
| `handlers/admin`      | `config`, `db`, `translations`, `mostbet`                       |
| `stats_server`        | `db`                                                             |
| `dashboard`           | — (отдельный процесс; только HTTP к stats_server)                |
| `main`                | `config`, `db`, `mostbet`, `handlers`, `translations`, `stats_server` |

## Основной флоу прогноза

```
Пользователь жмёт «⚽ Прогноз»
        │
        ▼
forecast_menu_start ──► _mostbet_load_matches()        [mostbet]
        │                загружает матчи (кеш 15 мин)
        │                normalize_fixture/_resolve_competition
        │                разделяют турнир и стадию                [event_list]
        ▼
fm_sport_cb      выбор вида спорта (lineCategory)               [nav_guard]
        ▼
fm_day_cb        выбор дня: Live/Сегодня/Завтра/дата/«Все»       [nav_guard]
        ▼
fm_ctry_cb       выбор страны/региона — пропускается,            [nav_guard]
        │        если она одна (available_countries)
        ▼
fm_league_cb     выбор турнира — group_by_league сортирует по
        │        priority_score (Match Priority Engine: престиж,  [nav_guard]
        │        стадия, дерби, популярность команд, время,
        │        спрос db_match_demand()); список постраничный
        │        (paginate(), «Показать ещё», без обрезки)
        ▼
fm_match_cb      выбор матча                                     [cb_guard]
        │  ├─ mostbet_get_odds()        реальные кэфы   [mostbet]
        │  └─ fetch_real_data() / enrich_football_match()
        │      форма+H2H+голы, HIGH-confidence         [football_api/enrichment]
        ▼
_generate_forecast
        │  собирает system_prompt (язык + профиль + данные)
        ▼
claude_forecast()  ──► Claude Opus 4.8                  [claude_client]
        │              + история разговора из db
        ▼
db_save_history()  ──► отправка прогноза пользователю
```

Каждый шаг меню (спорт/день/страна/турнир/пагинация/назад) защищён `nav_guard`
с ОТДЕЛЬНЫМ, щедрым бюджетом (`nav_rate_check`: `NAV_RATE_MAX` кликов за
`NAV_RATE_WINDOW` с, счётчик `nav_times`), чтобы обычное листание не упиралось
в строгий текстовый лимит и не копило violation к авто-блоку — превышение лишь
мягко тормозит (тост). `fm_match_cb` — единственный шаг, который тратит деньги
(Mostbet + Opus), защищён более строгим `cb_guard` (строгий rate-limit +
per-user in-flight lock, чтобы двойной клик не запустил два параллельных Opus).

## Фоновые задачи (post_init в main.py)

| Задача                   | Назначение                                              |
|--------------------------|---------------------------------------------------------|
| `poller`                 | Опрос live-матчей, события (голы, карточки)             |
| `daily_push`             | Ежедневная рассылка топ-матчей                          |
| `_preload_mostbet`       | Прогрев кеша матчей Mostbet, обновление каждые 15 мин   |
| `check_odds_changes`     | Отслеживание движения коэффициентов                    |
| `_broadcast_menu_update` | Рассылка обновлённого меню при старте                   |
| `broadcast.scheduler`    | Отложенные рассылки из таблицы `broadcasts`             |

## Модели Claude

| Задача                          | Модель              | Где                    |
|---------------------------------|---------------------|------------------------|
| Прогноз матча (extended thinking, budget 2500) | `claude-opus-4-8` | `claude_forecast` |
| Оценка формы (нет API-данных)   | `claude-opus-4-8`   | `_sonnet_form_estimate`|
| Сравнение команд (`/compare`)   | `claude-haiku-4-5`  | `handlers/compare.py`  |
| Перевод имён команд             | `claude-haiku-4-5`  | `_normalize_names`     |
| Сопоставление с ростером football-data | `claude-haiku-4-5` | `_fd_resolve_ai`  |
| Live-подсказки                  | `claude-haiku-4-5`  | `live_tip`             |

## Внешние интеграции

- **Anthropic API** — генерация прогнозов и вспомогательная обработка
- **Mostbet Odds Checker API** (`mostbet2.com`, IP-whitelist) — матчи и коэффициенты
- **api-sports.io** — форма команд, H2H, статистика (100 req/day free)
- **football-data.org** — резервный источник формы команд
- **Telegram Bot API** — через `python-telegram-bot 21.5`

## Партнёры и промокоды (операционные данные)

Управляются из дашборда (`/partners`), **без редеплоя и рестарта**.

**Источник истины — БД, а не env.**

| Таблица | Назначение |
|---|---|
| `partners` | `id, name, url, is_active, is_archived, sort_order, created_at, updated_at` — партнёрские кнопки |
| `promo_campaign` | Кампания на партнёра: `partner, code, max_uses, is_active, is_archived, mode` |
| `promo_claims` | Кто какой код получил, PK `(user_id, code)` — источник `used` |
| `promo_pool` | Одноразовые коды pool-кампании: `partner, code UNIQUE, user_id, claimed_at`. `user_id IS NULL` = код свободен |
| `partner_aliases` | Все имена, которые когда-либо носил партнёр → `partner_id`. Редирект `/r/<name>` резолвится через неё, поэтому переименование не ломает кнопки в уже отправленных сообщениях |
| `partner_clicks` | Клики по редиректу `/r/<partner>` |

Поток записи: дашборд (`web`) → `PATCH /partners/<id>` на воркере → SQLite.
У `web`-процесса своей БД нет, поэтому весь CRUD живёт в `stats_server.py`.

Поток чтения бота: `db_active_partners()` на **каждый** рендер клавиатуры —
модульных констант с редактируемыми значениями нет, поэтому следующий
пользователь сразу видит новое значение. Таблица маленькая, запрос дешевле
одного round-trip к Telegram.

`PARTNERS` / `PARTNERS_URL` — **только bootstrap**: `db_init()` импортирует их
один раз в пустую таблицу и ставит флаг `partners_env_bootstrap` в
`_migrations`. После этого env игнорируется — правка в дашборде переживает
рестарт, а удалённый партнёр не воскресает.

Ротация кода (`/setpromo`) и правка кода из дашборда — разные операции:
первая начинает счёт заново, вторая переносит claims на новый код и сохраняет
`used`. `db_claim_promos()` остаётся на `BEGIN IMMEDIATE` — это то, что не даёт
двум параллельным claim'ам превысить cap.

### Два вида кампании (`promo_campaign.mode`)

| | `shared` | `pool` |
|---|---|---|
| Что это | один код на всех | список уникальных кодов |
| Сколько активаций | `max_uses` на код | ровно 1 на код |
| Где коды | `promo_campaign.code` | `promo_pool` |
| Что значит «исчерпано» | `claimed >= max_uses` | свободных строк нет |

`shared` — исходное поведение и значение по умолчанию, поэтому все строки,
записанные до появления пула, продолжают работать без изменений.

`pool` — то, что имеет в виду партнёр, когда присылает список ваучеров:
`db_promo_pool_import()` грузит его из дашборда (`/partners` → «Загрузить
коды»), импорт аддитивный и идемпотентный — дубли пропускаются, поэтому список
можно донести повторно. Выдача идемпотентна **на партнёра**, а не на код:
пользователь не знает, какой из 200 кодов его, поэтому повторный запрос
возвращает уже выданный, а не тратит второй. Пустой пул ведёт себя ровно как
исчерпанный cap — партнёр молча исчезает из ответа.

Жизненный цикл (`is_active`, `is_archived`, принадлежность партнёру) в обоих
режимах живёт на строке `promo_campaign`, поэтому включение, выключение и
архивация работают одинаково; в `promo_pool` лежат только сами коды. Режимы взаимоисключающие, но только пока кампания **живая**:
`db_set_promo_code()` на партнёре с активным пулом и правка кода/лимита
pool-кампании отбиваются `ValueError`, иначе импортированные коды остались бы в
таблице, но никогда бы не выдавались. **Архивная** кампания считается
отсутствующей — её можно заменить кампанией другого типа, и новая создаётся
живой. Это то же правило, по которому уже работает `_apply_promo_patch`, и без
него кнопка «Удалить промокод» (мягкая архивация) вела в тупик: удалить
промокод — это ровно то, что просило сообщение об ошибке.

Каждая pool-выдача дублируется в `promo_claims`, чтобы воронка, счётчик за 7
дней и метрика уникальных пользователей видели её так же, как shared.

## In-memory состояние (config.py)

Всё живёт в памяти worker-процесса и теряется при рестарте:

| Структура | Назначение |
|---|---|
| `msg_times`, `violations`, `blocked_until` | Rate-limit текста/дорогих колбэков и авто-блокировки |
| `nav_times` | Отдельный, щедрый бюджет rate-limit для навигации меню |
| `reg_step` | Шаг регистрации/онбординга пользователя |
| `live_subs` | Live-подписки `match_id → {uid}` (восстанавливаются из БД при старте) |
| `mostbet_cache` | Кэш списка матчей и коэффициентов Mostbet (TTL 900 с) |
| `demand_cache` | Кэш `db_match_demand()` по окну `days` (TTL 300 с) |
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

1. ~~Rate-limit не покрывает callback-кнопки.~~ **Исправлено.** Все
   callback-кнопки меню (спорт/день/страна/турнир/пагинация/назад) защищены
   `nav_guard` с отдельным щедрым бюджетом (`nav_times`, `NAV_RATE_MAX`/
   `NAV_RATE_WINDOW`) — листание не тормозится текстовым лимитом и не ведёт к
   авто-блоку; выбор матча — строгий `cb_guard` (+ in-flight lock). См.
   `handlers/utils.py`, `security.nav_rate_check`.
2. **Персистентность SQLite** зависит от volume на Railway (`BOT_DB_DIR`).
3. ~~Частичный фид Mostbet кэшируется на полный TTL при обрыве пагинации.~~
   **Исправлено (ранее этой сессии).** `_publish_generation`/
   `_merge_generations` в `mostbet.py` подмешивают хвост предыдущей генерации
   к частичному/подозрительно усечённому фетчу вместо публикации урезанного
   списка — единственный неизбежный пробел: самый первый холодный старт
   (`prev` пуст), когда подставить нечего.
4. ~~Двойная инъекция коэффициентов в меню-флоу.~~ **Исправлено (ранее этой
   сессии).** Флаг `context.user_data["odds_attached"]` (`handlers/forecast.py`)
   заставляет `_generate_forecast` пропустить повторный fuzzy-поиск матча,
   когда `_fm_match_run` уже вложил коэффициенты для этого же события;
   повторный поиск остаётся только для текстового/фото-флоу, где коэффициенты
   ещё не прикреплены. Покрыто тестами (`test_event_menu_snapshot.py`,
   `test_forecast_data_reliability.py`).
5. ~~`db_log_req` пишет `last_active` в локальном времени процесса.~~
   **Исправлено.** `last_active=datetime('now')` теперь считается SQLite
   (UTC), в той же шкале, что `joined_at`/`requests.created_at` и запросы
   сегментации в `stats_server.py` — раньше `datetime.now()` на стороне
   Python писал локальное время процесса, что могло сдвигать сегменты
   активности на сутки в зависимости от таймзоны сервера.
6. **Синхронные вызовы SQLite внутри async-хендлеров** — при большой
   нагрузке могут подтормаживать event loop (сейчас запросы короткие, WAL).
7. **In-memory rate-limit/блокировки** сбрасываются при каждом рестарте.
8. **`priority_score` пересчитывается на каждый запрос меню**, а не раз за
   цикл обновления фида (каждые 15 мин, как `_preload_mostbet`) — дёшево при
   текущих объёмах, но не масштабируется линейно с ростом DAU.
9. ~~`db_match_demand()` — полный скан двух таблиц на каждое открытие меню.~~
   **Исправлено.** Кэшируется в `config.demand_cache` (TTL 5 мин, ключ —
   окно `days`) — сигнал грубый и логарифмически ограничен (макс. 5 очков
   в `priority_score`), несколько минут устаревания результат не меняют.
   В отличие от `time_proximity` (риск №8), кэшировать этот компонент
   безопасно — поэтому фиксируется отдельно от риска №8.
10. ~~Тиры престижа/популярности в `priority_config.py` — статический
    список только для футбола.~~ **Исправлено.** Добавлены tier 1/2 для
    тенниса (турниры Большого шлема, ATP/WTA Finals, Masters 1000),
    баскетбола (NBA, EuroLeague) и MMA (UFC, Bellator) — консервативный
    список только общеизвестных турниров/имён, без выдумывания значимости.
