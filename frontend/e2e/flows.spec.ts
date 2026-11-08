import { expect, test, type Page } from '@playwright/test';

// Demo build, fixed clock: navigation flows across the main scenarios (M3.4).
const FIXED_NOW = new Date('2026-11-08T06:00:00Z');

async function open(page: Page, route: string, width: number) {
  await page.setViewportSize({ width, height: 844 });
  await page.addInitScript(() => {
    if (!localStorage.getItem('sporai.settings.v1')) {
      localStorage.setItem('sporai.settings.v1', JSON.stringify({ lang: 'ru', timeZone: 'auto', reduceMotion: true, haptics: true }));
    }
  });
  await page.route('https://telegram.org/**', (r) => r.fulfill({ body: '', contentType: 'text/javascript' }));
  await page.clock.setFixedTime(FIXED_NOW);
  await page.goto(`/#${route}`);
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0, { timeout: 15_000 });
}

for (const width of [360, 390, 430]) {
  test(`home → calendar → date → match @${width}px`, async ({ page }) => {
    await open(page, '/', width);
    await page.getByTestId('calendar-open').click();
    const sheet = page.getByTestId('calendar-sheet');
    await expect(sheet).toBeVisible();
    const box = await sheet.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    await sheet.getByRole('gridcell', { name: '9 ноября', exact: true }).click();
    await expect(sheet).toHaveCount(0);
    await expect(page.getByTestId('date-strip').getByRole('button', { name: '9 ноября' })).toHaveAttribute('aria-pressed', 'true');
    await page.getByTestId('hero').getByRole('button').click();
    await expect(page).toHaveURL(/#\/event\//);
  });

  test(`home → tournament → calendar → match → back @${width}px`, async ({ page }) => {
    await open(page, '/', width);
    await page.getByTestId('tournament-list').getByRole('link').first().click();
    await expect(page.getByTestId('tournament-head')).toBeVisible();
    await page.getByTestId('calendar-events').getByTestId('event-card').first().click();
    await expect(page.getByTestId('event-hero')).toBeVisible();
    await page.goBack();
    await expect(page.getByTestId('tournament-head')).toBeVisible();
  });

  test(`match → AI analysis → back → reopen @${width}px`, async ({ page }) => {
    await open(page, '/event/fb-rm-fcb', width);
    await page.getByRole('tab', { name: 'AI-разбор' }).click().catch(() => {});
    await page.goto('/#/event/fb-rm-fcb/ai');
    await expect(page.getByTestId('ai-result')).toBeVisible({ timeout: 10_000 });
    await page.goBack();
    await page.goForward();
    await expect(page.getByTestId('ai-result')).toBeVisible({ timeout: 10_000 });
  });

  test(`global search ignores the chosen date and tournament @${width}px`, async ({ page }) => {
    await open(page, '/matches', width);
    await page.getByTestId('date-strip').getByRole('button', { name: '11 ноября' }).click();
    await page.getByPlaceholder('Поиск матча или команды').fill('barcelona');
    await expect(page.getByTestId('search-results')).toBeVisible();
    await page.getByTestId('search-team').first().click();
    await expect(page.getByTestId('team-head')).toBeVisible();
    await page.goBack();
    await page.getByPlaceholder('Поиск матча или команды').fill('');
    await expect(page.getByTestId('date-strip').getByRole('button', { name: '11 ноября' })).toHaveAttribute('aria-pressed', 'true');
  });
}

test('profile → help and support → back', async ({ page }) => {
  await open(page, '/profile', 390);
  await page.getByRole('link', { name: /Помощь и поддержка/ }).click();
  await expect(page.getByTestId('support-faq')).toBeVisible();
  await expect(page.getByTestId('support-prepare')).toBeDisabled();
  await page.goBack();
  await expect(page.getByTestId('account')).toBeVisible();
});
