import { AuthController } from './controller';
import { fail, ok, scriptedApi, SESSION } from './testing';

const INIT = 'query_id=AA&user=%7B%22id%22%3A501%7D&auth_date=1&hash=' + '0'.repeat(64);

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('AuthController bootstrap', () => {
  it('reuses an existing server session without sending initData', async () => {
    const api = scriptedApi({ me: [ok(SESSION)] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    expect(auth.getState()).toEqual({ status: 'authenticated', session: SESSION });
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it('exchanges initData once when there is no session', async () => {
    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [ok(SESSION)] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    expect(auth.getState().status).toBe('authenticated');
    expect(api.createSession).toHaveBeenCalledTimes(1);
    expect(api.createSession).toHaveBeenCalledWith(INIT);
  });

  it('runs a single bootstrap for repeated / concurrent start (StrictMode)', async () => {
    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [ok(SESSION)] });
    const auth = new AuthController(api, () => INIT);
    await Promise.all([auth.start(), auth.start(), auth.start()]);
    await auth.start();
    expect(api.me).toHaveBeenCalledTimes(1);
    expect(api.createSession).toHaveBeenCalledTimes(1);
  });

  it('outside Telegram stays in demo mode and never posts', async () => {
    const api = scriptedApi({ me: [fail('unavailable')] });
    const auth = new AuthController(api, () => null);
    await auth.start();
    expect(auth.getState().status).toBe('outside_telegram');
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it('reports an expired session when it cannot be renewed', async () => {
    const noTelegram = new AuthController(scriptedApi({ me: [fail('session_expired')] }), () => null);
    await noTelegram.start();
    expect(noTelegram.getState().status).toBe('session_expired');

    const staleLaunch = new AuthController(
      scriptedApi({ me: [fail('session_expired')], createSession: [fail('init_data_expired')] }),
      () => INIT,
    );
    await staleLaunch.start();
    expect(staleLaunch.getState().status).toBe('session_expired');
  });

  it('distinguishes expired initData from invalid initData', async () => {
    const expired = new AuthController(
      scriptedApi({ me: [fail('unauthenticated')], createSession: [fail('init_data_expired')] }),
      () => INIT,
    );
    await expired.start();
    expect(expired.getState().status).toBe('init_data_expired');

    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [fail('init_data_invalid')] });
    const invalid = new AuthController(api, () => INIT);
    await invalid.start();
    expect(invalid.getState().status).toBe('init_data_invalid');
    // Not retryable: a retry would resend the same rejected payload.
    await invalid.retry();
    expect(api.createSession).toHaveBeenCalledTimes(1);
  });

  it('a failed sign-in never turns into a demo user session', async () => {
    const auth = new AuthController(
      scriptedApi({ me: [fail('unauthenticated')], createSession: [fail('init_data_invalid')] }),
      () => INIT,
    );
    await auth.start();
    expect(auth.getState().status).not.toBe('authenticated');
  });

  it('server unavailable: no automatic retries, manual retry recovers', async () => {
    const api = scriptedApi({ me: [fail('unavailable'), fail('unauthenticated')], createSession: [ok(SESSION)] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    expect(auth.getState().status).toBe('unavailable');
    for (let i = 0; i < 5; i++) await flush();
    expect(api.me).toHaveBeenCalledTimes(1);

    const seen: string[] = [];
    auth.subscribe(() => seen.push(auth.getState().status));
    await Promise.all([auth.retry(), auth.retry()]);
    expect(seen).toEqual(['checking', 'authenticated']);
    expect(api.me).toHaveBeenCalledTimes(2);
  });

  it('rate limited is retryable', async () => {
    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [fail('rate_limited'), ok(SESSION)] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    expect(auth.getState().status).toBe('rate_limited');
    await auth.retry();
    expect(auth.getState().status).toBe('authenticated');
  });

  it('initData reused after logout is shown as signed out', async () => {
    const auth = new AuthController(
      scriptedApi({ me: [fail('unauthenticated')], createSession: [fail('init_data_reused')] }),
      () => INIT,
    );
    await auth.start();
    expect(auth.getState().status).toBe('signed_out');
  });
});

describe('AuthController logout and language', () => {
  it('logs out with the CSRF token and does not sign in again automatically', async () => {
    const api = scriptedApi({ me: [ok(SESSION)], logout: [ok(null)] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    expect(await auth.logout()).toBe(true);
    expect(api.logout).toHaveBeenCalledWith('csrf-test');
    expect(auth.getState().status).toBe('signed_out');
    await auth.start();
    await auth.retry();
    expect(api.createSession).not.toHaveBeenCalled();
    expect(auth.getState().status).toBe('signed_out');
  });

  it('keeps the session when logout fails', async () => {
    const auth = new AuthController(scriptedApi({ me: [ok(SESSION)], logout: [fail('unavailable')] }), () => INIT);
    await auth.start();
    expect(await auth.logout()).toBe(false);
    expect(auth.getState().status).toBe('authenticated');
  });

  it('syncs an explicit language choice and notices an expired session', async () => {
    const api = scriptedApi({ me: [ok(SESSION)], setLanguage: [ok(null), fail('session_expired')] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    await auth.syncLanguage('az');
    expect(api.setLanguage).toHaveBeenCalledWith('az', 'csrf-test');
    await auth.syncLanguage('en');
    expect(auth.getState().status).toBe('session_expired');
  });
});
