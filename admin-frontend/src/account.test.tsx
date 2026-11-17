import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';

const calls: { url: string; body: unknown }[] = [];

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
    const body = url.startsWith('/api/admin/auth/me')
      ? { admin: { id: 3, email: 'p@x', role: 'promoter' }, grants: [], csrf: 'c', pilot: { dataset: 'demo', demoTelemetry: false } }
      : url === '/api/admin/auth/password' ? { revokedSessions: 2 } : { revoked: 1 };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});

afterEach(() => vi.unstubAllGlobals());

it('changes the own password with the current one and a 2FA code (any role, the promoter included)', async () => {
  const user = userEvent.setup();
  window.history.pushState({}, '', '/account');
  render(<App />);
  expect(await screen.findByRole('heading', { name: 'Безопасность' })).toBeInTheDocument();
  const submit = screen.getByRole('button', { name: 'Сменить пароль' });
  await user.type(screen.getByLabelText('Текущий пароль'), 'old-password');
  await user.type(screen.getByLabelText('Новый пароль'), 'a-brand-new-password');
  await user.type(screen.getByLabelText('Повторите новый пароль'), 'a-brand-new-passwor');
  expect(screen.getByText('Пароли не совпадают')).toBeInTheDocument();
  await user.type(screen.getByLabelText('Повторите новый пароль'), 'd');
  expect(submit).toBeDisabled();  // no code yet
  await user.type(screen.getByLabelText('Код двухфакторной аутентификации'), '123456');
  await user.click(submit);
  await waitFor(() => expect(calls.some((c) => c.url === '/api/admin/auth/password')).toBe(true));
  expect(calls.find((c) => c.url === '/api/admin/auth/password')!.body)
    .toEqual({ current: 'old-password', new: 'a-brand-new-password', code: '123456' });
  expect(await screen.findByText('Пароль изменён. Завершено других сессий: 2')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Выйти на всех других устройствах' }));
  expect(await screen.findByText('Завершено других сессий: 1')).toBeInTheDocument();
});
