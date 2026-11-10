import type { Tournament } from './models';

/** The quick-filter subset: the featured competitions plus any already chosen. A catalogue
 *  without featured marks (demo data, few competitions) is shown whole. */
export function quickPicks(list: readonly Tournament[], keep: readonly string[] = []): Tournament[] {
  if (!list.some((t) => t.featured)) return [...list];
  return list.filter((t) => t.featured || keep.includes(t.id));
}

/** Case- and accent-insensitive match on the name and the (translated) area. */
export function matchesQuery(t: Tournament, query: string, area: string): boolean {
  const norm = (s: string) => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase();
  const q = norm(query.trim());
  return !q || norm(t.name).includes(q) || norm(area).includes(q);
}
