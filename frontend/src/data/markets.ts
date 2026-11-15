import { MARKET_KEYS, type MarketFamily, type MarketKey } from './models';

/** Same families as the backend (miniapp/forecast/markets.py). */
export const FAMILY: Record<MarketKey, MarketFamily> = {
  '1': '1x2', X: '1x2', '2': '1x2',
  '1X': 'double', X2: 'double', '12': 'double',
  'O1.5': 'total', 'U1.5': 'total', 'O2.5': 'total', 'U2.5': 'total', 'O3.5': 'total', 'U3.5': 'total',
  BTTS_Y: 'btts', BTTS_N: 'btts',
  'H1-0.5': 'handicap', 'H1-1.5': 'handicap', 'H2+0.5': 'handicap', 'H2+1.5': 'handicap',
  'HO0.5': 'team_total', 'HU0.5': 'team_total', 'HO1.5': 'team_total', 'HU1.5': 'team_total',
  'AO0.5': 'team_total', 'AU0.5': 'team_total', 'AO1.5': 'team_total', 'AU1.5': 'team_total',
};

export const FAMILIES: readonly MarketFamily[] = ['1x2', 'double', 'total', 'btts', 'handicap', 'team_total'];

/** Two-way markets: both sides always add up to 100. */
export const PAIRS: readonly (readonly [MarketKey, MarketKey])[] = [
  ['O1.5', 'U1.5'], ['O2.5', 'U2.5'], ['O3.5', 'U3.5'], ['BTTS_Y', 'BTTS_N'],
  ['H1-0.5', 'H2+0.5'], ['H1-1.5', 'H2+1.5'],
  ['HO0.5', 'HU0.5'], ['HO1.5', 'HU1.5'], ['AO0.5', 'AU0.5'], ['AO1.5', 'AU1.5'],
];

export function isMarketKey(value: unknown): value is MarketKey {
  return typeof value === 'string' && (MARKET_KEYS as readonly string[]).includes(value);
}

/**
 * The identities every shown set of market percents keeps (they come from one score
 * distribution, rounded with care on the backend): checked, never repaired.
 */
export function marketsConsistent(m: Record<MarketKey, number>): boolean {
  if (MARKET_KEYS.some((k) => !Number.isInteger(m[k]) || m[k] < 0 || m[k] > 100)) return false;
  if (m['1'] + m.X + m['2'] !== 100) return false;
  if (m['1X'] !== m['1'] + m.X || m.X2 !== m.X + m['2'] || m['12'] !== m['1'] + m['2']) return false;
  if (m['H1-0.5'] !== m['1'] || m['H2+0.5'] !== m.X2) return false;
  if (PAIRS.some(([a, b]) => m[a] + m[b] !== 100)) return false;
  return m['O1.5'] >= m['O2.5'] && m['O2.5'] >= m['O3.5'] && m['H1-1.5'] <= m['1'];
}

/** What the markets section shows per family: two-way rows, in a fixed order. */
export const DISPLAY_ROWS: Record<Exclude<MarketFamily, '1x2'>, readonly (readonly MarketKey[])[]> = {
  double: [['1X'], ['X2'], ['12']],
  total: [['O1.5', 'U1.5'], ['O2.5', 'U2.5'], ['O3.5', 'U3.5']],
  btts: [['BTTS_Y', 'BTTS_N']],
  handicap: [['H1-0.5', 'H2+0.5'], ['H1-1.5', 'H2+1.5']],
  team_total: [['HO0.5', 'HU0.5'], ['HO1.5', 'HU1.5'], ['AO0.5', 'AU0.5'], ['AO1.5', 'AU1.5']],
};
