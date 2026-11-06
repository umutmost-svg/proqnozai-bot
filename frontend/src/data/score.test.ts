import { buildDemoEvents } from './fixtures';
import type { BasketballEvent, MmaEvent, SportEvent, TennisEvent } from './models';
import { basketballTotal, headlineScore, tennisSetsWon } from './score';
import { periodBreakdown, phaseText, statRows } from './present';
import { createTranslator } from '../i18n';

const events = buildDemoEvents(new Date('2026-11-08T06:00:00Z'));
const get = <T extends SportEvent>(id: string) => events.find((e) => e.id === id) as T;
const t = createTranslator('ru');

describe('sport-specific scoring', () => {
  it('football shows goals', () => {
    expect(headlineScore(get('fb-costa-sierra'))).toBe('2 : 1');
    expect(headlineScore(get('fb-rm-fcb'))).toBeNull();
  });

  it('tennis counts sets, not games, and ignores the set in progress', () => {
    const live = get<TennisEvent>('tn-live');
    expect(tennisSetsWon(live)).toEqual([1, 1]);
    expect(phaseText(t, live, '')).toBe('3-й сет · 40:30');
    expect(periodBreakdown(live)).toBe('6:4 · 3:6 · 2:1');
    expect(tennisSetsWon(get<TennisEvent>('tn-wta-finished'))).toEqual([2, 1]);
  });

  it('basketball sums quarters and overtimes', () => {
    expect(basketballTotal(get<BasketballEvent>('bb-live'))).toEqual([74, 68]);
    const ot = get<BasketballEvent>('bb-finished-ot');
    expect(basketballTotal(ot)).toEqual([106, 103]);
    expect(periodBreakdown(ot)).toContain('OT 12:9');
  });

  it('MMA shows rounds and the method of finish', () => {
    const live = get<MmaEvent>('mma-live');
    expect(headlineScore(live)).toBe('R2');
    expect(phaseText(t, live, '')).toBe('03:14 · из 3 раундов');
    expect(phaseText(t, get<MmaEvent>('mma-finished'), '')).toBe('KO/TKO · R1 · 04:12');
  });

  it('stats rows differ per sport', () => {
    expect(statRows(t, get('fb-costa-sierra'))?.map((r) => r.label)).toEqual(['Владение', 'Удары', 'В створ', 'Угловые']);
    expect(statRows(t, get('tn-live'))?.[2]).toEqual({ label: 'Брейк-пойнты', home: '3/6', away: '2/5' });
    expect(statRows(t, get('mma-live'))?.[2]).toEqual({ label: 'Контроль', home: '02:14', away: '01:37' });
    expect(statRows(t, get('fb-rm-fcb'))).toBeNull();
  });
});
