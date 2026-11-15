import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';

function profile(id: number) {
  return {
    id, username: `u${id}`, firstName: `User ${id}`, lastName: null, lang: 'ru', createdAt: 1, lastLoginAt: 2,
    activeSessions: 1, sessions: [{ createdAt: 1, expiresAt: 4_000_000_000, revokedAt: null, state: 'active' }],
    events: [], aiCalls: null, credits: { enabled: false, balance: 0, unlimitedUntil: null },
    tags: [], notes: [], messages: [], ledger: [], blocked: null, timeline: [],
  };
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/api/admin/auth/me')
      ? { admin: { id: 1, email: 'o@x', role: 'owner' }, csrf: 'c', pilot: { dataset: 'demo', demoTelemetry: true } }
      : profile(Number(url.split('/').pop()!.split('?')[0]));
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});

afterEach(() => vi.unstubAllGlobals());

it('closes the revoke dialog and drops the old profile when the user id changes', async () => {
  window.history.pushState({}, '', '/users/101');
  render(<App />);
  expect(await screen.findByRole('heading', { name: /User 101/ })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Отозвать сессии' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('101');
  await act(async () => {
    window.history.pushState({}, '', '/users/202');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(await screen.findByRole('heading', { name: /User 202/ })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: /User 101/ })).not.toBeInTheDocument();
});

it('shows activity, country, MB id history and block history', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/api/admin/auth/me')
      ? { admin: { id: 1, email: 'o@x', role: 'owner' }, csrf: 'c', pilot: { dataset: 'demo', demoTelemetry: true } }
      : {
        ...profile(303), lastSeenAt: 3, geo: { country: 'AZ', source: 'lang' },
        activity: { collected: true, summary: { lastSeenAt: 3, forecasts: 7, aiCreated: 2, aiOpened: 3, requests: 9, activeDays: 4, firstSeenAt: 1 },
          daily: [{ day: '2026-11-01', requests: 1, forecasts: 7, aiOpened: 0 }] },
        mbIdHistory: [{ old: '111', new: '222', at: 5, by: 'a@x' }],
        blockHistory: [{ blocked: false, at: 6, by: 'a@x', reason: 'ошибка' }],
      };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  window.history.pushState({}, '', '/users/303');
  render(<App />);
  expect(await screen.findByText('Азербайджан · по языку')).toBeInTheDocument();
  expect(screen.getByText('111 → 222')).toBeInTheDocument();
  expect(screen.getByText(/Разблокирован · ошибка/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('tab', { name: 'Активность' }));
  expect(screen.getByText('2 / 3')).toBeInTheDocument();
});
