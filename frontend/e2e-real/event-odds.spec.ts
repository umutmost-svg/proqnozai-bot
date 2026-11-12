import { expect, test } from '@playwright/test';
import { fixtures, mockApi } from './api';

/** The match page's odds: shown while valid, numbers only, gone when they expire; fits 320 px. */
const NOW = '2099-11-21T06:00:00Z';

function withOdds(validFor: number) {
  const data = fixtures();
  const ev = data['/api/sports/event/fd:4'] as Record<string, unknown>;
  data['/api/sports/event/fd:4'] = {
    ...ev,
    odds: {
      fetchedAt: '2099-11-21T05:59:00Z', validFor,
      quotes: [
        { market: '1', odds: 4.2, marketProbability: 22.6, margin: 4.8 },
        { market: 'X', odds: 3.9, marketProbability: 24.3, margin: 4.8 },
        { market: '2', odds: 1.85, marketProbability: 53.1, margin: 4.8 },
        { market: 'O2.5', odds: 1.8, marketProbability: 53, margin: 4.1 },
        { market: 'U2.5', odds: 2.05, marketProbability: 47, margin: 4.1 },
      ],
    },
  };
  return data;
}

for (const width of [320, 390]) {
  test(`odds on the match page at ${width}px, gone when they expire`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.clock.install({ time: NOW });
    await mockApi(page, withOdds(120));
    await page.goto('/#/event/fd%3A4');
    const card = page.getByTestId('event-odds');
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.locator('li')).toHaveCount(5);
    await expect(card).toContainText('4,20');
    await expect(page.locator('body')).not.toContainText(/mostbet/i);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.clock.fastForward(125_000);
    await expect(card).toHaveCount(0); // never shown after its validity, not even greyed out
  });
}
