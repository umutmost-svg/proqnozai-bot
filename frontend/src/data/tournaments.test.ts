import { matchesQuery, quickPicks } from './tournaments';
import type { Tournament } from './models';

const t = (id: string, featured = false, area = 'England'): Tournament => ({ id, sport: 'football', name: id, area, ...(featured ? { featured } : {}) });

describe('quickPicks', () => {
  it('keeps the featured competitions and any chosen one', () => {
    const minor = Array.from({ length: 12 }, (_, i) => t(`fd:X${i}`));
    const list = [t('fd:PL', true), t('fd:TSL', true), ...minor];
    expect(quickPicks(list).map((x) => x.id)).toEqual(['fd:PL', 'fd:TSL']);
    expect(quickPicks(list, ['fd:X3']).map((x) => x.id)).toEqual(['fd:PL', 'fd:TSL', 'fd:X3']);
  });
  it('keeps every chip of the classic small catalogue', () => {
    const small = [t('fd:PL', true), t('fd:ELC'), t('fd:BSA')];
    expect(quickPicks(small)).toHaveLength(3);
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
