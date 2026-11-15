import { dayKeyOf, type DayKey } from '../time/zoned';
import type { EventStatus, Sport, SportEvent, Tournament } from './models';

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

/** Every word of the query appears in one of the names (case and accent insensitive). */
export function namesMatch(query: string, names: readonly (string | undefined)[]): boolean {
  const q = normalize(query);
  if (!q) return true;
  const haystack = normalize(names.filter(Boolean).join(' '));
  return q.split(' ').every((token) => haystack.includes(token));
}

export function matchesSearch(event: SportEvent, tournament: Tournament | undefined, query: string): boolean {
  return namesMatch(query, [event.home.name, event.home.fullName, event.away.name, event.away.fullName, tournament?.name]);
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

const STATUS_ORDER: Record<EventStatus, number> = {
  live: 0,
  upcoming: 1,
  suspended: 2,
  postponed: 3,
  finished: 4,
  cancelled: 5,
  unknown: 6,
};

export function compareEvents(a: SportEvent, b: SportEvent): number {
  return STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.startsAt.localeCompare(b.startsAt);
}

const FEATURABLE: ReadonlySet<EventStatus> = new Set(['live', 'upcoming', 'finished']);

/**
 * Demo only: the day's top matches without real tables — the featured one first, then
 * live, upcoming and finished by tournament priority (the real ranking is the backend's).
 */
export function pickTop(events: readonly SportEvent[], rank: (tournamentId: string) => number = () => 0, max = 5): SportEvent[] {
  const status = (e: SportEvent) => (e.status === 'finished' ? 1 : 0);
  return events
    .filter((e) => FEATURABLE.has(e.status))
    .sort((a, b) => Number(!!b.featured) - Number(!!a.featured) || status(a) - status(b) || rank(a.tournamentId) - rank(b.tournamentId) || a.startsAt.localeCompare(b.startsAt) || a.id.localeCompare(b.id))
    .slice(0, max);
}
