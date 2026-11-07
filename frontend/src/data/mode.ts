/**
 * Build-time data mode (VITE_DATA_MODE):
 *   demo — offline fixtures, for local development and tests (the default);
 *   real — the Mini App backend with real providers. A real build never falls
 *          back to demo, and an unknown value stops the start instead of guessing.
 */
export type DataMode = 'demo' | 'real';

export function resolveDataMode(raw: string | undefined): DataMode {
  const value = (raw ?? '').trim();
  if (value === '' || value === 'demo') return 'demo';
  if (value === 'real') return 'real';
  throw new Error(`VITE_DATA_MODE must be "demo" or "real", got "${value}"`);
}
