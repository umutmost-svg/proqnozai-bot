# История прогнозов и основа для оценки качества — технический план

Статус: **реализовано в M3.5 с изменениями** — см. `m35-goal-model.md`, раздел 7.
Отличия от плана ниже: отдельный файл `<db>.forecasts.db` вместо таблицы в базе
Mini App (откат на M3.4 не ломается), записываются прогнозы статистической модели
`goals-1` (все рынки), сверка — из уже загружаемых календарей без фоновой задачи и
без новых запросов. Ниже — исходный план M3.4 для истории решений.

## Что уже есть и переиспользуется

| Есть сейчас | Где | Как используется в плане |
|---|---|---|
| Структурный ответ модели: 1/X/2, факторы, `pick` (рынок + причины) | `miniapp/ai/analysis.py`, `miniapp/ai/pick.py` | сохраняется как есть, без пересчёта |
| Проверенный `basis` (форма, голы, очки, таблица) — ровно то, что видела модель | `AnalysisResult.basis` | snapshot входных данных |
| Ключ кеша `ai:v2:{event}:{digest}` — digest входных фактов | `AiService.analyse` | уникальность записи |
| Время создания `generatedAt`, модель, ответившая на самом деле (`response.model`) | `AnalysisResult` | поля записи |
| Готовность READY / PARTIAL / INSUFFICIENT | `readiness.py` | поле записи (INSUFFICIENT не пишется — модели не было) |
| Сезонные календари турниров с финальными счетами (один запрос на турнир, общий кеш) | `SportsService.calendar` | сверка без запросов по одному матчу |
| Отдельная SQLite Mini App, `ai_usage`, атомарные транзакции, `restore_from` с белым списком таблиц | `miniapp/store.py` | новая таблица + её добавление в `_OWN_TABLES` |

Сейчас кеш разборов **только в памяти** (6 часов) — после перезапуска история
теряется. Это и закрывает план.

## Таблица `ai_forecasts` (аддитивно, `CREATE TABLE IF NOT EXISTS`)

```sql
CREATE TABLE IF NOT EXISTS ai_forecasts (
    forecast_key     TEXT PRIMARY KEY,   -- 'v2:fd:560643:<digest>' = ключ кеша без 'ai:'
    event_id         TEXT NOT NULL,      -- 'fd:560643'
    tournament_id    TEXT NOT NULL,      -- 'fd:PL'
    home_team_id     TEXT NOT NULL,      -- 'fd:team:354'
    away_team_id     TEXT NOT NULL,
    created_at       TEXT NOT NULL,      -- ISO UTC, момент ответа модели
    kickoff_at       TEXT NOT NULL,      -- ISO UTC из события на момент прогноза
    readiness        TEXT NOT NULL CHECK (readiness IN ('ready', 'partial')),
    p_home           INTEGER NOT NULL, p_draw INTEGER NOT NULL, p_away INTEGER NOT NULL,
    pick_market      TEXT NOT NULL,      -- '1'|'X'|'2'|'1X'|'X2'|'12'|'NO_BET' (после проверки правил)
    pick_reasons     TEXT NOT NULL,      -- JSON-массив кодов
    factors          TEXT NOT NULL,      -- JSON-массив ключей
    basis_json       TEXT NOT NULL,      -- проверенные входные цифры (snapshot)
    schema_version   TEXT NOT NULL,      -- 'v2' (форма ответа)
    rules_version    TEXT NOT NULL,      -- версия правил pick.py / evidence.py
    model            TEXT NOT NULL,      -- модель, ответившая на самом деле
    source           TEXT NOT NULL,      -- 'football-data.org'
    -- Заполняется сверкой, никогда не трогает поля выше:
    outcome          TEXT CHECK (outcome IN ('1', 'X', '2')),
    final_score      TEXT,               -- '2:1' (основное время)
    settled_status   TEXT NOT NULL DEFAULT 'pending'
                     CHECK (settled_status IN ('pending', 'settled', 'void')),
    settled_at       TEXT,
    revision         INTEGER NOT NULL DEFAULT 0,  -- +1 при каждом исправлении итога источником
    CHECK (p_home + p_draw + p_away = 100),
    CHECK (created_at < kickoff_at)
);
CREATE INDEX IF NOT EXISTS ai_forecasts_pending ON ai_forecasts (settled_status, kickoff_at);
```

- **Без пользователя.** Ни `telegram_user_id`, ни сессии: качество прогноза не
  зависит от того, кто его открыл (п. 10).
- **Исправленные результаты.** Источник иногда исправляет счёт или позже
  присуждает результат (`AWARDED`). Колонки прогноза при этом не меняются;
  меняется только итог: сверка повторно проверяет settled-записи последних
  30 дней по тем же календарям, и при расхождении пишет новый итог в
  `outcome` / `final_score` / `settled_status` (присуждённый → `void`),
  увеличивает `revision` и кладёт прежний итог в `ai_forecast_settlements`
  (журнал: forecast_key, revision, outcome, final_score, settled_status,
  recorded_at). Метрики всегда считаются по последней ревизии.
- **Неизменяемость.** Запись делается `INSERT OR IGNORE` в момент ответа модели
  (тот же код, что кладёт ответ в кеш, ДО начала матча — модель и так не
  вызывается позже чем за 5 минут до старта). Сверка обновляет только колонки
  `outcome`, `final_score`, `settled_status`, `settled_at`, `revision`
  (первичная сверка `pending` → итог; исправление — см. выше, с журналом). Триггер `BEFORE UPDATE` запрещает менять
  вероятности, `pick_*`, `basis_json`, `created_at`, `kickoff_at` (п. 4).
- **Без дублей.** Повторное открытие того же кеша не вызывает модель и не пишет
  ничего; новый digest (изменились входные факты до матча) — отдельная запись,
  у оценки остаётся только последняя запись события до `kickoff_at` (п. 5).

## Сверка результатов (п. 6, 7)

- Источник — уже кешируемые сезонные календари (`/competitions/{code}/matches`):
  один запрос на турнир покрывает все его матчи; запросы по одному матчу не
  нужны. Фоновая задача раз в N часов берёт `pending` с `kickoff_at` старше
  3 часов, группирует по турнирам и обновляет календарь через тот же кеш и тот
  же лимит клиента (≤ 1 турнир за 15 секунд, как прогрев поиска).
- `FINISHED` → `outcome` по счёту основного времени, `settled`.
  `AWARDED` → `void` (результат присуждён, не сыгран). `POSTPONED`,
  `SUSPENDED`, `CANCELLED` → остаётся `pending`, через 14 дней — `void`.
  Незавершённые и перенесённые матчи никогда не считаются проигрышем прогноза.

## Будущая оценка (п. 8) — только чтение таблицы

- **Brier score** по 1X2: `Σ (p_i − o_i)² / 3` на settled-записи.
- **Log loss**: `−ln p_outcome` с вероятностями ∈ [0,01; 0,99].
- **Калибровка**: корзины по 10 п.п. предсказанной вероятности против частоты.
- Срезы: турнир, `readiness`, `schema_version`, `rules_version`, `model`;
  отдельно — точность `pick_market` (без NO_BET) и доля NO_BET.
- Скрипт оценки — офлайн-команда администратора (как `miniapp.admin`), не API.

## Утечки будущего (п. 9)

`basis_json` фиксируется в момент прогноза; `created_at < kickoff_at`
проверяется CHECK; форма собирается только из `FINISHED`-матчей до запроса.
Результат пишется в отдельные колонки и в обучение/промпт не попадает.

## Миграция и откат (п. 11)

- Только `CREATE TABLE IF NOT EXISTS` + индекс + триггер — существующие
  таблицы не меняются; `_OWN_TABLES` и `restore_from` расширяются (merge по
  `forecast_key`, settled побеждает pending).
- Откат кода: старая версия просто не знает таблицу; `restore_from` старой
  версии отвергнет базу с неизвестной таблицей — поэтому перед откатом делается
  `miniapp.admin backup`, а сама таблица удаляется отдельной командой только по
  решению владельца.

## Оценка объёма

Backend: таблица + запись в `AiService` + фоновая сверка + admin-команды
backup/evaluate + тесты (≈ 1 день). Новых платных AI-вызовов нет; запросы к
football-data.org — не больше одного на турнир за проход сверки.
