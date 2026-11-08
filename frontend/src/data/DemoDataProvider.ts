import type { Clock } from '../time/clock';
import { buildDemoEvents, TOURNAMENTS } from './fixtures';
import { ALL_TOURNAMENTS, filterEvents, pickFeatured, type EventQuery } from './filter';
import type { AiAnalysis, Sport, SportEvent, Tournament } from './models';
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

  coverage(): SportCoverage {
    return { status: 'demo', provider: null, attribution: null };
  }

  getTournament(id: string): Tournament | undefined {
    return TOURNAMENTS.find((t) => t.id === id);
  }

  listTournaments(sport: Sport): Tournament[] {
    return TOURNAMENTS.filter((t) => t.sport === sport);
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
