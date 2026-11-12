import { describe, expect, it } from 'vitest';
import { readEventOdds } from '../data/BackendSportsDataProvider';

const sent = { mono: 1000, wall: Date.parse('2027-01-15T10:00:00Z') };
const ok = {
  fetchedAt: '2027-01-15T09:59:00Z', validFor: 600,
  quotes: [
    { market: '1', odds: 1.8, marketProbability: 52.4, margin: 4.8 },
    { market: 'X', odds: 3.9, marketProbability: 24.1, margin: 4.8 },
    { market: '2', odds: 4.5, marketProbability: 23.5, margin: 4.8 },
  ],
};

describe('match page odds (numbers only, valid until a deadline)', () => {
  it('reads the backend shape and dates the deadline from when the request was sent', () => {
    const got = readEventOdds(ok, sent)!;
    expect(got.quotes.map((q) => q.market)).toEqual(['1', 'X', '2']);
    expect(got.expires).toEqual({ mono: 1000 + 600_000, wall: sent.wall + 600_000 });
  });
  it('drops anything outside the shape: nothing is shown rather than a wrong price', () => {
    expect(readEventOdds(null, sent)).toBeNull();
    expect(readEventOdds({ ...ok, quotes: [] }, sent)).toBeNull();
    expect(readEventOdds({ ...ok, validFor: 0 }, sent)).toBeNull();
    expect(readEventOdds({ ...ok, validFor: 3600 }, sent)).toBeNull(); // longer than 15 min is impossible
    expect(readEventOdds({ ...ok, fetchedAt: 'yesterday' }, sent)).toBeNull();
    for (const bad of [{ market: 'Mostbet', odds: 2, marketProbability: 50, margin: 5 }, { market: '1', odds: 1, marketProbability: 50, margin: 5 },
      { market: '1', odds: 2, marketProbability: 120, margin: 5 }, { market: '1', odds: '2', marketProbability: 50, margin: 5 }]) {
      expect(readEventOdds({ ...ok, quotes: [bad] }, sent)).toBeNull();
    }
    expect(readEventOdds({ ...ok, quotes: [ok.quotes[0], ok.quotes[0]] }, sent)).toBeNull(); // duplicate market
  });
});
