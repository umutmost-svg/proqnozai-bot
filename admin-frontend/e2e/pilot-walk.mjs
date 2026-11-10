// Browser walk-through of the LOCAL admin pilot (CRM v2): every section + the main scenarios, console errors,
// screenshots. Needs a freshly seeded pilot (python -m miniapp.admin_web init) served on BASE; it CHANGES that
// pilot's data (tags, notes, credits, broadcasts, blocks, settings), so run it on a throw-away directory:
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
const used = new Set();
async function freshCode(email) {
  for (;;) { const c = code(email); if (!used.has(c)) { used.add(c); return c; } await sleep(2000); }
}
const vis = (loc, t = 6000) => loc.first().waitFor({ state: 'visible', timeout: t }).then(() => true).catch(() => false);
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`); };

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));
const shot = (n) => page.screenshot({ path: `${SHOTS}/${n}.png`, fullPage: true });
const confirm2fa = async (email, button) => {
  await page.getByLabel('Причина').fill('e2e: проверка');
  await page.getByLabel('Код двухфакторной аутентификации').fill(await freshCode(email));
  await page.getByRole('dialog').getByRole('button', { name: button }).click();
};

async function login(email, role) {
  await page.goto(BASE + '/');
  await page.getByLabel('Электронная почта').fill(email);
  await page.getByLabel('Пароль').fill(pw(role));
  await page.getByRole('button', { name: 'Продолжить' }).click();
  await page.getByText('Подтверждение входа').waitFor();
  const c = await freshCode(email);
  for (let i = 0; i < 6; i++) await page.getByLabel(`Цифра ${i + 1}`).fill(c[i]);
  await page.getByRole('button', { name: 'Подтвердить' }).click();
  await page.getByRole('heading', { name: 'Аналитика', level: 1 }).waitFor();
}

// 1. login, wrong password / code
await page.goto(BASE + '/');
await page.getByLabel('Электронная почта').fill('owner@sporbot.local');
await page.getByLabel('Пароль').fill('wrong-password');
await page.getByRole('button', { name: 'Продолжить' }).click();
check('wrong password message', await vis(page.getByText('Неверная электронная почта или пароль')));
await page.getByLabel('Пароль').fill(pw('owner'));
await page.getByRole('button', { name: 'Продолжить' }).click();
await page.getByText('Подтверждение входа').waitFor();
for (let i = 0; i < 6; i++) await page.getByLabel(`Цифра ${i + 1}`).fill('0');
await page.getByRole('button', { name: 'Подтвердить' }).click();
check('wrong 2fa message', await vis(page.getByText('Неверный или уже использованный код')));
const c0 = await freshCode('owner@sporbot.local');
for (let i = 0; i < 6; i++) await page.getByLabel(`Цифра ${i + 1}`).fill(c0[i]);
await page.getByRole('button', { name: 'Подтвердить' }).click();
check('owner logged in', await vis(page.getByRole('heading', { name: 'Аналитика', level: 1 })));
await shot('01-analytics');

// 2. navigation
for (const [nav, title] of [['Пользователи', 'Пользователи'], ['Диалоги', 'Диалоги'], ['Рассылки', 'Рассылки'], ['Сегменты', 'Сегменты'],
  ['Кредиты', 'Кредиты'], ['Прогнозы', 'Аналитика прогнозов'], ['AI и расходы', 'AI и расходы'], ['Спортивные API', 'Спортивные API'],
  ['Система', 'Система и логи'], ['Настройки', 'Настройки'], ['Аналитика', 'Аналитика']]) {
  await page.getByRole('link', { name: nav, exact: true }).click();
  check(`nav ${nav}`, await vis(page.getByRole('heading', { level: 1, name: title, exact: true })));
}

// 3. analytics: tabs, period, empty states vs demo
await page.getByRole('tab', { name: 'Аудитория и гео' }).click();
check('geo not collected without demo', await vis(page.getByText('География пока не собирается')));
check('real languages shown', await vis(page.getByText('Языки интерфейса')));
const ov7 = page.waitForResponse((r) => r.url().includes('/api/admin/analytics?days=7'));
await page.getByLabel('Период').selectOption('7');
check('period 7 request', Boolean(await ov7));
await page.getByRole('checkbox', { name: 'Демо-телеметрия' }).check();
check('demo geo appears', await vis(page.getByText('Азербайджан')));
await page.getByRole('tab', { name: 'Время активности' }).click();
check('heatmap + best time', await vis(page.getByText('Лучшее время для рассылки')));
await shot('02-analytics-time');
await page.getByRole('tab', { name: 'Удержание' }).click();
check('cohorts', await vis(page.getByText('Удержание по когортам')));
await page.getByRole('checkbox', { name: 'Демо-телеметрия' }).uncheck();

// 4. users: global search, chips, tag filter, bulk tag
await page.getByLabel('Глобальный поиск').fill('demo_elvin');
await page.getByLabel('Глобальный поиск').press('Enter');
await page.locator('tbody tr').first().waitFor();
await page.locator('table[aria-busy="false"]').waitFor();
const found = await page.locator('tbody tr').count();
check('global search -> users', found >= 1 && found < 15, String(found));
await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
await page.locator('table[aria-busy="false"]').waitFor();
await page.getByRole('button', { name: /^Уходящие 14д\+/ }).click();
await page.locator('table[aria-busy="false"]').waitFor();
check('chip filter', await vis(page.getByRole('button', { name: /^Уходящие 14д\+/, pressed: true })));
await page.getByRole('button', { name: /^Все/ }).first().click();
await page.locator('table[aria-busy="false"]').waitFor();
const tagResp = page.waitForResponse((r) => r.url().includes('tag=VIP'));
await page.getByLabel('Тег').selectOption('VIP');
await tagResp;
await page.locator('table[aria-busy="false"]').waitFor();
const vipRows = await page.locator('tbody tr').count();
check('tag filter', vipRows === 5, String(vipRows));
await page.getByLabel('Тег').selectOption('');
await page.locator('table[aria-busy="false"]').waitFor();
await page.waitForTimeout(300);
await page.getByLabel(/^Выбрать 99000/).nth(0).check();
await page.getByLabel(/^Выбрать 99000/).nth(1).check();
check('bulk bar', await vis(page.getByText('Выбрано: 2')));
await page.getByRole('region', { name: 'Массовые действия' }).getByRole('button', { name: 'Добавить тег' }).click();
await page.locator('#tag-input').fill('e2e-тег');
await page.getByRole('dialog').getByRole('button', { name: 'Добавить' }).click();
check('bulk tag toast', await vis(page.getByText('Тег «e2e-тег» добавлен: 2')));
await shot('03-users');

// 5. user card: note, message, credits (2FA), block (2FA), back
await page.locator('tbody tr').first().click();
await page.getByRole('heading', { level: 2 }).first().waitFor();
await page.getByLabel('Новая заметка').fill('e2e: заметка команды');
await page.getByRole('button', { name: '+ заметка' }).click();
check('note added', await vis(page.getByText('Заметка добавлена')));
await page.getByLabel('Текст сообщения').fill('Здравствуйте! Это тест e2e.');
await page.getByRole('button', { name: 'Отправить', exact: true }).click();
check('message stored (pilot)', await vis(page.getByText('Сообщение записано')));
check('message in timeline', await vis(page.getByText('Здравствуйте! Это тест e2e.')));
await page.getByRole('button', { name: 'Начислить / списать' }).click();
await page.getByLabel('Сумма').fill('25');
await confirm2fa('owner@sporbot.local', 'Провести операцию');
check('credits granted', await vis(page.getByText('Начислено 25 × 1')));
await page.getByRole('tab', { name: 'Кредиты' }).click();
check('ledger in card', await vis(page.getByRole('cell', { name: '+25' })));
await shot('04-card');
await page.getByRole('button', { name: 'Заблокировать' }).click();
await confirm2fa('owner@sporbot.local', 'Заблокировать');
check('blocked', await vis(page.getByText('Пользователь заблокирован')));
check('unblock offered', await vis(page.getByRole('button', { name: 'Разблокировать' })));
await page.getByRole('link', { name: '← Пользователи' }).click();
check('back to list', await vis(page.getByRole('heading', { name: 'Пользователи', level: 1 })));

// 6. segments: create, link to broadcast
await page.goto(BASE + '/segments');
await page.getByRole('button', { name: 'Новый сегмент' }).click();
await page.getByLabel('Название').fill('e2e: Ла Лига без жалоб');
await page.getByLabel('Есть тег').selectOption('Ла Лига');
await page.getByLabel('Исключить тег').selectOption('Жалоба');
await page.getByRole('button', { name: 'Сохранить' }).click();
check('segment saved', await vis(page.getByText('Сегмент сохранён')));
check('segment in list', await vis(page.getByRole('cell', { name: /e2e: Ла Лига без жалоб/ })));
await shot('05-segments');

// 7. broadcast: create -> schedule with 2FA -> cancel
await page.getByRole('button', { name: 'Рассылка по сегменту' }).click();
await page.getByLabel('Название кампании (видно только в админке)').fill('e2e: тестовая кампания');
await page.locator('#b-body').fill('{имя}, привет! Новые разборы уже ждут.');
check('telegram preview', await vis(page.getByText('Айгерим, привет! Новые разборы уже ждут.')));
await page.getByRole('button', { name: 'Запланировать' }).click();
await page.getByRole('dialog').waitFor();
await confirm2fa('owner@sporbot.local', 'Запланировать');
check('broadcast scheduled', await vis(page.getByText('Рассылка запланирована')));
await shot('06-broadcast');
await page.goto(BASE + '/broadcasts');
check('broadcast listed', await vis(page.getByRole('link', { name: 'e2e: тестовая кампания' })));
await page.getByRole('row', { name: /e2e: тестовая кампания/ }).getByRole('button', { name: 'Отменить' }).click();
check('broadcast cancelled', await vis(page.getByText('Рассылка отменена')));

// 8. credits page, dialogs demo, settings + journal
await page.goto(BASE + '/credits');
check('credits ledger has e2e grant', await vis(page.getByText('e2e: проверка')));
await page.goto(BASE + '/dialogs');
check('dialogs not collected without demo', await vis(page.getByText('Переписка с пользователями пока не собирается')));
await page.getByRole('checkbox', { name: 'Демо-телеметрия' }).check();
check('demo dialogs', await vis(page.getByRole('button', { name: /Опять ошибка/ })));
await shot('07-dialogs');
await page.getByRole('checkbox', { name: 'Демо-телеметрия' }).uncheck();
await page.goto(BASE + '/settings');
await page.getByRole('switch', { name: 'Быстрый прогноз' }).click();
await confirm2fa('owner@sporbot.local', 'Сохранить изменение');
check('setting saved', await vis(page.getByText('Настройка сохранена')));
await page.getByRole('link', { name: 'Журнал действий', exact: true }).click();
await page.locator('tbody tr').first().waitFor();
const journal = await page.locator('tbody tr').count();
check('journal has CRM events', journal >= 8, String(journal));
await page.goto(BASE + '/nope');
check('404 page', await vis(page.getByText('Страница не найдена')));

// 9. analyst: no personal data, no CRM writes
await page.goto(BASE + '/');
await page.getByRole('button', { name: 'Выйти' }).click();
await page.getByText('Вход в систему').waitFor();
await login('analyst@sporbot.local', 'analyst');
await page.goto(BASE + '/users');
check('analyst: no users', await vis(page.getByText('Недостаточно прав')));
await page.goto(BASE + '/credits');
check('analyst: no credits', await vis(page.getByText('Недостаточно прав')));
await page.goto(BASE + '/broadcasts');
await page.getByRole('heading', { name: 'Рассылки', level: 1 }).waitFor();
check('analyst: broadcasts read-only', await page.getByRole('button', { name: 'Новая рассылка' }).isDisabled());

// 10. error state
await page.route('**/api/admin/analytics**', (r) => r.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"internal","requestId":"abc123"}' }));
await page.goto(BASE + '/');
check('error state with request id', await vis(page.getByText('request_id: abc123')));
await page.unroute('**/api/admin/analytics**');

const real = consoleErrors.filter((e) => !e.includes('500') && !e.includes('401') && !e.includes('403'));
check('no console errors', real.length === 0, real.slice(0, 5).join(' | '));
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
