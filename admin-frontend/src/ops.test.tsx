import { render, screen } from '@testing-library/react';
import { App } from './App';

const BODIES: Record<string, unknown> = {
  '/api/admin/outbox': {
    summary: { connected: true, byStatus: { queued: 2, sent: 5, failed: 1 }, last24h: { sent: 3 }, nextDueAt: 10, lastSentAt: 9 },
    limits: { batchPerRun: 25, maxAttempts: 3, stuckS: 600, textRetentionDays: 30, rowRetentionDays: 400, jobTtlS: 259200 },
    statuses: ['queued', 'sent', 'failed'],
    items: [{ key: 'msg:1', kind: 'message', userId: 42, batch: null, status: 'failed', attempts: 1, error: 'blocked_by_user', notBefore: 1, createdAt: 1, sentAt: null }],
    log: [{ id: 1, at: 1, action: 'user.message', adminEmail: 'a@x', result: 'completed' }],
  },
  '/api/admin/roles': {
    roles: ['owner', 'admin', 'analyst'],
    permissions: [{ permission: 'read', roles: ['owner', 'admin', 'analyst'] }, { permission: 'manage_admins', roles: ['owner'] }],
    grants: [{ permission: 'view_dialogs', roles: ['owner', 'admin'] }],
  },
  '/api/admin/admins': { items: [{ id: 2, email: 'adm@x', role: 'admin', state: 'active', twoFactor: true, lastLoginAt: null, createdAt: 1, grants: [] }] },
  '/api/admin/product': {
    period: 30,
    product: { source: 'telemetry', series: [{ day: '2026-11-13', dau: 4, quick: 1, coupons: 0, aiCreated: 2, aiOpened: 3, quickFailed: 0 }],
      dau: 4, wau: 9, mau: 17, totals: { quick: 1, coupons: 0, aiCreated: 2, aiOpened: 3, quickFailed: 0, forecasts: 3 },
      cohorts: [{ week: '2026-11-09', size: 5, retention: [0.6, null] }] },
    referrals: { starts: 7, inMiniApp: 4, active7d: 2, converted: null }, oddsCoverage: { source: 'not_collected' },
  },
};

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/api/admin/auth/me')
      ? { admin: { id: 1, email: 'o@x', role: 'owner' }, csrf: 'c', pilot: { dataset: 'demo', demoTelemetry: false } }
      : BODIES[url.split('?')[0]!];
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

it('shows the queue with statuses, error codes and limits', async () => {
  window.history.pushState({}, '', '/outbox');
  render(<App />);
  expect(await screen.findByText('blocked_by_user')).toBeInTheDocument();
  expect(screen.getByText('Личное сообщение')).toBeInTheDocument();
  expect(screen.getByText('25')).toBeInTheDocument();
});

it('shows the role matrix and lets the owner grant dialogs', async () => {
  window.history.pushState({}, '', '/settings/roles');
  render(<App />);
  expect(await screen.findByText('Администраторы и права')).toBeInTheDocument();
  expect(await screen.findByRole('button', { name: /Выдать «Чтение обращений/ })).toBeInTheDocument();
});

it('shows product analytics with WAU/MAU, cohorts and the referral funnel', async () => {
  window.history.pushState({}, '', '/product');
  render(<App />);
  expect(await screen.findByText('17')).toBeInTheDocument();
  expect(screen.getByText('60%')).toBeInTheDocument();
  expect(screen.getByText('Запуски по ссылкам')).toBeInTheDocument();
});

it('hides the queue link from analysts', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/api/admin/auth/me')
      ? { admin: { id: 3, email: 'an@x', role: 'analyst' }, csrf: 'c', pilot: { dataset: 'demo', demoTelemetry: false } }
      : BODIES['/api/admin/product'];
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  window.history.pushState({}, '', '/product');
  render(<App />);
  expect(await screen.findByText('Запуски по ссылкам')).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: 'Очередь' })).not.toBeInTheDocument();
});

it('a "24 h" period chosen elsewhere shows and requests 7 days on the product page', async () => {
  window.localStorage.setItem('sporbot-admin.period', '1');
  window.history.pushState({}, '', '/product');
  render(<App />);
  const select = (await screen.findByLabelText('Период')) as HTMLSelectElement;
  expect(select.value).toBe('7');
  const calls = (fetch as unknown as { mock: { calls: [string][] } }).mock.calls.map((c) => c[0]);
  expect(calls).toContain('/api/admin/product?days=7');
  expect(calls.some((u) => u === '/api/admin/product?days=1')).toBe(false);
  window.localStorage.removeItem('sporbot-admin.period');
});
