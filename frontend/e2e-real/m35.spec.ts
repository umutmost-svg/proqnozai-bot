import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { fixtures, mockApi } from './api';

/**
 * M3.5 in the real-mode build: tournament → matches → table → team → match → AI analysis
 * → back to the tournament with its tab kept; at 360 / 390 / 430 px and in all 7 languages.
 * M35_SHOTS=<dir> also saves screenshots (with M35_FIXTURES: real provider data, offline).
 */
const DATA = fixtures();
const MATCH = process.env.M35_MATCH ?? 'fd:4';
const SHOTS = process.env.M35_SHOTS;
const LANGS = ['ru', 'az', 'en', 'tr', 'uz', 'pt-BR', 'es'] as const;
const enc = (id: string) => encodeURIComponent(id);
const matchEvent = (DATA[`/api/sports/event/${MATCH}`] as { event: { home: { id: string; name: string } } }).event;

async function open(page: Page, route: string, width: number, lang = 'ru', height = 844) {
  await page.setViewportSize({ width, height });
  const asked = await mockApi(page, DATA, lang);
  await page.goto(`/#${route}`);
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0, { timeout: 15_000 });
  return asked;
}

async function noHorizontalScroll(page: Page, width: number) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(overflow).toBeLessThanOrEqual(width);
}

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  // The app scrolls inside its shell: make the page tall enough to hold all of it.
  const height = await page.evaluate(() => {
    const main = document.querySelector('main') ?? document.body;
    return Math.ceil(main.scrollHeight + 200);
  });
  await page.setViewportSize({ width: page.viewportSize()!.width, height: Math.min(Math.max(height, 844), 7000) });
  await page.waitForTimeout(200);
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
}

for (const width of [360, 390, 430]) {
  test(`tournament → table → team → match → AI → back keeps the tab @${width}px`, async ({ page }) => {
    const asked = await open(page, '/tournament/fd%3APL', width);
    await expect(page.getByTestId('calendar-events')).toBeVisible();
    await page.getByRole('tab', { name: 'Таблица' }).click();
    await expect(page).toHaveURL(/tab=table/);
    const rows = page.getByTestId('standings-row');
    await expect(rows.first()).toBeVisible();
    await noHorizontalScroll(page, width);
    // Priority columns are visible without scrolling the table.
    const points = page.getByTestId('standings-block').first().locator('thead th').nth(4);
    const box = await points.boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    await page.getByRole('tab', { name: 'Матчи' }).click();
    await page.getByRole('tab', { name: 'Таблица' }).click();
    expect(asked.filter((a) => a.endsWith('/standings'))).toHaveLength(1);
    expect(asked.filter((a) => a.endsWith('/calendar'))).toHaveLength(1);

    await page.locator(`[data-team="${matchEvent.home.id}"]`).first().getByRole('button').click();
    await expect(page).toHaveURL(new RegExp(`#/team/${enc(matchEvent.home.id)}`));
    await page.locator(`[data-testid="event-card"]`).first().click();
    await expect(page).toHaveURL(/#\/event\//);
    await page.goto(`/#/event/${enc(MATCH)}/ai`);
    await page.getByTestId('ai-create').click();
    await expect(page.getByTestId('ai-markets')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('ai-pick-caveat')).toBeVisible();
    await noHorizontalScroll(page, width);
    await page.goBack(); // the match opened from the team
    await page.goBack(); // team
    await page.goBack(); // tournament, table tab
    await expect(page).toHaveURL(/#\/tournament\/fd%3APL\?tab=table/);
    await expect(page.getByRole('tab', { name: 'Таблица' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('standings-row').first()).toBeVisible();
  });
}

test('Champions League league phase is one table', async ({ page }) => {
  await open(page, '/tournament/fd%3ACL?tab=table', 390);
  await expect(page.getByTestId('standings-block')).toHaveCount(1);
  await expect(page.getByTestId('standings-block')).toHaveAttribute('data-stage', 'LEAGUE_STAGE');
  await shot(page, 'table-cl-ru-390');
});

test('a source error on the table shows retry, not a made-up table', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, DATA);
  await page.route((url) => url.pathname.endsWith('/standings'), (r) =>
    r.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"source_unavailable","reason":"upstream_error"}' }));
  await page.goto('/#/tournament/fd%3APL?tab=table');
  await expect(page.getByText('Таблица недоступна')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('standings-row')).toHaveCount(0);
});

test('after kickoff no analysis is shown', async ({ page }) => {
  test.skip(!!process.env.M35_FIXTURES, 'synthetic fixtures only');
  await open(page, '/event/fd%3A7/ai', 390);
  await expect(page.getByTestId('ai-insufficient')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('ai-markets')).toHaveCount(0);
  await expect(page.getByTestId('ai-pick')).toHaveCount(0);
});

for (const lang of LANGS) {
  test(`AI analysis and table in ${lang}`, async ({ page }) => {
    await open(page, `/event/${enc(MATCH)}/ai`, 390, lang);
    await page.getByTestId('ai-create').click();
    await expect(page.getByTestId('ai-markets')).toBeVisible({ timeout: 15_000 });
    const text = (await page.locator('body').textContent()) ?? '';
    expect(text).not.toMatch(/\bai\.(pick|family|markets|stats|sources|alt|limits)\./);
    expect(text).not.toMatch(/\{\w+\}/);
    await noHorizontalScroll(page, 390);
    await shot(page, `ai-${lang}-390`);
    await page.goto('/#/tournament/fd%3APL?tab=table');
    await expect(page.getByTestId('standings-row').first()).toBeVisible();
    expect((await page.locator('body').textContent()) ?? '').not.toMatch(/\b(standings|tournament)\.\w/);
    await shot(page, `table-${lang}-390`);
  });
}

for (const width of [360, 430]) {
  test(`screenshots at ${width}px`, async ({ page }) => {
    test.skip(!SHOTS, 'screenshots only on request');
    await open(page, `/event/${enc(MATCH)}/ai`, width);
    await page.getByTestId('ai-create').click();
    await expect(page.getByTestId('ai-markets')).toBeVisible({ timeout: 15_000 });
    await shot(page, `ai-ru-${width}`);
    await page.goto('/#/tournament/fd%3APL?tab=table');
    await expect(page.getByTestId('standings-row').first()).toBeVisible();
    await shot(page, `table-ru-${width}`);
  });
}
