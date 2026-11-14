import { expect, test } from '@playwright/test';
import { fixtures, mockApi } from './api';

/** No odds on the match page (owner, 15.11): prices are shown only inside an analysis. Even an older backend that
 * still sends an `odds` block with the match shows nothing. */
const NOW = '2099-11-21T06:00:00Z';

test('the match page never shows odds, even when an old answer carries them', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install({ time: NOW });
  const data = fixtures();
  const ev = data['/api/sports/event/fd:4'] as Record<string, unknown>;
  data['/api/sports/event/fd:4'] = {
    ...ev,
    odds: {
      fetchedAt: '2099-11-21T05:59:00Z', validFor: 600,
      quotes: [{ market: '1', odds: 4.2, marketProbability: 22.6, margin: 4.8 }],
    },
  };
  await mockApi(page, data);
  await page.goto('/#/event/fd%3A4');
  await expect(page.getByTestId('event-hero')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('event-odds')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('4,20');
});
