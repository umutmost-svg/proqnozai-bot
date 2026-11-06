import { AuthController } from './controller';
import type { AuthApi } from './api';
import type { SignedOutMarker } from './signedOut';
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

describe('AuthController: a cookie of another Telegram account', () => {
  // The browser context keeps account A's cookie; the Mini App is opened by account B.
  const OTHER: typeof SESSION = { ...SESSION, user: { ...SESSION.user, id: 777, firstName: 'Other' } };
  const recordStates = (auth: AuthController) => {
    const seen: ReturnType<AuthController['getState']>[] = [];
    auth.subscribe(() => seen.push(auth.getState()));
    return seen;
  };
  const shown = (seen: ReturnType<AuthController['getState']>[], id: number) =>
    seen.some((s) => s.status === 'authenticated' && s.session.user.id === id);

  it('never shows the other account; the signed launch is exchanged on the server', async () => {
    const api = scriptedApi({ me: [ok(OTHER)], createSession: [ok(SESSION)] });
    const auth = new AuthController(api, () => INIT); // INIT names user 501
    const seen = recordStates(auth);
    await auth.start();
    expect(api.createSession).toHaveBeenCalledWith(INIT);
    expect(auth.getState()).toEqual({ status: 'authenticated', session: SESSION });
    expect(shown(seen, 777)).toBe(false);
  });

  it('if the exchange fails, the other account is still never shown', async () => {
    const api = scriptedApi({ me: [ok(OTHER)], createSession: [fail('init_data_expired')] });
    const auth = new AuthController(api, () => INIT);
    const seen = recordStates(auth);
    await auth.start();
    expect(auth.getState().status).toBe('init_data_expired');
    expect(shown(seen, 777)).toBe(false);
  });

  it('a launch whose user cannot be read goes through the server check too', async () => {
    const api = scriptedApi({ me: [ok(SESSION)], createSession: [ok(SESSION)] });
    const auth = new AuthController(api, () => 'query_id=AA&user=not-json&auth_date=1&hash=' + '0'.repeat(64));
    await auth.start();
    expect(api.createSession).toHaveBeenCalledTimes(1);
  });

  it('outside Telegram (no launch) the cookie session is used as before', async () => {
    const api = scriptedApi({ me: [ok(OTHER)] });
    const auth = new AuthController(api, () => null);
    await auth.start();
    expect(auth.getState()).toEqual({ status: 'authenticated', session: OTHER });
    expect(api.createSession).not.toHaveBeenCalled();
  });
});

describe('AuthController logout and language', () => {
  it('logs out with the CSRF token and does not sign in again automatically', async () => {
    const api = scriptedApi({ me: [ok(SESSION)], logout: [ok(null)] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    expect(await auth.logout()).toBe(true);
    expect(api.logout).toHaveBeenCalledWith('csrf-test', INIT);
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

/** In-memory stand-in for the sessionStorage marker: shared between "page loads". */
function memoryMarker(): SignedOutMarker & { saved: Set<string> } {
  const saved = new Set<string>();
  return { saved, matches: async (d) => saved.has(d), remember: async (d) => void saved.add(d) };
}

describe('AuthController logout survives a reload', () => {
  it('after logout and reload, no bootstrap request is made', async () => {
    const marker = memoryMarker();
    const first = new AuthController(scriptedApi({ me: [ok(SESSION)], logout: [ok(null)] }), () => INIT, marker);
    await first.start();
    expect(await first.logout()).toBe(true);

    // Reload: a new controller (new page), same launch initData, same storage.
    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [ok(SESSION)] });
    const reloaded = new AuthController(api, () => INIT, marker);
    await reloaded.start();
    expect(reloaded.getState().status).toBe('signed_out');
    expect(api.me).not.toHaveBeenCalled();
    expect(api.createSession).not.toHaveBeenCalled();
    await reloaded.retry();
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it('a new launch (reopened Mini App) signs in normally', async () => {
    const marker = memoryMarker();
    marker.saved.add(INIT);
    const api = scriptedApi({ me: [fail('unauthenticated')], createSession: [ok(SESSION)] });
    const next = new AuthController(api, () => INIT.replace('auth_date=1', 'auth_date=2'), marker);
    await next.start();
    expect(next.getState().status).toBe('authenticated');
    expect(api.createSession).toHaveBeenCalledTimes(1);
  });

  it('a failed logout leaves no marker', async () => {
    const marker = memoryMarker();
    const auth = new AuthController(scriptedApi({ me: [ok(SESSION)], logout: [fail('unavailable')] }), () => INIT, marker);
    await auth.start();
    expect(await auth.logout()).toBe(false);
    expect(marker.saved.size).toBe(0);
  });

  it('outside Telegram logout sends no launch data', async () => {
    const api = scriptedApi({ me: [ok(SESSION)], logout: [ok(null)] });
    const auth = new AuthController(api, () => null, memoryMarker());
    await auth.start();
    await auth.logout();
    expect(api.logout).toHaveBeenCalledWith('csrf-test', null);
  });
});

describe('AuthController late responses', () => {
  it('a language update answered after logout does not overwrite signed_out', async () => {
    let answer: (r: Awaited<ReturnType<AuthApi['setLanguage']>>) => void = () => {};
    const api = scriptedApi({ me: [ok(SESSION)], logout: [ok(null)] });
    api.setLanguage.mockImplementation(() => new Promise((r) => (answer = r)));
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    const pending = auth.syncLanguage('az');
    await auth.logout();
    answer(fail('unauthenticated'));
    await pending;
    expect(auth.getState().status).toBe('signed_out');
  });
});

describe('AuthController logout on all devices', () => {
  it('calls logout-all with CSRF and the current launch, then signs out', async () => {
    const marker = memoryMarker();
    const api = scriptedApi({ me: [ok(SESSION)], logoutAll: [ok(null)] });
    const auth = new AuthController(api, () => INIT, marker);
    await auth.start();
    expect(await auth.logout(true)).toBe(true);
    expect(api.logoutAll).toHaveBeenCalledWith('csrf-test', INIT);
    expect(api.logout).not.toHaveBeenCalled();
    expect(auth.getState().status).toBe('signed_out');
    expect(marker.saved.has(INIT)).toBe(true);
  });

  it('an unconfirmed logout-all is never shown as success', async () => {
    const api = scriptedApi({ me: [ok(SESSION)], logoutAll: [fail('unavailable')] });
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    expect(await auth.logout(true)).toBe(false);
    expect(auth.getState().status).toBe('authenticated');
  });

  it('logout-all with an ended session reports the expiry instead of success', async () => {
    const marker = memoryMarker();
    const api = scriptedApi({ me: [ok(SESSION)], logoutAll: [fail('session_expired')] });
    const auth = new AuthController(api, () => INIT, marker);
    await auth.start();
    expect(await auth.logout(true)).toBe(false);
    expect(auth.getState().status).toBe('session_expired');
    expect(marker.saved.size).toBe(0);
  });
});

describe('AuthController sign-out is serialized', () => {
  it('a second tap while one sign-out is in flight does nothing', async () => {
    let answer: (r: Awaited<ReturnType<AuthApi['logoutAll']>>) => void = () => {};
    const api = scriptedApi({ me: [ok(SESSION)], logout: [ok(null)] });
    api.logoutAll.mockImplementation(() => new Promise((r) => (answer = r)));
    const auth = new AuthController(api, () => INIT);
    await auth.start();
    const all = auth.logout(true);
    expect(await auth.logout()).toBe(false);
    expect(api.logout).not.toHaveBeenCalled();
    answer(ok(null));
    expect(await all).toBe(true);
    expect(auth.getState().status).toBe('signed_out');
  });
});
