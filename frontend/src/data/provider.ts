import type { AiAnalysis, SportEvent, Tournament, Sport } from './models';
import type { EventQuery } from './filter';

/**
 * Whether a sport has data. "demo": the offline demo provider; "connected": a real
 * provider answers; "not_connected": no real source for this sport (nothing is
 * substituted); "unknown": not asked yet or the source is unreachable.
 */
export interface SportCoverage {
  status: 'demo' | 'connected' | 'not_connected' | 'unknown';
  provider: string | null;
  /** Attribution the provider's terms require wherever its data is shown. */
  attribution: string | null;
}

/**
 * Data boundary of the Mini App: the offline demo provider (M1) and the backend
 * provider (M3, real data) implement the same interface.
 */
export interface SportsDataProvider {
  readonly isDemo: boolean;
  /** Last known coverage for a sport (synchronous; updated by every response). */
  coverage(sport: Sport): SportCoverage;
  /** Tournament catalogue is small and static per release, so it is synchronous. */
  listTournaments(sport: Sport): Tournament[];
  listEvents(query: EventQuery): Promise<SportEvent[]>;
  getFeatured(query: EventQuery): Promise<SportEvent | null>;
  getEvent(id: string): Promise<SportEvent | null>;
  getTournament(id: string): Tournament | undefined;
  requestAnalysis(eventId: string): Promise<AiAnalysis>;
}

export class DataError extends Error {
  constructor(
    public readonly operation: string,
    /** Why it failed, when known: unauthenticated | source_unavailable | network | bad_response. */
    public readonly reason?: string,
  ) {
    super(`Sports data request failed: ${operation}${reason ? ` (${reason})` : ''}`);
    this.name = 'DataError';
  }
}
