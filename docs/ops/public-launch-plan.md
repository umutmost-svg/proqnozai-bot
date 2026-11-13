# Публичный запуск Mini App — план, гейты, команды

Подготовлено 2026-11-13. **Ничего не выполнено.** Каждый шаг с **[разрешение]** — только по отдельному
разрешению владельца; **[владелец]** — делает владелец. Детальные процедуры не дублируются, а даны ссылками:
`docs/miniapp/production.md` (§6 runbook, §7 откат, §8 мониторинг), `docs/miniapp/migration.md` (перенос
пользователей и вывод бота), `docs/ops/infra-hardening.md`, `docs/ops/offsite-backup.md`,
`docs/ops/healthchecks-owner.md`, `docs/ops/security-review.md`.

Обозначения ниже: `SSH=root@164.90.180.208`, `SVC=proqnozai-miniapp-olj3ry`, `BOT=proqnozai-proqnozaibot-uejbpm`.

## 1. Предусловия (чек-лист GO)

Каждый пункт — с доказательством (вывод команды/скрин/строка лога), записанным в журнал запуска.

| # | Предусловие | Как проверить | Источник |
|---|---|---|---|
| P1 | Закрытый тест пройден: ≥ 72 ч без инцидентов, вход на iOS/Android/Desktop/Web, 0 `auth.rejected reason=bad_signature…`, нет 5xx кроме ожидаемых 503 AI | команда P1 ниже → `0`; Cloudflare Analytics: 5xx по `app.proqnozai.com` ≈ 0; отчёт тестеров | `production.md` §6 п. 9–10 |
| P2 | Качество AI-разбора проверено (матчдей 20–22.11), решение владельца включать/нет | `ai-matchday-check.md` — итог PASS | `tech-debt.md` |
| P3 | Мониторинг живой: 6 проверок healthchecks **up**, тест тревоги пришёл в Telegram и на e-mail | страница проекта healthchecks; `infra-hardening.md` §4 «Тест тревоги» | `healthchecks-owner.md` |
| P4 | Бэкапы: автоматическая выгрузка ≥ 3 суток OK; третья копия в бакете проверена; учения из бакета PASS для обеих БД | `tail ~/Library/Logs/proqnozai-backup.log ~/Library/Logs/proqnozai-offsite.log`; `sh ~/proqnozai-ops/restore-drill-offsite.sh bot` / `miniapp` → `PASS` | `offsite-backup.md` §5 |
| P5 | Dokploy `:3000` и обход через `:80` закрыты, сторож `dokploy-bypass` up | снаружи: `curl -m 5 -s -o /dev/null -w '%{http_code}' http://164.90.180.208:3000/` → `000`; `curl -s -o /dev/null -w '%{http_code}' -H 'Host: dokploy.docker.localhost' http://164.90.180.208/` → `404` | `infra-hardening.md` §1–2 |
| P6 | Сырые копии в томе бота удалены по гейту третьей копии | команда P6 ниже → `0` | `offsite-backup.md` §4 |
| P7 | Исправления безопасности выложены (`fix/miniapp-security`: пустой allowlist — ошибка, test-DC в prod запрещён) | в логе старта `startup access_mode=allowlist(1)` | `security-review.md` §3 |
| P8 | Лимиты на публичный режим: F2 (пер-пользовательский лимит неизвестных id / некэшированных загрузок) реализован и одобрен Codex, `MINIAPP_FOOTBALL_DATA_SHARED_RESERVE` > 0; лимит входа ужесточён; правило Cloudflare для `/api/` пересмотрено | env в Dokploy; правило в Cloudflare WAF | `security-review.md` §4 |
| P9 | Бюджет AI определён (§4) и лимит расходов в Anthropic Console выставлен; тревога `budget_exhausted` настроена | Console → Limits; §4 | — |
| P10 | Миграция пользователей: финальная дельта из **свежей** копии бота, `RESULT PASS`, blocklist перенесён | вывод `migrate_users.py` | `migration.md` §3 |
| P11 | Записаны текущие значения BotFather (Menu Button, Main Mini App, описание, about, команды) — для отката | скриншоты/текст в журнале | §3, шаг S3 |
| P12 | Нагрузка: `loadtest.py` на релизном образе — p95 и ошибки в норме для ожидаемой волны (≤ 300 открытий/мин на ядро) | отчёт loadtest | `production.md` §1 |
| P13 | Откат отрепетирован на бумаге: у дежурного есть команды §5 и доступ (Access + 2FA Dokploy, SSH-ключ) | — | — |

Команды проверок (чтение; по SSH — с разрешением):
```sh
# P1: отказы подписи и блокировки БД за 72 ч (ожидается 0)
ssh root@164.90.180.208 "docker service logs --since 72h proqnozai-miniapp-olj3ry 2>&1 | grep -cE 'auth\.rejected reason=(bad_signature|missing_signature|bad_signature_format)|event=db_busy'"
# P6: незашифрованные копии в томе бота (ожидается 0)
ssh root@164.90.180.208 'c=$(docker ps -q -f name=proqnozai-proqnozaibot-uejbpm); docker exec "$c" sh -c "ls /app/data/bot-backup-*.db 2>/dev/null | wc -l"'
```

## 2. Go / No-Go

**GO** — все P1–P13 выполнены, владелец письменно дал разрешение на конкретный этап (S1–S4 отдельно).
**NO-GO / стоп** на любом этапе, если за окно наблюдения:
- 5xx по Cloudflare Analytics (кроме 503 AI с кодом причины) > 0,5 % запросов или любой `event=db_busy` повторяется > 5/ч;
- `auth.rejected reason=bad_signature|missing_signature` > 0 у реальных клиентов;
- `host` или `miniapp-health` в healthchecks down > 10 мин; `MemAvailable` < 400 МБ;
- `budget_exhausted` раньше 18:00 UTC два дня подряд (бюджет мал или злоупотребление — разобрать до расширения);
- бот (если ещё работает) деградировал: ошибки футбольного API `rate_limited` в боте выросли.

## 3. Поэтапное открытие

Каждый этап — отдельное разрешение; наблюдение — минимум указанное время, затем GO/NO-GO по §2.

**S0. Выкладка релиза с исправлениями** [разрешение] — по `production.md` §6 (Dokploy Deploy коммита
релиза, smoke). Allowlist = владелец. Проверка: `ssh $SSH "docker service logs --since 10m $SVC 2>&1 | grep 'startup access_mode'"`
→ `allowlist(1)`.

**S1. Расширенный allowlist (10–30 доверенных пользователей бота), 48 ч** [разрешение]
1. [владелец] список id (из бота: активные за 7 дней, без блокировок).
2. Dokploy → `sporbot-miniapp` → Environment: `MINIAPP_ALLOWED_USER_IDS=<id через запятую>` → Redeploy.
   Проверка: в логе `startup access_mode=allowlist(N)`; smoke с `SMOKE_EXPECT_ALLOWLIST`.
3. Кнопка Menu Button — **только этим чатам** (`setChatMenuButton` с `chat_id`, как в `production.md` §6 п. 9;
   токен бота через `curl -K -`, не в аргументах). Сообщение тестерам — от владельца.
4. Наблюдение: §2 + `ssh $SSH "docker service logs --since 24h $SVC 2>&1 | grep -E 'auth\.(session|rejected)|ai\.unavailable' | awk '{print \$NF}' | sort | uniq -c"`.

**S2. Открыто всем, без объявления (тихое открытие), 24 ч** [разрешение]
1. P8–P11 выполнены. Финальная дельта пользователей и `MINIAPP_BLOCKED_USER_IDS` (`migration.md` §3 п. 6).
2. Dokploy: **удалить** переменную `MINIAPP_ALLOWED_USER_IDS` (не оставлять пустой — после исправления F3 пустое
   значение остановит старт) → Redeploy → smoke без `SMOKE_EXPECT_ALLOWLIST`; в логе `startup access_mode=public`.
3. [владелец, BotFather] `/mybots` → бот → Bot Settings → **Configure Mini App** → Main Mini App URL =
   `https://app.proqnozai.com`; **Menu Button** → URL `https://app.proqnozai.com`, текст кнопки (например «Открыть»);
   `/setdescription` и `/setabouttext` — новые тексты про Mini App (7 языков бота: az, ru, en, tr, kz, uz, ar —
   описание задаётся на языке по умолчанию + переводы через Bot API `setMyDescription` с `language_code`, по
   желанию). Проверка: профиль бота → «Открыть», меню чата, `t.me/<bot>?startapp` — вход на 4 платформах.
4. Бот продолжает работать как раньше (параллельный режим) — нет объявления, нет рассылки.

**S3. Объявление порциями, 3–5 дней** [разрешение]
1. Рассылка через бота **порциями** (≤ 100–150 пользователей за волну, интервал ≥ 1 ч; предел — ~300 открытий/мин
   на ядро по loadtest). Перед каждой волной — §2 по предыдущей.
2. AI-разбор (если P2 = включать): ключ и лимиты по §4 — отдельный Redeploy после S2, не в один шаг с открытием.

**S4. Вывод бота из эксплуатации** — отдельное решение владельца, по `migration.md` §5–6 (лаунчер вместо
полного выключения: `/start` отвечает кнопкой Mini App; legacy-кнопки, включая `expr_`, остаются
зарегистрированы). Перед остановкой — финальный бэкап бота (`backup-export.sh` через `pull`, + `offsite sync`,
`verify` PASS) и сохранение `bot.db` вне сервера по гейту третьей копии.

## 4. Бюджет AI (размер и защита)

- Модель `claude-opus-5-5`: $4 / 1M входных, $20 / 1M выходных токенов (прайс Anthropic, 2026). Запрос — до
  `max_tokens=8000` (включая размышления, effort `medium`), вход — данные матча + промпт.
- Оценка сверху на один платный анализ: ~15 тыс. входных (≈ $0,06) + до 8 тыс. выходных (≈ $0,16) ≈ **≤ $0,22**;
  до 2 платных попыток на ключ анализа; повторные открытия — из кэша, бесплатно. Реальное значение —
  **замерить** в закрытом тесте (Anthropic Console → Usage, по дням, делить на `ai_usage` за тот же день).
- Сейчас `MINIAPP_AI_PER_USER_DAILY=10`, `MINIAPP_AI_DAILY_BUDGET=100` → худший случай ≈ $22/сутки ≈ $660/мес.
  200/сутки → ≈ $44/сутки. Рекомендация на S3: `PER_USER_DAILY=5`, `DAILY_BUDGET` = (месячный бюджет владельца
  / 30) / (замеренная p95-стоимость), но не меньше 100; Anthropic Console → **spend limit** месяца = бюджет × 1,2
  (жёсткий потолок на случай ошибки в квотах). [владелец] — сумма бюджета.
- F1 (общий бюджет выбирается группой аккаунтов): тревога — ежечасно
  `docker service logs --since 1h $SVC 2>&1 | grep -c 'reason=budget_exhausted'` > 0 до 18:00 UTC → разбор
  (`ai_usage` по `subject`, блокировка злоупотребляющих через `MINIAPP_BLOCKED_USER_IDS`). Автоматизация этой
  проверки в `host-monitor.sh` — отдельная задача (одна строка + тест), до S3.

## 5. Откат (на любом этапе)

| Ситуация | Действие | Время |
|---|---|---|
| S1: проблемы у тестеров | вернуть Menu Button тестерам (сохранённое значение), allowlist = владелец → Redeploy | 5 мин |
| S2/S3: сбой Mini App | BotFather: вернуть **записанные** Menu Button и Main Mini App (P11) → бот работает как раньше; затем `MINIAPP_ALLOWED_USER_IDS=<владелец>` → Redeploy | 5–10 мин |
| Массовые проблемы входа | `MINIAPP_LOGINS_ENABLED=false` → Redeploy; при компрометации сессий — `docker exec -u miniapp <ctr> python -m miniapp.admin revoke-all --yes` | 5 мин |
| Перерасход AI | убрать `MINIAPP_ANTHROPIC_API_KEY` (кнопка AI скрывается) → Redeploy; spend limit Console — страховка | 5 мин |
| Повреждение данных | `production.md` §7 «Восстановление» из ближайшего архива (Mac → бакет), учения — `offsite-backup.md` §5 | 30–60 мин |
| Плохой релиз | Dokploy → Deploy предыдущего коммита (схема только расширяется) | 10 мин |

## 6. Что требует разрешения владельца — по порядку

1. Создать аккаунт healthchecks.io и 6 проверок, передать URL (`healthchecks-owner.md`) — **владелец**.
2. Пароли архивов и rclone — в менеджер паролей и Keychain; SSH-ключ выгрузки — **владелец**.
3. Регистрация Backblaze B2, бакет, ключ без удаления, `rclone config` (`offsite-backup.md` §2) — **владелец**
   (бесплатно до 10 ГБ; платно — отдельное разрешение).
4. Скрипты в `/opt/proqnozai-ops/`, строка ключа в `authorized_keys`, cron мониторинга — **разрешение** (SSH, запись).
5. Закрытие `:3000` (DOCKER-USER + DO Cloud Firewall) и правка `dokploy.yml` — **разрешение** (firewall).
6. Гейт третьей копии и удаление `bot-backup-20261113T080743Z.db` (и старых копий) — **разрешение** (SSH чтение, затем запись).
7. Выкладка `fix/miniapp-security` (S0) — **разрешение** (merge + deploy).
8. Реализация F2 + Codex — разрешение на работу; выкладка — отдельно.
9. S1 → S2 → S3 → S4 — каждое отдельно.
10. Бюджет AI и spend limit — **владелец**.
