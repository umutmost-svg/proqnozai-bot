import type { Clock } from '../time/clock';
import { buildDemoEvents, TOURNAMENTS } from './fixtures';
import { ALL_TOURNAMENTS, filterEvents, namesMatch, pickTop, type EventQuery } from './filter';
import { dayKeyOf, type DayKey } from '../time/zoned';
import type { AiAnalysis, QuickAnswer, TopMatch, Competitor, SearchResults, Sport, SportEvent, TeamHit, TeamMatches, Tournament, TournamentCalendar, TournamentStandings } from './models';
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
  /** Analyses "created" in this session (the real backend keeps them in its cache and history). */
  private readonly created = new Map<string, AiAnalysis>();

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

  /** Illustrative demo table (marked as demo by the screen): football tournaments only. */
  getStandings(id: string): Promise<TournamentStandings | null> {
    return this.respond('events', () => {
      const tournament = this.getTournament(id);
      if (!tournament) return null;
      if (tournament.sport !== 'football') return { tournament, season: null, blocks: [], fetchedAt: null, stale: false };
      const teams = new Map<string, Competitor>();
      for (const e of this.events()) {
        if (e.tournamentId !== id) continue;
        for (const c of [e.home, e.away]) if (!teams.has(c.id)) teams.set(c.id, c);
      }
      const rows = [...teams.values()].map((team, i) => {
        const won = Math.max(0, 8 - i);
        const draw = Math.min(10 - won, 1 + (i % 3));
        const lost = 10 - won - draw;
        const [goalsFor, goalsAgainst] = [Math.max(4, 20 - i), 8 + i];
        return { position: i + 1, team, played: 10, won, draw, lost, goalsFor, goalsAgainst, goalDifference: goalsFor - goalsAgainst, points: won * 3 + draw };
      });
      return { tournament, season: null, blocks: [{ stage: 'REGULAR_SEASON', group: null, rows }], fetchedAt: null, stale: false };
    });
  }

  listEvents(query: EventQuery): Promise<SportEvent[]> {
    const op: DemoOperation = query.query.trim() ? 'search' : 'events';
    return this.respond(op, () => filterEvents(this.events(), TOURNAMENTS, query));
  }

  getTopMatches(query: EventQuery): Promise<TopMatch[]> {
    const order = TOURNAMENTS.filter((t) => t.sport === query.sport).map((t) => t.id);
    return this.respond('events', () =>
      pickTop(filterEvents(this.events(), TOURNAMENTS, { ...query, query: '' }), (id) => order.indexOf(id)).map((event) => ({ event, leaders: false })),
    );
  }

  /** The quick prediction needs the real goal model: the demo never invents its numbers. */
  quickAvailable(): boolean {
    return false;
  }

  quickPick(): Promise<QuickAnswer> {
    return Promise.reject(new DataError('quick', 'not_available'));
  }

  getEvent(id: string): Promise<SportEvent | null> {
    return this.respond('events', () => this.events().find((e) => e.id === id) ?? null, this.latencyMs / 2);
  }

  /** The demo keeps the real flow: nothing exists until "Create AI analysis" is tapped; then it is kept. */
  getCachedAnalysis(eventId: string): Promise<AiAnalysis | null> {
    return Promise.resolve(this.created.get(eventId) ?? null);
  }

  requestAnalysis(eventId: string): Promise<AiAnalysis> {
    return this.respond(
      'ai',
      () => {
        const made = this.make(eventId);
        this.created.set(eventId, made);
        return made;
      },
      this.aiLatencyMs,
    );
  }

  private make(eventId: string): AiAnalysis {
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
  }
}

export { ALL_TOURNAMENTS };
