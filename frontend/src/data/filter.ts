import { dayKeyOf, type DayKey } from '../time/zoned';
import type { Sport, SportEvent, Tournament } from './models';

export const ALL_TOURNAMENTS = 'all';

export interface EventQuery {
  sport: Sport;
  day: DayKey;
  /** Calendar day is evaluated in this IANA zone. */
  timeZone: string;
  tournamentId: string;
  query: string;
}

/** Case- and diacritics-insensitive form used for search. */
export function normalize(text: string): string {
  return text
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .replace(/ı/g, 'i')
    .toLocaleLowerCase('en')
    .replace(/\s+/g, ' ')
    .trim();
}

export function matchesSearch(event: SportEvent, tournament: Tournament | undefined, query: string): boolean {
  const q = normalize(query);
  if (!q) return true;
  const haystack = normalize([event.home.name, event.away.name, tournament?.name ?? ''].join(' '));
  return q.split(' ').every((token) => haystack.includes(token));
}

export function filterEvents(
  events: readonly SportEvent[],
  tournaments: readonly Tournament[],
  q: EventQuery,
): SportEvent[] {
  const byId = new Map(tournaments.map((t) => [t.id, t]));
  return events
    .filter((e) => e.sport === q.sport)
    .filter((e) => q.tournamentId === ALL_TOURNAMENTS || e.tournamentId === q.tournamentId)
    .filter((e) => dayKeyOf(new Date(e.startsAt), q.timeZone) === q.day)
    .filter((e) => matchesSearch(e, byId.get(e.tournamentId), q.query))
    .sort(compareEvents);
}

const STATUS_ORDER = { live: 0, upcoming: 1, finished: 2 } as const;

export function compareEvents(a: SportEvent, b: SportEvent): number {
  return STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.startsAt.localeCompare(b.startsAt);
}

/** Featured event for the home hero: an explicitly featured one first, then the next relevant. */
export function pickFeatured(events: readonly SportEvent[]): SportEvent | null {
  return events.find((e) => e.featured) ?? events.find((e) => e.status !== 'finished') ?? events[0] ?? null;
}
