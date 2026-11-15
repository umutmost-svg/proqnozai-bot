# M1 → M2: состояние на старте M2

Зафиксировано 2026-11-06 перед началом M2.

## Репозиторий

| | |
|---|---|
| Git root | `/Users/muradimamaliev/proqnozai-bot` |
| Рабочая ветка | `feat/miniapp-m1` (локальная, не запушена) |
| База | `origin/main` = `8ad4eec` (`Merge branch 'feat/growth-ltv-metrics'`) |
| Коммиты M1 | нет — вся работа M1 не закоммичена |
| Статус на старте M2 | ` M .gitignore`, `?? docs/miniapp/implementation-plan.md`, `?? frontend/` (108 путей вместе с untracked-файлами) |

M2 продолжает работу в той же ветке и в том же рабочем дереве, поверх
незакоммиченного M1. reset, clean, stash, rebase и переключение веток не
выполнялись.

## Что было в M1

- `frontend/` — Mini App на React + TypeScript + Vite, демо-данные
  (`DemoDataProvider`), экраны по Figma, AZ/RU/EN, часовые пояса,
  Telegram-адаптер без авторизации.
- `docs/miniapp/implementation-plan.md` — этапы M1–M5 и статус сценариев.
- `.gitignore` — добавлены `frontend/node_modules`, `dist`, `test-results`,
  `playwright-report`.
- Python-код бота, `bot.db`, `.env`, Railway, BotFather и webhook не менялись.

## Перепроверка M1 в ходе M2

Результаты из отчёта M1 не использовались как текущие. Проверки
перезапущены локально (Node 24.0.2, Python 3.13.14). Тесты M1 прогнаны уже
после подключения auth-контроллера к приложению, но до добавления тестов M2.
Это регрессионная проверка, а не снимок нетронутого M1.

| Проверка | Результат |
|---|---|
| `npm run typecheck`, `npm run lint` | без ошибок |
| `npx vitest run` (52 теста M1) | 52/52 |
| `npx playwright test` (вёрстка M1, 360/390/430) | 42/42 |
| `pytest tests/` (офлайн-тесты бота) | 1090 passed |

## Ограничения, действующие в M2

- Без commit, push, merge, rebase и deploy.
- Не трогать Railway, BotFather, webhook и polling, реальные `.env` и
  секреты, рабочие базы данных.
- Не ставить зависимости глобально. Python-зависимости Mini App ставятся в
  `miniapp/.venv`, Node-зависимости — в `frontend/node_modules`.
- Не запускать `test_e2e.py` и production-бота.
- Реальные спортивные данные, AI, избранное, история и уведомления — вне M2.

## Известные риски на старте

- `Dockerfile` делает `COPY . .` при отсутствии `.dockerignore`: в
  локальный образ попали бы `.env`, `.venv`, `frontend/node_modules`, `.git`,
  логи и кэши. Закрыто в M2, см. `.dockerignore` и `auth.md` → «Docker».
- Scaffold `origin/claude/miniapp-scaffold` (`e5e6253`) не влит: он
  ответвлён от старого коммита. Используется в M2 выборочно.
