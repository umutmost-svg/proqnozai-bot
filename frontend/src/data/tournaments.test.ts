import { matchesQuery, quickPicks } from './tournaments';
import type { Tournament } from './models';

const t = (id: string, featured = false, area = 'England'): Tournament => ({ id, sport: 'football', name: id, area, ...(featured ? { featured } : {}) });

describe('quickPicks', () => {
  it('keeps the featured competitions and any chosen one', () => {
    const list = [t('fd:PL', true), t('fd:EL1'), t('fd:TSL', true), t('fd:SSL')];
    expect(quickPicks(list).map((x) => x.id)).toEqual(['fd:PL', 'fd:TSL']);
    expect(quickPicks(list, ['fd:SSL']).map((x) => x.id)).toEqual(['fd:PL', 'fd:TSL', 'fd:SSL']);
  });
  it('shows a catalogue without featured marks whole', () => {
    expect(quickPicks([t('a'), t('b')])).toHaveLength(2);
  });
});

describe('matchesQuery', () => {
  it('matches name or translated area, ignoring case and accents', () => {
    expect(matchesQuery({ ...t('x'), name: 'Süper Lig' }, 'super', 'Турция')).toBe(true);
    expect(matchesQuery(t('x'), 'турц', 'Турция')).toBe(true);
    expect(matchesQuery(t('x'), 'zzz', 'Турция')).toBe(false);
  });
});
