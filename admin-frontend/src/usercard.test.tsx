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
