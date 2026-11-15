import { vi } from 'vitest';
import type { ApiResult, AuthApi, AuthSession } from './api';

export const SESSION: AuthSession = {
  user: { id: 501, firstName: 'Verified', lastName: 'Name', username: 'verified', lang: 'ru' },
  expiresAt: '2026-11-08T18:00:00Z',
  csrfToken: 'csrf-test',
};

type Script = Partial<Record<keyof AuthApi, ApiResult<unknown>[]>>;

/** Fake API returning scripted results in order (the last one repeats). */
export function scriptedApi(script: Script) {
  const next = <T>(name: keyof AuthApi): Promise<ApiResult<T>> => {
    const queue = script[name] ?? [{ ok: false, error: 'unavailable' }];
    const result = queue.length > 1 ? queue.shift()! : queue[0]!;
    return Promise.resolve(result as ApiResult<T>);
  };
  return {
    me: vi.fn<AuthApi['me']>(() => next<AuthSession>('me')),
    createSession: vi.fn<AuthApi['createSession']>(() => next<AuthSession>('createSession')),
    logout: vi.fn<AuthApi['logout']>(() => next<null>('logout')),
    logoutAll: vi.fn<AuthApi['logoutAll']>(() => next<null>('logoutAll')),
    setLanguage: vi.fn<AuthApi['setLanguage']>(() => next<null>('setLanguage')),
  } satisfies AuthApi;
}

export const ok = <T>(value: T): ApiResult<T> => ({ ok: true, value });
export const fail = (error: Extract<ApiResult<unknown>, { ok: false }>['error']): ApiResult<never> => ({
  ok: false,
  error,
});
