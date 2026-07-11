# ProqnozAI — Telegram-бот AI-прогнозов на спорт

## О проекте

ProqnozAI — Telegram-бот на базе Anthropic Claude, который даёт пользователям
AI-прогнозы на спортивные матчи (футбол, UFC/MMA, баскетбол, теннис, хоккей).
Целевой рынок — Азербайджан, Турция, Казахстан, Узбекистан, арабские страны
и русскоязычная аудитория. Интерфейс — на 7 языках: `az, ru, en, tr, kz, uz, ar`.

Бот **не является букмекером** — только аналитика. Прогнозы носят
информационно-аналитический характер и не являются гарантией результата.
Реальные коэффициенты подтягиваются с Mostbet через партнёрский
Odds Checker API (доступ по IP whitelist).

## Стек

- **Язык:** Python 3.13
- **Telegram:** `python-telegram-bot==21.5`
- **AI:** Anthropic Claude API (`anthropic` SDK) — см. «Модели Claude» ниже
- **БД:** SQLite (`bot.db`, создаётся автоматически при первом запуске)
- **HTTP-клиент:** `httpx` (async)
- **Дашборд:** Flask
- **Хостинг:** Railway Pro (статичный outbound IP для whitelist Mostbet)

## Точки входа и процессы

Проект — **не монолит**: код разбит на модули (см. `ARCHITECTURE.md`).
Railway запускает два процесса из `Procfile`:

| Процесс | Команда | Назначение |
|---|---|---|
| `worker` | `python main.py` | Telegram-бот + фоновые задачи + внутренний stats-сервер (`stats_server.py`, порт `STATS_PORT`) |
| `web` | `python dashboard.py` | Flask-дашборд администратора (Basic Auth) |

Дашборд **не обращается к SQLite напрямую** — все данные он получает по HTTP
от stats-сервера воркера через приватную сеть Railway
(`BOT_API_URL`, по умолчанию `http://worker.railway.internal:8888`).

### Polling vs webhook

- Если задана переменная `WEBHOOK_URL` — бот стартует через
  `run_webhook()` на порту `PORT`.
- Иначе (по умолчанию) — long polling (`run_polling()`). Прод сейчас живёт
  на polling.

## Переменные окружения

Полный список с плейсхолдерами — в `.env.example`. Секреты хранятся только
в env (Railway → Variables), никогда в коде и репозитории.

| Переменная | Назначение | Обязательна |
|---|---|---|
| `TELEGRAM_TOKEN` | Токен бота от BotFather | Да (worker) |
| `ANTHROPIC_API_KEY` | Ключ Anthropic API | Да (worker) |
| `ADMIN_ID` | Telegram ID администратора (доступ к `/admin`) | Нет (без него админка недоступна) |
| `DASHBOARD_TOKEN` | Токен stats-API и пароль Basic Auth дашборда | Да для web/stats |
| `DASHBOARD_USER` | Логин Basic Auth дашборда (по умолчанию `admin`) | Нет |
| `BOT_DB_DIR` | Каталог для `bot.db` — **на Railway должен указывать на volume** | Нет (по умолчанию `.`) |
| `FOOTBALL_KEY` | football-data.org — форма команд текущих сезонов (предпочтительное имя) | Нет |
| `FOOTBALL_API_KEY` | Legacy-алиас `FOOTBALL_KEY`, читается как fallback | Нет |
| `APIFOOTBALL_KEY` | api-football.com (api-sports.io) — live-статусы, события, H2H | Нет |
| `WEBHOOK_URL` | Если задан — webhook вместо polling | Нет |
| `PORT` | Порт webhook-режима / Flask-дашборда | Нет |
| `STATS_PORT` | Порт внутреннего stats-сервера (по умолчанию 8888) | Нет |
| `BOT_API_URL` | Базовый URL stats-сервера для дашборда | Нет |
| `STATS_URL` | Полный URL `/stats` (переопределяет `BOT_API_URL`) | Нет |

Без `APIFOOTBALL_KEY` не работают live-уведомления и кнопка «Следить за
матчем». Без `FOOTBALL_KEY` форма команд оценивается моделью и помечается
как «(оценочно)». Mostbet API отдельного ключа не требует — доступ по IP.

### ⚠️ Персистентность SQLite на Railway

Файловая система контейнера Railway **эфемерна**: без volume каждый редеплой
стирает `bot.db` со всеми пользователями. `BOT_DB_DIR` обязан указывать на
примонтированный volume. По умолчанию (`.`) БД создаётся рядом с кодом —
это подходит только для локальной разработки.

## Модели Claude (фактический роутинг)

| Задача | Модель | Где |
|---|---|---|
| Прогноз матча (extended thinking, budget 2500; при отказе thinking — fallback на обычный вызов) | `claude-opus-4-8` | `claude_client.claude_forecast` |
| Оценка формы, когда реальные API пусты (помечается «(оценочно)») | `claude-opus-4-8` | `football_api._sonnet_form_estimate` |
| Экспресс-купоны | `claude-haiku-4-5-20251001` | `handlers/express.py` |
| Сравнение команд (`/compare`) | `claude-haiku-4-5-20251001` | `handlers/express.py` |
| Live-подсказки при событиях матча | `claude-haiku-4-5-20251001` | `claude_client.live_tip` |
| Перевод имён команд на английский | `claude-haiku-4-5-20251001` | `football_api._normalize_names` |
| Сопоставление имён с ростером football-data | `claude-haiku-4-5-20251001` | `football_api._fd_resolve_ai` |

Все вызовы идут через `_create_with_retry` (backoff на транзиентных
ошибках) под `asyncio.Semaphore(5)`.

## Mostbet Odds Checker API

- Базовый URL: `https://mostbet2.com`
- `GET /api/v3/advertiser/oddschecker/line/list` — список матчей
  (пагинация по `lastId`, лимит 100/страница)
- `GET /api/v3/advertiser/oddschecker/line/{lineId}/outcomes/list` — коэффициенты
- Доступ только с whitelisted IP.

### Кэширование

- Полный список матчей кэшируется в памяти (`mostbet_cache`) на
  `MOSTBET_CACHE_TTL` (900 сек) и прогревается фоновой задачей каждые 15 минут.
- Загрузка защищена `asyncio.Lock` — к Mostbet идёт только один
  конкурентный fetch, остальные ждут и переиспользуют результат.
- На 429 — retry с `Retry-After`; при исчерпании лимита ретраев загрузка
  останавливается и **частичный список кэшируется на полный TTL**
  (известный трейд-офф — см. риски в `ARCHITECTURE.md`). Если не собрано
  ничего — возвращается устаревший кэш.
- Коэффициенты матча кэшируются отдельно (`odds_{line_id}`, тот же TTL).
- Из выдачи фильтруются виртуальные/киберспортивные события и
  outright-рынки («победитель турнира», второй участник `?`).

## Локальный запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить TELEGRAM_TOKEN и ANTHROPIC_API_KEY
set -a && source .env && set +a
python main.py              # бот (polling)
python dashboard.py         # дашборд (отдельный терминал; нужен DASHBOARD_TOKEN)
```

## Тесты и линт

```bash
pip install -r requirements-dev.txt

pytest tests/               # офлайн-юнит-тесты (без сети и секретов)
ruff check .                # линт (постепенная конфигурация, см. pyproject.toml)
python -m compileall .      # проверка компиляции

python test_e2e.py          # РУЧНОЙ интеграционный прогон: ходит в реальные
                            # Mostbet/Anthropic API, требует ANTHROPIC_API_KEY.
                            # В CI не запускается.
```

CI (GitHub Actions, `.github/workflows/ci.yml`) на каждый PR/push в main:
установка зависимостей → `compileall` → проверка Procfile → `ruff` →
`pytest tests/`. CI не требует продовых секретов и не ходит во внешние API.

## Дисклеймер

Бот предоставляет **только аналитику**. Прогнозы генерируются AI-моделью на
основе доступных данных и не являются финансовой рекомендацией или
гарантией исхода. Бот не принимает ставки и не работает с деньгами.
