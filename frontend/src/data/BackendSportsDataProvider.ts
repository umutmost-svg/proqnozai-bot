import { addDays, dayKeyOf, zonedTimeToInstant, type DayKey } from '../time/zoned';
import { ALL_TOURNAMENTS, filterEvents, type EventQuery } from './filter';
import { MODEL_CONFIRMED, quickRange } from './quick';
import { TOURNAMENTS as DEMO_TOURNAMENTS } from './fixtures';
import type { BookQuote, EventOdds,
  AiAnalysis,
  MarketPrices,
  PriceDeadline,
  MarketQuote,
  AiFactor,
  QuickAnswer,
  QuickCriteria,
  QuickLeg,
  QuickPreview,
  QuickReason,
  TopMatch,
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
  MarketChance,
  MarketFamily,
  MarketKey,
  ModelForecast,
  NoBetReason,
  StandingBlock,
  StandingRow,
  TournamentStandings,
} from './models';
import { MARKET_KEYS, QUICK_REASONS } from './models';
import { FAMILIES, FAMILY, isMarketKey, marketsConsistent } from './markets';
import { pickProbability } from './probabilities';
import { DataError, type SportCoverage, type SportsDataProvider } from './provider';

/** One API exchange may not hang the app (the loading screen waits on first data). */
export const DATA_TIMEOUT_MS = 10_000;
/**
 * The AI request waits for the backend's whole bounded path: evidence ≤ 20 s, quota
 * reservation ≤ ~10 s, one model attempt ≤ 60 s — plus headroom.
 */
export const AI_TIMEOUT_MS = 120_000;
/** Matches the backend's own table cache order of magnitude; tab switches never refetch. */
export const STANDINGS_CACHE_MS = 5 * 60_000;
/** Reading an AI analysis: the backend may gather its free evidence for up to 20 s first. */
export const AI_READ_TIMEOUT_MS = 30_000;
/** Quick prediction: up to ~10 provider requests (≤ 10 s each at worst) plus the model. */
export const QUICK_TIMEOUT_MS = 60_000;

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
  // Optional (older backends omit it); present but unreadable makes the season unreadable, like any field.
  if (!('pendingFixtures' in v)) return { startDate, endDate, currentMatchday };
  const pending = int(v.pendingFixtures);
  if (pending === null) return undefined;
  return { startDate, endDate, currentMatchday, pendingFixtures: pending };
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
  private readonly standingsCache = new Map<string, { at: number; promise: Promise<TournamentStandings | null> }>();
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

  async getTopMatches(query: EventQuery): Promise<TopMatch[]> {
    if (!REAL_SPORTS.has(query.sport)) return [];
    const from = zonedTimeToInstant(query.day, '00:00', query.timeZone).toISOString();
    const to = zonedTimeToInstant(addDays(query.day, 1), '00:00', query.timeZone).toISOString();
    const op = 'top';
    const params = new URLSearchParams({ from, to });
    if (query.tournamentId !== ALL_TOURNAMENTS) params.set('tournament', query.tournamentId);
    const body = await this.get(op, `/sports/football/top?${params.toString()}`);
    if (!body || !Array.isArray(body.top) || !isObj(body.coverage) || !Array.isArray(body.tournaments)) {
      throw new DataError(op, 'bad_response');
    }
    const catalogue = body.tournaments.map((raw) => this.parseTournament(raw));
    if (catalogue.some((t) => !t || t.sport !== 'football')) throw new DataError(op, 'bad_response');
    for (const t of catalogue as Tournament[]) this.tournaments.set(t.id, t);
    const coverage = this.readCoverage('football', body.coverage);
    this.readAiStatus(body.ai);
    // As for a listing: an empty day served from stale cache cannot confirm "no matches".
    if (body.stale === true && body.top.length === 0) throw new DataError(op, 'source_unavailable');
    const top = body.top.map((raw) => {
      if (!isObj(raw) || typeof raw.leaders !== 'boolean') return null;
      const event = this.readEvent(raw.event, coverage);
      // Postponed, suspended, cancelled or unknown matches are never top matches.
      if (event && !TOP_STATUSES.has(event.status)) return null;
      // The day check needs a real instant: a zone-less time would be read in the device's zone.
      if (event && !ZONED_INSTANT.test(event.startsAt)) return null;
      // Only the chosen local day, as the backend was asked (never another date's match).
      if (!event || event.sport !== 'football' || dayKeyOf(new Date(event.startsAt), query.timeZone) !== query.day) return null;
      if (query.tournamentId !== ALL_TOURNAMENTS && event.tournamentId !== query.tournamentId) return null;
      return { event, leaders: raw.leaders };
    });
    if (top.some((x) => x === null) || top.length > 5) throw new DataError(op, 'bad_response');
    const ids = new Set(top.map((x) => x!.event.id));
    if (ids.size !== top.length) throw new DataError(op, 'bad_response');
    return top as TopMatch[];
  }

  quickAvailable(): boolean {
    return true;
  }

  async quickPick(criteria: QuickCriteria, now: Date, timeZone: string): Promise<QuickAnswer> {
    const { from, to } = quickRange(criteria.period, now, timeZone);
    const params = new URLSearchParams({
      mode: criteria.mode,
      from: from.toISOString(),
      to: to.toISOString(),
      market: criteria.market,
      risk: criteria.risk,
    });
    if (criteria.mode === 'coupon') params.set('size', String(criteria.size));
    if (criteria.tournaments.length) params.set('tournaments', criteria.tournaments.join(','));
    const op = 'quick';
    // The backend's work is bounded (a few provider requests, each with its own timeout, then
    // the model): a longer wait than an ordinary listing, never an endless one.
    const sent = clocksNow();
    const body = await this.request(op, `/quick/pick?${params.toString()}`, { method: 'GET' }, false, QUICK_TIMEOUT_MS);
    const coverage = this.coverage('football');
    const answer = body ? readQuick(body, criteria, { from, to }, (raw) => this.readEvent(raw, { ...coverage, delayed: false }), sent) : null;
    if (!answer) throw new DataError(op, 'bad_response');
    return answer;
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

  /**
   * The provider's current tables. Shared per tournament for STANDINGS_CACHE_MS and
   * de-duplicated while in flight: switching tabs or coming back never asks again.
   */
  getStandings(id: string): Promise<TournamentStandings | null> {
    const now = Date.now();
    const cached = this.standingsCache.get(id);
    if (cached && now - cached.at < STANDINGS_CACHE_MS) return cached.promise;
    const promise = this.loadStandings(id);
    this.standingsCache.set(id, { at: now, promise });
    // A failure is not remembered: "try again" asks again.
    promise.catch(() => {
      if (this.standingsCache.get(id)?.promise === promise) this.standingsCache.delete(id);
    });
    return promise;
  }

  private async loadStandings(id: string): Promise<TournamentStandings | null> {
    const sport: Sport = this.tournaments.get(id)?.sport ?? 'football';
    const op = 'standings';
    const body = await this.get(op, `/sports/${sport}/tournaments/${encodeURIComponent(id)}/standings`, true);
    if (body === null) return null;
    if (body.sport !== sport || !Array.isArray(body.blocks) || !isObj(body.coverage)) throw new DataError(op, 'bad_response');
    const tournament = this.readTournament(body.tournament);
    const season = readSeason(body.season);
    if (!tournament || tournament.id !== id || season === undefined) throw new DataError(op, 'bad_response');
    this.readCoverage(sport, body.coverage);
    const blocks = body.blocks.map(readStandingBlock);
    // One unreadable row would shift every position below it: the whole table is unusable.
    if (blocks.some((b) => b === null)) throw new DataError(op, 'bad_response');
    if (body.stale === true && blocks.length === 0) throw new DataError(op, 'source_unavailable');
    return { tournament, season, blocks: blocks as StandingBlock[], fetchedAt: validInstant(body.fetchedAt), stale: body.stale === true };
  }

  /** Odds of the last answer per match (bounded): read with the event, shown only while valid. */
  private odds = new Map<string, EventOdds | null>();

  eventOdds(id: string): EventOdds | null {
    return this.odds.get(id) ?? null;
  }

  async getEvent(id: string): Promise<SportEvent | null> {
    const sent = clocksNow();
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
    // Odds are an extra: a malformed block is dropped (never shown), the match page still opens.
    if (this.odds.size > 200) this.odds.clear();
    this.odds.set(id, readEventOdds(body.odds, sent));
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
    const sent = clocksNow();
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
    const analysis = readAnalysis(body, eventId, event.sport, sent);
    if (!analysis) throw new DataError('ai', 'bad_response');
    return analysis;
  }

  async getCachedAnalysis(eventId: string): Promise<AiAnalysis | null> {
    const event = await this.getEvent(eventId);
    if (!event) throw new DataError('ai', 'not_found');
    // Read-only on the backend: no limit reserved, no model called; 404 = not created yet. Asked
    // even when the AI is not connected: an analysis made earlier is still shown then.
    // Longer than the backend's free evidence deadline (20 s): a cold but successful read is never cut.
    const sent = clocksNow();
    const body = await this.request('ai', `/ai/analysis?${new URLSearchParams({ event_id: eventId }).toString()}`, { method: 'GET' }, true, AI_READ_TIMEOUT_MS);
    if (!body) {
      return this.aiAvailable() ? null : { eventId, sport: event.sport, kind: 'unavailable', probabilities: [], reasons: [], generatedAt: null };
    }
    const analysis = readAnalysis(body, eventId, event.sport, sent);
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
    if (response.status === 429) throw new DataError(op, 'limit_reached', retryAfterHeader(response));
    if (response.status === 503) {
      const { reason, retryAfter } = await unavailable(response);
      throw new DataError(op, reason, retryAfter);
    }
    // A date range the server refused (e.g. a device clock far from the server's) is its own case.
    if (response.status === 400 && (await errorCode(response)) === 'bad_range') throw new DataError(op, 'bad_range');
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
    return { id, sport, name, ...(area ? { area } : {}), ...(logo ? { logo } : {}), ...(raw.featured === true ? { featured: true } : {}) };
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

async function errorCode(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json();
    return isObj(body) && typeof body.error === 'string' ? body.error : null;
  } catch {
    return null;
  }
}

/** Longest wait accepted from the server (as the backend caps it); anything else is ignored. */
const MAX_RETRY_AFTER_S = 3600;

function retrySeconds(v: unknown): number | undefined {
  const n = typeof v === 'string' && /^\d{1,6}$/.test(v.trim()) ? Number(v.trim()) : v;
  return typeof n === 'number' && Number.isInteger(n) && n >= 1 && n <= MAX_RETRY_AFTER_S ? n : undefined;
}

function retryAfterHeader(response: Response): number | undefined {
  return retrySeconds(response.headers?.get?.('Retry-After') ?? undefined);
}

/**
 * A 503: the AI's own reasons, otherwise "source_unavailable". A wait is attached only when the
 * data source is rate limited (the body's retryAfter, else the Retry-After header).
 */
async function unavailable(response: Response): Promise<{ reason: string; retryAfter?: number }> {
  try {
    const body: unknown = await response.json();
    if (isObj(body) && body.error === 'ai_unavailable' && typeof body.reason === 'string' && AI_REASONS.has(body.reason)) {
      return { reason: body.reason };
    }
    if (isObj(body) && body.error === 'source_unavailable' && body.reason === 'rate_limited') {
      const retryAfter = retrySeconds(body.retryAfter) ?? retryAfterHeader(response);
      return retryAfter === undefined ? { reason: 'source_unavailable' } : { reason: 'source_unavailable', retryAfter };
    }
  } catch {
    // not JSON: the generic reason below
  }
  return { reason: 'source_unavailable' };
}

const FACTORS: Record<string, AiFactor> = {
  home_form: 'homeForm',
  away_form: 'awayForm',
  home_goals: 'homeGoals',
  away_goals: 'awayGoals',
  home_table: 'homeTable',
  away_table: 'awayTable',
  home_rating: 'homeRating',
  away_rating: 'awayRating',
  home_advantage: 'homeAdvantage',
};

const REASONS: Record<string, InsufficientReason> = {
  no_verified_form: 'noVerifiedForm',
  not_upcoming: 'notUpcoming',
  no_odds_source: 'noOddsSource',
  model_small_sample: 'modelSmallSample',
  model_team_history: 'modelTeamHistory',
  model_extreme_rates: 'modelExtremeRates',
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
  // Table points may be negative after a deduction (the table itself accepts them).
  const tablePoints =
    v.tablePoints === null || v.tablePoints === undefined
      ? null
      : typeof v.tablePoints === 'number' && Number.isInteger(v.tablePoints) ? v.tablePoints : undefined;
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
const V3_ONLY_FACTORS: ReadonlySet<string> = new Set(['home_rating', 'away_rating', 'home_advantage']);
const V3_ONLY_REASONS: ReadonlySet<string> = new Set(['home_stronger_rating', 'away_stronger_rating', 'home_advantage']);
const NO_BET_REASONS: Record<string, NoBetReason> = {
  limited_competition: 'limitedCompetition',
  small_sample: 'smallSample',
  no_qualifying_market: 'noQualifyingMarket',
  partial_history: 'partialHistory',
  unvalidated_competition: 'unvalidatedCompetition',
  early_forecast: 'earlyForecast',
};
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
  home_stronger_rating: 'homeStrongerRating',
  away_stronger_rating: 'awayStrongerRating',
  home_advantage: 'homeAdvantage',
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
  // v2 reasons only: the model-figure reasons exist only with a v3 forecast to render them from.
  const reasons = v.reasons.map((r) => (typeof r === 'string' && !V3_ONLY_REASONS.has(r) ? PICK_REASONS[r] : undefined));
  if (reasons.some((r) => r === undefined) || new Set(reasons).size !== reasons.length || reasons.length > 3) return null;
  const expected = pickProbability(market, home!, draw!, away!);
  if (market === 'NO_BET') return v.probability === null && reasons.length === 0 ? { market, reasons: [], probability: null } : null;
  if (reasons.length === 0 || v.probability !== expected) return null;
  return { market, reasons: reasons as PickReason[], probability: expected };
}

function readAnalysis(b: Json, eventId: string, sport: Sport, sent: PriceDeadline = clocksNow()): AiAnalysis | null {
  if (b.eventId !== eventId || !Array.isArray(b.reasons)) return null;
  // v3 (M3.5) answers carry the goal model; answers without a schema are the older v2 shape.
  if (b.schema === 'v3') return readAnalysisV3(b, eventId, sport, sent);
  if (b.schema !== undefined) return null;
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
  const factors = Array.isArray(b.factors)
    ? b.factors.map((f) => (typeof f === 'string' && !V3_ONLY_FACTORS.has(f) ? FACTORS[f] : undefined))
    : [];
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

const pos = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) && v > 0 && v < 100 ? v : null;
const nonNegative = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) && v >= 0 && v < 20 ? v : null);

function readRating(v: unknown): { attack: number; defence: number } | null {
  if (!isObj(v)) return null;
  const attack = pos(v.attack);
  const defence = pos(v.defence);
  return attack !== null && defence !== null ? { attack, defence } : null;
}

/** The goal model's figures; any off-contract value rejects the whole analysis. */
function readForecast(v: unknown): ModelForecast | null {
  if (!isObj(v) || !str(v.version) || (v.kind !== 'poisson' && v.kind !== 'dixon_coles') || typeof v.limited !== 'boolean'
    || typeof v.experimental !== 'boolean') {
    return null;
  }
  const computedAt = validInstant(v.computedAt);
  const dataUntil = validInstant(v.dataUntil);
  const sample = int(v.sample);
  const tm = isObj(v.teamMatches) ? v.teamMatches : {};
  const xg = isObj(v.expectedGoals) ? v.expectedGoals : {};
  const lg = isObj(v.leagueGoals) ? v.leagueGoals : {};
  const [homeMatches, awayMatches] = [int(tm.home), int(tm.away)];
  const [xgHome, xgAway, lgHome, lgAway] = [nonNegative(xg.home), nonNegative(xg.away), nonNegative(lg.home), nonNegative(lg.away)];
  if (!computedAt || !dataUntil || sample === null || homeMatches === null || awayMatches === null) return null;
  if (xgHome === null || xgAway === null || lgHome === null || lgAway === null) return null;
  if (!isObj(v.markets) || Object.keys(v.markets).length !== MARKET_KEYS.length) return null;
  const markets = {} as Record<MarketKey, number>;
  for (const key of MARKET_KEYS) {
    const value = int(v.markets[key]);
    if (value === null) return null;
    markets[key] = value;
  }
  if (!marketsConsistent(markets)) return null;
  if (!Array.isArray(v.unprovenFamilies) || v.unprovenFamilies.some((f) => !(FAMILIES as readonly unknown[]).includes(f))) {
    return null;
  }
  if (!Array.isArray(v.topScores) || v.topScores.length > 3) return null;
  const topScores = v.topScores.map((x) => {
    if (!isObj(x)) return null;
    const [home, away, probability] = [int(x.home), int(x.away), int(x.probability)];
    return home !== null && away !== null && probability !== null && probability >= 1 && probability <= 100
      ? { home, away, probability }
      : null;
  });
  if (topScores.some((x) => x === null)) return null;
  const ratings = isObj(v.ratings) ? { home: readRating(v.ratings.home), away: readRating(v.ratings.away) } : null;
  const homeAdvantage = pos(v.homeAdvantage);
  if (!ratings?.home || !ratings.away || homeAdvantage === null) return null;
  let noBetReason: NoBetReason | null = null;
  if (v.noBetReason !== null) {
    noBetReason = typeof v.noBetReason === 'string' ? (NO_BET_REASONS[v.noBetReason] ?? null) : null;
    if (noBetReason === null) return null;
  }
  return {
    version: v.version as string,
    kind: v.kind,
    limited: v.limited,
    computedAt,
    dataUntil,
    sample,
    teamMatches: { home: homeMatches, away: awayMatches },
    expectedGoals: { home: xgHome, away: xgAway },
    markets,
    unprovenFamilies: v.unprovenFamilies as MarketFamily[],
    topScores: topScores as ModelForecast['topScores'],
    ratings: { home: ratings.home, away: ratings.away },
    homeAdvantage,
    leagueGoals: { home: lgHome, away: lgAway },
    noBetReason,
    experimental: v.experimental,
  };
}

const TABLE_FACTORS: Record<string, 'home' | 'away'> = { homeTable: 'home', awayTable: 'away' };

/**
 * v3: every number comes from the goal model and must agree with it — the 1X2 bars,
 * the recommended market's percent, the alternatives'. The AI only chose factors and
 * reasons; NO_BET carries nothing, and an unproven or limited market is never recommended.
 */
/** Markets the odds feed may price (backend: miniapp/odds/book.py). */
const PRICED_MARKETS: ReadonlySet<string> = new Set(['1', 'X', '2', '1X', 'X2', '12', 'O1.5', 'U1.5', 'O2.5', 'U2.5', 'O3.5', 'U3.5', 'BTTS_Y', 'BTTS_N']);

function finite(v: unknown, min: number, max: number): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max ? v : null;
}

/**
 * One price against the model, strictly: every figure must agree with the others as the
 * backend computes them (to its rounding) and the model's figure with the percent shown for
 * that market — a quote that does not add up is a broken answer, never shown.
 */
export function readQuote(v: unknown, shown: number | undefined): MarketQuote | null {
  if (!isObj(v) || !isMarketKey(v.market) || !PRICED_MARKETS.has(v.market) || shown === undefined) return null;
  const odds = finite(v.odds, 1.01, 100);
  const implied = finite(v.impliedProbability, 0, 100);
  const market = finite(v.marketProbability, 0, 100);
  const model = finite(v.modelProbability, 0, 100);
  const edge = finite(v.edge, -100, 100);
  const ev = finite(v.expectedValue, -100, 10_000);
  const margin = finite(v.margin, -1, 35);
  if (odds === null || implied === null || market === null || model === null || edge === null || ev === null || margin === null) return null;
  if (Math.abs(odds * 100 - Math.round(odds * 100)) > 1e-6) return null;  // the backend sends cents
  if (Math.abs(implied - 100 / odds) > 0.051) return null;
  if (Math.abs(model - shown) > SHOWN_TOLERANCE + 0.051) return null;
  if (Math.abs(edge - (model - market)) > 0.101) return null;
  if (Math.abs(ev - (model * odds - 100)) > 0.05 * odds + 0.051) return null;
  return { market: v.market, odds, impliedProbability: implied, marketProbability: market, modelProbability: model, edge, expectedValue: ev, margin };
}

/** The server's validity is at most 15 minutes (miniapp/odds/store.py MAX_AGE_S). */
const MAX_VALID_FOR_S = 15 * 60;

/** Both clocks now: taken when a request is sent. */
function clocksNow(): PriceDeadline {
  return { mono: performance.now(), wall: Date.now() };
}

/**
 * The deadline of prices the server called valid for `v` seconds, counted from when the request
 * was SENT (the server measured them before answering): transit time never extends them.
 */
function priceDeadline(v: unknown, sent: PriceDeadline): PriceDeadline | null {
  const seconds = int(v);
  if (seconds === null || seconds < 1 || seconds > MAX_VALID_FOR_S) return null;
  return { mono: sent.mono + seconds * 1000, wall: sent.wall + seconds * 1000 };
}

/** The match page's odds block; anything outside the backend's shape gives null (nothing shown). */
export function readEventOdds(v: unknown, sent: PriceDeadline): EventOdds | null {
  if (!isObj(v) || !Array.isArray(v.quotes) || v.quotes.length === 0 || v.quotes.length > PRICED_MARKETS.size) return null;
  const fetchedAt = str(v.fetchedAt);
  const expires = priceDeadline(v.validFor, sent);
  if (!fetchedAt || !ZONED_INSTANT.test(fetchedAt) || !expires) return null;
  const quotes: BookQuote[] = [];
  for (const q of v.quotes) {
    if (!isObj(q) || !isMarketKey(q.market) || !PRICED_MARKETS.has(q.market)) return null;
    const { odds, marketProbability, margin } = q;
    if (typeof odds !== 'number' || !(odds > 1 && odds < 1000)) return null;
    if (typeof marketProbability !== 'number' || !(marketProbability > 0 && marketProbability < 100)) return null;
    if (typeof margin !== 'number' || !(margin > -50 && margin < 100)) return null;
    quotes.push({ market: q.market, odds, marketProbability, margin });
  }
  if (new Set(quotes.map((q) => q.market)).size !== quotes.length) return null;
  return { fetchedAt, expires, quotes };
}

function readPrices(v: unknown, markets: Record<MarketKey, number>, sent: PriceDeadline): MarketPrices | null {
  if (!isObj(v) || !Array.isArray(v.quotes) || v.quotes.length === 0 || v.quotes.length > PRICED_MARKETS.size) return null;
  const fetchedAt = str(v.fetchedAt);
  const expires = priceDeadline(v.validFor, sent);
  if (!fetchedAt || !ZONED_INSTANT.test(fetchedAt) || !expires) return null;
  const quotes = v.quotes.map((q) => readQuote(q, isObj(q) && isMarketKey(q.market) ? markets[q.market] : undefined));
  if (quotes.some((q) => q === null)) return null;
  const ok = quotes as MarketQuote[];
  if (new Set(ok.map((q) => q.market)).size !== ok.length) return null;
  return { fetchedAt, expires, quotes: ok };
}

function readAnalysisV3(b: Json, eventId: string, sport: Sport, sent: PriceDeadline): AiAnalysis | null {
  const reasons = (b.reasons as unknown[]).map((r) => (typeof r === 'string' ? REASONS[r] : undefined));
  if (reasons.some((r) => r === undefined)) return null;
  const known = reasons as InsufficientReason[];
  let basis: { home: TeamBasis; away: TeamBasis } | undefined;
  if (b.basis !== null && b.basis !== undefined) {
    const home = isObj(b.basis) ? readBasis(b.basis.home) : null;
    const away = isObj(b.basis) ? readBasis(b.basis.away) : null;
    if (!home || !away) return null;
    basis = { home, away };
  }
  if ('summary' in b) return null;
  if (b.kind === 'insufficient') {
    if (b.probabilities !== null || b.forecast !== null || b.pick !== null) return null;
    if (!Array.isArray(b.alternatives) || b.alternatives.length) return null;
    return { eventId, sport, kind: 'insufficient', probabilities: [], reasons: known, generatedAt: null, basis };
  }
  if ((b.kind !== 'partial' && b.kind !== 'ready') || !basis) return null;
  const forecast = readForecast(b.forecast);
  const generatedAt = validInstant(b.generatedAt);
  if (!forecast || !generatedAt) return null;
  const m = forecast.markets;
  const p = isObj(b.probabilities) ? b.probabilities : {};
  if (p.home !== m['1'] || p.draw !== m.X || p.away !== m['2']) return null;
  const factors = Array.isArray(b.factors) ? b.factors.map((f) => (typeof f === 'string' ? FACTORS[f] : undefined)) : [];
  if (factors.length < 2 || factors.length > 4 || factors.some((f) => f === undefined)) return null;
  if (new Set(factors).size !== factors.length) return null;
  if (factors.some((f) => TABLE_FACTORS[f!] && basis![TABLE_FACTORS[f!]!].tablePosition === null)) return null;
  const pick = b.pick;
  if (!isObj(pick) || typeof pick.market !== 'string' || !Array.isArray(pick.reasons)) return null;
  const pickReasons = pick.reasons.map((r) => (typeof r === 'string' ? PICK_REASONS[r] : undefined));
  if (pickReasons.some((r) => r === undefined) || new Set(pickReasons).size !== pickReasons.length || pickReasons.length > 3) {
    return null;
  }
  if (!Array.isArray(b.alternatives) || b.alternatives.length > 3) return null;
  const alternatives: MarketChance[] = [];
  for (const a of b.alternatives) {
    if (!isObj(a) || !isMarketKey(a.market) || a.probability !== m[a.market]) return null;
    // Alternatives passed the same rules as the pick: never an unproven family or a limited competition.
    if (forecast.limited || forecast.unprovenFamilies.includes(FAMILY[a.market])) return null;
    alternatives.push({ market: a.market, probability: a.probability as number });
  }
  let chosen: AiPick;
  if (pick.market === 'NO_BET') {
    if (pick.probability !== null || pickReasons.length || alternatives.length || forecast.noBetReason === null) return null;
    chosen = { market: 'NO_BET', reasons: [], probability: null };
  } else {
    if (!isMarketKey(pick.market) || pick.probability !== m[pick.market] || forecast.noBetReason !== null) return null;
    if (forecast.limited || forecast.unprovenFamilies.includes(FAMILY[pick.market])) return null;
    const markets = [pick.market, ...alternatives.map((a) => a.market)];
    if (new Set(markets).size !== markets.length) return null;
    chosen = { market: pick.market, reasons: pickReasons as PickReason[], probability: m[pick.market] };
  }
  // Prices come only with a complete analysis (READY), and a READY analysis has them.
  let market: MarketPrices | undefined;
  if (b.market !== null && b.market !== undefined) {
    const prices = readPrices(b.market, m, sent);
    if (!prices) return null;
    market = prices;
  }
  if ((market !== undefined) !== (b.kind === 'ready') || (market && known.length)) return null;
  return {
    eventId,
    sport,
    kind: b.kind,
    ...(market ? { market } : {}),
    pick: chosen,
    probabilities: [m['1'], m.X, m['2']],
    reasons: known,
    generatedAt,
    factors: factors as AiFactor[],
    basis,
    estimated: b.kind === 'partial' || b.estimated === true,
    forecast,
    alternatives,
  };
}

function readStandingRow(v: unknown): StandingRow | null {
  if (!isObj(v)) return null;
  const team = competitor(v.team);
  const [position, played, won, draw, lost, goalsFor, goalsAgainst] = [
    int(v.position), int(v.played), int(v.won), int(v.draw), int(v.lost), int(v.goalsFor), int(v.goalsAgainst),
  ];
  // Points may be negative after a deduction: any whole number, as the provider gives it.
  const points = typeof v.points === 'number' && Number.isInteger(v.points) ? v.points : null;
  const goalDifference = typeof v.goalDifference === 'number' && Number.isInteger(v.goalDifference) ? v.goalDifference : null;
  if (!team || position === null || position < 1 || played === null || won === null || draw === null || lost === null) return null;
  if (goalsFor === null || goalsAgainst === null || points === null || goalDifference === null) return null;
  return { position, team, played, won, draw, lost, goalsFor, goalsAgainst, goalDifference, points };
}

function readStandingBlock(v: unknown): StandingBlock | null {
  if (!isObj(v) || !Array.isArray(v.rows)) return null;
  const stage = v.stage === null ? null : str(v.stage);
  const group = v.group === null ? null : str(v.group);
  if ((v.stage !== null && stage === null) || (v.group !== null && group === null)) return null;
  const rows = v.rows.map(readStandingRow);
  if (rows.some((r) => r === null)) return null;
  const teams = new Set((rows as StandingRow[]).map((r) => r.team.id));
  if (teams.size !== rows.length) return null;
  // The provider's order is kept as given: never re-sorted here.
  return { stage, group, rows: rows as StandingRow[] };
}

const TOP_STATUSES: ReadonlySet<EventStatus> = new Set(['upcoming', 'live', 'finished']);
const QUICK_STATUSES: ReadonlySet<QuickAnswer['status']> = new Set(['ok', 'no_bet', 'not_enough', 'unavailable']);
const REASON_SET: ReadonlySet<string> = new Set(QUICK_REASONS);
/** An ISO instant with an explicit zone (Z or ±hh:mm): never read in the device's own zone. */
const ZONED_RE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(:\d{2}(\.\d+)?)?(Z|([+-])(\d{2}):(\d{2}))$/;
/**
 * An ISO instant with an explicit zone (Z or ±hh:mm) and a real calendar date and time: Date
 * would silently turn "02-30" into 2 March, so the parsed instant must read back as written.
 */
const ZONED_INSTANT = {
  test(v: string): boolean {
    const m = ZONED_RE.exec(v);
    if (!m) return false;
    const ms = Date.parse(v);
    if (Number.isNaN(ms)) return false;
    const offset = m[4] === 'Z' ? 0 : (m[5] === '-' ? -1 : 1) * (Number(m[6]) * 60 + Number(m[7]));
    const seconds = m[2] ? Number(m[2].slice(1, 3)) : 0;
    if (seconds > 59) return false;
    return new Date(ms + offset * 60_000).toISOString().slice(0, 16) === m[1];
  },
};
/** Whole-percent floors of the risk levels (backend: 0.70 / 0.60 / P_MIN 0.55, rounded half up). */
const RISK_FLOOR: Record<QuickCriteria['risk'], number> = { conservative: 70, balanced: 60, elevated: 55 };
/** How far a shown whole percent may be from the exact probability (sums of rounded 1X2 values). */
const SHOWN_TOLERANCE = 1;
/** The backend's clock may be ahead of the device's by this much ("from" = its own now). */
const CLOCK_SLACK_MS = 3_600_000;

/**
 * Whether [low, high] can be the backend's Fréchet bounds of these legs, for ONE set of exact
 * probabilities. A shown percent is within SHOWN_TOLERANCE of its exact value (double chance
 * and handicaps are sums of rounded 1X2 percents, so they may be off by up to one point); the
 * backend rounds the bounds outward: high = ceil(min p), low = floor(max(0, Σp − 100(n − 1))).
 */
export function boundsFit(bounds: readonly [number, number], legs: readonly number[]): boolean {
  const [low, high] = bounds;
  const EPS = 1e-9;
  const n = legs.length;
  if (low > high || n === 0) return false;
  // high = ceil(min p): every p > high − 1 and at least one p ≤ high.
  const lo = legs.map((d) => Math.max(d - SHOWN_TOLERANCE, high - 1 + EPS));
  const hi = legs.map((d) => d + SHOWN_TOLERANCE - EPS);
  if (lo.some((x, i) => x > hi[i]!)) return false;
  const capped = legs.map((_, i) => (lo[i]! <= high ? Math.min(hi[i]!, high) : null));
  if (capped.every((x) => x === null)) return false;
  const sumLo = lo.reduce((a, x) => a + x, 0);
  // Largest sum: every leg at its top, except one held at ≤ high (the cheapest such cut).
  const cut = Math.min(...capped.map((c, i) => (c === null ? Infinity : hi[i]! - c)));
  const sumHi = hi.reduce((a, x) => a + x, 0) - cut;
  const floorLow = (sum: number) => Math.floor(Math.max(0, sum - 100 * (n - 1)) + EPS);
  return low >= floorLow(sumLo) && low <= floorLow(sumHi);
}

function goalsPair(v: unknown): { home: number; away: number } | null {
  if (!isObj(v) || typeof v.home !== 'number' || typeof v.away !== 'number') return null;
  if (!Number.isFinite(v.home) || !Number.isFinite(v.away) || v.home < 0 || v.away < 0) return null;
  return { home: v.home, away: v.away };
}

function percent(v: unknown): number | null {
  const n = int(v);
  return n !== null && n <= 100 ? n : null;
}

function readQuickLeg(v: unknown, readEvent: (raw: unknown) => SportEvent | null, sent: PriceDeadline): QuickLeg | null {
  if (!isObj(v) || !isMarketKey(v.market) || v.family !== FAMILY[v.market]) return null;
  const event = readEvent(v.event);
  const probability = percent(v.probability);
  const leagueRate = percent(v.leagueRate);
  const expectedGoals = goalsPair(v.expectedGoals);
  const teams = isObj(v.teamMatches) ? { home: int(v.teamMatches.home), away: int(v.teamMatches.away) } : null;
  const sample = int(v.sample);
  const dataUntil = str(v.dataUntil);
  if (!event || event.sport !== 'football' || event.status !== 'upcoming') return null;
  if (probability === null || leagueRate === null || !expectedGoals || !teams || teams.home === null || teams.away === null) return null;
  if (sample === null || !dataUntil || Number.isNaN(Date.parse(dataUntil))) return null;
  let price: QuickLeg['price'] = null;
  if (v.price !== null) {
    const quote = readQuote(v.price, probability);
    const fetchedAt = isObj(v.price) ? str(v.price.fetchedAt) : null;
    const expires = isObj(v.price) ? priceDeadline(v.price.validFor, sent) : null;
    if (!quote || quote.market !== v.market || !fetchedAt || !ZONED_INSTANT.test(fetchedAt) || !expires) return null;
    price = { ...quote, fetchedAt, expires };
  }
  return {
    price,
    event,
    market: v.market,
    family: v.family as QuickLeg['family'],
    probability,
    leagueRate,
    expectedGoals,
    sample,
    teamMatches: { home: teams.home, away: teams.away },
    dataUntil,
  };
}

/** The quick prediction, strictly: one broken leg or an inconsistent answer is a bad response. */
export function readQuick(
  b: Json,
  criteria: QuickCriteria,
  range: { from: Date; to: Date },
  readEvent: (raw: unknown) => SportEvent | null,
  sent: PriceDeadline = clocksNow(),
): QuickAnswer | null {
  const status = b.status as QuickAnswer['status'];
  if (b.mode !== criteria.mode || !QUICK_STATUSES.has(status) || !Array.isArray(b.legs)) return null;
  const size = int(b.size);
  const suitable = int(b.suitable);
  if (size !== (criteria.mode === 'single' ? 1 : criteria.size) || suitable === null) return null;
  if (!isObj(b.counts) || int(b.counts.matches) === null || int(b.counts.forecasts) === null) return null;
  if (!isObj(b.reasons) || typeof b.experimental !== 'boolean' || !Array.isArray(b.unavailableTournaments)) return null;
  // Until the model is confirmed on forecasts stored before kickoff, an unlabelled pick is a broken answer.
  if (!MODEL_CONFIRMED && !b.experimental) return null;
  const uncheckedDays = int(b.uncheckedDays);
  if (uncheckedDays === null) return null;
  // The next match the rules may pick later: a zoned instant after the pick's own UTC day.
  const nextMatchAt = b.nextMatchAt === null ? null : str(b.nextMatchAt);
  if (b.nextMatchAt !== null && (!nextMatchAt || !ZONED_INSTANT.test(nextMatchAt))) return null;
  const computedAt = str(b.computedAt);
  if (!computedAt || !ZONED_INSTANT.test(computedAt) || Number.isNaN(Date.parse(computedAt))) return null;
  const reasons: Partial<Record<QuickReason, number>> = {};
  for (const [k, n] of Object.entries(b.reasons)) {
    if (!REASON_SET.has(k) || int(n) === null) return null;
    reasons[k as QuickReason] = n as number;
  }
  const unavailable = b.unavailableTournaments.filter((x): x is string => typeof x === 'string');
  if (unavailable.length !== b.unavailableTournaments.length) return null;
  const legs = b.legs.map((raw) => readQuickLeg(raw, readEvent, sent));
  if (legs.some((l) => l === null)) return null;
  const ok = legs as QuickLeg[];
  const matches = b.counts.matches as number;
  const forecasts = b.counts.forecasts as number;
  // Counts must be consistent with each other and with the legs shown.
  if (forecasts > matches || ok.length > forecasts || ok.length > suitable || suitable > forecasts) return null;
  // The status must agree with the mode and the count: "no pick" means nothing suitable for a
  // single; "not enough" means fewer suitable matches than the coupon asked for.
  const want = criteria.mode === 'single' ? 1 : criteria.size;
  if (status === 'no_bet' && (criteria.mode !== 'single' || suitable !== 0)) return null;
  if (status === 'not_enough' && (criteria.mode !== 'coupon' || suitable >= want)) return null;
  if (status === 'unavailable' && suitable >= want) return null;
  // With data missing, "nothing qualifies" is not a conclusion the backend may draw (it says
  // "unavailable" then): such an answer is broken.
  const missing = (reasons.source_unavailable ?? 0) > 0 || unavailable.length > 0;
  if ((status === 'no_bet' || status === 'not_enough') && missing) return null;
  // Shape must match the status: a result has exactly the asked number of different matches.
  if (status === 'ok' ? ok.length !== size : ok.length !== 0) return null;
  if (new Set(ok.map((l) => l.event.id)).size !== ok.length) return null;
  const utcDay = (iso: string) => new Date(iso).toISOString().slice(0, 10);
  if (nextMatchAt && (Number.isNaN(Date.parse(nextMatchAt)) || utcDay(nextMatchAt) <= utcDay(computedAt))) return null;
  // ...and inside the period that was asked for.
  if (nextMatchAt && (Date.parse(nextMatchAt) < range.from.getTime() - CLOCK_SLACK_MS || Date.parse(nextMatchAt) >= range.to.getTime())) return null;
  const teams = ok.flatMap((l) => [l.event.home.id, l.event.away.id]);
  if (new Set(teams).size !== teams.length) return null;
  let combined: [number, number] | null = null;
  if (Array.isArray(b.combined)) {
    const [low, high] = b.combined as unknown[];
    const pair: [number, number] | null = b.combined.length === 2 && percent(low) !== null && percent(high) !== null ? [low as number, high as number] : null;
    // Bounds the legs shown cannot produce are a broken answer.
    if (!pair || !boundsFit(pair, ok.map((l) => l.probability))) return null;
    combined = pair;
  } else if (b.combined !== null) return null;
  if ((criteria.mode === 'coupon' && status === 'ok') !== (combined !== null)) return null;
  // The computed coupon price: only for a coupon whose every leg has a price, equal to their
  // product (each leg rounded to cents, so within rounding) and dated by the oldest of them.
  let couponOdds: QuickAnswer['couponOdds'] = null;
  if (b.couponOdds !== null) {
    const c = b.couponOdds;
    const fetchedAt = isObj(c) ? str(c.fetchedAt) : null;
    // Up to five legs of at most 100 each (backend: miniapp/odds/book.py MAX_ODDS).
    const odds = isObj(c) ? finite(c.odds, 1, 1e10) : null;
    const expires = isObj(c) ? priceDeadline(c.validFor, sent) : null;
    if (odds === null || !fetchedAt || !ZONED_INSTANT.test(fetchedAt) || !expires) return null;
    if (combined === null || ok.some((l) => l.price === null)) return null;
    // The backend rounds the product of the legs' (cent) prices to cents: within half a cent of
    // the exact product (Python and JS may round an exact half cent differently), nothing more.
    const product = ok.reduce((a, l) => a * l.price!.odds, 1);
    // Cents, with a tolerance that grows with the number (floating point at nine-digit prices).
    const cents = odds * 100;
    if (Math.abs(cents - Math.round(cents)) > Math.max(1e-6, Math.abs(cents) * 8 * Number.EPSILON)) return null;
    if (Math.abs(odds - product) > 0.005 + Math.abs(product) * 8 * Number.EPSILON) return null;
    const oldest = ok.map((l) => l.price!.fetchedAt).sort((x, y) => Date.parse(x) - Date.parse(y))[0];
    if (Date.parse(fetchedAt) !== Date.parse(oldest!)) return null;
    couponOdds = { odds, fetchedAt, expires };
  } else if (combined !== null && ok.length > 0 && ok.every((l) => l.price !== null)) {
    return null;  // every leg priced: the backend always computes the product
  }
  // Every leg must meet what was asked: period, competitions, market and risk.
  const floor = RISK_FLOOR[criteria.risk];
  for (const l of ok) {
    if (!ZONED_INSTANT.test(l.event.startsAt)) return null;
    const kickoff = Date.parse(l.event.startsAt);
    if (kickoff < range.from.getTime() - CLOCK_SLACK_MS || kickoff >= range.to.getTime()) return null;
    // Picks exist only for matches on the pick's own UTC day (the model's match-day rule).
    if (new Date(kickoff).toISOString().slice(0, 10) !== new Date(computedAt).toISOString().slice(0, 10)) return null;
    if (criteria.tournaments.length && !criteria.tournaments.includes(l.event.tournamentId)) return null;
    if (criteria.market !== 'any' && l.family !== criteria.market) return null;
    // The floor applies to the exact probability; the shown percent may be a point below it.
    if (l.probability < floor - SHOWN_TOLERANCE) return null;
  }
  // Preview: only without a pick, at most five different later matches, each in the asked competitions,
  // after the pick's own UTC day, with whole-percent 1/X/2 that add up (rounding: 99..101).
  const rawPreview = b.preview === undefined ? [] : b.preview;
  if (!Array.isArray(rawPreview) || rawPreview.length > 5 || (status === 'ok' && rawPreview.length > 0)) return null;
  const preview: QuickPreview[] = [];
  for (const raw of rawPreview) {
    if (!isObj(raw) || !isObj(raw.probabilities)) return null;
    const event = readEvent(raw.event);
    const p = raw.probabilities;
    const [home, draw, away] = [percent(p.home), percent(p.draw), percent(p.away)];
    const day = str(raw.recommendationDay);
    if (!event || home === null || draw === null || away === null || !day || !ZONED_INSTANT.test(event.startsAt)) return null;
    // Only upcoming football matches; the validated-competition choice is the backend's (its frozen rules).
    if (event.sport !== 'football' || event.status !== 'upcoming') return null;
    if (home + draw + away < 99 || home + draw + away > 101) return null;
    if (day !== utcDay(event.startsAt) || day <= utcDay(computedAt)) return null;
    if (criteria.tournaments.length && !criteria.tournaments.includes(event.tournamentId)) return null;
    preview.push({ event, probabilities: { home, draw, away }, recommendationDay: day });
  }
  if (new Set(preview.map((x) => x.event.id)).size !== preview.length) return null;
  return {
    mode: criteria.mode,
    status,
    legs: ok,
    size,
    suitable,
    combined,
    counts: { matches, forecasts },
    reasons,
    unavailableTournaments: unavailable,
    uncheckedDays,
    nextMatchAt,
    couponOdds,
    experimental: b.experimental,
    computedAt,
    preview,
  };
}
