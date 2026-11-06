import { expect, test, type Page } from '@playwright/test';

// Visual checks use a fixed date; the app itself reads the real clock.
const FIXED_NOW = new Date('2026-11-08T06:00:00Z');
const WIDTHS = [360, 390, 430] as const;
const ROUTES = [
  ['home', '/'],
  ['catalog', '/matches'],
  ['event-football', '/event/fb-rm-fcb'],
  ['event-long-names', '/event/fb-long-names'],
  ['event-tennis-stats', '/event/tn-live?tab=stats'],
  ['ai-football', '/event/fb-rm-fcb/ai'],
  ['ai-insufficient', '/event/fb-long-names/ai'],
  ['profile', '/profile'],
  ['timezone', '/profile/timezone'],
] as const;

async function open(page: Page, route: string, width: number, lang: 'ru' | 'az' | 'en' = 'ru') {
  await page.setViewportSize({ width, height: 844 });
  await page.addInitScript((l) => {
    if (!localStorage.getItem('sporai.settings.v1')) {
      localStorage.setItem('sporai.settings.v1', JSON.stringify({ lang: l, timeZone: 'auto', reduceMotion: false, haptics: true }));
    }
  }, lang);
  // Outside Telegram: the SDK is stubbed so tests never touch the network.
  await page.route('https://telegram.org/**', (r) => r.fulfill({ body: '', contentType: 'text/javascript' }));
  await page.clock.setFixedTime(FIXED_NOW);
  await page.goto(`/#${route}`);
  await expect(page.getByTestId('loading-state')).toHaveCount(0, { timeout: 10_000 });
  await expect(page.getByTestId('ai-loading')).toHaveCount(0, { timeout: 10_000 });
}

/** Layout invariants shared by every screen. */
async function layoutProblems(page: Page) {
  return page.evaluate(() => {
    const problems: string[] = [];
    const doc = document.documentElement;
    if (doc.scrollWidth > window.innerWidth) problems.push(`page scrolls sideways: ${doc.scrollWidth}px`);

    for (const el of document.querySelectorAll<HTMLElement>('body *')) {
      const style = getComputedStyle(el);
      const scrollsX = ['auto', 'scroll'].includes(style.overflowX) && el.scrollWidth > el.clientWidth + 1;
      if (scrollsX && !['date-strip', 'tournament-strip'].includes(el.dataset.testid ?? '')) {
        problems.push(`unexpected horizontal scroller: ${el.className}`);
      }
      // scrollWidth is unreliable for <button>; measure the laid-out text instead.
      const srOnly = el.closest('.visually-hidden');
      if (!srOnly && el.children.length === 0 && el.textContent?.trim() && style.overflow !== 'visible') {
        const range = document.createRange();
        range.selectNodeContents(el);
        const text = range.getBoundingClientRect();
        const box = el.getBoundingClientRect();
        if (text.width > box.width + 1 || text.height > box.height + 1) {
          problems.push(`clipped text: "${el.textContent.trim()}" (${Math.round(text.width)}>${Math.round(box.width)})`);
        }
      }
      if (el.matches('h1,h2,h3,p,span,button,a') && el.textContent?.trim()) {
        const range = document.createRange();
        range.selectNodeContents(el);
        const text = range.getBoundingClientRect();
        const inStrip = el.closest('[data-testid="date-strip"],[data-testid="tournament-strip"]');
        if (!inStrip && (text.right > window.innerWidth + 1 || text.left < -1)) problems.push(`text off-screen: "${el.textContent.trim()}"`);
      }
    }

    const viewport = document.querySelector<HTMLElement>('[data-testid="scroll-viewport"]')!;
    const dock = document.querySelector('nav')!.getBoundingClientRect();
    viewport.scrollTop = viewport.scrollHeight;
    const content = viewport.firstElementChild!.getBoundingClientRect();
    if (content.bottom > dock.top + 1 && viewport.getBoundingClientRect().bottom > dock.top + 1) {
      problems.push('content can end under the bottom navigation');
    }
    viewport.scrollTop = 0;
    return problems;
  });
}

for (const width of WIDTHS) {
  for (const [name, route] of ROUTES) {
    test(`${name} @${width}px has no overflow, clipping or nav overlap`, async ({ page }) => {
      await open(page, route, width);
      expect(await layoutProblems(page)).toEqual([]);
      await page.screenshot({ path: `test-results/screens/${width}-${name}.png`, animations: 'disabled' });
    });
  }
}

for (const lang of ['az', 'en'] as const) {
  for (const [name, route] of [['home', '/'], ['catalog', '/matches'], ['event', '/event/tn-live'], ['ai', '/event/fb-rm-fcb/ai'], ['profile', '/profile']] as const) {
    test(`${name} in ${lang.toUpperCase()} @360px fits`, async ({ page }) => {
      await open(page, route, 360, lang);
      expect(await layoutProblems(page)).toEqual([]);
      await page.screenshot({ path: `test-results/screens/360-${lang}-${name}.png`, animations: 'disabled' });
    });
  }
}

test('long team names wrap inside cards', async ({ page }) => {
  await open(page, '/matches', 360);
  await page.getByRole('button', { name: '9 ноября' }).click();
  const card = page.locator('[data-event-id="fb-long-names"]');
  await expect(card).toBeVisible();
  const fits = await card.evaluate((el) =>
    [...el.querySelectorAll('p')].every((p) => p.scrollWidth <= p.clientWidth + 1 && p.getBoundingClientRect().right <= el.getBoundingClientRect().right),
  );
  expect(fits).toBe(true);
  expect(await layoutProblems(page)).toEqual([]);
  await page.screenshot({ path: 'test-results/screens/360-long-names.png', fullPage: true, animations: 'disabled' });
});

test('only the date and tournament strips scroll sideways', async ({ page }) => {
  await open(page, '/matches', 360);
  const strip = page.getByTestId('tournament-strip');
  const scrollable = await strip.evaluate((el) => el.scrollWidth > el.clientWidth);
  expect(scrollable).toBe(true);
  await strip.evaluate((el) => (el.scrollLeft = 200));
  expect(await page.evaluate(() => document.documentElement.scrollLeft)).toBe(0);
});

test('system reduced-motion disables decorative animation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, '/', 390);
  const animation = await page.getByTestId('hero').evaluate((el) => getComputedStyle(el).animationName);
  expect(animation).toBe('none');
});

test('hero entrance plays once instead of looping', async ({ page }) => {
  await open(page, '/', 390);
  const iterations = await page.getByTestId('hero').evaluate((el) => getComputedStyle(el).animationIterationCount);
  expect(iterations).toBe('1');
});

test('works outside Telegram and shows demo marks', async ({ page }) => {
  await open(page, '/', 390);
  await expect(page.getByText('ДЕМО', { exact: true })).toBeVisible();
  await expect(page.getByTestId('demo-footer')).toContainText('Демо · 8 ноября 2026');
});
