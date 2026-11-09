import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { fixtures, mockApi } from './api';

/**
 * M3.6 in the real-mode build: the day's top matches on the home screen and the quick
 * prediction tab (single and coupon), at 360 / 390 / 430 px and in all 7 languages.
 * M35_FIXTURES + M35_SHOTS: answers the real backend produced offline, plus screenshots.
 */
const DATA = fixtures();
const REAL = !!process.env.M35_FIXTURES;
const SHOTS = process.env.M35_SHOTS;
// The day the recorded (or synthetic) matches are played, 10:00 in Baku.
const NOW = REAL ? '2026-11-21T06:00:00Z' : '2099-11-21T06:00:00Z';
const LANGS = ['ru', 'az', 'en', 'tr', 'uz', 'pt-BR', 'es'] as const;

async function open(page: Page, route: string, width: number, lang = 'ru') {
  await page.setViewportSize({ width, height: 844 });
  await page.clock.setFixedTime(NOW);
  const asked = await mockApi(page, DATA, lang);
  await page.goto(`/#${route}`);
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0, { timeout: 15_000 });
  return asked;
}

async function noHorizontalScroll(page: Page, width: number) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
}

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  const height = await page.evaluate(() => Math.ceil((document.querySelector('main') ?? document.body).scrollHeight + 200));
  await page.setViewportSize({ width: page.viewportSize()!.width, height: Math.min(Math.max(height, 844), 7000) });
  await page.waitForTimeout(200);
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
}

async function noRawKeys(page: Page) {
  const text = (await page.locator('body').textContent()) ?? '';
  expect(text).not.toMatch(/\b(quick|top|nav)\.[a-z]/);
  expect(text).not.toMatch(/\{\w+\}/);
}

for (const width of [360, 390, 430]) {
  test(`home: top matches carousel @${width}px`, async ({ page }) => {
    const asked = await open(page, '/', width);
    const cards = page.getByTestId('top-card');
    await expect(cards.first()).toBeVisible({ timeout: 15_000 });
    expect(await cards.count()).toBeGreaterThanOrEqual(1);
    expect(await cards.count()).toBeLessThanOrEqual(5);
    await noHorizontalScroll(page, width);
    // The carousel scrolls itself, the page does not; no request beyond the day's ranking.
    expect(asked.filter((a) => a.includes('/api/sports/football/top'))).toHaveLength(1);
    await shot(page, `top-ru-${width}`);
    await cards.first().getByRole('button').click();
    await expect(page).toHaveURL(/#\/event\//);
  });

  test(`quick: single and coupon @${width}px`, async ({ page }) => {
    await open(page, '/quick', width);
    await expect(page.getByTestId('quick-criteria')).toBeVisible();
    await noHorizontalScroll(page, width);
    await shot(page, `quick-criteria-ru-${width}`);
    await page.getByTestId('quick-submit').click();
    const result = page.getByTestId('quick-result');
    await expect(result).toBeVisible({ timeout: 15_000 });
    await expect(result.getByTestId('quick-leg')).toHaveCount(1);
    await expect(result.getByTestId('quick-caveat')).toHaveCount(0);
    await expect(result.getByTestId('quick-experimental')).toHaveCount(0);
    await noHorizontalScroll(page, width);
    await shot(page, `quick-single-ru-${width}`);

    await page.getByTestId('quick-change').click();
    await page.getByTestId('quick-mode').getByRole('button', { name: 'Купон' }).click();
    await page.getByTestId('quick-size').getByRole('button', { name: '3' }).click();
    await page.getByTestId('quick-submit').click();
    await expect(result.getByTestId('quick-leg')).toHaveCount(3, { timeout: 15_000 });
    await expect(result.getByTestId('quick-combined')).toContainText('диапазон верен при любой связи');
    if (!REAL) await expect(page.getByTestId('quick-counts')).toContainText('Не в день матча по UTC: 1');
    await noHorizontalScroll(page, width);
    await shot(page, `quick-coupon-ru-${width}`);
    // The tab remembers the criteria and the last result after visiting a match.
    await result.getByTestId('quick-leg').first().getByRole('button').click();
    await expect(page).toHaveURL(/#\/event\//);
    await page.goBack();
    await expect(page.getByTestId('quick-result').getByTestId('quick-leg')).toHaveCount(3);
  });
}

test('quick: a coupon larger than the suitable matches is not padded', async ({ page }) => {
  test.skip(REAL, 'synthetic fixtures only');
  await open(page, '/quick', 390);
  await page.getByTestId('quick-mode').getByRole('button', { name: 'Купон' }).click();
  await page.getByTestId('quick-size').getByRole('button', { name: '5' }).click();
  await page.getByTestId('quick-submit').click();
  await expect(page.getByTestId('quick-not-enough')).toContainText('Нужно матчей: 5, подходят: 3');
  await expect(page.getByTestId('quick-leg')).toHaveCount(0);
  await shot(page, 'quick-not-enough-ru-390');
});

for (const lang of LANGS) {
  test(`top matches and quick prediction in ${lang}`, async ({ page }) => {
    await open(page, '/', 360, lang);
    await expect(page.getByTestId('top-card').first()).toBeVisible({ timeout: 15_000 });
    await noRawKeys(page);
    await noHorizontalScroll(page, 360);
    await page.goto('/#/quick');
    await page.getByTestId('quick-mode').locator('button').nth(1).click();
    await page.getByTestId('quick-submit').click();
    await expect(page.getByTestId('quick-result')).toBeVisible({ timeout: 15_000 });
    await noRawKeys(page);
    await noHorizontalScroll(page, 360);
    await shot(page, `quick-coupon-${lang}-360`);
  });
}
