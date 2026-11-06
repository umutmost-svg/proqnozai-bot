import { createHmac } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';
import { E2E_BOT_TOKEN } from '../playwright.auth.config';

/** Test-runner-only signer (Telegram HMAC scheme); never shipped in the app. */
function signInitData(user: Record<string, unknown>, opts: { token?: string; authDate?: number } = {}): string {
  const fields: Record<string, string> = {
    query_id: `AAH${user.id}`,
    auth_date: String(opts.authDate ?? Math.floor(Date.now() / 1000)),
    user: JSON.stringify(user),
  };
  const dcs = Object.keys(fields)
    .sort()
    .map((k) => `${k}=${fields[k]}`)
    .join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(opts.token ?? E2E_BOT_TOKEN).digest();
  const hash = createHmac('sha256', secret).update(dcs).digest('hex');
  return new URLSearchParams({ ...fields, hash }).toString();
}

async function openInFakeTelegram(page: Page, initData: string, hash = '#/profile') {
  // The official SDK script is not loaded; a minimal WebApp object stands in for the client.
  await page.route('https://telegram.org/**', (route) => route.abort());
  await page.addInitScript((data) => {
    (window as unknown as { Telegram: unknown }).Telegram = {
      WebApp: {
        initData: data,
        initDataUnsafe: { user: { first_name: 'Unsafe Display', language_code: 'ru' } },
        isVersionAtLeast: () => false,
        ready: () => {},
        expand: () => {},
      },
    };
  }, initData);
  const posts: string[] = [];
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().endsWith('/api/auth/session')) posts.push(req.url());
  });
  await page.goto(`/${hash}`);
  return posts;
}

const account = (page: Page) => page.getByTestId('account');

test('signs in via the backend; session is an HttpOnly cookie and survives reload', async ({ page, context }) => {
  const initData = signInitData({ id: 91001, first_name: 'Verified', last_name: 'Person', language_code: 'ru' });
  const posts = await openInFakeTelegram(page, initData);
  await expect(account(page)).toHaveAttribute('data-auth-status', 'authenticated');
  await expect(account(page)).toContainText('Verified Person');
  await expect(account(page)).not.toContainText('Unsafe Display');
  expect(posts).toHaveLength(1);

  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === 'sporai_session');
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe('Lax');
  expect(session?.domain).toBe('127.0.0.1');

  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toContain(session!.value);
  expect(storage).not.toContain('auth_date');
  expect(await page.evaluate(() => document.cookie)).not.toContain('sporai_session');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'test-results/screens/auth-390-profile-verified.png', animations: 'disabled' });

  await page.reload();
  await expect(account(page)).toHaveAttribute('data-auth-status', 'authenticated');
  expect(posts).toHaveLength(1); // existing session reused, initData not re-sent
});

test('sports data stays demo after sign-in', async ({ page }) => {
  await openInFakeTelegram(page, signInitData({ id: 91002, first_name: 'Demo' }), '#/');
  await expect(page.getByTestId('hero')).toContainText('ДЕМО');
  await page.goto('/#/profile');
  await expect(account(page)).toHaveAttribute('data-auth-status', 'authenticated');
});

test('logout revokes the session; reopening with the same initData does not sign in', async ({ page }) => {
  const posts = await openInFakeTelegram(page, signInitData({ id: 91003, first_name: 'Leaving' }));
  await expect(account(page)).toHaveAttribute('data-auth-status', 'authenticated');
  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect(account(page)).toHaveAttribute('data-auth-status', 'signed_out');

  await page.reload();
  await expect(account(page)).toHaveAttribute('data-auth-status', 'signed_out');
  await page.waitForTimeout(1000);
  expect(posts).toHaveLength(2); // one login, one rejected re-use; no loop
});

test('initData signed with another token is rejected once, without retries', async ({ page }) => {
  const posts = await openInFakeTelegram(page, signInitData({ id: 91004 }, { token: '7000000002:other-bot-token' }));
  await expect(account(page)).toHaveAttribute('data-auth-status', 'init_data_invalid');
  await page.setViewportSize({ width: 360, height: 780 });
  await page.screenshot({ path: 'test-results/screens/auth-360-profile-invalid.png', animations: 'disabled' });
  await page.waitForTimeout(1000);
  expect(posts).toHaveLength(1);
});

test('stale initData is reported as expired', async ({ page }) => {
  const stale = signInitData({ id: 91005 }, { authDate: Math.floor(Date.now() / 1000) - 400 });
  await openInFakeTelegram(page, stale);
  await expect(account(page)).toHaveAttribute('data-auth-status', 'init_data_expired');
});

test('outside Telegram: demo mode, no sign-in attempt', async ({ page }) => {
  await page.route('https://telegram.org/**', (route) => route.abort());
  let posted = false;
  page.on('request', (req) => {
    if (req.method() === 'POST') posted = true;
  });
  await page.goto('/#/profile');
  await expect(account(page)).toHaveAttribute('data-auth-status', 'outside_telegram');
  expect(posted).toBe(false);
});
