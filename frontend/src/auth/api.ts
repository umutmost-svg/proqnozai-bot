/**
 * HTTP client of the Mini App API (same origin, `/api`).
 *
 * The session itself is an HttpOnly cookie the browser attaches; this code
 * never sees or stores it. Raw initData is sent only in a POST body, once,
 * when there is no live session.
 */
import { isLang, type Lang } from '../i18n';

export interface AuthUser {
  id: number;
  firstName: string | null;
  lastName: string | null;
  username: string | null;
  lang: Lang;
}

export interface AuthSession {
  user: AuthUser;
  expiresAt: string;
  /** Per-session anti-CSRF token for state-changing requests; kept in memory only. */
  csrfToken: string;
}

export type AuthFailure =
  | 'unauthenticated'
  | 'session_expired'
  | 'init_data_invalid'
  | 'init_data_expired'
  | 'init_data_reused'
  | 'rate_limited'
  | 'forbidden'
  | 'unavailable';

export type ApiResult<T> = { ok: true; value: T } | { ok: false; error: AuthFailure };

export interface AuthApi {
  me(): Promise<ApiResult<AuthSession>>;
  createSession(initData: string): Promise<ApiResult<AuthSession>>;
  /** `initData`: the current launch, so the server burns it too (null outside Telegram). */
  logout(csrfToken: string, initData: string | null): Promise<ApiResult<null>>;
  setLanguage(lang: Lang, csrfToken: string): Promise<ApiResult<null>>;
}

const KNOWN_401 = new Set<AuthFailure>([
  'unauthenticated',
  'session_expired',
  'init_data_invalid',
  'init_data_expired',
  'init_data_reused',
]);

interface RawSession {
  user?: { id?: unknown; first_name?: unknown; last_name?: unknown; username?: unknown; lang?: unknown };
  session?: { expires_at?: unknown; csrf_token?: unknown };
}

const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);

function parseSession(raw: RawSession): AuthSession | null {
  const u = raw.user;
  const s = raw.session;
  if (!u || !s || typeof u.id !== 'number' || typeof s.csrf_token !== 'string' || typeof s.expires_at !== 'string') {
    return null;
  }
  return {
    user: {
      id: u.id,
      firstName: str(u.first_name),
      lastName: str(u.last_name),
      username: str(u.username),
      lang: isLang(u.lang) ? u.lang : 'ru',
    },
    expiresAt: s.expires_at,
    csrfToken: s.csrf_token,
  };
}

export function createHttpAuthApi(fetchImpl: typeof fetch = (...a) => fetch(...a), base = '/api'): AuthApi {
  async function call(path: string, init: RequestInit = {}): Promise<ApiResult<unknown>> {
    let response: Response;
    try {
      response = await fetchImpl(`${base}${path}`, {
        credentials: 'same-origin',
        cache: 'no-store',
        ...init,
        headers: { Accept: 'application/json', ...init.headers },
      });
    } catch {
      return { ok: false, error: 'unavailable' };
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      // Not our API (e.g. a static server without /api) — treat as unavailable.
      return { ok: false, error: 'unavailable' };
    }
    if (response.ok) return { ok: true, value: body };
    const code = (body as { error?: unknown } | null)?.error;
    if (response.status === 401) {
      return { ok: false, error: KNOWN_401.has(code as AuthFailure) ? (code as AuthFailure) : 'unauthenticated' };
    }
    if (response.status === 429) return { ok: false, error: 'rate_limited' };
    if (response.status === 403) return { ok: false, error: 'forbidden' };
    return { ok: false, error: 'unavailable' };
  }

  const toSession = (r: ApiResult<unknown>): ApiResult<AuthSession> => {
    if (!r.ok) return r;
    const session = parseSession(r.value as RawSession);
    return session ? { ok: true, value: session } : { ok: false, error: 'unavailable' };
  };

  const postJson = (path: string, body: unknown, headers: Record<string, string> = {}) =>
    call(path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });

  return {
    me: async () => toSession(await call('/me')),
    createSession: async (initData) => toSession(await postJson('/auth/session', { init_data: initData })),
    logout: async (csrfToken, initData) => {
      const headers = { 'X-CSRF-Token': csrfToken };
      const r = initData
        ? await postJson('/auth/logout', { init_data: initData }, headers)
        : await call('/auth/logout', { method: 'POST', headers });
      return r.ok ? { ok: true, value: null } : r;
    },
    setLanguage: async (lang, csrfToken) => {
      const r = await postJson('/me/language', { lang }, { 'X-CSRF-Token': csrfToken });
      return r.ok ? { ok: true, value: null } : r;
    },
  };
}
