import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.startsWith('/api/admin/auth/me')) {
      return new Response(JSON.stringify({ admin: { id: 3, email: 'p@x', role: 'promoter' }, grants: [], csrf: 'c',
        pilot: { dataset: 'demo', demoTelemetry: false } }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(JSON.stringify({ error: 'unauthenticated' }), { status: 401, headers: { 'Content-Type': 'application/json' } });
  }));
});

afterEach(() => vi.unstubAllGlobals());

it('an expired session asks to sign in over the open page and keeps what was typed', async () => {
  const user = userEvent.setup();
  window.history.pushState({}, '', '/account');
  render(<App />);
  await user.type(await screen.findByLabelText('Новый пароль'), 'typed-before-expiry');
  await user.click(screen.getByRole('button', { name: 'Выйти на всех других устройствах' }));
  expect(await screen.findByRole('dialog', { name: 'Сессия истекла' })).toBeInTheDocument();
  expect(screen.getByLabelText('Новый пароль')).toHaveValue('typed-before-expiry');  // the page is still mounted
});
