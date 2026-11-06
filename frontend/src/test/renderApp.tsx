import { render } from '@testing-library/react';
import { StrictMode } from 'react';
import { App } from '../App';
import type { AuthApi } from '../auth/api';
import { AuthController } from '../auth/controller';
import { DemoDataProvider, type DemoOperation } from '../data/DemoDataProvider';
import { STORAGE_KEY, type Settings } from '../settings/settings';
import { BootTracker } from '../loading/boot';
import { createTelegramAdapter } from '../telegram/telegram';
import { fixedClock } from '../time/clock';

/** No backend: every call fails like an unreachable server (plain M1 demo). */
export const offlineAuthApi: AuthApi = {
  me: async () => ({ ok: false, error: 'unavailable' }),
  createSession: async () => ({ ok: false, error: 'unavailable' }),
  logout: async () => ({ ok: false, error: 'unavailable' }),
  logoutAll: async () => ({ ok: false, error: 'unavailable' }),
  setLanguage: async () => ({ ok: false, error: 'unavailable' }),
};

export const TEST_NOW = '2026-11-08T06:00:00Z'; // 10:00 in Baku

export function renderApp(
  hash = '#/',
  opts: {
    settings?: Partial<Settings>;
    failOnce?: DemoOperation[];
    aiLatencyMs?: number;
    latencyMs?: number;
    authApi?: AuthApi;
    initData?: string | null;
    strict?: boolean;
  } = {},
) {
  const settings: Settings = { lang: 'ru', timeZone: 'Asia/Baku', reduceMotion: false, haptics: false, ...opts.settings };
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  window.history.replaceState(null, '', `/${hash}`);
  const clock = fixedClock(TEST_NOW);
  const services = {
    clock,
    data: new DemoDataProvider(clock, { latencyMs: opts.latencyMs ?? 0, aiLatencyMs: opts.aiLatencyMs ?? 0, failOnce: opts.failOnce }),
    telegram: createTelegramAdapter(undefined),
    auth: new AuthController(opts.authApi ?? offlineAuthApi, () => opts.initData ?? null),
    scrollMemory: new Map<string, number>(),
    boot: new BootTracker(),
  };
  const app = <App services={services} />;
  return { ...render(opts.strict ? <StrictMode>{app}</StrictMode> : app), services };
}
