import type { AiAnalysis, SportEvent, Tournament, Sport } from './models';
import type { EventQuery } from './filter';

/**
 * Data boundary of the Mini App. M1 ships only the demo implementation;
 * M3 adds a backend-backed one behind the same interface.
 */
export interface SportsDataProvider {
  readonly isDemo: boolean;
  /** Tournament catalogue is small and static per release, so it is synchronous. */
  listTournaments(sport: Sport): Tournament[];
  listEvents(query: EventQuery): Promise<SportEvent[]>;
  getFeatured(query: EventQuery): Promise<SportEvent | null>;
  getEvent(id: string): Promise<SportEvent | null>;
  getTournament(id: string): Tournament | undefined;
  requestAnalysis(eventId: string): Promise<AiAnalysis>;
}

export class DataError extends Error {
  constructor(public readonly operation: string) {
    super(`Demo data request failed: ${operation}`);
    this.name = 'DataError';
  }
}
