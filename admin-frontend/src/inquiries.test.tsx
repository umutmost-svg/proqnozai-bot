import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';

function respond(url: string, role: string, denied = false) {
  if (url.startsWith('/api/admin/auth/me')) {
    return { status: 200, body: { admin: { id: 1, email: 'o@x', role }, csrf: 'c', pilot: { dataset: 'demo', demoTelemetry: false } } };
  }
  if (denied) return { status: 403, body: { error: 'dialogs_not_allowed', requestId: 'r' } };
  if (url.startsWith('/api/admin/inquiries/')) {
    return { status: 200, body: { userId: 5, noticeAt: 1, status: 'open', user: { firstName: 'Ann', lastName: null, username: 'ann', lang: 'ru' },
      history: [{ id: 'in:1', from: 'user', at: 2, text: 'Не работает' }, { id: 'out:1', from: 'admin', at: 3, text: 'Проверим', by: 'o@x', status: 'sent' }] } };
  }
  return { status: 200, body: { collected: true, retentionDays: 90,
    items: [{ userId: 5, count: 1, lastAt: 2, name: 'Ann', username: 'ann', status: 'open' }] } };
}

afterEach(() => vi.unstubAllGlobals());

function stub(role: string, denied = false) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const r = respond(url, role, denied);
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'Content-Type': 'application/json' } });
  }));
}

it('lists inquiries and shows the merged history with delivery status', async () => {
  stub('owner');
  window.history.pushState({}, '', '/inquiries');
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: /Ann/ }));
  expect(await screen.findByText('Проверим')).toBeInTheDocument();
  expect(screen.getByText(/доставлено/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Срок хранения: 90/ })).toBeInTheDocument();
});

it('explains the missing grant', async () => {
  stub('admin', true);
  window.history.pushState({}, '', '/inquiries');
  render(<App />);
  expect(await screen.findByText('Нет доступа к обращениям')).toBeInTheDocument();
});
