import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, ApiError, setCsrf, setUnauthenticatedHandler, type Me } from './api';

// ---- auth ----

// `expired`: the session ended while pages were open (a 401): they stay mounted under a sign-in dialog (CRM audit B2).
type AuthState = { status: 'loading' } | { status: 'anonymous' } | { status: 'unreachable' }
  | { status: 'signed-in'; me: Me; expired?: boolean };
interface AuthApi {
  state: AuthState;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}
const AuthContext = createContext<AuthApi | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading' });
  const current = useRef<AuthState>(state);
  current.current = state;
  const refresh = useCallback(async () => {
    try {
      // A brief network failure right after sign-in must not drop an expired page with its unsaved text: retried, and
      // only "not signed in" (401) ends the session view (Codex CRM-B2 r2).
      let me: Me | null = null;
      for (let attempt = 0; me === null; attempt += 1) {
        try {
          me = await api<Me>('/api/admin/auth/me');
        } catch (err) {
          if ((err instanceof ApiError && err.status === 401) || attempt >= 2) throw err;
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
      }
      const prev = current.current;
      const access = (m: Me) => JSON.stringify([m.admin.id, m.admin.role, [...(m.admin.grants ?? [])].sort()]);
      if (prev.status === 'signed-in' && access(prev.me) !== access(me)) {
        // Someone else, or the same account with another role or grants, signed in over an expired page: what it
        // loaded is never shown under the new access, not even for a moment (the dialog stays until the reload).
        window.location.assign('/');
        return;
      }
      setCsrf(me.csrf);
      setState({ status: 'signed-in', me });
    } catch (err) {
      const prev = current.current;
      if (prev.status === 'signed-in' && !(err instanceof ApiError && err.status === 401)) {
        throw err;  // the page and its sign-in dialog stay; the dialog shows the error and the admin can retry
      }
      setCsrf('');
      setState({ status: 'anonymous' });
    }
  }, []);
  const logout = useCallback(async () => {
    try {
      await api('/api/admin/auth/logout', { method: 'POST', body: {} });
    } finally {
      setCsrf('');
      setState({ status: 'anonymous' });
    }
  }, []);
  useEffect(() => {
    setUnauthenticatedHandler(() => {
      setCsrf('');
      setState((prev) => (prev.status === 'signed-in' ? { ...prev, expired: true } : { status: 'anonymous' }));
    });
    api<Me>('/api/admin/auth/me')
      .then((me) => { setCsrf(me.csrf); setState({ status: 'signed-in', me }); })
      // Only "not signed in" leads to the sign-in form; a server that cannot be reached says so (CRM audit B2).
      .catch((err: unknown) => {
        setCsrf('');
        setState(err instanceof ApiError && err.status === 401 ? { status: 'anonymous' } : { status: 'unreachable' });
      });
  }, []);
  const value = useMemo(() => ({ state, refresh, logout }), [state, refresh, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthApi {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('AuthProvider missing');
  return ctx;
}

export function useMe(): Me {
  const { state } = useAuth();
  if (state.status !== 'signed-in') throw new Error('not signed in');
  return state.me;
}

// ---- preferences (per viewer, browser storage is a convenience only) ----

export type Period = 1 | 7 | 30 | 90;
export const PERIOD_LABELS: Record<Period, string> = {
  1: 'Последние 24 часа', 7: 'Последние 7 дней', 30: 'Последние 30 дней', 90: 'Последние 90 дней',
};

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice lasts until reload */
  }
}

interface Prefs {
  period: Period;
  setPeriod: (p: Period) => void;
  demo: boolean;
  setDemo: (on: boolean) => void;
}
const PrefsContext = createContext<Prefs | null>(null);

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [period, setPeriodState] = useState<Period>(() => {
    const saved = Number(read('sporbot-admin.period'));
    return ([1, 7, 30, 90] as number[]).includes(saved) ? (saved as Period) : 30;
  });
  const [demo, setDemoState] = useState<boolean>(() => read('sporbot-admin.demo') === '1');
  const value = useMemo<Prefs>(() => ({
    period,
    setPeriod: (p) => { setPeriodState(p); write('sporbot-admin.period', String(p)); },
    demo,
    setDemo: (on) => { setDemoState(on); write('sporbot-admin.demo', on ? '1' : '0'); },
  }), [period, demo]);
  return <PrefsContext.Provider value={value}>{children}</PrefsContext.Provider>;
}

export function usePrefs(): Prefs {
  const ctx = useContext(PrefsContext);
  const auth = useContext(AuthContext);
  if (!ctx) throw new Error('PrefsProvider missing');
  // The server never serves demo data: there the switch does not exist (a stored "on" from a pilot is ignored).
  const demoAllowed = auth?.state.status === 'signed-in' && auth.state.me.pilot.demoTelemetry;
  return demoAllowed ? ctx : { ...ctx, demo: false, setDemo: () => undefined };
}

/** The panel runs on the server (real data, no demo switch) rather than as the local pilot. */
export function useServerMode(): boolean {
  const auth = useContext(AuthContext);
  return auth?.state.status === 'signed-in' && !auth.state.me.pilot.demoTelemetry;
}

// ---- data loading ----

export interface Load<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

export function useLoad<T>(path: string | null): Load<T> {
  const [tick, setTick] = useState(0);
  const key = path === null ? null : `${path}#${tick}`;
  const [result, setResult] = useState<{ key: string; data: T | null; error: unknown } | null>(null);
  useEffect(() => {
    if (path === null || key === null) return;
    let alive = true;
    api<T>(path)
      .then((data) => { if (alive) setResult({ key, data, error: null }); })
      .catch((error: unknown) => { if (alive) setResult({ key, data: null, error }); });
    return () => { alive = false; };
  }, [path, key]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const current = result !== null && result.key === key;
  return {
    // While a new request runs the previous data stays visible (no flicker on filter changes).
    data: result?.data ?? null,
    error: current ? result.error : null,
    loading: key !== null && !current,
    reload,
  };
}

export function can(role: string, permission: 'read_personal' | 'manage_users' | 'manage_settings' | 'manage_admins'): boolean {
  const allowed = {
    read_personal: ['owner', 'admin'],
    manage_users: ['owner', 'admin'],
    manage_settings: ['owner', 'admin'],
    manage_admins: ['owner'],
  };
  return allowed[permission].includes(role);
}
