import { useMemo } from 'react';
import type { EventQuery } from '../data/filter';
import { useFilters, useSettings } from '../state/contexts';

/** Current filters as a provider query, plus a stable key for re-fetching. */
export function useEventQuery(): { query: EventQuery; key: string } {
  const { filters, day } = useFilters();
  const { timeZone } = useSettings();
  return useMemo(() => {
    const query: EventQuery = {
      sport: filters.sport,
      day,
      timeZone,
      tournamentId: filters.tournamentId,
      query: filters.query,
    };
    return { query, key: JSON.stringify(query) };
  }, [filters.sport, filters.tournamentId, filters.query, day, timeZone]);
}
