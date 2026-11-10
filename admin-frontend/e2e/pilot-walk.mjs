// Browser walk-through of the LOCAL admin pilot: every screen + the main scenarios, console errors, screenshots.
// Needs a freshly seeded pilot (python -m miniapp.admin_web init) served on BASE; it CHANGES that pilot's data
// (revokes sessions, toggles a setting, invites an admin), so run it on a throw-away directory, not your pilot:
//   PILOT_DIR=/tmp/x BASE=http://127.0.0.1:8796 SHOTS=/tmp/shots node e2e/pilot-walk.mjs
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import { readFileSync, mkdirSync } from 'node:fs';

const BASE = process.env.BASE ?? 'http://127.0.0.1:8796';
const DIR = process.env.PILOT_DIR;
const SHOTS = process.env.SHOTS ?? '/tmp/sporbot-admin-shots';
if (!DIR) throw new Error('PILOT_DIR is required');
const REPO = new URL('../..', import.meta.url).pathname;
mkdirSync(SHOTS, { recursive: true });
const creds = readFileSync(`${DIR}/credentials.txt`, 'utf8');
const pw = (role) => creds.match(new RegExp(`${role}: \\S+\\n  password: (\\S+)`))[1];
const code = (email) => execFileSync(`${REPO}/miniapp/.venv/bin/python`, ['-m', 'miniapp.admin_web', 'code', email],
  { cwd: REPO, env: { ...process.env, ADMIN_PILOT_DIR: DIR } }).toString().trim().split(' ')[0];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function freshCode(email, used) {
  for (;;) { const c = code(email); if (!used.has(c)) { used.add(c); return c; } await sleep(2000); }
}

const vis = (loc, t = 6000) => loc.first().waitFor({ state: 'visible', timeout: t }).then(() => true).catch(() => false);
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));
const used = new Set();
const shot = (n) => page.screenshot({ path: `${SHOTS}/${n}.png`, fullPage: true });

async function login(email, role) {
  await page.goto(BASE + '/');
  await page.getByLabel('Электронная почта').fill(email);
  await page.getByLabel('Пароль').fill(pw(role));
  await page.getByRole('button', { name: 'Продолжить' }).click();
  await page.getByText('Подтверждение входа').waitFor();
  const c = await freshCode(email, used);
  await page.getByLabel('Цифра 1').fill(c[0]);
  for (let i = 1; i < 6; i++) await page.getByLabel(`Цифра ${i + 1}`).fill(c[i]);
  await page.getByRole('button', { name: 'Подтвердить' }).click();
  await page.getByRole('heading', { name: 'Обзор продукта' }).waitFor();
}

// 1. login errors + screens
await page.goto(BASE + '/');
await shot('01-login');
await page.getByLabel('Электронная почта').fill('owner@sporbot.local');
await page.getByLabel('Пароль').fill('wrong-password');
await page.getByRole('button', { name: 'Продолжить' }).click();
check('wrong password message', await vis(page.getByText('Неверная электронная почта или пароль')));
await page.getByLabel('Пароль').fill(pw('owner'));
await page.getByRole('button', { name: 'Продолжить' }).click();
await page.getByText('Подтверждение входа').waitFor();
await shot('02-2fa');
for (let i = 0; i < 6; i++) await page.getByLabel(`Цифра ${i + 1}`).fill('0');
await page.getByRole('button', { name: 'Подтвердить' }).click();
check('wrong 2fa message', await vis(page.getByText('Неверный или уже использованный код')));
const c0 = await freshCode('owner@sporbot.local', used);
await page.getByLabel('Цифра 1').focus();
await page.keyboard.insertText(c0); // paste-like
for (let i = 0; i < 6; i++) await page.getByLabel(`Цифра ${i + 1}`).fill(c0[i]);
await page.getByRole('button', { name: 'Подтвердить' }).click();
await page.getByRole('heading', { name: 'Обзор продукта' }).waitFor();
check('owner logged in', true);
await shot('03-overview');

// 2. navigation through all sections
const sections = [['Пользователи', 'Пользователи'], ['Прогнозы', 'Аналитика прогнозов'], ['AI и расходы', 'AI и расходы'],
  ['Спортивные API', 'Спортивные API'], ['Система и логи', 'Система и логи'], ['Настройки', 'Настройки'], ['Кредиты', 'Кредиты'], ['Обзор', 'Обзор продукта']];
for (const [nav, title] of sections) {
  await page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: nav, exact: true }).click();
  const ok = await vis(page.getByRole('heading', { level: 1, name: title, exact: true }));
  check(`nav ${nav}`, ok);
}
// empty states without demo
await page.goto(BASE + '/ai');
check('AI not collected empty state', await vis(page.getByText('История AI-вызовов пока не собирается')));
await shot('04-ai-empty');
await page.goto(BASE + '/sports-api');
check('API not collected', await vis(page.getByText('Состояние провайдеров пока не собирается')));
await page.goto(BASE + '/system');
check('System not collected', await vis(page.getByText('Журнал событий пока не собирается')));
await shot('05-system-empty');

// 3. period selection
await page.goto(BASE + '/');
const ov7 = page.waitForResponse((r) => r.url().includes('/api/admin/overview?days=7'));
await page.getByLabel('Период').selectOption('7');
check('period 7 request', Boolean(await ov7));

// 4. users: search, filter, sort, card, back
await page.goto(BASE + '/users');
await page.locator('tbody tr').first().waitFor();
await shot('06-users');
const firstBefore = await page.locator('tbody tr td').first().textContent();
await page.getByRole('button', { name: /Telegram ID/ }).click();
await page.waitForTimeout(400);
const firstAsc = await page.locator('tbody tr td').first().textContent();
check('users sort changes order', firstBefore !== firstAsc, `${firstBefore} -> ${firstAsc}`);
await page.getByLabel('Поиск').fill('demo_elvin');
await page.waitForTimeout(700);
const rowsSearch = await page.locator('tbody tr').count();
check('users search filters', rowsSearch >= 1 && rowsSearch < 15, String(rowsSearch));
await page.getByLabel('Поиск').fill('zzzz-nobody');
check('users empty search state', await vis(page.getByText('Никого не нашли')));
await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
const azLoaded = page.waitForResponse((r) => r.url().includes('lang=az'));
await page.getByLabel('Язык').selectOption('az');
await azLoaded;
await page.locator('table[aria-busy="false"]').waitFor();
const langs = await page.locator('tbody .sub').allTextContents();
check('lang filter', langs.length > 0 && langs.every((t) => t.endsWith('az')), String(langs.length));
await page.getByLabel('Язык').selectOption('');
await page.waitForTimeout(400);
// choose a user with active sessions
await page.getByRole('button', { name: /Сессии/ }).click();
await page.waitForTimeout(400);
await page.locator('tbody tr').first().click();
await page.getByRole('button', { name: 'Посмотреть карточку' }).click();
await page.getByRole('heading', { name: 'Карточка пользователя' }).waitFor();
await shot('07-user-card');
// revoke dialog: cancel, then confirm
await page.getByRole('button', { name: 'Отозвать сессии' }).click();
await shot('08-revoke-dialog');
check('revoke confirm disabled until valid', await page.getByRole('button', { name: 'Отозвать сессии' }).last().isDisabled());
await page.getByRole('button', { name: 'Отмена' }).click();
await page.getByRole('button', { name: 'Отозвать сессии' }).click();
await page.getByLabel('Причина').fill('Пилот: проверка отзыва');
await page.getByLabel('Код двухфакторной аутентификации').fill('000000');
await page.getByRole('dialog').getByRole('button', { name: 'Отозвать сессии' }).click();
check('revoke wrong code error', await vis(page.getByText('Неверный или уже использованный код')));
await page.getByLabel('Код двухфакторной аутентификации').fill(await freshCode('owner@sporbot.local', used));
await page.getByRole('dialog').getByRole('button', { name: 'Отозвать сессии' }).click();
check('revoke toast', await vis(page.getByText(/Отозвано сессий: \d+/)));
check('revoke in user journal', await vis(page.getByText('Пилот: проверка отзыва').first()));
await page.getByRole('button', { name: '← К списку' }).click();
check('back to list', await vis(page.getByRole('heading', { name: 'Пользователи', level: 1 })));

// 5. forecasts: filters, drawer
await page.goto(BASE + '/forecasts');
await page.locator('tbody tr').first().waitFor();
await shot('09-forecasts');
await page.getByLabel('Статус').selectOption('settled');
await page.waitForTimeout(500);
const statuses = await page.locator('tbody td:nth-child(5) .chip').allTextContents();
check('status filter', statuses.length > 0 && statuses.every((s) => s === 'Сверено'), String(statuses.length));
await page.locator('tbody tr').first().click();
await page.getByRole('dialog', { name: 'Карточка прогноза' }).waitFor();
check('forecast drawer journal', await vis(page.getByText('Журнал ревизий итога')));
await shot('10-forecast-drawer');
await page.keyboard.press('Escape');
await page.getByLabel('Статус').selectOption('');
await page.getByLabel('Поиск матча').fill('Барселона');
await page.waitForTimeout(700);
const matches = await page.locator('tbody td.strong').allTextContents();
check('match search', matches.length > 0 && matches.every((m) => m.includes('Барселона')), String(matches.length));
await page.getByLabel('Поиск матча').fill('');
await page.getByLabel('Турнир').selectOption({ index: 1 });
await page.waitForTimeout(500);
check('tournament filter rows', (await page.locator('tbody tr').count()) > 0);

// 6. demo telemetry
await page.goto(BASE + '/ai');
await page.getByRole('button', { name: 'Показать демо-данные' }).click();
await page.locator('tbody tr').first().waitFor();
check('demo banner', await vis(page.getByText('Демо-телеметрия включена.')));
await shot('11-ai-demo');
await page.getByRole('button', { name: /Стоимость/ }).click();
await page.getByLabel('Модель').selectOption({ index: 1 });
check('ai filtered rows', (await page.locator('tbody tr').count()) > 0);
await page.goto(BASE + '/');
await shot('12-overview-demo');
await page.goto(BASE + '/sports-api');
await shot('13-api-demo');
await page.goto(BASE + '/system');
await page.getByLabel('Уровень').selectOption('error');
await shot('14-system-demo');
await page.getByRole('checkbox', { name: 'Демо-телеметрия' }).uncheck();

// 7. settings tabs + toggle with confirmation
await page.goto(BASE + '/settings');
await shot('15-settings');
await page.getByRole('switch', { name: 'Быстрый прогноз' }).click();
await page.getByLabel('Причина').fill('Пилот: проверка переключателя');
await page.getByLabel('Код двухфакторной аутентификации').fill(await freshCode('owner@sporbot.local', used));
await page.getByRole('button', { name: 'Сохранить изменение' }).click();
check('toggle saved', await vis(page.getByText('Настройка сохранена')));
check('toggle off now', (await page.getByRole('switch', { name: 'Быстрый прогноз' }).getAttribute('aria-checked')) === 'false');
await page.getByRole('link', { name: 'Администраторы', exact: true }).click();
await page.getByRole('heading', { name: 'Администраторы и роли' }).waitFor();
await shot('16-admins');
check('last owner protected', await page.getByLabel('Роль owner@sporbot.local').isDisabled());
await page.getByRole('button', { name: 'Добавить администратора' }).click();
await page.locator('#inv-email').fill('new.analyst@sporbot.local');
await page.getByLabel('Причина').fill('Пилот: новый аналитик');
await page.getByLabel('Код двухфакторной аутентификации').fill(await freshCode('owner@sporbot.local', used));
await page.getByRole('button', { name: 'Создать приглашение' }).click();
check('invite created', await vis(page.getByText('new.analyst@sporbot.local').first()));
await page.getByRole('link', { name: 'Журнал действий', exact: true }).click();
await page.getByRole('heading', { name: 'Журнал действий', level: 1 }).waitFor();
await page.locator('tbody tr').first().waitFor();
await shot('17-journal');
const journalRows = await page.locator('tbody tr').count();
check('journal has events', journalRows >= 4, String(journalRows));
await page.getByLabel('Действие').selectOption('settings.');
await page.waitForTimeout(400);
check('journal action filter', (await page.locator('tbody tr').count()) === 1);
await page.goto(BASE + '/credits');
await shot('18-credits');
check('credits buttons disabled', await page.getByRole('button', { name: 'Начислить' }).isDisabled());
await page.goto(BASE + '/nope');
check('404 page', await vis(page.getByText('Страница не найдена')));

// 8. logout + analyst permissions
await page.goto(BASE + '/');
await page.getByRole('button', { name: 'Выйти' }).click();
await page.getByText('Вход в систему').waitFor();
check('logout', true);
await login('analyst@sporbot.local', 'analyst');
await page.goto(BASE + '/users');
check('analyst no personal data', await vis(page.getByText('Недостаточно прав')));
await page.goto(BASE + '/settings');
check('analyst toggles disabled', await page.getByRole('switch', { name: 'AI-разборы' }).isDisabled());
await shot('19-analyst-users');

// 9. error state: backend error
await page.route('**/api/admin/overview**', (r) => r.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"internal","requestId":"abc123"}' }));
await page.goto(BASE + '/');
check('error state with request id', await vis(page.getByText('request_id: abc123')));
await shot('20-error');
await page.unroute('**/api/admin/overview**');

const real = consoleErrors.filter((e) => !e.includes('500') && !e.includes('401') && !e.includes('403'));
check('no console errors', real.length === 0, real.slice(0, 5).join(' | '));
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
