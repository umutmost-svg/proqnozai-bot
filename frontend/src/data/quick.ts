import { addDays, dayKeyOf, zonedTimeToInstant } from '../time/zoned';
import { FAMILIES } from './markets';
import type { QuickCriteria, QuickMarket, QuickMode, QuickPeriod, QuickRisk } from './models';

export const QUICK_MODES: readonly QuickMode[] = ['single', 'coupon'];
export const QUICK_PERIODS: readonly QuickPeriod[] = ['today', '24h', '3d', '7d'];
export const QUICK_MARKETS: readonly QuickMarket[] = ['any', ...FAMILIES];
export const QUICK_RISKS: readonly QuickRisk[] = ['conservative', 'balanced', 'elevated'];
export const COUPON_SIZES = [2, 3, 4, 5] as const;

/**
 * The goal model's recommendations are experimental until the prospective check of M3.5
 * passes; flip together with the backend's frozen status, never on the backend alone.
 */
export const MODEL_CONFIRMED = false;

export const DEFAULT_CRITERIA: QuickCriteria = {
  mode: 'single',
  period: 'today',
  tournaments: [],
  market: 'any',
  risk: 'balanced',
  size: 3,
};

const HOURS: Record<Exclude<QuickPeriod, 'today'>, number> = { '24h': 24, '3d': 72, '7d': 168 };

/** The period as an instant range: "today" is the rest of the user's local day, not the UTC one. */
export function quickRange(period: QuickPeriod, now: Date, timeZone: string): { from: Date; to: Date } {
  if (period === 'today') {
    const tomorrow = addDays(dayKeyOf(now, timeZone), 1);
    return { from: now, to: zonedTimeToInstant(tomorrow, '00:00', timeZone) };
  }
  return { from: now, to: new Date(now.getTime() + HOURS[period] * 3_600_000) };
}

/** A stored value back to criteria; anything unknown falls back to the default of that field. */
export function readCriteria(raw: unknown, known: ReadonlySet<string>): QuickCriteria {
  const v = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const pick = <T extends string>(value: unknown, allowed: readonly T[], fallback: T): T =>
    allowed.includes(value as T) ? (value as T) : fallback;
  const size = typeof v.size === 'number' && (COUPON_SIZES as readonly number[]).includes(v.size) ? v.size : DEFAULT_CRITERIA.size;
  const tournaments = Array.isArray(v.tournaments)
    ? [...new Set(v.tournaments.filter((x): x is string => typeof x === 'string' && (known.size === 0 || known.has(x))))]
    : [];
  return {
    mode: pick(v.mode, QUICK_MODES, DEFAULT_CRITERIA.mode),
    period: pick(v.period, QUICK_PERIODS, DEFAULT_CRITERIA.period),
    tournaments,
    market: pick(v.market, QUICK_MARKETS, DEFAULT_CRITERIA.market),
    risk: pick(v.risk, QUICK_RISKS, DEFAULT_CRITERIA.risk),
    size,
  };
}

const STORAGE_KEY = 'proqnozai.quick.v1';

/** Remembered per device (a convenience: the screen works without storage). */
export function loadCriteria(known: ReadonlySet<string>): QuickCriteria {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    return readCriteria(raw ? JSON.parse(raw) : null, known);
  } catch {
    return DEFAULT_CRITERIA;
  }
}

export function saveCriteria(criteria: QuickCriteria): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(criteria));
  } catch {
    // Private mode or blocked storage: the choice simply is not remembered.
  }
}
