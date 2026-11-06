# Mini App: авторизация Telegram и сессия (M2)

Backend Mini App — отдельный Python-пакет `miniapp/` (FastAPI) со своей
SQLite-базой, своим окружением (`miniapp/.venv`) и своими зависимостями
(`miniapp/requirements*.txt`). Он не импортирует модули бота (`config`, `db`,
`handlers`), не запускает бота и не читает `.env`. С ботом его связывает
только токен бота, нужный для проверки `initData`.

```
miniapp/
  settings.py       # конфигурация из MINIAPP_* (явно, не при импорте)
  telegram_auth.py  # проверка initData (HMAC-SHA256)
  store.py          # SQLite: users, sessions, consumed_init_data
  security.py       # Origin, CSRF, rate limit
  app.py            # create_app(settings, clock=..., store=...)
  asgi.py           # create_app_from_env() для uvicorn --factory
  tests/            # офлайн-тесты, только временная БД
```

Импорт пакета ничего не создаёт и не читает. Это проверяет тест
`test_importing_package_has_no_side_effects`. Приложение, настройки, путь к
БД и часы передаются в `create_app()` явно и подменяются в тестах.

## Поток входа

```
Telegram ──initData──▶ frontend
frontend  GET  /api/me                      (cookie, если есть)
           ├─ 200 → вход подтверждён, initData не отправляется
           └─ 401 → POST /api/auth/session {"init_data": "<raw>"}   (один раз)
                       backend: Origin → rate limit → строгий JSON →
                                HMAC → auth_date → user → upsert → сессия
                       ◀── 200 + Set-Cookie (HttpOnly)
```

Raw `Telegram.WebApp.initData` уходит только в теле POST, никогда в URL.
`initDataUnsafe`, `user_id` из query, заголовков, localStorage и сам факт
наличия SDK в расчёт не принимаются: `user_id` берётся только из проверенной
подписи.

## Проверка initData

Источник — официальная документация:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

- `secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)`.
- data-check-string: все полученные поля, кроме `hash`, в виде
  `key=<value>`, отсортированные по ключу и разделённые `\n`. Значения
  берутся URL-декодированными и **как получены**: JSON `user` не
  пересериализуется (это проверяет тест `test_user_json_is_not_reserialized`).
- `hash` сравнивается через `hmac.compare_digest`.
- **`signature`.** Это поле — Ed25519-подпись для проверки третьими
  сторонами, отдельный алгоритм с публичным ключом Telegram. Мы его не
  проверяем и в HMAC-проверке не обрабатываем особо. Как и любое другое
  полученное поле, оно входит в data-check-string; исключается только `hash`.
  Тесты: эталонный вектор с `signature` и вектор, где `signature` удалён после
  подписи — подпись тогда не сходится.

Отклоняется:

| Проверка | Код |
|---|---|
| пустая строка; длиннее 4096 символов | `empty`, `too_large` |
| часть без `=`, пустой ключ, ключ не `[A-Za-z0-9_]`, битое `%`-кодирование, невалидный UTF-8, перевод строки в значении | `malformed` |
| повтор параметра, в том числе после декодирования (`auth_date` и `auth%5Fdate`) | `duplicate_field` |
| `hash` отсутствует или не 64 строчных hex-символа | `bad_hash_format` |
| подпись не сходится (подмена поля, другой токен бота) | `bad_signature` |
| нет `auth_date` / не целое неотрицательное число | `missing_auth_date` / `bad_auth_date` |
| `now − auth_date > 300 с` | `expired` |
| `auth_date − now > 30 с` | `from_future` |
| нет `user`, невалидный JSON, дубли ключей, `NaN`; `id` — bool, дробь, строка, ≤ 0, > 2⁵³−1; неверные типы полей; `is_bot: true` | `missing_user` / `bad_user` |

Наружу (HTTP 401) уходят только `init_data_invalid` или `init_data_expired`.
В лог пишется только код причины. initData, токен бота, cookie и данные
пользователя не попадают ни в исключения, ни в логи, ни в ответы.

Хотя бы один эталон проверен независимо: хэш в `REFERENCE_HASH` посчитан
`openssl dgst -sha256 -mac HMAC`, а не Python-хелпером. Тестовый подписчик
`miniapp/tests/signing.py` написан отдельно от `compute_hash` и существует
только в test runner.

## Настройки (`MINIAPP_*`)

Пример — в `miniapp/.env.example` (только плейсхолдеры). 300 и 30 секунд —
политика приложения, а не требования Telegram.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `MINIAPP_BOT_TOKEN` | — (обязательна) | токен бота для HMAC |
| `MINIAPP_DB_PATH` | — (обязательна) | файл SQLite Mini App; `bot.db*` запрещён |
| `MINIAPP_TRUSTED_ORIGINS` | — (обязательна) | точные `scheme://host[:port]` через запятую; `*`, `null` и пути запрещены |
| `MINIAPP_ENV` | `production` | `development` разрешён только с loopback-origin по http |
| `MINIAPP_INIT_DATA_MAX_AGE_S` | `300` | максимальный возраст initData при обмене |
| `MINIAPP_INIT_DATA_FUTURE_SKEW_S` | `30` | допустимое расхождение часов «в будущее» |
| `MINIAPP_SESSION_TTL_S` | `43200` (12 ч) | срок сессии |
| `MINIAPP_LOGIN_RATE_LIMIT` / `_WINDOW_S` | `10` / `60` | попыток входа с одного адреса за окно |
| `MINIAPP_COOKIE_SAMESITE` | `lax` | `lax` / `strict` / `none` (`none` только с Secure) |

Ошибочная конфигурация останавливает запуск (`SettingsError`). Значения
секретов в сообщения не попадают.

## Сессия

- Токен — `secrets.token_urlsafe(32)`, то есть 32 байта
  криптографической случайности.
- В БД хранится только `sha256(token)`, `user_id`, `created_at`,
  `expires_at`, `revoked_at` и `sha256(hash initData)`. Тест
  `test_store_keeps_only_token_hash` проверяет, что raw-токена нет в файлах
  БД и WAL.
- Raw-токен передаётся только в cookie:
  - production: `__Host-sporai_session`; `Secure; HttpOnly; SameSite=Lax;
    Path=/`, без `Domain` (host-only, это гарантирует префикс `__Host-`);
  - development (http на loopback): `sporai_session` без `Secure`, остальное
    то же.
- Срок фиксированный: `MINIAPP_SESSION_TTL_S` от создания, без продления.
- JWT и refresh-токенов нет.
- Frontend не хранит ни токен, ни initData ни в localStorage, ни в
  sessionStorage. Это проверяют unit- и e2e-тесты.
- `csrf_token` в ответах — производная `HMAC(key(bot_token), session_token)`.
  Он не хранится в БД, привязан к сессии и без HttpOnly-cookie бесполезен.
  Frontend держит его только в памяти.

## API

Все ответы `/api/*` идут с `Cache-Control: no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. Ошибки
имеют вид `{"error": "<code>"}` и не повторяют входные данные. `/docs` и
`/openapi.json` отключены.

| Метод и путь | Защита | Ответ |
|---|---|---|
| `GET /api/health` | — | `{"status":"ok"}` |
| `POST /api/auth/session` | Origin, rate limit, `Content-Type: application/json`, ≤ 8 КиБ, тело ровно `{"init_data": "<string>"}` | 200 `{user, session:{expires_at, csrf_token}, created}` + cookie · 400 · 401 `init_data_invalid`/`init_data_expired`/`init_data_reused` · 403 `origin_not_allowed` · 413 · 415 · 429 + `Retry-After` |
| `GET /api/me` | cookie | 200 `{user, session}` · 401 `unauthenticated`/`session_expired` (cookie очищается) |
| `POST /api/me/language` | Origin, cookie, `X-CSRF-Token`, тело `{"lang":"az"\|"ru"\|"en"}` | 200 · 401 · 403 `csrf_failed` · 400 |
| `POST /api/auth/logout` | Origin, `X-CSRF-Token` при живой сессии | 200 `{"ok":true}` + очистка cookie |

`user` в ответе — `{id, first_name, last_name, username, lang, lang_source}`.
`/api/me` отдаёт только профиль сессии из cookie. Параметры `?user_id=` и
заголовки вроде `X-User-Id` игнорируются (тест
`test_cannot_read_other_users_profile`). Через GET состояние не меняется:
`GET /api/auth/logout` и `GET /api/auth/session` отвечают 405.

## Угрозы и меры

| Угроза | Мера |
|---|---|
| Подделка личности | HMAC по токену бота; `user_id` только из подписанных данных |
| Перехват и повтор initData | короткое окно 300 с; запрет повторного обмена после logout; initData не логируется; TLS в production. Внутри окна это **не полная** защита от повтора (см. ниже). |
| Login CSRF (вход жертвы в чужой аккаунт) | точная проверка `Origin` (схема, хост, порт; `null` и отсутствие отклоняются) и строгий JSON-контракт: кросс-сайтовая форма не может отправить `application/json` без CORS preflight, а preflight мы не одобряем |
| CSRF на изменения под cookie | Origin + `X-CSRF-Token`, привязанный к сессии (logout, язык) |
| CORS | CORS-middleware нет, `Access-Control-Allow-*` не отдаются (тест). Wildcard с credentials невозможен; `*` в trusted origins запрещён настройками |
| Подмена `X-Forwarded-*` | не используются. uvicorn запускается с `--no-proxy-headers`; rate limit считается по адресу TCP-пира |
| Перебор и злоупотребление входом | лимит 10 попыток в минуту с адреса, ответ 429. **Счётчики в памяти одного процесса**: при нескольких воркерах или репликах у каждого свои, это не распределённая защита. За прокси все клиенты видны с одного адреса, поэтому для production нужна настройка доверенного прокси (M5) |
| Большие payload | `Content-Length` и потоковое чтение ограничены 8 КиБ; initData — 4096 символов |
| Утечка из БД | хранятся только хэши токенов; без cookie по хэшу сессию не восстановить |
| Кэширование персональных ответов | `no-store` на всех `/api/*` |

## Повторные запросы: принятая политика

1. **Повторный bootstrap при живой сессии идемпотентен.** Если в запросе есть
   действующая cookie того же пользователя, новая сессия не создаётся:
   возвращается текущая (`created: false`), cookie переустанавливается с
   оставшимся сроком.
2. **Другой пользователь на том же устройстве.** Старая сессия отзывается,
   создаётся новая.
3. **Конкурентные входы** не создают дублей пользователя: atomic upsert
   `INSERT … ON CONFLICT(telegram_user_id)`, `telegram_user_id` — PRIMARY
   KEY. Проверено 8 параллельными запросами и 16 потоками. Параллельные
   обмены без cookie могут создать несколько сессий одного пользователя. Это
   допустимо: frontend выполняет bootstrap ровно один раз.
4. **Повтор того же initData внутри окна 300 с** (без cookie) создаёт ещё
   одну сессию того же пользователя. Ограничение возраста **не является**
   полной защитой от replay. Тот, у кого есть initData, может войти до
   истечения окна. Одноразовым initData не сделан намеренно: Telegram отдаёт
   один и тот же initData на всё время жизни открытого Mini App, и
   одноразовость сломала бы перезагрузку страницы при недоступной cookie.
5. **После logout** `hash` этого initData заносится в `consumed_init_data`
   на `max_age + skew` секунд. Повторный обмен даёт 401 `init_data_reused`.
   Frontend после logout тоже не входит заново автоматически: состояние
   `signed_out` до повторного открытия Mini App, когда Telegram выдаёт новый
   initData.

## Пользователи и язык

- Запись в `users` создаётся только после успешной проверки подписи.
- `lang` — `az`/`ru`/`en`. При первом входе он берётся из
  `language_code` Telegram (иначе `ru`), `lang_source = 'telegram'`.
- После явного выбора (`POST /api/me/language`, который вызывается при
  сохранении языка в профиле) `lang_source = 'user'`. Последующие входы
  обновляют имя и username, но язык не перезаписывают.
- Остальные настройки M1 (пояс, анимации) остаются локальными. Синхронизации
  профиля, избранного и истории в M2 нет.
- База Mini App — отдельный файл. Путь `bot.db*` запрещён настройками.
  Тесты используют только `tmp_path`.

## Frontend

`src/auth/` — `api.ts` (HTTP-клиент), `controller.ts` (машина состояний),
`useAuth.ts`. Контроллер создаётся один раз в `main.tsx`, вне React, поэтому
StrictMode и повторные эффекты не запускают второй bootstrap.

| Состояние | Когда | Действие пользователя |
|---|---|---|
| `checking` | проверка или повторная попытка | — |
| `authenticated` | `/api/me` 200 или успешный обмен | «Выйти» |
| `outside_telegram` | нет initData (браузер); демо-режим M1 без входа | — |
| `init_data_invalid` | подпись или формат отклонены | открыть Mini App заново |
| `init_data_expired` | initData старше окна | открыть заново |
| `session_expired` | cookie истекла и обновить её нечем | открыть заново |
| `unavailable` | сеть, 5xx или не наш ответ (в Telegram) | «Повторить» |
| `rate_limited` | 429 | «Повторить» |
| `signed_out` | после logout или `init_data_reused` | открыть заново |

Автоматических повторов нет. «Повторить» доступно только для `unavailable`
и `rate_limited`, параллельные нажатия объединяются. Ошибка входа никогда не
превращается во вход демо-пользователя. Признак «личность подтверждена» не
зависит от признака «данные демонстрационные»: после входа события и
AI-разбор остаются с отметкой «Демо». Обходов (`DEV_AUTH_BYPASS`,
`?user_id=`, тестового login endpoint) нет.

## Локальный запуск

```bash
# backend (один раз: отдельное окружение, без глобальной установки)
/path/to/python3.13 -m venv miniapp/.venv
miniapp/.venv/bin/pip install -r miniapp/requirements-dev.txt

# тесты backend (из miniapp/)
cd miniapp && .venv/bin/python -m pytest

# запуск API на loopback (из корня репозитория)
MINIAPP_ENV=development \
MINIAPP_BOT_TOKEN='<токен тестового бота>' \
MINIAPP_DB_PATH=/tmp/sporai-miniapp-dev.db \
MINIAPP_TRUSTED_ORIGINS=http://127.0.0.1:5173 \
miniapp/.venv/bin/python -m uvicorn --factory miniapp.asgi:create_app_from_env \
  --host 127.0.0.1 --port 8000 --no-proxy-headers

# frontend: Vite проксирует /api → 127.0.0.1:8000 (тот же origin)
cd frontend && npm run dev     # http://127.0.0.1:5173/

# сквозная проверка auth: настоящий backend + preview, initData подписан в тесте
cd frontend && npm run test:e2e:auth
```

В обычном браузере initData нет, поэтому профиль показывает «Открыто вне
Telegram». Реальный вход возможен только внутри Telegram, а для этого нужен
HTTPS-адрес. Публичные туннели в M2 не используются.

## Что проверено и что нет

Проверено **только тестовыми данными**:
- backend-тесты с initData, подписанным тестовым токеном в test runner,
  плюс эталон, посчитанный через openssl;
- сквозная проверка Playwright в локальном Google Chrome: заглушка
  `window.Telegram.WebApp` с подписанным тестовым initData, настоящий
  backend, cookie через Vite preview proxy на `http://127.0.0.1`.

**Не проверено** и требует запуска внутри Telegram:
- настоящий initData от Telegram-клиентов (поля, кодирование, `signature`);
- cookie в WebView **Telegram iOS** (WKWebView) и **Android** (WebView):
  сохранение HttpOnly-cookie между открытиями, поведение при сворачивании;
- **Telegram Web** (web.telegram.org, Mini App в cross-site iframe): cookie
  `SameSite=Lax` в cross-site iframe не отправляются. Вероятно, понадобятся
  `MINIAPP_COOKIE_SAMESITE=none` (Secure + Partitioned/CHIPS), а Safari может
  блокировать такие cookie совсем. Совместимость **не заявляется** и не
  выводится из проверки в локальном Chrome. Ослаблять настройки ради неё
  без отдельного решения нельзя;
- поведение за production-прокси (адрес клиента для rate limit, HTTPS,
  `__Host-` cookie) и в нескольких воркерах.

## Docker

`Dockerfile` бота копирует весь контекст (`COPY . .`). `.dockerignore`
исключает категории:

| Категория | Шаблоны |
|---|---|
| Секреты и локальные env | `.env`, `.env.*`, `**/.env*`, `*.pem`, `*.key`, `*.p12`, `secrets/`, `.claude/` (кроме `.env.example`) |
| Зависимости и окружения | `**/node_modules/`, `.venv/`, `venv/`, `**/.venv/` |
| Базы | `**/*.db`, `*.db-wal`, `*.db-shm`, `*.db-journal`, `*.sqlite*`, `*.bak`, `*.backup`, `backups/` |
| VCS | `.git/`, `.gitignore` |
| Кэши, логи, результаты | `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, coverage, `*.log`, `frontend/dist`, `frontend/test-results`, `frontend/playwright-report`, `.DS_Store` |

Фактический контекст проверен сборкой временного образа `FROM scratch` +
`COPY . /` локальным Docker (`DOCKER_BUILDKIT=0`), выгрузкой списка файлов
(`docker export`) и удалением образа. В контексте 261 запись: исходники бота,
`tests/`, `miniapp/` (17 файлов, без `.venv`), `frontend/` (исходники и
`package-lock.json`, без `node_modules`/`dist`/`test-results`), `docs/`,
manifests, оба `.env.example`. Не попали `.env`, `.venv`,
`miniapp/.venv`, `frontend/node_modules`, `.git`, `bot.log`,
`suspicious.log`, `__pycache__`, кэши.

Схема production-сборки (Dockerfile, Procfile, Railway) не менялась.
Mini App backend в production не развёрнут. Отдельный сервис и его сборка —
задача M5.
