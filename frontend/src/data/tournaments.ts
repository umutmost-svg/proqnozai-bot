import type { Tournament } from './models';

/** The quick-filter subset: the featured competitions plus any already chosen. A small catalogue or
 *  one without featured marks (demo data) is shown whole. */
export const QUICK_MAX = 12;

export function quickPicks(list: readonly Tournament[], keep: readonly string[] = []): Tournament[] {
  // A small catalogue (the classic ten competitions) keeps every chip.
  if (list.length <= QUICK_MAX || !list.some((t) => t.featured)) return [...list];
  return list.filter((t) => t.featured || keep.includes(t.id));
}

/** Case- and accent-insensitive match on the name and the (translated) area. */
export function matchesQuery(t: Tournament, query: string, area: string): boolean {
  const norm = (s: string) => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase();
  const q = norm(query.trim());
  return !q || norm(t.name).includes(q) || norm(area).includes(q);
}
