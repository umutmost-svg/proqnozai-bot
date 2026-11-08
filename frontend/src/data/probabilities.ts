import type { PickMarket } from './models';

/**
 * Same rule as the backend (miniapp/ai/pick.py, CLOSE_MARGIN): a team is the favourite
 * only when its win probability exceeds the other team's by at least this many points.
 * Closer than that, neither team is marked: no false favourite.
 */
export const CLOSE_MARGIN = 8;

/** green = favourite, red = outsider, orange = draw, neutral = teams too close to call. */
export type OutcomeTone = 'favourite' | 'outsider' | 'draw' | 'neutral';

/** Tones for [home, draw, away]. The probabilities themselves are never changed. */
export function outcomeTones(home: number, away: number): [OutcomeTone, OutcomeTone, OutcomeTone] {
  if (home - away >= CLOSE_MARGIN) return ['favourite', 'draw', 'outsider'];
  if (away - home >= CLOSE_MARGIN) return ['outsider', 'draw', 'favourite'];
  return ['neutral', 'draw', 'neutral'];
}

/** A market's probability, computed from the 1X2 estimate (double chances add up two outcomes). */
export function pickProbability(market: PickMarket, home: number, draw: number, away: number): number | null {
  switch (market) {
    case '1':
      return home;
    case 'X':
      return draw;
    case '2':
      return away;
    case '1X':
      return home + draw;
    case 'X2':
      return draw + away;
    case '12':
      return home + away;
    default:
      return null;
  }
}
