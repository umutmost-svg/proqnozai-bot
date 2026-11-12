import { expect, test, type Page } from '@playwright/test';
import { fixtures, mockApi } from './api';

/**
 * Regression (iPhone report 2026-11-12): on a tournament whose chosen day sits at the END of a long day
 * strip (a finished season), centring the chip with scrollIntoView also scrolled the app's own
 * overflow:hidden containers, and the whole page slid off the left edge. At 320/375/390/430 px: no
 * container of the page may be scrolled sideways, nothing may stick out of the viewport, and the
 * flow list -> tournament -> matches -> table -> back keeps the page in place.
 */
const WIDTHS = [320, 375, 390, 430];
const NOW = '2100-01-10T06:00:00Z'; // after the last match: the strip opens on its last day

function longSeason() {
  const data = fixtures();
  const cal = data['/api/sports/football/tournaments/fd:PL/calendar'] as { events: Record<string, unknown>[] };
  const base = cal.events[0]!;
  // 24 match days: far more chips than any phone shows at once.
  const events = Array.from({ length: 24 }, (_, i) => ({
    ...base, id: `fd:${900 + i}`, status: 'finished', sourceStatus: 'FINISHED',
    score: { home: 1, away: 0 }, startsAt: new Date(Date.UTC(2099, 10, 1 + i * 2, 15)).toISOString(),
  }));
  data['/api/sports/football/tournaments/fd:PL/calendar'] = { ...cal, events };
  return data;
}

async function pageIsInPlace(page: Page, width: number) {
  const state = await page.evaluate(() => {
    const shifted: string[] = [];
    for (const el of [document.documentElement, document.body, ...document.querySelectorAll('body *')]) {
      if (el.scrollLeft <= 0) continue;
      const st = getComputedStyle(el);
      const strip = (st.overflowX === 'auto' || st.overflowX === 'scroll') && el.getBoundingClientRect().height < 160;
      if (!strip) shifted.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 30)}=${el.scrollLeft}`);
    }
    const main = document.querySelector('[data-testid="scroll-viewport"]')!.getBoundingClientRect();
    return { shifted, scrollWidth: document.documentElement.scrollWidth, mainLeft: main.left, mainRight: main.right };
  });
  expect(state.shifted, 'containers scrolled sideways').toEqual([]);
  expect(state.scrollWidth).toBeLessThanOrEqual(width);
  expect(state.mainLeft).toBeGreaterThanOrEqual(0);
  expect(state.mainRight).toBeLessThanOrEqual(width);
}

for (const width of WIDTHS) {
  test(`tournament page stays in place at ${width}px (last day of a long season)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.clock.setFixedTime(NOW);
    await mockApi(page, longSeason());
    await page.goto('/#/');
    await expect(page.getByTestId('app-loading-gate')).toHaveCount(0, { timeout: 15_000 });
    // list -> tournament
    await page.locator('a[href="#/tournament/fd%3APL"]').first().click();
    const days = page.getByTestId('calendar-days');
    await expect(days).toBeVisible();
    await expect(days.locator('button[aria-pressed="true"]')).toBeInViewport();
    await pageIsInPlace(page, width);
    // -> table -> matches -> back to the list
    await page.locator('[data-tab="table"]').click();
    await expect(page.getByTestId('tab-table')).toBeVisible();
    await pageIsInPlace(page, width);
    await page.locator('[data-tab="matches"]').click();
    await pageIsInPlace(page, width);
    await page.goBack();
    await expect(page.getByTestId('tournament-list')).toBeVisible();
    await pageIsInPlace(page, width);
  });
}
