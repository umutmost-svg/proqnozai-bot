import type { AiAnalysis, SportEvent, Tournament, TournamentCalendar, Sport } from './models';
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
  /** Asks for the sport's tournaments on its own (not only as a side effect of a match listing). */
  loadTournaments(sport: Sport): Promise<Tournament[]>;
  listEvents(query: EventQuery): Promise<SportEvent[]>;
  getFeatured(query: EventQuery): Promise<SportEvent | null>;
  getEvent(id: string): Promise<SportEvent | null>;
  getTournament(id: string): Tournament | undefined;
  /** The tournament's season schedule; null when it is not a tournament we serve. */
  getTournamentCalendar(id: string): Promise<TournamentCalendar | null>;
  /** `lang`: language of the written analysis (the interface language). */
  requestAnalysis(eventId: string): Promise<AiAnalysis>;
  /** The AI analysis can be requested for real events (always true for the demo). */
  aiAvailable(): boolean;
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
