# Spor AI — Telegram Mini App (M1 + M2)

React + TypeScript + Vite, CSS Modules + общие CSS variables
(`src/styles/tokens.css`). Работает полностью офлайн на `DemoDataProvider`:
без токенов, backend и платных API. План этапов и статус сценариев Figma —
`docs/miniapp/implementation-plan.md`.

Требуется Node `^22.12` или `>=24` (проверено на 24.0.2). Зависимости
ставятся локально, версии зафиксированы в `package-lock.json`.

```bash
cd frontend
npm ci

npm run dev          # http://127.0.0.1:5173/
npm run typecheck
npm run lint
npm test             # vitest: фильтры, поиск, часовые пояса, состояния, языки
npm run build        # dist/
npm run check:bundle # нет временных URL Figma и секретов в dist/
npm run test:e2e     # Playwright в локальном Google Chrome: 360/390/430 px
npm run test:e2e:auth # вход через настоящий backend (miniapp/.venv), тестовый initData
```

`npm run dev` проксирует `/api` на `http://127.0.0.1:8000` (backend Mini App,
запуск — `docs/miniapp/auth.md`). Без backend приложение работает в
демо-режиме, профиль показывает статус входа.

Демо-ошибки: `http://127.0.0.1:5173/?demoFail=events,search,ai` — каждая
операция падает один раз, «Повторить» возвращает данные.

## Структура

```
src/
  config/brand.ts         # название и знак продукта (рабочее «Spor AI»)
  data/                   # модели по видам спорта, фикстуры, фильтры, DemoDataProvider
  i18n/                   # словари az / ru / en, форматирование дат
  settings/               # язык, часовой пояс, анимации — localStorage
  telegram/telegram.ts    # адаптер WebApp: ready/expand, BackButton, viewport, safe area, initData
  auth/                   # клиент /api, машина состояний входа (без автоповторов)
  time/                   # Clock (системный / фиксированный), календарь в IANA-поясе
  components/             # AppShell, Header, BottomNavigation, SportTabs, DateStrip,
                          # TournamentStrip, SearchField, EventCard, StatusBadge, States
  screens/                # Home, Catalog, Event, Ai, Profile, Language/Timezone/Appearance
  assets/figma/           # SVG из Figma (логотипы турниров, эмблемы, иконки)
```

## Ограничения

- Вход (M2): raw `initData` один раз уходит в POST `/api/auth/session`,
  дальше работает HttpOnly-cookie сервера. `initDataUnsafe` даёт только
  начальный язык, имя в профиле берётся из проверенной сессии. Токен бота,
  ключи API и тестовый ключ подписи во фронтенд не попадают (проверяет
  `npm run check:bundle`).
- Спортивные данные и AI остаются демо независимо от входа.
- Вне Telegram приложение работает в локальном демо-режиме; SDK Telegram
  подключается официальным скриптом `telegram-web-app.js`.
- Проверки ширин — эмуляция в настольном Chrome, не реальные устройства.
