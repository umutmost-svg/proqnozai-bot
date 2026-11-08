import type { Clock } from '../time/clock';
import { buildDemoEvents, TOURNAMENTS } from './fixtures';
import { ALL_TOURNAMENTS, filterEvents, namesMatch, pickFeatured, type EventQuery } from './filter';
import { dayKeyOf, type DayKey } from '../time/zoned';
import type { AiAnalysis, Competitor, SearchResults, Sport, SportEvent, TeamHit, TeamMatches, Tournament, TournamentCalendar } from './models';
import { DataError, type SportCoverage, type SportsDataProvider } from './provider';

export type DemoOperation = 'events' | 'search' | 'ai';

export interface DemoOptions {
  /** Artificial latency so loading states are visible. */
  latencyMs?: number;
  /** AI "analysis" takes longer to show the waiting screen. */
  aiLatencyMs?: number;
  /** Operations that fail exactly once, to demonstrate error + retry. */
  failOnce?: Iterable<DemoOperation>;
}

/** Offline provider: no tokens, no backend, no paid APIs. */
export class DemoDataProvider implements SportsDataProvider {
  readonly isDemo = true;
  private readonly failOnce: Set<DemoOperation>;
  private readonly latencyMs: number;
  private readonly aiLatencyMs: number;

  constructor(
    private readonly clock: Clock,
    options: DemoOptions = {},
  ) {
    this.latencyMs = options.latencyMs ?? 350;
    this.aiLatencyMs = options.aiLatencyMs ?? 1800;
    this.failOnce = new Set(options.failOnce ?? []);
  }

  private events(): SportEvent[] {
    return buildDemoEvents(this.clock.now());
  }

  private async respond<T>(op: DemoOperation, value: () => T, latency = this.latencyMs): Promise<T> {
    if (latency > 0) await new Promise((r) => setTimeout(r, latency));
    if (this.failOnce.delete(op)) throw new DataError(op);
    return value();
  }

  aiAvailable(): boolean {
    return true;
  }

  sportAvailable(): boolean {
    return true;
  }

  dateWindow(): null {
    return null;
  }

  knownMatchDays(sport: Sport, timeZone: string): ReadonlyMap<DayKey, number> {
    const days = new Map<DayKey, number>();
    for (const e of this.events()) {
      if (e.sport !== sport) continue;
      const day = dayKeyOf(new Date(e.startsAt), timeZone);
      days.set(day, (days.get(day) ?? 0) + 1);
    }
    return days;
  }

  private teams(sport: Sport): TeamHit[] {
    const byId = new Map<string, { team: Competitor; tournaments: Set<string> }>();
    for (const e of this.events()) {
      if (e.sport !== sport) continue;
      for (const c of [e.home, e.away]) {
        const known = byId.get(c.id) ?? { team: c, tournaments: new Set<string>() };
        known.tournaments.add(e.tournamentId);
        byId.set(c.id, known);
      }
    }
    return [...byId.values()].map((x) => ({ team: x.team, tournamentIds: [...x.tournaments] }));
  }

  private ordered(events: SportEvent[]): SportEvent[] {
    const ahead = events.filter((e) => e.status === 'live' || e.status === 'upcoming');
    ahead.sort((a, b) => Number(b.status === 'live') - Number(a.status === 'live') || a.startsAt.localeCompare(b.startsAt));
    const rest = events.filter((e) => e.status !== 'live' && e.status !== 'upcoming');
    rest.sort((a, b) => b.startsAt.localeCompare(a.startsAt));
    return [...ahead, ...rest];
  }

  search(sport: Sport, query: string): Promise<SearchResults> {
    return this.respond('search', () => {
      const teams = this.teams(sport).filter((h) => namesMatch(query, [h.team.name, h.team.fullName]));
      const ids = new Set(teams.map((h) => h.team.id));
      const events = this.ordered(this.events().filter((e) => e.sport === sport && (ids.has(e.home.id) || ids.has(e.away.id))));
      return { teams, events, coverage: { indexed: 1, total: 1, complete: true } };
    });
  }

  getTeamMatches(teamId: string): Promise<TeamMatches | null> {
    return this.respond('events', () => {
      const all = this.events();
      const event = all.find((e) => e.home.id === teamId || e.away.id === teamId);
      if (!event) return null;
      const team = this.teams(event.sport).find((h) => h.team.id === teamId)!;
      const events = this.ordered(all.filter((e) => e.home.id === teamId || e.away.id === teamId));
      return { team, events, coverage: { indexed: 1, total: 1, complete: true } };
    });
  }

  coverage(): SportCoverage {
    return { status: 'demo', provider: null, attribution: null };
  }

  getTournament(id: string): Tournament | undefined {
    return TOURNAMENTS.find((t) => t.id === id);
  }

  listTournaments(sport: Sport): Tournament[] {
    return TOURNAMENTS.filter((t) => t.sport === sport);
  }

  loadTournaments(sport: Sport): Promise<Tournament[]> {
    // Static demo catalogue: no latency and no demo failure (that is reserved for the match list).
    return Promise.resolve(this.listTournaments(sport));
  }

  getTournamentCalendar(id: string): Promise<TournamentCalendar | null> {
    return this.respond('events', () => {
      const tournament = this.getTournament(id);
      if (!tournament) return null;
      const events = this.events()
        .filter((e) => e.tournamentId === id)
        .sort((a, b) => a.startsAt.localeCompare(b.startsAt) || a.id.localeCompare(b.id));
      return { tournament, events, season: null };
    });
  }

  listEvents(query: EventQuery): Promise<SportEvent[]> {
    const op: DemoOperation = query.query.trim() ? 'search' : 'events';
    return this.respond(op, () => filterEvents(this.events(), TOURNAMENTS, query));
  }

  getFeatured(query: EventQuery): Promise<SportEvent | null> {
    return this.respond('events', () =>
      pickFeatured(filterEvents(this.events(), TOURNAMENTS, { ...query, query: '' })),
    );
  }

  getEvent(id: string): Promise<SportEvent | null> {
    return this.respond('events', () => this.events().find((e) => e.id === id) ?? null, this.latencyMs / 2);
  }

  requestAnalysis(eventId: string): Promise<AiAnalysis> {
    return this.respond(
      'ai',
      () => {
        const event = this.events().find((e) => e.id === eventId);
        if (!event) throw new DataError('ai');
        const ai = event.ai;
        return {
          eventId,
          sport: event.sport,
          kind: ai.kind,
          probabilities: ai.kind === 'ready' ? ai.probabilities : [],
          reasons: ai.kind === 'insufficient' ? ai.reasons : [],
          generatedAt: this.clock.now().toISOString(),
        };
      },
      this.aiLatencyMs,
    );
  }
}

export { ALL_TOURNAMENTS };
