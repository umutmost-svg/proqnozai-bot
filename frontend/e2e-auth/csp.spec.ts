import { expect, test, type Page } from '@playwright/test';

/**
 * The enforced CSP (served by the backend, same origin; development mode, so
 * without the production-only upgrade-insecure-requests) must not block
 * anything the built app does. Any violation event or CSP console error
 * fails the test. The Telegram SDK script is stubbed: its own behaviour is
 * verified in real Telegram clients in report-only mode, a required gate
 * before switching the stand to enforce (docs/miniapp/stand.md).
 */
const SCREENS = [
  '#/',
  '#/matches',
  '#/event/fb-rm-fcb',
  '#/event/fb-rm-fcb/ai',
  '#/profile',
  '#/profile/language',
  '#/profile/timezone',
  '#/profile/appearance',
];

async function watchViolations(page: Page): Promise<string[]> {
  const found: string[] = [];
  await page.route('https://telegram.org/**', (route) => route.fulfill({ contentType: 'text/javascript', body: '' }));
  await page.exposeFunction('reportCspViolation', (v: string) => found.push(v));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => {
      (window as unknown as { reportCspViolation: (v: string) => void }).reportCspViolation(
        `${e.effectiveDirective} ${e.blockedURI}`,
      );
    });
  });
  page.on('console', (msg) => {
    if (/Content Security Policy/i.test(msg.text())) found.push(msg.text());
  });
  return found;
}

test('enforced CSP is served and nothing on any screen violates it', async ({ page }) => {
  const violations = await watchViolations(page);
  const response = await page.goto('/');
  const csp = response!.headers()['content-security-policy'];
  expect(csp).toContain("default-src 'none'");
  expect(csp).toContain('frame-ancestors https://web.telegram.org');
  expect(csp).not.toContain('unsafe-inline');

  for (const screen of SCREENS) {
    await page.goto(`/${screen}`);
    await expect(page.locator('#root')).not.toBeEmpty();
    // Fonts and lazy images load after first paint.
    await page.waitForLoadState('networkidle');
  }
  expect(violations).toEqual([]);
});

test('the ProqnozAI favicon loads in the browser under the enforced CSP, via a relative URL', async ({ page }) => {
  const violations = await watchViolations(page);
  await page.goto('/');
  const icon = page.locator('link[rel="icon"]');
  // Relative, so the page also works when served from a sub-path (vite base './').
  expect(await icon.getAttribute('href')).toMatch(/^\.\/assets\/brand-symbol-favicon-[\w-]+\.svg$/);
  // A real browser image load, subject to img-src (an API request would bypass CSP).
  const size = await icon.evaluate(
    (l: HTMLLinkElement) =>
      new Promise<number[]>((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve([img.naturalWidth, img.naturalHeight]);
        img.onerror = () => reject(new Error('favicon blocked or missing'));
        img.src = l.href;
      }),
  );
  expect(size[0]).toBeGreaterThan(0);
  expect(size[0]).toBe(size[1]);
  expect(violations).toEqual([]);
});

test('the CSP really blocks foreign code (the check above is not vacuous)', async ({ page }) => {
  const violations = await watchViolations(page);
  await page.goto('/');
  await page.evaluate(() => {
    const s = document.createElement('script');
    s.src = 'https://evil.example/x.js';
    document.head.appendChild(s);
    const st = document.createElement('style');
    st.textContent = 'body{}';
    document.head.appendChild(st);
  });
  await expect.poll(() => violations.length).toBeGreaterThanOrEqual(2);
  expect(violations.some((v) => v.includes('script-src'))).toBe(true);
  expect(violations.some((v) => v.includes('style-src'))).toBe(true);
});

test('hashed assets are cached immutably, the page and the API are not', async ({ request }) => {
  const index = await request.get('/');
  expect(index.headers()['cache-control']).toBe('no-store');
  const asset = (await index.text()).match(/assets\/index-[\w-]+\.js/)![0];
  expect((await request.get(`/${asset}`)).headers()['cache-control']).toBe('public, max-age=31536000, immutable');
  expect((await request.get('/api/health')).headers()['cache-control']).toBe('no-store');
  expect((await request.get('/api/nope')).status()).toBe(404);
});
