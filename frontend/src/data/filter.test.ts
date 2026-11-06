import { buildDemoEvents, TOURNAMENTS } from './fixtures';
import { ALL_TOURNAMENTS, filterEvents, normalize, pickFeatured, type EventQuery } from './filter';
import { SPORTS } from './models';

const NOW = new Date('2026-11-08T06:00:00Z'); // 10:00 in Baku
const events = buildDemoEvents(NOW);
const base: EventQuery = { sport: 'football', day: '2026-11-08', timeZone: 'Asia/Baku', tournamentId: ALL_TOURNAMENTS, query: '' };
const ids = (q: Partial<EventQuery>) => filterEvents(events, TOURNAMENTS, { ...base, ...q }).map((e) => e.id);

describe('filterEvents', () => {
  it('has fixtures for every sport today', () => {
    for (const sport of SPORTS) expect(ids({ sport }).length).toBeGreaterThan(0);
  });

  it('filters by sport', () => {
    const tennis = filterEvents(events, TOURNAMENTS, { ...base, sport: 'tennis' });
    expect(tennis.every((e) => e.sport === 'tennis')).toBe(true);
  });

  it('filters by tournament', () => {
    expect(ids({ tournamentId: 'premier' })).toEqual(['fb-kingsford-harbour']);
    expect(ids({ tournamentId: 'laliga' })).toEqual(['fb-costa-sierra', 'fb-rm-fcb']);
  });

  it('filters by calendar day and puts live first', () => {
    expect(ids({})).toEqual(['fb-costa-sierra', 'fb-kingsford-harbour', 'fb-rm-fcb']);
    expect(ids({ day: '2026-11-07' })).toEqual(['fb-finished']);
    expect(ids({ day: '2026-11-11' })).toEqual([]);
  });

  it('uses the selected zone across midnight', () => {
    // fb-midnight starts 00:30 Baku on the 9th = 20:30 UTC / 23:30 Moscow on the 8th.
    expect(ids({ day: '2026-11-09' })).toContain('fb-midnight');
    expect(ids({ day: '2026-11-08', timeZone: 'UTC' })).toContain('fb-midnight');
    expect(ids({ day: '2026-11-08', timeZone: 'Europe/Moscow' })).toContain('fb-midnight');
    expect(ids({ day: '2026-11-09', timeZone: 'Europe/Moscow' })).not.toContain('fb-midnight');
  });

  it('searches team and participant names case- and accent-insensitively', () => {
    expect(ids({ query: 'real' })).toEqual(['fb-rm-fcb']);
    expect(ids({ query: 'BARCELONA real' })).toEqual(['fb-rm-fcb']);
    expect(ids({ query: 'premier' })).toEqual(['fb-kingsford-harbour']);
    expect(ids({ query: 'zzz' })).toEqual([]);
    expect(ids({ sport: 'mma', day: '2026-11-11', query: 'tomas' })).toEqual(['mma-main-event']);
    expect(ids({ sport: 'tennis', day: '2026-11-09', query: 'yilmaz' })).toEqual(['tn-atp-upcoming']);
  });

  it('combines search with other filters', () => {
    expect(ids({ query: 'real', tournamentId: 'premier' })).toEqual([]);
  });

  it('normalizes text', () => {
    expect(normalize('  Tomás   VARGA ')).toBe('tomas varga');
  });

  it('picks the featured event for the hero', () => {
    expect(pickFeatured(filterEvents(events, TOURNAMENTS, base))?.id).toBe('fb-rm-fcb');
    expect(pickFeatured([])).toBeNull();
  });

  it('generates dates from the clock instead of a fixed mockup date', () => {
    const later = buildDemoEvents(new Date('2027-03-15T06:00:00Z'));
    expect(later.find((e) => e.id === 'fb-rm-fcb')?.startsAt).toBe('2027-03-15T18:00:00.000Z');
  });
});
