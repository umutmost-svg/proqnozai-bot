import { useMemo } from 'react';
import type { EventQuery } from '../data/filter';
import { useFilters, useSettings } from '../state/contexts';

/** Current date and tournament filters as a provider query, plus a stable key for re-fetching. */
export function useEventQuery(): { query: EventQuery; key: string } {
  const { filters, day } = useFilters();
  const { timeZone } = useSettings();
  return useMemo(() => {
    const query: EventQuery = {
      sport: filters.sport,
      day,
      timeZone,
      tournamentId: filters.tournamentId,
      // The search box is global (its own results); it never narrows the day's list.
      query: '',
    };
    return { query, key: JSON.stringify(query) };
  }, [filters.sport, filters.tournamentId, day, timeZone]);
}
