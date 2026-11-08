import { addDays, zonedTimeToInstant, type DayKey } from '../time/zoned';
import { ALL_TOURNAMENTS, filterEvents, pickFeatured, type EventQuery } from './filter';
import { TOURNAMENTS as DEMO_TOURNAMENTS } from './fixtures';
import type {
  AiAnalysis,
  AiFactor,
  BasketballEvent,
  InsufficientReason,
  TeamBasis,
  Competitor,
  EventSource,
  EventStatus,
  FootballEvent,
  ImageAsset,
  MmaEvent,
  MmaMethod,
  Pair,
  Sport,
  SportEvent,
  TennisEvent,
  Tournament,
  TournamentCalendar,
  AiPick,
  PickMarket,
  PickReason,
  SearchResults,
  SearchCoverage,
  TeamHit,
  TeamMatches,
} from './models';
import { pickProbability } from './probabilities';
import { DataError, type SportCoverage, type SportsDataProvider } from './provider';

/** One API exchange may not hang the app (the loading screen waits on first data). */
export const DATA_TIMEOUT_MS = 10_000;
/**
 * The AI request waits for the backend's whole bounded path: evidence ≤ 20 s, quota
 * reservation ≤ ~10 s, one model attempt ≤ 60 s — plus headroom.
 */
export const AI_TIMEOUT_MS = 120_000;

const STATUSES: ReadonlySet<EventStatus> = new Set([
  'upcoming',
  'live',
  'finished',
  'postponed',
  'suspended',
  'cancelled',
  'unknown',
]);

// Licensed local assets already shipped with M1; any other real tournament shows its name only.
const demoLogo = (id: string): ImageAsset | undefined => DEMO_TOURNAMENTS.find((t) => t.id === id)?.logo;
/** Sports with a real source in this release; the others show "coming soon" and call nothing. */
const REAL_SPORTS: ReadonlySet<Sport> = new Set(['football']);

const LOCAL_LOGOS: Record<string, ImageAsset | undefined> = {
  'fd:PL': demoLogo('premier'),
  'fd:PD': demoLogo('laliga'),
};

/** Attribution required by a provider's terms (football-data.org terms, §7). */
const REQUIRED_ATTRIBUTION: Record<string, string> = {
  'football-data.org': 'Football data provided by the Football-Data.org API',
};

export interface BackendOptions {
  /** Resolves once the Telegram sign-in is settled: true = there is a session. */
  waitForSession(): Promise<boolean>;
  /** The session's anti-CSRF token, sent with the (paid) AI request. */
  getCsrfToken?(): string | null;
  fetchImpl?: typeof fetch;
  base?: string;
  timeoutMs?: number;
  aiTimeoutMs?: number;
}

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null);
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null);

function validInstant(v: unknown): string | null {
  const s = str(v);
  return s && !Number.isNaN(Date.parse(s)) ? s : null;
}

function pair(v: unknown): Pair | undefined {
  if (!Array.isArray(v) || v.length !== 2) return undefined;
  const [a, b] = [int(v[0]), int(v[1])];
  return a === null || b === null ? undefined : [a, b];
}

function pairs(v: unknown): Pair[] {
  return Array.isArray(v) ? v.map(pair).filter((p): p is Pair => p !== undefined) : [];
}

// Pictures only from our own backend (never a third-party URL): /api/crests/<kind>/<id>.
const CREST_PATH = /^\/api\/crests\/(team|competition)\/[A-Za-z0-9]{1,12}$/;

/** A backend-served crest or emblem; proportions unknown, so fitted with object-fit. */
export function remoteImage(v: unknown): ImageAsset | undefined {
  const path = str(v);
  return path && CREST_PATH.test(path) ? { src: path, width: 1, height: 1, remote: true } : undefined;
}

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Season metadata: every field present, each a valid value or null; undefined = broken. */
function readSeason(v: unknown): TournamentCalendar['season'] | undefined {
  if (!isObj(v) || !('startDate' in v) || !('endDate' in v) || !('currentMatchday' in v)) return undefined;
  const day = (x: unknown) => (x === null ? null : typeof x === 'string' && DAY.test(x) ? x : undefined);
  const startDate = day(v.startDate);
  const endDate = day(v.endDate);
  const round = v.currentMatchday;
  const currentMatchday = round === null ? null : int(round) !== null && (round as number) > 0 ? (round as number) : undefined;
  if (startDate === undefined || endDate === undefined || currentMatchday === undefined) return undefined;
  return { startDate, endDate, currentMatchday };
}

function readTeamHit(v: unknown): TeamHit | null {
  const team = competitor(v);
  if (!team || !isObj(v) || !Array.isArray(v.tournaments) || v.tournaments.some((x) => typeof x !== 'string')) return null;
  return { team, tournamentIds: v.tournaments as string[] };
}

function competitor(v: unknown): Competitor | null {
  if (!isObj(v)) return null;
  const id = str(v.id);
  const full = str(v.name)?.trim();
  if (!id || !full) return null;
  // The short familiar name is displayed; the official one stays for search and labels.
  const short = str(v.shortName)?.trim();
  const emblem = remoteImage(v.crest);
  return {
    id,
    name: short ?? full,
    ...(short && short !== full ? { fullName: full } : {}),
    ...(emblem ? { emblem } : {}),
  };
}

/** Real-data provider: everything comes from the Mini App backend; never falls back to demo. */
export class BackendSportsDataProvider implements SportsDataProvider {
  readonly isDemo = false;
  private readonly tournaments = new Map<string, Tournament>();
  private readonly coverages = new Map<Sport, SportCoverage>();
  private aiStatus: 'connected' | 'not_connected' | 'unknown' = 'unknown';
  private readonly knownDays = new Map<string, Map<DayKey, number>>();
  private readonly fetchImpl: typeof fetch;
  private readonly base: string;
  private readonly timeoutMs: number;
  private readonly aiTimeoutMs: number;

  constructor(private readonly options: BackendOptions) {
    this.fetchImpl = options.fetchImpl ?? ((...a) => fetch(...a));
    this.base = options.base ?? '/api';
    this.timeoutMs = options.timeoutMs ?? DATA_TIMEOUT_MS;
    this.aiTimeoutMs = options.aiTimeoutMs ?? AI_TIMEOUT_MS;
  }

  coverage(sport: Sport): SportCoverage {
    return this.coverages.get(sport) ?? { status: 'unknown', provider: null, attribution: null };
  }

  getTournament(id: string): Tournament | undefined {
    return this.tournaments.get(id);
  }

  listTournaments(sport: Sport): Tournament[] {
    return [...this.tournaments.values()].filter((t) => t.sport === sport);
  }

  async listEvents(query: EventQuery): Promise<SportEvent[]> {
    const from = zonedTimeToInstant(query.day, '00:00', query.timeZone).toISOString();
    const to = zonedTimeToInstant(addDays(query.day, 1), '00:00', query.timeZone).toISOString();
    const params = new URLSearchParams({ from, to });
    if (query.tournamentId !== ALL_TOURNAMENTS) params.set('tournament', query.tournamentId);
    const op = query.query.trim() ? 'search' : 'events';
    const body = await this.get(op, `/sports/${query.sport}/events?${params.toString()}`);
    // A malformed answer is an error: it must never read as "no matches today".
    if (!body || !Array.isArray(body.events) || !isObj(body.coverage)) throw new DataError(op, 'bad_response');
    // The tournament catalogue rides along: all valid or the answer is broken (never a partial list).
    if (!Array.isArray(body.tournaments)) throw new DataError(op, 'bad_response');
    const catalogue = body.tournaments.map((raw) => this.parseTournament(raw));
    if (catalogue.some((t) => !t || t.sport !== query.sport)) throw new DataError(op, 'bad_response');
    const coverage = this.readCoverage(query.sport, body.coverage);
    this.readAiStatus(body.ai);
    for (const t of catalogue as Tournament[]) this.tournaments.set(t.id, t);
    // Staleness is per event (a two-day range may mix a fresh day and a cached one).
    const parsed = body.events.map((raw) => this.readEvent(raw, coverage));
    // Like the backend: one broken event makes the answer unusable — a partial schedule
    // would silently hide a match.
    // An empty day served from stale cache during an outage cannot confirm "no matches".
    if (body.stale === true && body.events.length === 0) throw new DataError(op, 'source_unavailable');
    // An event of another sport is equally a broken answer, never a silent omission.
    if (parsed.some((e) => e === null || e.sport !== query.sport)) throw new DataError(op, 'bad_response');
    const events = parsed as SportEvent[];
    // Incomplete coverage must not drop the footer attribution the provider's terms require.
    const sourceProvider = events.find((e) => e.source)?.source?.provider;
    if (!coverage.attribution && sourceProvider && REQUIRED_ATTRIBUTION[sourceProvider]) {
      this.coverages.set(query.sport, {
        ...this.coverage(query.sport),
        provider: coverage.provider ?? sourceProvider,
        attribution: REQUIRED_ATTRIBUTION[sourceProvider],
      });
    }
    // Same filters as the demo: calendar day in the chosen zone, tournament, search, order.
    const filtered = filterEvents(events, [...this.tournaments.values()], query);
    // A complete day (all tournaments, no search) is a fact for the calendar's dots.
    if (query.tournamentId === ALL_TOURNAMENTS && !query.query.trim() && body.stale !== true) {
      const key = `${query.sport}|${query.timeZone}`;
      const days = this.knownDays.get(key) ?? new Map<DayKey, number>();
      days.set(query.day, filtered.length);
      this.knownDays.set(key, days);
    }
    return filtered;
  }

  async getFeatured(query: EventQuery): Promise<SportEvent | null> {
    return pickFeatured(await this.listEvents({ ...query, query: '' }));
  }

  async loadTournaments(sport: Sport): Promise<Tournament[]> {
    const op = 'tournaments';
    const body = await this.get(op, `/sports/${sport}/tournaments`);
    if (!body || !Array.isArray(body.tournaments) || !isObj(body.coverage)) throw new DataError(op, 'bad_response');
    const list = body.tournaments.map((raw) => this.parseTournament(raw));
    // All or nothing: a partial catalogue must never look like the whole one.
    if (list.some((t) => !t || t.sport !== sport)) throw new DataError(op, 'bad_response');
    this.readCoverage(sport, body.coverage);
    for (const t of list as Tournament[]) this.tournaments.set(t.id, t);
    return list as Tournament[];
  }

  dateWindow(): { past: number; future: number } {
    // The backend's window (miniapp/sports/service.py: PAST_WINDOW, FUTURE_WINDOW), one day
    // inside it on each side so a local day never reaches past the server's instant bounds.
    return { past: 29, future: 59 };
  }

  sportAvailable(sport: Sport): boolean {
    return REAL_SPORTS.has(sport);
  }

  knownMatchDays(sport: Sport, timeZone: string): ReadonlyMap<DayKey, number> {
    return this.knownDays.get(`${sport}|${timeZone}`) ?? new Map();
  }

  async search(sport: Sport, query: string): Promise<SearchResults> {
    const op = 'search';
    if (!this.sportAvailable(sport)) return { teams: [], events: [], coverage: { indexed: 0, total: 0, complete: true } };
    const body = await this.get(op, `/sports/${sport}/search?${new URLSearchParams({ q: query }).toString()}`);
    if (!body || !Array.isArray(body.teams) || !Array.isArray(body.events)) throw new DataError(op, 'bad_response');
    const coverage = this.readSearchCoverage(op, body.coverage);
    this.readCatalogue(op, sport, body.tournaments);
    const source = this.readCoverage(sport, body.source);
    const teams = body.teams.map((raw) => readTeamHit(raw));
    const events = body.events.map((raw) => this.readEvent(raw, source));
    // One broken entry makes the answer unusable: a partial list must not look complete.
    if (teams.some((t) => !t) || events.some((e) => !e || e.sport !== sport)) throw new DataError(op, 'bad_response');
    return { teams: teams as TeamHit[], events: events as SportEvent[], coverage };
  }

  async getTeamMatches(teamId: string): Promise<TeamMatches | null> {
    const op = 'team';
    const body = await this.get(op, `/sports/football/teams/${encodeURIComponent(teamId)}/matches`, true);
    if (body === null) return null;
    if (!Array.isArray(body.events)) throw new DataError(op, 'bad_response');
    const coverage = this.readSearchCoverage(op, body.coverage);
    this.readCatalogue(op, 'football', body.tournaments);
    const source = this.readCoverage('football', body.source);
    const team = readTeamHit(body.team);
    const events = body.events.map((raw) => this.readEvent(raw, source));
    if (!team || team.team.id !== teamId || events.some((e) => !e || (e.home.id !== teamId && e.away.id !== teamId))) {
      throw new DataError(op, 'bad_response');
    }
    return { team, events: events as SportEvent[], coverage };
  }

  private readSearchCoverage(op: string, v: unknown): SearchCoverage {
    if (!isObj(v) || !Array.isArray(v.indexed) || int(v.total) === null || typeof v.complete !== 'boolean') {
      throw new DataError(op, 'bad_response');
    }
    const indexed = v.indexed.length;
    if (indexed > (v.total as number) || v.complete !== (indexed === v.total)) throw new DataError(op, 'bad_response');
    return { indexed, total: v.total as number, complete: v.complete };
  }

  /** A catalogue riding along: stored only when every entry is valid. */
  private readCatalogue(op: string, sport: Sport, raw: unknown): void {
    if (!Array.isArray(raw)) throw new DataError(op, 'bad_response');
    const list = raw.map((x) => this.parseTournament(x));
    if (list.some((t) => !t || t.sport !== sport)) throw new DataError(op, 'bad_response');
    for (const t of list as Tournament[]) this.tournaments.set(t.id, t);
  }

  async getTournamentCalendar(id: string): Promise<TournamentCalendar | null> {
    // Real calendars exist for football only; a deep link may arrive before the catalogue.
    const sport: Sport = this.tournaments.get(id)?.sport ?? 'football';
    const op = 'calendar';
    const body = await this.get(op, `/sports/${sport}/tournaments/${encodeURIComponent(id)}/calendar`, true);
    if (body === null) return null;
    if (!Array.isArray(body.events) || !isObj(body.coverage) || body.sport !== sport) {
      throw new DataError(op, 'bad_response');
    }
    const season = readSeason(body.season);
    const tournament = this.readTournament(body.tournament);
    if (!tournament || tournament.id !== id || tournament.sport !== sport || season === undefined) {
      throw new DataError(op, 'bad_response');
    }
    const coverage = this.readCoverage(sport, body.coverage);
    this.readAiStatus(body.ai);
    // Like a day listing: one broken match makes the calendar unusable (a gap would read as "no match").
    const parsed = body.events.map((raw) => this.readEvent(raw, coverage));
    if (body.stale === true && body.events.length === 0) throw new DataError(op, 'source_unavailable');
    if (parsed.some((e) => e === null || e.sport !== sport || e.tournamentId !== id)) {
      throw new DataError(op, 'bad_response');
    }
    const events = (parsed as SportEvent[]).sort(
      (a, b) => Date.parse(a.startsAt) - Date.parse(b.startsAt) || a.id.localeCompare(b.id),
    );
    return { tournament, events, season };
  }

  async getEvent(id: string): Promise<SportEvent | null> {
    const body = await this.get('event', `/sports/event/${encodeURIComponent(id)}`, true);
    if (body === null) return null;
    if (!isObj(body.event)) throw new DataError('event', 'bad_response');
    if (body.tournament) this.readTournament(body.tournament);
    const sport = body.event.sport as Sport;
    const coverage = this.readCoverage(sport, body.coverage);
    this.readAiStatus(body.ai);
    const event = this.readEvent(body.event, coverage);
    // A malformed real event, or a different event than asked for, is an error —
    // never "no such event" and never another match under this id.
    if (!event || event.id !== id) throw new DataError('event', 'bad_response');
    return event;
  }

  /** True once the backend has said its AI is connected (from the last events/event answer). */
  aiAvailable(): boolean {
    return this.aiStatus === 'connected';
  }

  /**
   * Real AI analysis from the backend (verified data → PARTIAL / INSUFFICIENT).
   * Without a connected AI the answer is "unavailable" — never a demo example.
   */
  async requestAnalysis(eventId: string): Promise<AiAnalysis> {
    const event = await this.getEvent(eventId);
    if (!event) throw new DataError('ai', 'not_found');
    if (!this.aiAvailable()) {
      return { eventId, sport: event.sport, kind: 'unavailable', probabilities: [], reasons: [], generatedAt: null };
    }
    const csrf = this.options.getCsrfToken?.() ?? null;
    if (!csrf) throw new DataError('ai', 'unauthenticated');
    const body = await this.request(
      'ai',
      '/ai/analysis',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ event_id: eventId }),
      },
      true,
      this.aiTimeoutMs,
    );
    if (!body) throw new DataError('ai', 'not_found');
    const analysis = readAnalysis(body, eventId, event.sport);
    if (!analysis) throw new DataError('ai', 'bad_response');
    return analysis;
  }

  // --- transport ------------------------------------------------------------------

  /** GET a backend JSON body; `nullOn404` turns "no such event" into null. */
  private get(op: string, path: string, nullOn404 = false): Promise<Json | null> {
    return this.request(op, path, { method: 'GET' }, nullOn404, this.timeoutMs);
  }

  private async request(
    op: string,
    path: string,
    init: RequestInit,
    nullOn404: boolean,
    timeoutMs: number,
  ): Promise<Json | null> {
    if (!(await this.options.waitForSession())) throw new DataError(op, 'unauthenticated');
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timedOut = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        controller?.abort();
        reject(new DataError(op, 'network'));
      }, timeoutMs);
    });
    try {
      return await Promise.race([this.exchange(op, path, init, nullOn404, controller?.signal), timedOut]);
    } finally {
      clearTimeout(timer);
    }
  }

  private async exchange(
    op: string,
    path: string,
    init: RequestInit,
    nullOn404: boolean,
    signal?: AbortSignal,
  ): Promise<Json | null> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.base}${path}`, {
        ...init,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json', ...(init.headers as Record<string, string> | undefined) },
        signal,
      });
    } catch {
      throw new DataError(op, 'network');
    }
    if (response.status === 404 && nullOn404) return null;
    if (response.status === 401) throw new DataError(op, 'unauthenticated');
    if (response.status === 429) throw new DataError(op, 'limit_reached');
    if (response.status === 503) throw new DataError(op, await aiReason(response));
    if (!response.ok) throw new DataError(op, 'bad_response');
    try {
      const body: unknown = await response.json();
      if (!isObj(body)) throw new Error('not an object');
      return body;
    } catch {
      throw new DataError(op, 'bad_response');
    }
  }

  // --- parsing --------------------------------------------------------------------

  private readAiStatus(raw: unknown): void {
    const status = isObj(raw) ? raw.status : null;
    if (status === 'connected' || status === 'not_connected') this.aiStatus = status;
  }

  private readCoverage(sport: Sport, raw: unknown): SportCoverage & { delayed: boolean } {
    const c = isObj(raw) ? raw : {};
    const status = c.status === 'connected' ? 'connected' : c.status === 'not_connected' ? 'not_connected' : 'unknown';
    const provider = str(c.provider);
    // Attribution the provider's terms require is shown even if the backend omits it.
    const attribution = str(c.attribution) ?? (provider ? (REQUIRED_ATTRIBUTION[provider] ?? null) : null);
    const coverage = { status, provider, attribution } as const;
    this.coverages.set(sport, coverage);
    return { ...coverage, delayed: c.liveScores === 'delayed' };
  }

  private readTournament(raw: unknown): Tournament | undefined {
    const tournament = this.parseTournament(raw);
    if (tournament) this.tournaments.set(tournament.id, tournament);
    return tournament;
  }

  /** Parsed without storing: a list is stored only once every entry is valid. */
  private parseTournament(raw: unknown): Tournament | undefined {
    if (!isObj(raw)) return;
    const id = str(raw.id);
    const name = str(raw.name);
    const sport = raw.sport as Sport;
    if (!id || !name || !['football', 'tennis', 'basketball', 'mma'].includes(sport)) return;
    // A licensed local logo wins; otherwise the provider's emblem as served by our backend.
    // An explicit emblem field is the current truth (null = withdrawn); without the field, keep what we had.
    const logo = LOCAL_LOGOS[id] ?? ('emblem' in raw ? remoteImage(raw.emblem) : this.tournaments.get(id)?.logo);
    const area = str(raw.area) ?? undefined;
    return { id, sport, name, ...(area ? { area } : {}), ...(logo ? { logo } : {}) };
  }

  private readEvent(raw: unknown, coverage: SportCoverage & { delayed: boolean }): SportEvent | null {
    if (!isObj(raw)) return null;
    const id = str(raw.id);
    const tournamentId = str(raw.tournamentId);
    const startsAt = str(raw.startsAt);
    const home = competitor(raw.home);
    const away = competitor(raw.away);
    if (!id || !tournamentId || !startsAt || Number.isNaN(Date.parse(startsAt)) || !home || !away) return null;
    const status: EventStatus = STATUSES.has(raw.status as EventStatus) ? (raw.status as EventStatus) : 'unknown';
    const src = isObj(raw.source) ? raw.source : {};
    const provider = str(src.provider);
    const fetchedAt = str(src.fetchedAt);
    // Real data must say where it came from and when; otherwise it is not shown.
    if (!provider || !fetchedAt || Number.isNaN(Date.parse(fetchedAt))) return null;
    const source: EventSource = {
      provider,
      fetchedAt,
      // Optional, but only a parseable instant (an invalid one would crash date formatting).
      sourceUpdatedAt: validInstant(src.sourceUpdatedAt),
      stale: src.stale === true,
      // Required attribution follows the event's own provider even if coverage is missing.
      attribution: coverage.attribution ?? REQUIRED_ATTRIBUTION[provider] ?? null,
      delayed: coverage.delayed,
    };
    // Real events carry no AI result until the real analysis exists.
    const base = { id, tournamentId, startsAt, status, home, away, ai: { kind: 'unavailable' } as const, source };
    switch (raw.sport) {
      case 'football': {
        const minute = int(raw.minute);
        const event: FootballEvent = {
          ...base,
          sport: 'football',
          score: pair(raw.score),
          halfTime: pair(raw.halfTime),
          minute: minute ?? undefined,
          awarded: raw.awarded === true,
          ...(int(raw.matchday) ? { matchday: int(raw.matchday) as number } : {}),
        };
        return event;
      }
      case 'tennis': {
        const event: TennisEvent = {
          ...base,
          sport: 'tennis',
          format: raw.format === 'singles' || raw.format === 'doubles' ? raw.format : undefined,
          surface: raw.surface === 'hard' || raw.surface === 'clay' || raw.surface === 'grass' ? raw.surface : undefined,
          bestOf: raw.bestOf === 3 || raw.bestOf === 5 ? raw.bestOf : undefined,
          sets: pairs(raw.sets),
          currentGame: str(raw.currentGame) ?? undefined,
        };
        return event;
      }
      case 'basketball': {
        const event: BasketballEvent = {
          ...base,
          sport: 'basketball',
          quarters: pairs(raw.quarters),
          overtimes: pairs(raw.overtimes),
          clock: str(raw.clock) ?? undefined,
        };
        return event;
      }
      case 'mma': {
        const r = isObj(raw.result) ? raw.result : null;
        const methods: readonly MmaMethod[] = ['ko_tko', 'submission', 'decision', 'draw'];
        const method = r && methods.includes(r.method as MmaMethod) ? (r.method as MmaMethod) : null;
        const event: MmaEvent = {
          ...base,
          sport: 'mma',
          weightClass: str(raw.weightClass) ?? undefined,
          scheduledRounds: raw.scheduledRounds === 3 || raw.scheduledRounds === 5 ? raw.scheduledRounds : undefined,
          currentRound: int(raw.currentRound) ?? undefined,
          result:
            r && method && int(r.round) !== null && str(r.time)
              ? {
                  winner: r.winner === 'home' || r.winner === 'away' ? r.winner : null,
                  method,
                  round: int(r.round)!,
                  time: str(r.time)!,
                }
              : undefined,
        };
        return event;
      }
      default:
        return null;
    }
  }
}

/** Why the AI is unavailable, when the backend says so (no free-form text reaches the UI). */
const AI_REASONS = new Set([
  'not_configured',
  'limit_reached',
  'budget_exhausted',
  'refused',
  'bad_output',
  'upstream_error',
  'timeout',
  'rate_limited',
]);

async function aiReason(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (isObj(body) && body.error === 'ai_unavailable' && typeof body.reason === 'string' && AI_REASONS.has(body.reason)) {
      return body.reason;
    }
  } catch {
    // not JSON: the generic reason below
  }
  return 'source_unavailable';
}

const FACTORS: Record<string, AiFactor> = {
  home_form: 'homeForm',
  away_form: 'awayForm',
  home_goals: 'homeGoals',
  away_goals: 'awayGoals',
  home_table: 'homeTable',
  away_table: 'awayTable',
};

const REASONS: Record<string, InsufficientReason> = {
  no_verified_form: 'noVerifiedForm',
  not_upcoming: 'notUpcoming',
  no_odds_source: 'noOddsSource',
};

function readBasis(v: unknown): TeamBasis | null {
  if (!isObj(v)) return null;
  const name = str(v.name);
  const form = typeof v.form === 'string' && /^[WDL]{0,10}$/.test(v.form) ? v.form : null;
  const [matches, points, goalsFor, goalsAgainst] = [int(v.matches), int(v.points), int(v.goalsFor), int(v.goalsAgainst)];
  if (!name || form === null || matches === null || points === null || goalsFor === null || goalsAgainst === null) {
    return null;
  }
  if (form.length !== matches) return null;
  // Optional table numbers: absent/null is fine, anything else must be a valid number.
  const optional = (x: unknown): number | null | undefined => (x === null || x === undefined ? null : (int(x) ?? undefined));
  const tablePosition = optional(v.tablePosition);
  const tablePoints = optional(v.tablePoints);
  if (tablePosition === undefined || tablePoints === undefined) return null;
  // The counts must agree with the form string (they are rendered as words).
  const [wins, draws, losses] = [int(v.wins), int(v.draws), int(v.losses)];
  const count = (c: string) => form.split('').filter((x) => x === c).length;
  if (wins !== count('W') || draws !== count('D') || losses !== count('L')) return null;
  if (points !== wins * 3 + draws) return null;
  return { name, form, matches, wins, draws, losses, points, goalsFor, goalsAgainst, tablePosition, tablePoints };
}

/** Strict reading of the backend analysis; anything off-contract is rejected (null), never repaired. */
const MARKETS: ReadonlySet<PickMarket> = new Set(['1', 'X', '2', '1X', 'X2', '12', 'NO_BET']);
const PICK_REASONS: Record<string, PickReason> = {
  home_better_form: 'homeBetterForm',
  away_better_form: 'awayBetterForm',
  home_higher_table: 'homeHigherTable',
  away_higher_table: 'awayHigherTable',
  home_more_goals: 'homeMoreGoals',
  away_more_goals: 'awayMoreGoals',
  home_fewer_conceded: 'homeFewerConceded',
  away_fewer_conceded: 'awayFewerConceded',
  evenly_matched: 'evenlyMatched',
};

/**
 * The preferred outcome, checked again here: its probability must be the sum the 1X2
 * estimate gives for that market (computed, never trusted), NO_BET carries nothing.
 */
function readPick(v: unknown, [home, draw, away]: number[]): AiPick | null {
  if (!isObj(v) || typeof v.market !== 'string' || !MARKETS.has(v.market as PickMarket) || !Array.isArray(v.reasons)) {
    return null;
  }
  const market = v.market as PickMarket;
  const reasons = v.reasons.map((r) => (typeof r === 'string' ? PICK_REASONS[r] : undefined));
  if (reasons.some((r) => r === undefined) || new Set(reasons).size !== reasons.length || reasons.length > 3) return null;
  const expected = pickProbability(market, home!, draw!, away!);
  if (market === 'NO_BET') return v.probability === null && reasons.length === 0 ? { market, reasons: [], probability: null } : null;
  if (reasons.length === 0 || v.probability !== expected) return null;
  return { market, reasons: reasons as PickReason[], probability: expected };
}

function readAnalysis(b: Json, eventId: string, sport: Sport): AiAnalysis | null {
  if (b.eventId !== eventId || !Array.isArray(b.reasons)) return null;
  const reasons = b.reasons.map((r) => (typeof r === 'string' ? REASONS[r] : undefined));
  if (reasons.some((r) => r === undefined)) return null;
  const known = reasons as InsufficientReason[];
  let basis: { home: TeamBasis; away: TeamBasis } | undefined;
  if (b.basis !== null && b.basis !== undefined) {
    const home = isObj(b.basis) ? readBasis(b.basis.home) : null;
    const away = isObj(b.basis) ? readBasis(b.basis.away) : null;
    if (!home || !away) return null;
    basis = { home, away };
  }
  if ('summary' in b) return null; // the model writes no free text; any text here is off-contract
  if (b.kind === 'insufficient') {
    if ((b.probabilities !== null && b.probabilities !== undefined) || (Array.isArray(b.factors) && b.factors.length)) {
      return null;
    }
    return { eventId, sport, kind: 'insufficient', probabilities: [], reasons: known, generatedAt: null, basis };
  }
  if ((b.kind !== 'partial' && b.kind !== 'ready') || !basis) return null;
  const p = isObj(b.probabilities) ? b.probabilities : {};
  const probabilities = [int(p.home), int(p.draw), int(p.away)];
  if (probabilities.some((n) => n === null || n > 100)) return null;
  if ((probabilities as number[]).reduce((a, n) => a + n, 0) !== 100) return null;
  const factors = Array.isArray(b.factors) ? b.factors.map((f) => (typeof f === 'string' ? FACTORS[f] : undefined)) : [];
  const generatedAt = validInstant(b.generatedAt);
  if (factors.length < 2 || factors.length > 4 || factors.some((f) => f === undefined) || !generatedAt) return null;
  if (new Set(factors).size !== factors.length) return null;
  // A table factor is rendered from that team's table row: it must exist.
  const tableOf = { homeTable: basis.home, awayTable: basis.away } as Record<string, TeamBasis>;
  if (factors.some((f) => tableOf[f!] && (tableOf[f!]!.tablePosition === null || tableOf[f!]!.tablePoints === null))) {
    return null;
  }
  // The v2 answer always carries the preferred outcome (NO_BET included): a missing one is a
  // broken answer, never a page that silently lacks the block.
  const pick = readPick(b.pick, probabilities as number[]);
  if (!pick) return null;
  return {
    eventId,
    sport,
    kind: b.kind,
    pick,
    probabilities: probabilities as number[],
    reasons: known,
    generatedAt,
    factors: factors as AiFactor[],
    basis,
    // PARTIAL is always an estimate, whatever the flag says.
    estimated: b.kind === 'partial' || b.estimated === true,
  };
}
