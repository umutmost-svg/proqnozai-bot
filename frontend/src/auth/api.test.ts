import { createHttpAuthApi } from './api';

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const SESSION_BODY = {
  user: { id: 7, first_name: 'Ann', last_name: null, username: 'ann', lang: 'az', lang_source: 'telegram' },
  session: { expires_at: '2026-11-08T18:00:00Z', csrf_token: 'c1' },
  created: true,
};

describe('HTTP auth API', () => {
  it('posts initData in a JSON body, never in the URL', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => json(200, SESSION_BODY));
    const api = createHttpAuthApi(fetchMock);
    const result = await api.createSession('query_id=1&hash=abc');
    expect(result).toEqual({
      ok: true,
      value: {
        user: { id: 7, firstName: 'Ann', lastName: null, username: 'ann', lang: 'az' },
        expiresAt: '2026-11-08T18:00:00Z',
        csrfToken: 'c1',
      },
    });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/auth/session');
    expect(String(url)).not.toContain('hash');
    expect(init?.method).toBe('POST');
    expect(init?.credentials).toBe('same-origin');
    expect(JSON.parse(String(init?.body))).toEqual({ init_data: 'query_id=1&hash=abc' });
    expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('maps server errors to auth failures', async () => {
    const cases: [Response | Error, string][] = [
      [json(401, { error: 'session_expired' }), 'session_expired'],
      [json(401, { error: 'init_data_reused' }), 'init_data_reused'],
      [json(401, { error: 'something_else' }), 'unauthenticated'],
      [json(429, { error: 'rate_limited' }), 'rate_limited'],
      // Edge rate limit (Cloudflare): HTML body, still a rate limit, not an outage.
      [new Response('<html>Too Many Requests</html>', { status: 429, headers: { 'Content-Type': 'text/html' } }), 'rate_limited'],
      [json(403, { error: 'origin_not_allowed' }), 'forbidden'],
      [json(502, { error: 'internal' }), 'unavailable'],
      [new Response('<html>404</html>', { status: 404 }), 'unavailable'],
      [new TypeError('Failed to fetch'), 'unavailable'],
    ];
    for (const [response, error] of cases) {
      const api = createHttpAuthApi(async () => {
        if (response instanceof Error) throw response;
        return response;
      });
      expect(await api.me()).toEqual({ ok: false, error });
    }
  });

  it('sends the CSRF header on logout and never touches web storage', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => json(200, { ok: true }));
    const api = createHttpAuthApi(fetchMock);
    expect(await api.logout('c1', null)).toEqual({ ok: true, value: null });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/auth/logout');
    expect((init?.headers as Record<string, string>)['X-CSRF-Token']).toBe('c1');
    expect(init?.body).toBeUndefined();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it('sends the current launch initData in the logout body so the server burns it', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => json(200, { ok: true }));
    await createHttpAuthApi(fetchMock).logout('c1', 'auth_date=1&hash=ab');
    const [, init] = fetchMock.mock.calls[0]!;
    const headers = init?.headers as Record<string, string>;
    expect(init?.method).toBe('POST');
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers['X-CSRF-Token']).toBe('c1');
    expect(JSON.parse(String(init?.body))).toEqual({ init_data: 'auth_date=1&hash=ab' });
  });
});

describe('HTTP logout on all devices', () => {
  it('posts to /api/auth/logout-all with CSRF and the current launch', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => json(200, { ok: true, revoked: 2 }));
    expect(await createHttpAuthApi(fetchMock).logoutAll('c1', 'auth_date=1&hash=ab')).toEqual({ ok: true, value: null });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/auth/logout-all');
    expect((init?.headers as Record<string, string>)['X-CSRF-Token']).toBe('c1');
    expect(JSON.parse(String(init?.body))).toEqual({ init_data: 'auth_date=1&hash=ab' });
  });
});

describe('HTTP API timeout', () => {
  afterEach(() => vi.useRealTimers());

  it('a request that never answers ends as unavailable and is aborted', async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    const fetchMock = vi.fn<typeof fetch>((_, init) => {
      signal = init?.signal ?? undefined;
      return new Promise<Response>(() => {}); // hangs, ignores the signal
    });
    const pending = createHttpAuthApi(fetchMock, '/api', 10_000).me();
    await vi.advanceTimersByTimeAsync(9_999);
    let settled = false;
    void pending.then(() => (settled = true));
    await Promise.resolve();
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await pending).toEqual({ ok: false, error: 'unavailable' });
    expect(signal?.aborted).toBe(true);
  });

  it('a body that never finishes is bounded too', async () => {
    vi.useFakeTimers();
    const hangingBody = { ok: true, status: 200, json: () => new Promise(() => {}) } as unknown as Response;
    const pending = createHttpAuthApi(vi.fn<typeof fetch>(async () => hangingBody), '/api', 5_000).me();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await pending).toEqual({ ok: false, error: 'unavailable' });
  });

  it('a fast answer is not affected and leaves no timer behind', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn<typeof fetch>(async () => json(401, { error: 'unauthenticated' }));
    expect(await createHttpAuthApi(fetchMock).me()).toEqual({ ok: false, error: 'unauthenticated' });
    expect(vi.getTimerCount()).toBe(0);
  });
});
