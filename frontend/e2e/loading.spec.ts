import { expect, test, type Page } from '@playwright/test';

/**
 * Brand loading screen in a real browser: bound to the real session check
 * (/api/me is held by the test), geometry at 360/390/430, motion and reduced
 * motion, return from the background, and a Telegram WebApp stub.
 */
const WIDTHS = [360, 390, 430] as const;

type Hold = { release(): void };

/** Holds the session check until released: that is what the splash waits for. */
async function holdSession(page: Page): Promise<Hold> {
  let release = () => {};
  const gate = new Promise<void>((r) => (release = r));
  await page.route('**/api/me', async (route) => {
    await gate;
    await route.fulfill({ status: 401, contentType: 'application/json', body: '{"error":"unauthenticated"}' });
  });
  return { release: () => release() };
}

async function prepare(page: Page, width: number, opts: { lang?: 'ru' | 'az' | 'en'; reduceMotion?: boolean } = {}) {
  await page.setViewportSize({ width, height: 844 });
  await page.addInitScript((o) => {
    localStorage.setItem(
      'sporai.settings.v1',
      JSON.stringify({ lang: o.lang ?? 'ru', timeZone: 'auto', reduceMotion: o.reduceMotion ?? false, haptics: true }),
    );
  }, opts);
  await page.route('https://telegram.org/**', (r) => r.fulfill({ body: '', contentType: 'text/javascript' }));
}

async function geometryProblems(page: Page) {
  return page.evaluate(() => {
    const problems: string[] = [];
    if (document.documentElement.scrollWidth > window.innerWidth) problems.push('page scrolls sideways');
    const screen = document.querySelector<HTMLElement>('[data-testid="app-loading"]')!;
    const box = screen.getBoundingClientRect();
    if (box.width !== window.innerWidth || box.height !== window.innerHeight) problems.push('splash does not cover the viewport');
    for (const el of screen.querySelectorAll<HTMLElement>('p, span, button')) {
      // The moving signal is clipped by its track on purpose.
      if (el.closest('[class*="track"]')) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0) continue;
      if (r.left < 0 || r.right > window.innerWidth || r.top < 0 || r.bottom > window.innerHeight) {
        problems.push(`outside the viewport: ${el.className} "${el.textContent?.trim()}"`);
      }
    }
    const stage = screen.querySelector<HTMLElement>('[class*="stage_"], [class*="stage"]')!.getBoundingClientRect();
    if (Math.abs(stage.left + stage.width / 2 - window.innerWidth / 2) > 1) problems.push('logo stage not centered');
    return problems;
  });
}

for (const width of WIDTHS) {
  test(`splash @${width}px: covers, centered, nothing clipped; leaves when the session check ends`, async ({ page }) => {
    const hold = await holdSession(page);
    await prepare(page, width);
    await page.goto('/');
    const splash = page.getByTestId('app-loading');
    await expect(splash).toBeVisible();
    await expect(splash).toHaveAttribute('data-phase', 'connecting');
    await expect(splash).toContainText('Подключаем Spor AI');
    await page.waitForTimeout(500); // past the 450 ms entrance
    expect(await geometryProblems(page)).toEqual([]);
    // Figma 390 × 844: the logo stage starts at y 242 (same composition at every width).
    const stageTop = await page.evaluate(
      () => document.querySelector('[class*="stage_"]')!.getBoundingClientRect().top,
    );
    expect(Math.abs(stageTop - 242)).toBeLessThanOrEqual(2);
    await page.screenshot({ path: `test-results/loading/${width}-loading.png`, animations: 'disabled' });

    hold.release();
    await expect(page.getByTestId('app-loading-gate')).toHaveCount(0);
    await page.getByRole('link', { name: 'Профиль' }).or(page.getByRole('button', { name: 'Профиль' })).first().click();
    await expect(page).toHaveURL(/#\/profile/);
  });
}

test('AZ and EN texts fit at 360px', async ({ page }) => {
  for (const [lang, text] of [['az', 'Spor AI-a qoşuluruq'], ['en', 'Connecting to Spor AI']] as const) {
    const hold = await holdSession(page);
    await prepare(page, 360, { lang });
    await page.goto('/');
    await expect(page.getByTestId('app-loading')).toContainText(text);
    expect(await geometryProblems(page)).toEqual([]);
    await page.screenshot({ path: `test-results/loading/360-${lang}.png`, animations: 'disabled' });
    hold.release();
    await page.unrouteAll({ behavior: 'ignoreErrors' });
  }
});

test('motion: 450 ms entrance, 1.8 s pulse, sweeping signal, three dot states', async ({ page }) => {
  await holdSession(page);
  await prepare(page, 390);
  await page.goto('/');
  await expect(page.getByTestId('app-loading')).toHaveAttribute('data-motion', 'full');
  const timing = await page.evaluate(() =>
    document
      .querySelector('[data-testid="app-loading"]')!
      .getAnimations({ subtree: true })
      .map((a) => {
        const t = (a.effect as KeyframeEffect).getComputedTiming();
        return `${(a as CSSAnimation).animationName}:${t.duration}`;
      })
      .sort(),
  );
  expect(timing).toEqual(
    expect.arrayContaining([
      // CSS Modules scope keyframe names (e.g. _logo-enter_abc12).
      expect.stringMatching(/logo-enter\w*:450$/),
      expect.stringMatching(/halo-pulse\w*:900$/), // alternate: 1.8 s per full cycle
      expect.stringMatching(/sweep\w*:1800$/),
      expect.stringMatching(/dot-step\w*:1800$/),
    ]),
  );
  // The signal really moves along the track.
  const x = () =>
    page.evaluate(() => new DOMMatrix(getComputedStyle(document.querySelector('[class*="signal"]')!).transform).m41);
  const a = await x();
  await page.waitForTimeout(300);
  expect(await x()).not.toBe(a);
});

test('system reduced motion: static mark and text, no animations at all', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await holdSession(page);
  await prepare(page, 390);
  await page.goto('/');
  const splash = page.getByTestId('app-loading');
  await expect(splash).toHaveAttribute('data-motion', 'static');
  await expect(splash).toContainText('Подключаем Spor AI');
  await expect(page.locator('[data-step]')).toHaveCount(0);
  const running = await page.evaluate(() =>
    document.querySelector('[data-testid="app-loading"]')!.getAnimations({ subtree: true }).length,
  );
  expect(running).toBe(0);
});

test('in-app "Меньше анимации" setting also stops the splash motion', async ({ page }) => {
  await holdSession(page);
  await prepare(page, 390, { reduceMotion: true });
  await page.goto('/');
  await expect(page.getByTestId('app-loading')).toHaveAttribute('data-motion', 'static');
  await expect(page.locator('[data-step]')).toHaveCount(0);
});

test('a fast boot shows no splash at all', async ({ page }) => {
  await prepare(page, 390);
  await page.route('**/api/me', (r) =>
    r.fulfill({ status: 401, contentType: 'application/json', body: '{"error":"unauthenticated"}' }),
  );
  await page.addInitScript(() => {
    (window as unknown as { splashSeen: boolean }).splashSeen = false;
    new MutationObserver(() => {
      if (document.querySelector('[data-testid="app-loading"]')) (window as unknown as { splashSeen: boolean }).splashSeen = true;
    }).observe(document, { childList: true, subtree: true });
  });
  await page.goto('/#/profile'); // no sports data on this screen
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { splashSeen: boolean }).splashSeen)).toBe(false);
});

test('return from the background: no stall, splash never comes back after boot', async ({ page }) => {
  const hold = await holdSession(page);
  await prepare(page, 390);
  await page.addInitScript(() => {
    let state: DocumentVisibilityState = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
    (window as unknown as { setVisibility: (s: DocumentVisibilityState) => void }).setVisibility = (s) => {
      state = s;
      document.dispatchEvent(new Event('visibilitychange'));
    };
  });
  await page.goto('/');
  await expect(page.getByTestId('app-loading')).toBeVisible();
  await page.evaluate(() => (window as unknown as { setVisibility: (s: string) => void }).setVisibility('hidden'));
  await page.evaluate(() => (window as unknown as { setVisibility: (s: string) => void }).setVisibility('visible'));
  await expect(page.getByTestId('app-loading')).toHaveAttribute('data-view', 'loading');
  hold.release();
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0);
  await page.evaluate(() => (window as unknown as { setVisibility: (s: string) => void }).setVisibility('hidden'));
  await page.evaluate(() => (window as unknown as { setVisibility: (s: string) => void }).setVisibility('visible'));
  await page.waitForTimeout(400);
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0);
});

test('inside a Telegram WebView: ready() is called at once, splash respects safe areas', async ({ page }) => {
  const hold = await holdSession(page);
  await prepare(page, 390);
  await page.addInitScript(() => {
    const calls: string[] = [];
    (window as unknown as { tgCalls: string[] }).tgCalls = calls;
    (window as unknown as { Telegram: unknown }).Telegram = {
      WebApp: {
        // Unsigned stand-in: the adapter only needs launch data to count as Telegram.
        initData: 'query_id=AA&auth_date=1&hash=' + '0'.repeat(64),
        initDataUnsafe: {},
        version: '8.0',
        isVersionAtLeast: () => true,
        ready: () => calls.push('ready'),
        expand: () => calls.push('expand'),
        viewportStableHeight: 700,
        safeAreaInset: { top: 47, bottom: 34, left: 0, right: 0 },
        contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
        onEvent: () => {},
        offEvent: () => {},
        BackButton: { show: () => {}, hide: () => {}, onClick: () => {}, offClick: () => {} },
      },
    };
  });
  await page.goto('/');
  await expect(page.getByTestId('app-loading')).toBeVisible();
  // Telegram's own placeholder is dismissed before our splash: no double loading screen.
  expect(await page.evaluate(() => (window as unknown as { tgCalls: string[] }).tgCalls)).toEqual(
    expect.arrayContaining(['ready', 'expand']),
  );
  const topGap = await page.evaluate(() => document.querySelector('[class*="wordmark"]')!.getBoundingClientRect().top);
  expect(topGap).toBeGreaterThan(47); // below the device safe area
  expect(await geometryProblems(page)).toEqual([]);
  hold.release();
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0);
});

test('short screen 360×640: the whole splash and the failed-view actions stay on screen', async ({ page }) => {
  await prepare(page, 360);
  await page.setViewportSize({ width: 360, height: 640 });
  await page.clock.install();
  // The session check is bounded by the API timeout (10 s < 12 s), so only a data
  // source can hang long enough: swallow the demo provider's 350 ms latency timer.
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = ((fn: TimerHandler, ms?: number, ...rest: unknown[]) =>
      ms === 350 ? 0 : original(fn, ms, ...rest)) as typeof window.setTimeout;
  });
  await page.goto('/');
  await expect(page.getByTestId('app-loading')).toBeVisible();
  expect(await geometryProblems(page)).toEqual([]);
  await page.clock.runFor(13_000); // past the wait limit: failed view with two buttons
  await expect(page.getByTestId('app-loading')).toHaveAttribute('data-view', 'failed');
  expect(await geometryProblems(page)).toEqual([]);
  await page.screenshot({ path: 'test-results/loading/360x640-failed.png', animations: 'disabled' });
});

test('short screen with Telegram safe areas and a two-line offline hint: nothing overlaps', async ({ page }) => {
  await prepare(page, 360);
  await page.setViewportSize({ width: 360, height: 640 });
  await holdSession(page);
  await page.addInitScript(() => {
    (window as unknown as { Telegram: unknown }).Telegram = {
      WebApp: {
        initData: 'query_id=AA&auth_date=1&hash=' + '0'.repeat(64),
        initDataUnsafe: {},
        version: '8.0',
        isVersionAtLeast: () => true,
        ready: () => {},
        expand: () => {},
        safeAreaInset: { top: 47, bottom: 34, left: 0, right: 0 },
        contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
        onEvent: () => {},
        offEvent: () => {},
        BackButton: { show: () => {}, hide: () => {}, onClick: () => {}, offClick: () => {} },
      },
    };
  });
  await page.goto('/');
  const splash = page.getByTestId('app-loading');
  await expect(splash).toBeVisible();
  await page.context().setOffline(true);
  await expect(page.getByTestId('app-loading-hint')).toBeVisible();
  const boxes = await page.evaluate(() => {
    const rect = (sel: string) => document.querySelector(sel)?.getBoundingClientRect() ?? null;
    return { dots: rect('[class*="dots_"]'), hint: rect('[data-testid="app-loading-hint"]'), tagline: rect('[class*="tagline"]') };
  });
  await page.screenshot({ path: 'test-results/loading/360x640-safe-offline.png', animations: 'disabled' });
  await page.context().setOffline(false);
  expect(boxes.dots!.bottom).toBeLessThanOrEqual(640 - 34);
  expect(boxes.hint!.top).toBeGreaterThanOrEqual(47);
  // The tagline yields to the hint on short screens; if shown, it must not overlap.
  if (boxes.tagline && boxes.tagline.height > 0) expect(boxes.dots!.bottom).toBeLessThanOrEqual(boxes.tagline.top);
});

test('short screen with Telegram safe areas: failed-view actions stay inside the safe area and work', async ({ page }) => {
  await prepare(page, 360);
  await page.setViewportSize({ width: 360, height: 640 });
  await page.clock.install();
  await page.route('**/api/**', (r) =>
    r.fulfill({ status: 401, contentType: 'application/json', body: '{"error":"unauthenticated"}' }),
  );
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = ((fn: TimerHandler, ms?: number, ...rest: unknown[]) =>
      ms === 350 ? 0 : original(fn, ms, ...rest)) as typeof window.setTimeout; // hanging data source
    (window as unknown as { Telegram: unknown }).Telegram = {
      WebApp: {
        initData: 'query_id=AA&auth_date=1&hash=' + '0'.repeat(64),
        initDataUnsafe: {},
        version: '8.0',
        isVersionAtLeast: () => true,
        ready: () => {},
        expand: () => {},
        safeAreaInset: { top: 47, bottom: 34, left: 0, right: 0 },
        contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
        onEvent: () => {},
        offEvent: () => {},
        BackButton: { show: () => {}, hide: () => {}, onClick: () => {}, offClick: () => {} },
      },
    };
  });
  await page.goto('/');
  await page.clock.runFor(13_000);
  const splash = page.getByTestId('app-loading');
  await expect(splash).toHaveAttribute('data-view', 'failed');
  for (const name of ['Повторить', 'Открыть без ожидания']) {
    const button = splash.getByRole('button', { name });
    await button.scrollIntoViewIfNeeded();
    const box = (await button.boundingBox())!;
    expect(box.y).toBeGreaterThanOrEqual(47);
    expect(box.y + box.height).toBeLessThanOrEqual(640 - 34);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/loading/360x640-safe-failed.png', animations: 'disabled' });
  await splash.getByRole('button', { name: 'Открыть без ожидания' }).click();
  await page.clock.runFor(500);
  await expect(page.getByTestId('app-loading-gate')).toHaveCount(0);
});

