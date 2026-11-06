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
  security.py       # Origin, CSRF, лимит входов
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
                       backend: Origin → строгий JSON → HMAC → auth_date →
                                user → лимит по user_id → upsert → сессия
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
политика приложения, а не требования Telegram. У окон есть верхние пределы:
значение выше предела останавливает запуск, а не расширяет окно повтора молча.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `MINIAPP_BOT_TOKEN` | — (обязательна) | токен бота для HMAC |
| `MINIAPP_DB_PATH` | — (обязательна) | файл SQLite Mini App; `bot.db*` запрещён (см. «База данных») |
| `MINIAPP_TRUSTED_ORIGINS` | — (обязательна) | точные `scheme://host[:port]` через запятую; `*`, `null` и пути запрещены |
| `MINIAPP_ENV` | `production` | `development` разрешён только с loopback-origin по http |
| `MINIAPP_INIT_DATA_MAX_AGE_S` | `300` | максимальный возраст initData при обмене; не больше 3600 |
| `MINIAPP_INIT_DATA_FUTURE_SKEW_S` | `30` | допустимое расхождение часов «в будущее»; 0…300 |
| `MINIAPP_SESSION_TTL_S` | `43200` (12 ч) | срок сессии; не больше 604800 (7 дней) |
| `MINIAPP_LOGIN_RATE_LIMIT` / `_WINDOW_S` | `10` / `60` | входов с **проверенной подписью** одного Telegram-пользователя за окно |
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
  sessionStorage. Это проверяют unit- и e2e-тесты. Единственная запись —
  SHA-256 от initData текущего запуска в `sessionStorage` после logout (см.
  «Logout»); по ней initData не восстановить и не предъявить.
- `csrf_token` в ответах — производная `HMAC(key(bot_token), session_token)`.
  Он не хранится в БД, привязан к сессии и без HttpOnly-cookie бесполезен.
  Frontend держит его только в памяти.

## API

Все ответы `/api/*`, включая 404 и 500, идут с `Cache-Control: no-store`,
`Pragma: no-cache`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` и `X-Request-ID` (16 hex, генерирует сервер;
присланный клиентом заголовок игнорируется). Ошибки имеют вид
`{"error": "<code>"}` и не повторяют входные данные. `/docs` и
`/openapi.json` отключены.

| Метод и путь | Защита | Ответ |
|---|---|---|
| `GET /api/health` | — | `{"status":"ok"}` |
| `POST /api/auth/session` | Origin, `Content-Type: application/json`, ≤ 8 КиБ, тело ровно `{"init_data": "<string>"}`, лимит по проверенному `user.id` | 200 `{user, session:{expires_at, csrf_token}, created}` + cookie · 400 · 401 `init_data_invalid`/`init_data_expired`/`init_data_reused` · 403 `origin_not_allowed` · 413 · 415 · 429 + `Retry-After` |
| `GET /api/me` | cookie | 200 `{user, session}` · 401 `unauthenticated`/`session_expired` (cookie очищается) |
| `POST /api/me/language` | Origin, cookie, `X-CSRF-Token`, тело `{"lang":"az"\|"ru"\|"en"}` | 200 · 401 · 403 `csrf_failed` · 400 |
| `POST /api/auth/logout` | Origin, `X-CSRF-Token` при неотозванной сессии в cookie; тело необязательно: `{"init_data": "<текущий запуск>"}` (тогда `application/json`, ≤ 8 КиБ) | 200 `{"ok":true}` + очистка cookie · 400 · 403 `csrf_failed` · 415 |
| любой | — | 500 `{"error":"internal","request_id":"<id>"}` при непредвиденной ошибке |

`user` в ответе — `{id, first_name, last_name, username, lang, lang_source}`.
`/api/me` отдаёт только профиль сессии из cookie. Параметры `?user_id=` и
заголовки вроде `X-User-Id` игнорируются (тест
`test_cannot_read_other_users_profile`). Через GET состояние не меняется:
`GET /api/auth/logout` и `GET /api/auth/session` отвечают 405.

## Угрозы и меры

| Угроза | Мера |
|---|---|
| Подделка личности | HMAC по токену бота; `user_id` только из подписанных данных |
| Перехват и повтор initData | короткое окно 300 с; сжигание initData при logout (см. «Logout»); initData не логируется; TLS в production. Внутри окна это **не полная** защита от повтора (см. ниже). |
| Login CSRF (вход жертвы в чужой аккаунт) | точная проверка `Origin` (схема, хост, порт; `null` и отсутствие отклоняются) и строгий JSON-контракт: кросс-сайтовая форма не может отправить `application/json` без CORS preflight, а preflight мы не одобряем |
| CSRF на изменения под cookie | Origin + `X-CSRF-Token`, привязанный к сессии (logout, язык) |
| CORS | CORS-middleware нет, `Access-Control-Allow-*` не отдаются (тест). Wildcard с credentials невозможен; `*` в trusted origins запрещён настройками |
| Подмена `X-Forwarded-*` | приложение не использует ни адрес клиента, ни `X-Forwarded-*`/`X-Real-IP`. uvicorn запускается с `--no-proxy-headers`, без `--forwarded-allow-ips`. Реальный IP знает только edge-прокси (см. «HTTPS-стенд») |
| Злоупотребление входом | лимит в приложении — 10 входов в минуту на **проверенного** Telegram-пользователя, после проверки подписи, ответ 429. Неподписанные запросы этот бюджет не тратят, а неподписанный `user_id` на него не влияет. Общего лимита по адресу в приложении нет: за прокси адрес общий, и он блокировал бы всех. Поток мусорных запросов (их проверка — один HMAC без обращения к БД) режется на edge-прокси по реальному IP. Счётчики в памяти одного процесса: при нескольких воркерах у каждого свои |
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
5. **Logout** — см. следующий раздел.

## Logout

Здесь два разных механизма, и они решают разные задачи.

**Запрет автоматического повторного входа (клиент).** После logout
frontend записывает в `sessionStorage` SHA-256 от initData текущего запуска.
При загрузке страницы, если initData совпадает с отметкой, bootstrap не
выполняется вовсе: ни `/api/me`, ни обмена initData. Перезагрузка WebView
оставляет `signed_out`. Отметка привязана к запуску: повторно открытый Mini
App получает от Telegram новый initData, отметка не совпадает, вход идёт
обычным путём. Поэтому блокировка не бывает бесконечной. Отметка требует
`sessionStorage` и WebCrypto (HTTPS-контекст). Если их нет, страница после
перезагрузки делает один запрос обмена; сервер отвечает `init_data_reused`
(launch сожжён), сессия не создаётся, состояние — `signed_out`. Гарантия «без
входа» сохраняется, гарантия «без запросов» — только при доступном хранилище.
Явный повторный вход —
это повторное открытие Mini App; в состоянии `signed_out` есть кнопка
«Закрыть Mini App» (`Telegram.WebApp.close()`). Войти заново в том же запуске
нельзя намеренно: свежих данных запуска, кроме сожжённых, у страницы нет.

**Защита от повторного обмена (сервер).** `POST /api/auth/logout` отзывает
сессию и сжигает в `consumed_init_data`:
- `hash` initData, по которому сессия была создана;
- `hash` initData текущего запуска, если frontend передал его в теле. Он
  может отличаться от первого: Mini App открыли снова, а сессия
  переиспользовалась по cookie. Сжигается, только если подпись верна,
  initData ещё свежий и принадлежит тому же пользователю, что и сессия.
  Невалидный или устаревший initData не мешает выходу: обменять его нельзя и
  так.

Logout работает и для истёкшей, но не отозванной сессии (она всё ещё
указывает на пользователя, CSRF-токен проверяется), чтобы текущий запуск
сжигался и в этом случае.

Если cookie нет вовсе (браузер удаляет её по `Max-Age`, а страница пережила
сессию), logout всё равно сжигает переданный initData, если подпись верна и он
свежий. CSRF-токена здесь нет. Основание — сам подписанный initData: его
владелец мог бы им и войти, а сжечь он может только этот один запуск.
`Origin` проверяется по-прежнему.

Проверка «сожжён ли initData» при входе выполняется в той же транзакции
`BEGIN IMMEDIATE`, что и создание сессии: вход, идущий одновременно с logout,
либо успевает до logout (как любой вход в окне), либо получает
`init_data_reused`. Сожжённый initData отклоняется до лимита входов и не
тратит бюджет пользователя.

Запись живёт `max_age + skew` секунд: к этому моменту сожжённый initData
истекает сам. Повторный обмен в этот срок даёт 401 `init_data_reused`.
Серверная мера не зависит от клиента: она срабатывает и при очищенном
`sessionStorage`. Но она не защищает от того, кто получил initData до logout
(см. п. 4 выше): это ограничение окна, а не logout.

## Пользователи и язык

- Запись в `users` создаётся только после успешной проверки подписи.
- `lang` — `az`/`ru`/`en`. При первом входе он берётся из
  `language_code` Telegram (иначе `ru`), `lang_source = 'telegram'`.
- После явного выбора (`POST /api/me/language`, который вызывается при
  сохранении языка в профиле) `lang_source = 'user'`. Последующие входы
  обновляют имя и username, но язык не перезаписывают.
- Остальные настройки M1 (пояс, анимации) остаются локальными. Синхронизации
  профиля, избранного и истории в M2 нет.
- База Mini App — отдельный файл. Тесты используют только `tmp_path`.

## База данных

- `MINIAPP_DB_PATH` проверяется дважды по имени: как задано и после
  `resolve()`, то есть с раскрытыми symlink самого файла и родительских
  каталогов. Имя `bot.db*` запрещено без учёта регистра (`BOT.DB` на
  регистронезависимой ФС macOS — тот же файл).
- Жёсткую ссылку или копию `bot.db` под другим именем по пути не распознать.
  Поэтому `Store.init_schema()` до любой записи читает список таблиц и
  отказывается (`ForeignDatabaseError`) работать с файлом, где есть таблицы
  кроме `users`, `sessions`, `consumed_init_data`. Чужой файл остаётся
  нетронутым, в том числе без переключения в WAL.
- Пустой файл чужих таблиц не содержит, поэтому жёсткая ссылка на ещё пустой
  `bot.db` проверку таблиц прошла бы. Отдельно отклоняется любой существующий
  файл с несколькими жёсткими ссылками (`st_nlink > 1`); у собственного файла
  Mini App их нет.
- Просроченные записи `consumed_init_data` удаляются при каждой записи в
  таблицу (logout с сессией и без неё) и при создании сессии, поэтому таблица
  ограничена числом сжиганий за `max_age + skew`.

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
| `signed_out` | после logout (в том числе после перезагрузки в том же запуске) или `init_data_reused` | «Закрыть Mini App» и открыть заново |

Автоматических повторов нет. «Повторить» доступно только для `unavailable`
и `rate_limited`, параллельные нажатия объединяются. Ошибка входа никогда не
превращается во вход демо-пользователя. Признак «личность подтверждена» не
зависит от признака «данные демонстрационные»: после входа события и
AI-разбор остаются с отметкой «Демо». Обходов (`DEV_AUTH_BYPASS`,
`?user_id=`, тестового login endpoint) нет.

## Непредвиденные ошибки и мониторинг

- Любое необработанное исключение перехватывает middleware приложения.
  Ответ — 500 `{"error":"internal","request_id":"<id>"}` с теми же
  защитными заголовками.
- Исключение не передаётся дальше в Starlette/uvicorn. Иначе
  `ServerErrorMiddleware` пробросил бы его, и uvicorn записал бы полный
  traceback вместе с текстом исключения. Текст библиотечного исключения может
  повторять данные запроса.
- В лог пишется одна строка уровня ERROR:
  `event=unhandled_error request_id=<id> type=<класс> site=<файл>:<строка>`.
  Ни текста исключения, ни initData, cookie, токенов в ней нет. `site` — место
  в коде, а не данные.
- Учёт: алерт на появление и частоту строк `event=unhandled_error` в логах
  сервиса. Пользователь или тестировщик сообщает `request_id` из ответа, по
  нему находится строка лога.
- Access-лог uvicorn содержит метод, путь и код ответа. initData передаётся
  только в теле, cookie в access-лог не пишутся.

## HTTPS-стенд: один origin

Схема для закрытого тестового стенда. Railway и production не меняются;
реальные значения — только в окружении стенда.

```
Telegram-клиент (WebView)
        │ https://miniapp-stand.example.com      (один origin)
        ▼
edge: nginx — TLS termination, реальный IP клиента = TCP-пир
        ├─ /api/*  → http://127.0.0.1:8000  (uvicorn, только loopback)
        └─ /*      → frontend/dist (статические файлы)
```

- **Один origin.** Страница и `/api` отдаются с одного хоста по HTTPS.
  `MINIAPP_TRUSTED_ORIGINS=https://miniapp-stand.example.com`,
  `MINIAPP_ENV=production`. Cookie `__Host-sporai_session`, `Secure`,
  `SameSite=Lax`, без `Domain`. CORS не нужен.
- **TLS** завершается на nginx (сертификат Let's Encrypt). Между nginx и
  uvicorn — loopback, поэтому `Secure`-cookie и `__Host-` корректны: браузер
  видит только HTTPS.
- **uvicorn:** `--host 127.0.0.1 --port 8000 --no-proxy-headers`, один
  процесс. `--forwarded-allow-ips` не задаётся, тем более не `*`. Приложению
  IP клиента не нужен.
- **Реальный IP и лимиты по IP — на nginx.** nginx принимает соединение от
  клиента напрямую, поэтому `$binary_remote_addr` — настоящий адрес. Клиентские
  `X-Forwarded-For`/`X-Real-IP` nginx не доверяет: `real_ip_header` и
  `set_real_ip_from` не используются, заголовки к backend не пробрасываются.
- **Если перед стендом есть ещё один прокси или CDN** (Railway edge,
  Cloudflare), его адреса заранее неизвестны или меняются. Тогда лимит по IP
  настраивается **на этом внешнем уровне** (например, rate limiting rules
  Cloudflare). `set_real_ip_from` с широкими диапазонами и
  `--forwarded-allow-ips=*` не используются. Приложение в любом случае
  ограничивает только проверенных пользователей.

Пример конфигурации nginx (шаблон, не применён нигде):

```nginx
limit_req_zone $binary_remote_addr zone=miniapp_login:10m rate=20r/m;
limit_req_zone $binary_remote_addr zone=miniapp_api:10m  rate=10r/s;

server {
    listen 443 ssl;
    http2 on;
    server_name miniapp-stand.example.com;
    ssl_certificate     /etc/letsencrypt/live/miniapp-stand.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/miniapp-stand.example.com/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' https://telegram.org; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors https://web.telegram.org" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header X-Content-Type-Options "nosniff" always;

    client_max_body_size 16k;

    location = /api/auth/session {
        limit_req zone=miniapp_login burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For "";
        proxy_set_header X-Real-IP "";
    }
    location /api/ {
        limit_req zone=miniapp_api burst=40 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For "";
        proxy_set_header X-Real-IP "";
    }
    location / {
        root /srv/miniapp/dist;
        try_files $uri /index.html;
    }
}
server {
    listen 80;
    server_name miniapp-stand.example.com;
    return 301 https://$host$request_uri;
}
```

- `add_header` внутри `location` с собственными `add_header` отменяет
  заголовки уровня `server`. Здесь в `location` своих `add_header` нет.
- CSP добавляется в конфигурацию хостинга **до** открытия стенда. Её нужно
  проверить в Telegram-клиентах: шрифты и SVG собраны в `dist`, внешний
  скрипт один — SDK Telegram.
- Бот для стенда — отдельный тестовый бот (свой токен в окружении стенда),
  Mini App подключается к нему через BotFather вручную. Это делается вне
  этой задачи.

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

**Не проверено** и требует запуска внутри Telegram на HTTPS-стенде:
- настоящий initData от Telegram-клиентов (поля, кодирование, `signature`);
- cookie в WebView **Telegram iOS** (WKWebView) и **Android** (WebView):
  сохранение HttpOnly-cookie между открытиями, поведение при сворачивании;
- **Telegram Web** (web.telegram.org, Mini App в cross-site iframe): cookie
  `SameSite=Lax` в cross-site iframe не отправляются. Вероятно, понадобятся
  `MINIAPP_COOKIE_SAMESITE=none` (Secure + Partitioned/CHIPS), а Safari может
  блокировать такие cookie совсем. Совместимость **не заявляется** и не
  выводится из проверки в локальном Chrome. Ослаблять настройки ради неё
  без отдельного решения нельзя;
- поведение за nginx-стендом (HTTPS, `__Host-` cookie, лимиты nginx) и в
  нескольких воркерах; схема выше — проект, не проверенная конфигурация;
- сохранение сессии между открытиями и поведение logout внутри Telegram:
  сохраняется ли `sessionStorage` при перезагрузке WebView, работает ли
  `Telegram.WebApp.close()`, приходит ли новый initData при повторном открытии.
  Локально это проверено только на заглушке `window.Telegram.WebApp`;
- CSP из шаблона выше в Telegram-клиентах.

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
