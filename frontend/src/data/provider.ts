import type {
  AiAnalysis,
  QuickAnswer,
  QuickCriteria,
  TopMatch,
  SearchResults,
  SportEvent,
  TeamMatches,
  Tournament,
  TournamentCalendar,
  TournamentStandings,
  Sport,
} from './models';
import type { DayKey } from '../time/zoned';
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
  /** Up to five of the day's top matches (query.day in query.timeZone), best first. */
  getTopMatches(query: EventQuery): Promise<TopMatch[]>;
  /** The goal model's quick prediction (real data only; see quickAvailable). */
  quickPick(criteria: QuickCriteria, now: Date, timeZone: string): Promise<QuickAnswer>;
  quickAvailable(): boolean;
  getEvent(id: string): Promise<SportEvent | null>;
  getTournament(id: string): Tournament | undefined;
  /** The tournament's season schedule; null when it is not a tournament we serve. */
  getTournamentCalendar(id: string): Promise<TournamentCalendar | null>;
  /** The tournament's current table(s) in the source's order; null when it is not a tournament we serve. */
  getStandings(id: string): Promise<TournamentStandings | null>;
  /** `lang`: language of the written analysis (the interface language). */
  requestAnalysis(eventId: string): Promise<AiAnalysis>;
  /**
   * An analysis already made (or one that costs nothing), never a new paid one: null when it
   * has not been created yet. Opening the AI screen uses only this.
   */
  getCachedAnalysis(eventId: string): Promise<AiAnalysis | null>;
  /** The AI analysis can be requested for real events (always true for the demo). */
  aiAvailable(): boolean;
  /** The sport has a data source here (real mode: football only; the demo shows every sport). */
  sportAvailable(sport: Sport): boolean;
  /** Global search, independent of the catalogue's date and tournament filters. */
  search(sport: Sport, query: string): Promise<SearchResults>;
  /** Every indexed match of one team; null when the team is unknown. */
  getTeamMatches(teamId: string): Promise<TeamMatches | null>;
  /**
   * Days already known to have matches (from listings this session), for calendar dots.
   * Only days that were actually loaded appear: no dot never means "no matches".
   */
  knownMatchDays(sport: Sport, timeZone: string): ReadonlyMap<DayKey, number>;
  /** Days before/after today the source can answer for (null: any day). */
  dateWindow(): { past: number; future: number } | null;
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
