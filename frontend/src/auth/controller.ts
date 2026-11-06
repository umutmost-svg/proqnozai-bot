/**
 * Auth state machine of the Mini App. Lives outside React (one instance per
 * page), so StrictMode double effects and re-renders cannot start parallel
 * bootstraps. There are no automatic retries: every repeat is a user action.
 */
import type { Lang } from '../i18n';
import type { AuthApi, AuthFailure, AuthSession } from './api';

export type AuthState =
  | { status: 'checking' }
  | { status: 'authenticated'; session: AuthSession }
  | { status: 'outside_telegram' }
  | { status: 'init_data_invalid' }
  | { status: 'init_data_expired' }
  | { status: 'session_expired' }
  | { status: 'unavailable' }
  | { status: 'rate_limited' }
  | { status: 'signed_out' };

export type AuthStatus = AuthState['status'];

/** States from which "Повторить" makes sense without reopening the Mini App. */
export const RETRYABLE: ReadonlySet<AuthStatus> = new Set(['unavailable', 'rate_limited']);

export class AuthController {
  private state: AuthState = { status: 'checking' };
  private listeners = new Set<() => void>();
  private inflight: Promise<void> | null = null;
  private started = false;
  /** Set by logout; this page never signs in again with its launch initData. */
  private signedOut = false;

  constructor(
    private readonly api: AuthApi,
    /** Raw Telegram.WebApp.initData, or null outside Telegram. */
    private readonly initData: () => string | null,
  ) {}

  getState = (): AuthState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Idempotent: the first call bootstraps, later calls return the same work. */
  start(): Promise<void> {
    if (this.started) return this.inflight ?? Promise.resolve();
    this.started = true;
    return this.run();
  }

  retry(): Promise<void> {
    if (this.inflight) return this.inflight;
    if (!RETRYABLE.has(this.state.status)) return Promise.resolve();
    return this.run();
  }

  async logout(): Promise<boolean> {
    if (this.state.status !== 'authenticated' || this.inflight) return false;
    const result = await this.api.logout(this.state.session.csrfToken);
    if (!result.ok && result.error !== 'unauthenticated' && result.error !== 'session_expired') return false;
    this.signedOut = true;
    this.set({ status: 'signed_out' });
    return true;
  }

  /** Best-effort: the local setting stays the UI source of truth in M2. */
  async syncLanguage(lang: Lang): Promise<void> {
    if (this.state.status !== 'authenticated') return;
    const result = await this.api.setLanguage(lang, this.state.session.csrfToken);
    if (!result.ok && (result.error === 'unauthenticated' || result.error === 'session_expired')) {
      this.set({ status: 'session_expired' });
    }
  }

  private run(): Promise<void> {
    this.set({ status: 'checking' });
    this.inflight = this.bootstrap()
      .then((next) => this.set(next))
      .finally(() => {
        this.inflight = null;
      });
    return this.inflight;
  }

  private async bootstrap(): Promise<AuthState> {
    // 1. An existing server session wins: no fresh initData needed.
    const me = await this.api.me();
    if (me.ok) return { status: 'authenticated', session: me.value };

    const initData = this.initData();
    if (!initData) {
      if (me.error === 'session_expired') return { status: 'session_expired' };
      return { status: 'outside_telegram' };
    }
    if (me.error === 'unavailable') return { status: 'unavailable' };
    if (me.error === 'rate_limited') return { status: 'rate_limited' };
    if (this.signedOut) return { status: 'signed_out' };

    // 2. Exchange the launch initData once for our own session.
    const created = await this.api.createSession(initData);
    if (created.ok) return { status: 'authenticated', session: created.value };
    return this.failure(created.error, me.error === 'session_expired');
  }

  private failure(error: AuthFailure, hadExpiredSession: boolean): AuthState {
    switch (error) {
      case 'unavailable':
        return { status: 'unavailable' };
      case 'rate_limited':
        return { status: 'rate_limited' };
      case 'init_data_reused':
        return { status: 'signed_out' };
      case 'init_data_expired':
        return { status: hadExpiredSession ? 'session_expired' : 'init_data_expired' };
      default:
        return { status: 'init_data_invalid' };
    }
  }

  private set(next: AuthState) {
    this.state = next;
    this.listeners.forEach((l) => l());
  }
}
