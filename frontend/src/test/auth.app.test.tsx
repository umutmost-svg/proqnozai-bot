import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { fail, ok, scriptedApi, SESSION } from '../auth/testing';
import { renderApp } from './renderApp';

const INIT = 'query_id=AA&auth_date=1&hash=' + '0'.repeat(64);

const status = () => screen.getByTestId('account').getAttribute('data-auth-status');

describe('auth in the app', () => {
  it('signs in once under StrictMode and shows the verified profile', async () => {
    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [ok(SESSION)] });
    renderApp('#/profile', { authApi: api, initData: INIT, strict: true });
    await waitFor(() => expect(status()).toBe('authenticated'));
    expect(screen.getByText('Verified Name')).toBeInTheDocument();
    expect(screen.getByTestId('auth-status')).toHaveTextContent('Вход подтвержден Telegram');
    expect(api.me).toHaveBeenCalledTimes(1);
    expect(api.createSession).toHaveBeenCalledTimes(1);
  });

  it('keeps demo marks on sports data after sign-in', async () => {
    renderApp('#/', { authApi: scriptedApi({ me: [ok(SESSION)] }), initData: INIT });
    const hero = await screen.findByTestId('hero');
    expect(within(hero).getByText('ДЕМО')).toBeInTheDocument();
    expect(screen.getByTestId('demo-footer')).toBeInTheDocument();
  });

  it('outside Telegram the M1 demo works and nothing is posted', async () => {
    const api = scriptedApi({ me: [fail('unavailable')] });
    renderApp('#/profile', { authApi: api });
    await waitFor(() => expect(status()).toBe('outside_telegram'));
    expect(screen.getByText('Telegram User')).toBeInTheDocument();
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it('shows invalid and expired sign-in states without retry loops', async () => {
    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [fail('init_data_invalid')] });
    renderApp('#/profile', { authApi: api, initData: INIT });
    await waitFor(() => expect(status()).toBe('init_data_invalid'));
    expect(screen.getByTestId('auth-status')).toHaveTextContent('Откройте Mini App заново');
    expect(screen.queryByRole('button', { name: 'Повторить' })).toBeNull();
    await new Promise((r) => setTimeout(r, 20));
    expect(api.createSession).toHaveBeenCalledTimes(1);
  });

  it('session expired is reported', async () => {
    renderApp('#/profile', { authApi: scriptedApi({ me: [fail('session_expired')] }) });
    await waitFor(() => expect(status()).toBe('session_expired'));
    expect(screen.getByTestId('auth-status')).toHaveTextContent('Сессия истекла');
  });

  it('server unavailable → manual retry signs in', async () => {
    const user = userEvent.setup();
    const api = scriptedApi({ me: [fail('unavailable'), fail('unauthenticated')], createSession: [ok(SESSION)] });
    renderApp('#/profile', { authApi: api, initData: INIT });
    await waitFor(() => expect(status()).toBe('unavailable'));
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    await waitFor(() => expect(status()).toBe('authenticated'));
    expect(api.me).toHaveBeenCalledTimes(2);
  });

  it('logout ends the session and does not sign in again', async () => {
    const user = userEvent.setup();
    const api = scriptedApi({ me: [ok(SESSION)], logout: [ok(null)] });
    renderApp('#/profile', { authApi: api, initData: INIT });
    await user.click(await screen.findByRole('button', { name: 'Выйти' }));
    await waitFor(() => expect(status()).toBe('signed_out'));
    expect(screen.queryByText('Verified Name')).toBeNull();
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it('both sign-out buttons are disabled while one sign-out runs; no false failure', async () => {
    const user = userEvent.setup();
    let answer: (r: ReturnType<typeof ok<null>>) => void = () => {};
    const api = scriptedApi({ me: [ok(SESSION)] });
    api.logoutAll.mockImplementation(() => new Promise((r) => (answer = r)));
    renderApp('#/profile', { authApi: api, initData: INIT });
    const all = await screen.findByRole('button', { name: 'Выйти на всех устройствах' });
    await user.click(all);
    const single = screen.getByRole('button', { name: 'Выйти' });
    expect(single).toBeDisabled();
    expect(all).toBeDisabled();
    await user.click(single);
    expect(api.logout).not.toHaveBeenCalled();
    expect(screen.queryByText('Не удалось выйти. Повторите.')).toBeNull();
    answer(ok(null));
    await waitFor(() => expect(status()).toBe('signed_out'));
    expect(screen.queryByText('Не удалось выйти. Повторите.')).toBeNull();
  });

  it('records an explicit language choice on the server', async () => {
    const user = userEvent.setup();
    const api = scriptedApi({ me: [ok(SESSION)], setLanguage: [ok(null)] });
    renderApp('#/profile/language', { authApi: api, initData: INIT });
    await waitFor(() => expect(api.me).toHaveBeenCalled());
    await user.click(await screen.findByRole('radio', { name: 'Azərbaycan' }));
    await user.click(screen.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(api.setLanguage).toHaveBeenCalledWith('az', 'csrf-test'));
  });
});
