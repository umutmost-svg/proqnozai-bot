# Mini App: закрытый HTTPS-стенд (M2.5)

> **Хостинг стенда требует пересмотра.** План ниже написан под отдельный проект
> Railway. С 2026-11 Railway в проекте не используется (production — DigitalOcean +
> Dokploy), поэтому Railway-шаги — исходный план, а не действующая инструкция.
> Требования (отдельный сервер и тестовый бот, Cloudflare + edge secret, нет обходного
> URL, постоянный volume для SQLite, бэкапы, откат) остаются в силе.
>
> **Не по хостингу и действует как есть:** CSP и заголовки; защита API (правила
> Cloudflare и приложения); сессии, лимит, отзыв, аварийное выключение; тестовый бот;
> проверки в Telegram; зависимости и сборка образа; настройка Cloudflare (общая часть).
>
> **Railway-специфичное, переписать после выбора хостинга** (до этого — не выполнять):
> схема «Архитектура» (узел Railway edge), «Доверие к заголовкам и обход защиты»
> (факты о Railway edge, `*.up.railway.app`, healthcheck), место хранения переменных,
> ротация токена (шаги в Railway), доступ к shell контейнера для `admin`-команд
> (`railway ssh` в примерах — заменить способом выбранного хостинга, например
> `docker exec` на сервере), healthcheck и алерты по логам, резервные копии volume,
> откат кода, «Настройка Railway», «Что проверено» (пункты про Railway).

Стенд нужен, чтобы проверить вход в настоящих Telegram-клиентах. Он
изолирован от production:

- отдельный сервер/проект хостинга, не тот, где работает production-бот
  (в исходном плане — отдельный проект Railway; см. примечание выше);
- отдельный тестовый бот (свой токен);
- свой тестовый домен;
- своя SQLite на своём volume.

Production (DigitalOcean + Dokploy) в этой работе не затрагивается.

Авторизация и сессии подробно описаны в [`auth.md`](auth.md).

## Архитектура

```
Telegram-клиент
  iOS / Android / Desktop — нативный WebView
  Web K / Web A           — iframe на https://web.telegram.org
        │  https://<stand-domain>        (один origin: страница + /api)
        ▼
Cloudflare (proxied DNS)
  · TLS для клиента, SSL mode = Full
  · rate limit: /api/auth/session, по IP
  · WAF custom rules: методы, пути, host
  · Transform Rule: добавляет X-Edge-Auth: <secret>
        │  HTTPS
        ▼
Railway edge (отдельный проект, публичный только custom domain;
              *.up.railway.app и TCP Proxy не созданы)
        │
        ▼
контейнер deploy/miniapp/Dockerfile, 1 реплика
  uvicorn :$PORT, 1 воркер, --no-proxy-headers
  FastAPI:
    · без X-Edge-Auth → 403 (кроме GET /api/health)
    · /api/*   → API (Origin, CSRF, cookie __Host-)
    · /*       → frontend/dist (StaticFiles, без symlink)
    · CSP, HSTS, no-store / immutable
        │
        ▼
volume /data → /data/miniapp.db (WAL), /data/backups/
```

Почему именно так:

- **Один origin.** Cookie `__Host-sporai_session` (`Secure`, `HttpOnly`,
  `SameSite=Lax`, без `Domain`), CSRF-проверка через Origin, CORS не нужен.
  Frontend и API отдаёт один процесс, поэтому nginx в контейнере не нужен.
- **Одна реплика, один воркер.**
  - Railway не позволяет использовать реплики с volume.
  - SQLite допускает одного писателя.
  - Лимитер входов живёт в памяти процесса.
  - Цена: при каждом деплое короткий простой. Для стенда это допустимо.
- **IP клиента приложение не использует** (см. «Доверие к заголовкам»).
  Лимиты по IP работают в Cloudflare, лимит на пользователя — в приложении
  (после проверки подписи).

## Доверие к заголовкам и обход защиты

Факты собраны в ноябре 2026 по документации Railway и Cloudflare и по
ответам их сотрудников. Часть из них противоречива, поэтому решение
строится так, чтобы от этих заголовков ничего не зависело.

**Какой IP видит контейнер.** TCP-пир контейнера — адрес из `100.64.0.0/10`.
Сотрудники Railway называют этот диапазон нестабильным, документированных
адресов edge нет. Значит, `--forwarded-allow-ips` нечем заполнить. Значение
`*` запрещено: тогда любой клиент подставит свой IP.

**`X-Forwarded-For` и `X-Real-IP` на Railway.** Публичного контракта нет.
Сотрудники Railway противоречат друг другу: один пишет, что edge заменяет
значения, другой — что дописывает. Пользователи сообщали, что присланный
клиентом `X-Real-IP` доходил до приложения. Заголовок `Forwarded` проходит
без изменений. **Итог: эти заголовки недоверенные.**

**`CF-Connecting-IP`** проходит через Railway без изменений. Его можно
учитывать только на запросах с верным `X-Edge-Auth`, то есть пришедших
через Cloudflare. Сейчас он не используется и в логи не пишется.

**Что делает приложение:**

- `uvicorn --no-proxy-headers`, без `--forwarded-allow-ips`;
- `X-Forwarded-*` и `X-Real-IP` игнорируются, это проверено тестами;
- лимит входов считается по проверенному Telegram `user.id`.

**Маршруты в обход Cloudflare и как они закрыты:**

| Маршрут | Мера |
|---|---|
| `*.up.railway.app` | Не генерировать. Railway не создаёт его сам. Если домен уже создан — удалить в Settings → Networking. Даже если он останется, всё, кроме health, получит 403 без секрета |
| TCP Proxy | Не создавать, иначе удалить |
| Прямой запрос на edge Railway с `Host: <stand-domain>` (IP Railway узнаётся из истории DNS) | **`MINIAPP_EDGE_SECRET`.** Cloudflare добавляет секрет в `X-Edge-Auth`, приложение сравнивает его за постоянное время. Без секрета — 403 и строка лога `event=edge_bypass_blocked` |
| Private network (`*.railway.internal`) | В проекте стенда нет других сервисов: окружения Railway изолированы |
| Healthcheck Railway (host `healthcheck.railway.app`) | Ходит мимо Cloudflare. Поэтому только `GET /api/health` освобождён от секрета; ответ — `{"status":"ok"}`, без данных |

**Authenticated Origin Pulls (mTLS) недоступны.** TLS завершается на edge
Railway, поэтому общий секрет остаётся единственным способом привязать
стенд к Cloudflare. Ещё строже — Cloudflare Tunnel без публичного домена
Railway; это вне M2.5.

**Если на тарифе Railway есть Edge Rules**, можно добавить второй рубеж:
правило Block для запросов, где заголовок `X-Edge-Auth` не равен секрету,
кроме пути `/api/health`. Тогда трафик в обход Cloudflare не дойдёт даже
до контейнера.

## Переменные окружения стенда

Значения задаются только в Variables сервиса Railway (секреты — как
sealed). В репозитории есть лишь плейсхолдеры в `miniapp/.env.example`.

| Переменная | Значение на стенде | Секрет |
|---|---|---|
| `MINIAPP_BOT_TOKEN` | токен **тестового** бота | да |
| `MINIAPP_DB_PATH` | `/data/miniapp.db` | — |
| `MINIAPP_TRUSTED_ORIGINS` | `https://<stand-domain>` | — |
| `MINIAPP_ENV` | `production` | — |
| `MINIAPP_EDGE_SECRET` | случайная строка ≥ 32 символов (`python -c "import secrets;print(secrets.token_urlsafe(48))"`); в production обязательна, без неё старт останавливается | да |
| `MINIAPP_CSP_MODE` | `report-only` на первом прогоне в клиентах, затем `enforce` | — |
| `MINIAPP_LOGINS_ENABLED` | `true` (аварийно — `false`) | — |
| `MINIAPP_MAX_SESSIONS_PER_USER` | `5` (необязательна; допустимо 1…5) | — |
| `MINIAPP_FOOTBALL_DATA_KEY` | ключ football-data.org **только для Mini App** (не `FOOTBALL_KEY` бота); без него футбол и AI — «не подключены» | да |
| `MINIAPP_FOOTBALL_DATA_COMPETITIONS`, `MINIAPP_FOOTBALL_DATA_LIVE_PLAN`, `MINIAPP_FOOTBALL_DATA_PER_MINUTE`, `MINIAPP_FOOTBALL_DATA_PER_DAY`, `MINIAPP_SPORTS_UNKNOWN_LOOKUPS_PER_HOUR` | по умолчанию (`PL,PD`, `false`, `8`, `3000`, `20`) | — |
| `MINIAPP_ANTHROPIC_API_KEY` | ключ Anthropic **только для Mini App** (не `ANTHROPIC_API_KEY` бота); без него кнопки AI нет | да |
| `MINIAPP_AI_MODEL`, `MINIAPP_AI_PER_USER_DAILY`, `MINIAPP_AI_DAILY_BUDGET` | по умолчанию (`claude-opus-5-5`, `10`, `200`); бюджет — потолок платных вызовов в сутки | — |
| `MINIAPP_SESSION_TTL_S`, `MINIAPP_INIT_DATA_MAX_AGE_S`, `MINIAPP_INIT_DATA_FUTURE_SKEW_S`, `MINIAPP_LOGIN_RATE_LIMIT`, `MINIAPP_LOGIN_RATE_WINDOW_S`, `MINIAPP_COOKIE_SAMESITE` | по умолчанию (необязательны) | — |
| `RAILWAY_DOCKERFILE_PATH` | `deploy/miniapp/Dockerfile` | — |

`MINIAPP_STATIC_DIR=/app/static` и `PORT` уже заданы в образе. Railway
подставляет свой `PORT`, entrypoint его использует.
`MINIAPP_ALLOW_EPHEMERAL_DB=true` — только для локальных опытов без
volume, на стенде не задаётся.

## CSP и заголовки

CSP задаёт приложение (`miniapp/app.py`, `content_security_policy`) на
каждом ответе:

```
default-src 'none'; script-src 'self' https://telegram.org; style-src 'self';
img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self';
base-uri 'none'; form-action 'none'; object-src 'none';
frame-ancestors https://web.telegram.org; upgrade-insecure-requests
```

**Детали политики:**

- **Без `'unsafe-inline'` и `'unsafe-eval'`.** В сборке нет inline-скриптов
  и `<style>`-блоков. React и SDK Telegram меняют стили через CSSOM, а это
  CSP не блокирует.
- **`frame-ancestors`.** Telegram Web K и Web A встраивают Mini App в iframe
  с `https://web.telegram.org`. Мобильные и десктопные клиенты используют
  нативный WebView, где директива не действует. `X-Frame-Options` не
  отправляется: он сломал бы Telegram Web.
- **`upgrade-insecure-requests` и HSTS** — только в `MINIAPP_ENV=production`.
  HSTS отправляется без `includeSubDomains`, потому что домен стенда может
  быть поддоменом.
- **Кэш.** `no-store` для страницы и API, `immutable` для `/assets/*`: у
  этих файлов хеш в имени.
- **Прочее:** `Permissions-Policy` запрещает камеру, микрофон, геолокацию,
  оплату и USB; `Referrer-Policy: no-referrer`; `nosniff`.

**Тесты политики:**

- `miniapp/tests/test_stand.py` проверяет директивы, режимы и заголовки.
- `frontend/e2e-auth/csp.spec.ts` проверяет политику в Chrome. Собранный
  frontend отдаёт настоящий backend с **enforce**-политикой. Тест обходит все
  экраны, и любое нарушение роняет его. Отдельный контрольный тест
  доказывает, что политика действительно блокирует чужой скрипт и
  inline-стиль, то есть проверка не пустая.

**Ограничение.** e2e идёт в режиме `development` (http на loopback):
`upgrade-insecure-requests` и HSTS там не отправляются, их проверяют
unit-тесты и smoke-тест контейнера. Скрипт SDK Telegram в e2e заглушён. Поведение настоящего
`telegram-web-app.js` под CSP проверяется в клиентах (см. «Проверки в
Telegram») в режиме `MINIAPP_CSP_MODE=report-only`. Нарушения видны в
консоли:

- Telegram Web — DevTools браузера;
- Android — `chrome://inspect` при включённой отладке WebView в Telegram;
- iOS — Safari Web Inspector.

Отдельного эндпоинта для отчётов CSP нет. Это сознательно: это ещё одна
открытая ручка для мусорных запросов.

## Защита `/api/auth/session` и API от массовых запросов

| Уровень | Мера |
|---|---|
| Cloudflare, Rate limiting rule (на Free — 1 правило, окно 10 с, счёт по IP) | **Обязательно до открытия стенда — весь API:** `starts_with(http.request.uri.path, "/api/")`, порог 30 запросов за 10 с, Block на 10 с. Иначе `GET /api/me` с произвольной cookie без ограничений доходит до SQLite (поиск сессии по хешу токена). На платном плане — второе, более строгое правило: `http.request.uri.path eq "/api/auth/session"` и `POST`, 10 за 10 с. На Free вход дополнительно защищён в приложении: HMAC до любой записи, лимит по `user.id` |
| Cloudflare, WAF custom rules (на Free — 5) | 1) Block, если метод не `GET`/`HEAD`/`POST`. 2) Block для `starts_with(http.request.uri.path, "/api/")` и метода `POST` вне списка `/api/auth/session`, `/api/auth/logout`, `/api/auth/logout-all`, `/api/me/language`, `/api/ai/analysis`. 3) Block, если `http.host` не равен домену стенда |
| Cloudflare | Always Use HTTPS; SSL mode — по разделу «Настройка Cloudflare», шаг 2 (не Flexible; Full (strict), если у сервера доверенный сертификат); Under Attack / Bot Fight не включать — это браузерные челленджи, они ломают API и WebView |
| Cloudflare Transform Rule | Modify Request Header → Set `X-Edge-Auth` = секрет. Set перезаписывает значение, присланное клиентом |
| Приложение | без секрета → 403 до любой логики. Origin проверяется до чтения тела. Тело ≤ 8 КиБ (`Content-Length` и потоковый счётчик), строгий JSON. HMAC проверяется до любой записи в БД. Лимит по проверенному `user.id`. Сожжённый initData отклоняется до лимита. Неподписанный трафик не тратит ничей бюджет |
| uvicorn | `--limit-concurrency 200` (сверх — 503), `--timeout-keep-alive 5`, 1 воркер |

Ответ 429 от Cloudflare — HTML. Frontend распознаёт 429 по статусу
(`api.ts`), поэтому пользователь видит «Слишком много попыток».

## Сессии: лимит, отзыв, аварийное выключение

- **Лимит.** Не больше 5 активных сессий на пользователя. Шестой вход
  отзывает старейшую сессию в той же транзакции; лог —
  `auth.session_evicted user=<id> count=N`. Вытесненное устройство выходит
  так же, как после logout: свежий initData его сессии сжигается. Поэтому
  перезагрузка не входит снова и не вытесняет уже другое устройство.
- **Сжигание initData при любом отзыве.** Это вытеснение, `logout-all`,
  `revoke-user`, `revoke-all` и `restore`. Сжигается initData **всех**
  затронутых сессий, созданных в окне свежести (`max_age + skew`), в каком
  бы состоянии они ни были: активные, недавно истёкшие или отозванные раньше.
- **«Выйти на всех устройствах».** Кнопка в профиле вызывает
  `POST /api/auth/logout-all` (Origin + CSRF + живая сессия). Отзываются все
  сессии пользователя и сжигается initData текущего запуска.
  - Если сервер не подтвердил выход, UI успеха не показывает.
  - Другие устройства с открытым Telegram смогут войти снова, открыв Mini
    App заново. Это ожидаемо: доступ к Telegram-аккаунту равен доступу к
    Mini App.
- **CLI.** Запускать только через entrypoint: он сбрасывает root, а файлы,
  созданные root, заблокировали бы приложению его же WAL. Прямой
  `python -m miniapp.admin` под root отказывается работать.
  ```
  <shell контейнера стенда>                     # у хостинга (было: railway ssh)
  miniapp-entrypoint admin stats
  miniapp-entrypoint admin revoke-user <telegram_user_id>
  miniapp-entrypoint admin revoke-all --yes
  miniapp-entrypoint admin purge
  miniapp-entrypoint admin backup /data/backups/miniapp-$(date +%Y%m%d-%H%M).db
  miniapp-entrypoint admin restore /data/backups/<файл>.db --yes
  ```
  - Вывод — только числа, без токенов, хешей и имён.
  - Отсутствующую БД CLI не создаёт; чужую (с таблицами бота) не трогает.
  - `revoke-all` и `restore` без `--yes` ничего не меняют.
- **Аварийное выключение входов.** Установить
  `MINIAPP_LOGINS_ENABLED=false` и задеплоить заново. Новые входы получают
  503 `logins_disabled`, существующие сессии работают. Чтобы закончить и
  их — затем `admin revoke-all --yes`.
- **Edge-секрет обязателен в production.** Без `MINIAPP_EDGE_SECRET` при
  `MINIAPP_ENV=production` сервис не стартует (`SettingsError`). Забытая
  переменная не может тихо открыть обходной маршрут.
- **Очистка.** Закончившиеся сессии и сожжённые запуски удаляются через 7
  дней: при каждом входе, отзыве, старте и командой `admin purge`. Таблицы
  ограничены и без ручных действий.

## Ротация токена Telegram (runbook)

Токен бота нужен в двух местах: для проверки подписи initData и как ключ
CSRF (HMAC от токена). После ротации:

- initData, подписанный старым токеном, перестаёт приниматься;
- CSRF-токены всех существующих сессий перестают совпадать
  (`403 csrf_failed`).

Поэтому ротация всегда завершается отзывом всех сессий.

1. Записать время начала; убедиться, что под рукой доступ к Variables
   стенда и `railway ssh`.
2. **@BotFather** → тестовый бот → API Token → Revoke current token. Старый
   токен сразу перестаёт работать.
3. Railway → проект стенда → сервис → Variables → `MINIAPP_BOT_TOKEN` = новый
   токен (sealed). Сохранение запускает новый деплой.
4. Дождаться, пока healthcheck станет зелёным (`GET /api/health` → 200).
5. `railway ssh` → `miniapp-entrypoint admin revoke-all --yes`. Ответ
   `{"revoked": N}` — N сессий закончено.
6. Открыть Mini App в Telegram: вход проходит с новым initData. В логах
   есть `auth.session ... created=True`, нет `event=unhandled_error`.
7. Если токен утёк: пройти журналы Railway за период утечки (`auth.session`
   от незнакомых `user`), при необходимости сделать `revoke-user`. Ротация
   секрета `MINIAPP_EDGE_SECRET` — по тем же шагам 3–4 плюс одновременное
   обновление Transform Rule в Cloudflare.
8. Токен **production**-бота этот runbook не меняет: у стенда свой бот.

## Тестовый бот (BotFather)

1. `/newbot` → имя, например «Spor AI Stand», и username `…_stand_bot`.
   Токен сразу положить в Variables стенда, нигде не сохранять в
   открытом виде.
2. `/newapp` или Bot Settings → Configure Mini App → URL
   `https://<stand-domain>/`; short name, например `stand`.
3. Bot Settings → Menu Button → тот же URL (кнопка «Открыть» в чате).
4. Bot Settings → Domain → `<stand-domain>`. С Bot API 10.2 Telegram по
   умолчанию запрещает вызывать методы Mini App с чужих origin, поэтому
   домен должен точно совпадать.
5. Доступ закрытый: ссылку на бота дают только тестировщикам. Боту не нужны
   ни webhook, ни polling — стенд только проверяет initData.

## Healthcheck и мониторинг

- **Healthcheck.** `GET /api/health` проверяет чтение БД (`SELECT` из
  `sessions`): 200 `{"status":"ok"}` или 503 `{"status":"unavailable"}` с
  записью в лог `event=health_db_error type=<класс>`.
  - Пробу видно мимо Cloudflare (она освобождена от секрета). Поэтому
    успешный результат переиспользуется 5 с: поток проб не доходит до
    SQLite.
  - Сбой не кэшируется: восстановление видно на следующей пробе. На Railway: Settings → Deploy →
  Healthcheck Path `/api/health`, timeout 60 с. Railway проверяет только при
  деплое: релиз, который не читает БД, не станет активным.
- **Постоянная проверка.** Внешний uptime-монитор (Better Stack,
  UptimeRobot) раз в минуту на `https://<stand-domain>/api/health` через
  Cloudflare, плюс алерт в Telegram/почту.
- **Алерты по логам Railway** (Observability → Log alerts / фильтры):
  - `event=unhandled_error` — любое появление;
  - `event=edge_bypass_blocked` — кто-то ходит в обход Cloudflare;
  - `event=health_db_error`;
  - всплески `auth.rate_limited` и `auth.rejected`;
  - `admin.` — любые действия оператора.
- **Секреты в логах.** В логи не попадают initData, cookie, токены и
  `X-Edge-Auth` (это проверено тестами и смоук-тестом контейнера). Access-лог
  uvicorn выключен (`--no-access-log`): он пишет query string; события
  приложения (`auth.*`, `event=*`, `request_id`) его заменяют.
- **Restart policy:** On Failure, до 10 попыток.

## Резервные копии и восстановление

- **Основное — бэкапы volume Railway** (Volume → Backups): ежедневные с
  хранением 7 дней плюс ручной перед каждым деплоем со схемными
  изменениями. Восстановление — средствами Railway (Restore), после него
  обязательно `admin revoke-all --yes`.
- **Файловые копии.** `admin backup <новый файл>` делает согласованную
  онлайн-копию через SQLite backup API. Существующий файл не
  перезаписывается, путь вида `bot.db*` отклоняется.
- **Восстановление из файла.** `admin restore <файл> --yes` выполняется
  **одной транзакцией**, источник подключается только на чтение и должен
  быть БД Mini App. Внутри транзакции:
  - `users` и `sessions` заменяются данными копии;
  - журнал сожжённых запусков **объединяется**, а не заменяется: запуск,
    сожжённый после снятия копии, остаётся сожжённым;
  - сначала отзываются живые сессии (в том числе созданные после копии), их
    свежий initData сжигается;
  - все восстановленные сессии отзываются, их свежий initData сжигается.

  API видит либо старые данные, либо уже восстановленные с отозванными
  сессиями; ожившей сессии не бывает ни на миг. При ошибке всё
  откатывается.
- Копии лежат на том же volume. Это защита от ошибок оператора, а не от
  потери volume; от потери защищают бэкапы Railway.

## Откат (rollback)

Стенд не связан с production, поэтому его откат production не задевает.

- **Код.** Railway → Deployments → предыдущий успешный деплой → Redeploy
  (тот же образ). Схема БД только расширяется
  (`CREATE TABLE IF NOT EXISTS`), поэтому старый код работает с новой БД.
- **Конфигурация.** Вернуть изменённую переменную в Variables, это
  запустит новый деплой.
- **Данные.** Бэкап volume Railway или `admin restore`, затем
  `admin revoke-all --yes`.
- **Аварийно, без отката кода.**
  - `MINIAPP_LOGINS_ENABLED=false` — закрыть новые входы;
  - `admin revoke-all --yes` — закончить все сессии;
  - в BotFather убрать URL Mini App и Menu Button тестового бота — стенд
    недоступен из Telegram;
  - в Cloudflare правило Block на весь хост — стенд недоступен совсем.
- **Проверка после отката:** `/api/health` 200; вход в Telegram-клиенте; в
  логах нет `event=unhandled_error`.

## Зависимости и воспроизводимая сборка

**Python:**

- `miniapp/requirements.lock` (runtime) и `miniapp/requirements-dev.lock`
  (тесты) — полный набор с хешами, получены командой
  `uv pip compile --generate-hashes --universal --python-version 3.13`.
  Команды — в `deploy/miniapp/check-deps.sh`.
- Образ ставит зависимости через
  `pip install --require-hashes --no-deps --only-binary=:all:`.
- Локальное окружение:
  `uv pip sync --python miniapp/.venv/bin/python --require-hashes miniapp/requirements-dev.lock`.
- Версия Python — 3.13 (`miniapp/.python-version`); в образе
  `python:3.13.18-slim@sha256:…`.

**Node:**

- 24.22.2 (`frontend/.nvmrc`, `engines` в `package.json`); в образе
  `node:24.22.2-slim@sha256:…`.
- Зависимости ставятся через `npm ci` строго по `package-lock.json`.

**Сборка образа:**

- Базовые образы закреплены по digest multi-arch индекса.
- frontend собирается внутри образа (`npm run build`), затем `check:bundle`
  проверяет бандл на токены и временные URL.
- В образ попадают только модули `miniapp/*.py`, без тестов и `.env`;
  контекст фильтрует корневой `.dockerignore`.

**Проверка уязвимостей.** `sh deploy/miniapp/check-deps.sh` нужен сеть.
Скрипт:

- проверяет, что lock-файлы совпадают с `requirements*.txt`;
- запускает `pip-audit` по lock;
- проверяет `npm ci --dry-run`;
- запускает `npm audit --omit=dev`.

Он падает при любой уязвимости runtime-зависимостей. Находки только в
dev-зависимостях печатаются, но не роняют проверку.

**Обновление зависимостей:** поднять диапазон в `requirements.txt` →
перегенерировать lock той же командой → `uv pip sync` → тесты → Codex.

## Настройка Railway (исторический пример, не выполнять)

> Railway в проекте больше не используется. Раздел сохранён как пример
> требований к хостингу стенда; при выборе хостинга заменить его.

Config as Code (`railway.json`/`railway.toml`) Railway объявил устаревшим:
для существующих сервисов он работает до 2026-12-01. Поэтому в репозитории
файла Railway нет. Корневой `railway.json`, кроме того, повлиял бы на
production-проект бота. Настройки задаются в панели стенда.

1. New Project → **отдельный** проект `sporai-miniapp-stand`; Deploy from
   GitHub repo → ветка `feat/miniapp-m1`. Production-проект не открывать и
   не менять.
2. Сервис → Variables: всё из «Переменные окружения стенда», включая
   `RAILWAY_DOCKERFILE_PATH=deploy/miniapp/Dockerfile`.
3. Сервис → Volume → mount path `/data`. Пользователь по умолчанию — root,
   entrypoint сам отдаёт каталог пользователю `miniapp`. `RAILWAY_RUN_UID` не
   нужен.
4. Settings → Deploy:
   - Healthcheck Path `/api/health`, timeout 60;
   - Restart Policy On Failure (10);
   - Replicas 1;
   - Start Command пустой (используется `CMD` образа).
5. Settings → Networking:
   - Custom Domain `<stand-domain>`, порт 8000;
   - **не** нажимать Generate Domain;
   - TCP Proxy не добавлять;
   - если `*.up.railway.app` уже есть — удалить.
6. Включить бэкапы volume (Backups), если они есть на тарифе.
7. После первого деплоя:
   - `railway ssh` → `miniapp-entrypoint admin stats` → `{"users": 0, …}`;
   - `curl -i https://<stand-domain>/` (через Cloudflare, секрет добавит
     Transform Rule) → 200 и заголовки CSP/HSTS;
   - запрос мимо Cloudflare на edge Railway с `Host` стенда → 403.

## Настройка Cloudflare

1. Зона домена в Cloudflare. DNS: запись `<stand-sub>` на адрес/цель, которую
   даёт выбранный хостинг стенда (A на IP сервера или CNAME на его custom
   domain), **Proxied**.
2. SSL/TLS: не **Flexible** (тогда трафик Cloudflare → сервер идёт по HTTP).
   **Full (strict)**, если на сервере стенда сертификат, которому доверяет
   Cloudflare (например, Cloudflare Origin Certificate или Let's Encrypt через
   Dokploy/Traefik); иначе **Full** — выбрать по возможностям хостинга.
   Always Use HTTPS; minimum TLS 1.2. Сервер принимает входящий HTTPS только
   с IP-диапазонов Cloudflare (firewall хостинга), если хостинг это позволяет.
3. Rules → Transform Rules → Modify Request Header: hostname равен
   `<stand-domain>` → **Set** `X-Edge-Auth` = значение
   `MINIAPP_EDGE_SECRET`.
4. Security → WAF → Rate limiting rule (на весь `/api/`) и Custom rules — по
   таблице «Защита `/api/auth/session` и API от массовых запросов».
5. Caching: правило Bypass cache для `/api/*`. Приложение и так отдаёт
   `no-store`; правило страхует от чужих настроек кэша.

## Проверки в Telegram (до открытия стенда тестировщикам)

**Клиенты:** iOS, Android, Desktop (macOS, Windows), Web K, Web A. Версии
клиентов записываются в таблицу результатов в `auth.md`, раздел «Что
проверено».

**Сценарии:**

1. Первое открытие → профиль «вход выполнен», имя из проверенных данных.
2. Закрыть и открыть → сессия из cookie, без нового обмена; в логах
   `created=False`.
3. «Выйти» → перезагрузить WebView → `signed_out` без запросов. «Закрыть»
   работает, повторное открытие входит.
4. «Выйти на всех устройствах» на одном устройстве → на втором `/api/me`
   даёт 401.
5. Шесть устройств или входов → самая старая сессия отозвана.
6. `MINIAPP_LOGINS_ENABLED=false` → новый вход показывает «Сервер
   недоступен», уже вошедший работает.
7. CSP в режиме `report-only` → в консоли клиента нет нарушений. После
   этого переключить на `enforce` и повторить 1–3.
8. Telegram Web: `SameSite=Lax` в cross-site iframe, скорее всего, не
   работает (`auth.md`). Результат фиксируется. Переход на `none` +
   Partitioned — отдельное решение с аудитом.

## Что проверено

- **Офлайн:**
  - pytest backend: лимит сессий, в том числе гонка из 12 параллельных
    входов; logout-all; CLI (в том числе backup/restore и отказ под root);
    edge-секрет; static, CSP, кэш, traversal, symlink; health 503; purge;
  - vitest;
  - Playwright с настоящим backend, который отдаёт `dist` и enforce-CSP.
- **Локальный Docker:**
  - без volume контейнер не стартует;
  - при плохой конфигурации — понятная ошибка, токен в ней не печатается;
  - процесс работает под uid 10001, volume принадлежит `miniapp`;
  - без `X-Edge-Auth` — 403, health — 200;
  - заголовки CSP и HSTS на месте;
  - вход подписанным initData даёт cookie `__Host-`;
  - admin под root отказывает, через entrypoint работает;
  - backup, `kill` + `start` (данные на месте), `revoke-all`;
  - в логах контейнера нет токена, секрета и данных пользователя.
- **Не проверено:**
  - Railway (edge, volume, healthcheck, удаление `*.up.railway.app`);
  - Cloudflare (правила, Transform Rule);
  - поведение в настоящих Telegram-клиентах и SDK Telegram под CSP;
  - есть ли Edge Rules на тарифе Railway.

## Перед первым запуском (ручные действия)

1. Купить или выбрать тестовый домен и перевести его зону в Cloudflare.
2. Создать тестового бота в BotFather (раздел «Тестовый бот»), шаги 1–4
   выполнить после того, как домен заработает.
3. Сгенерировать `MINIAPP_EDGE_SECRET`.
4. **Выбрать хостинг стенда** (отдельный от production сервер или проект;
   решение владельца) и настроить его по тем же требованиям: один контейнер
   `deploy/miniapp/Dockerfile` (обязательный build-аргумент
   `VITE_DATA_MODE=real` для реальных данных или `demo`; без него сборка
   останавливается), постоянный volume для SQLite, секреты только в
   настройках хостинга, публичен только домен стенда за Cloudflare, без
   обходного URL. Раздел «Настройка Railway» — исторический пример, не
   инструкция к выполнению.
5. Настроить Cloudflare по разделу «Настройка Cloudflare».
6. Выставить `MINIAPP_CSP_MODE=report-only` на первые проверки.
7. Убедиться: `https://<stand-domain>/api/health` — 200; прямой запрос
   мимо Cloudflare — 403; `admin stats` работает.
8. Пройти «Проверки в Telegram», затем переключить CSP на `enforce`.
9. Включить мониторинг и бэкапы volume.
