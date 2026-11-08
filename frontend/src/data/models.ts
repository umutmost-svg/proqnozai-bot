/**
 * Sport-specific event models. Each sport keeps its own scoring structure:
 * football — goals, tennis — sets/games, basketball — quarters (+ overtimes),
 * MMA — rounds and the method of finish.
 */

export type Sport = 'football' | 'tennis' | 'basketball' | 'mma';
export const SPORTS: readonly Sport[] = ['football', 'tennis', 'basketball', 'mma'];

/**
 * Real states are kept apart: a postponed, suspended or cancelled event is never
 * shown as an ordinary upcoming one; an unrecognised provider state is "unknown".
 */
export type EventStatus = 'upcoming' | 'live' | 'finished' | 'postponed' | 'suspended' | 'cancelled' | 'unknown';

/** Local image asset with its intrinsic size, so aspect ratio is never distorted. */
export interface ImageAsset {
  src: string;
  width: number;
  height: number;
  /** Figma renders some sourced logos mirrored vertically; keep that exact. */
  flipY?: boolean;
  /** Served by our backend with unknown proportions: fitted with object-fit, may fail to load. */
  remote?: boolean;
}

export interface Tournament {
  id: string;
  sport: Sport;
  name: string;
  /** Country or region, when the source gives one. */
  area?: string;
  /** A licensed local asset, or the provider's emblem served by our backend. */
  logo?: ImageAsset;
}

/** A tournament's season schedule: every known match, grouped into days by the screen. */
export interface TournamentCalendar {
  tournament: Tournament;
  events: SportEvent[];
  season: { startDate: string | null; endDate: string | null; currentMatchday: number | null } | null;
}

export interface Competitor {
  id: string;
  /** Display name: the short familiar form when the source gives one. */
  name: string;
  /** Official full name, when it differs from `name` (search, accessible labels). */
  fullName?: string;
  /** Club crest when we have the real asset; never substituted with emoji. */
  emblem?: ImageAsset;
  /** Larger "authentic" emblem used on hero cards, when it differs. */
  heroEmblem?: ImageAsset;
  /** ISO 3166 alpha-2, for individual athletes. */
  country?: string;
}

export type Pair = readonly [home: number, away: number];

export type AiDemo =
  | { kind: 'ready'; probabilities: readonly number[] }
  | { kind: 'insufficient'; reasons: readonly InsufficientReason[] }
  /** Real events: the AI analysis is not connected yet, nothing is shown instead. */
  | { kind: 'unavailable' };

export type InsufficientReason =
  | 'noFreshStats'
  | 'lineupUnconfirmed'
  | 'limitedCoverage'
  /** Real analysis: not enough verified recent results for both teams. */
  | 'noVerifiedForm'
  /** Real analysis: only upcoming matches are analysed. */
  | 'notUpcoming'
  /** Real analysis: no bookmaker odds source, so probabilities are an estimate. */
  | 'noOddsSource';

/** Verified numbers behind a real analysis (computed by the backend, not the model). */
export type AiFactor = 'homeForm' | 'awayForm' | 'homeGoals' | 'awayGoals' | 'homeTable' | 'awayTable';

export interface TeamBasis {
  name: string;
  matches: number;
  /** Newest first, W/D/L. */
  form: string;
  /** Counted from `form` (verified: they must agree). */
  wins: number;
  draws: number;
  losses: number;
  points: number;
  goalsFor: number;
  goalsAgainst: number;
  tablePosition: number | null;
  tablePoints: number | null;
}

/** Where a real event came from; absent for demo events. */
export interface EventSource {
  provider: string;
  /** When our backend received it (ISO-8601 UTC). */
  fetchedAt: string;
  /** The provider's own "last updated", if it gives one. */
  sourceUpdatedAt: string | null;
  /** Served from cache after a failed refresh. */
  stale: boolean;
  /** Attribution text the provider's terms require next to its data. */
  attribution: string | null;
  /** The provider's scores are delayed on the current plan. */
  delayed: boolean;
}

interface BaseEvent {
  id: string;
  tournamentId: string;
  /** ISO-8601 instant in UTC. */
  startsAt: string;
  status: EventStatus;
  home: Competitor;
  away: Competitor;
  featured?: boolean;
  ai: AiDemo;
  source?: EventSource;
}

export interface FootballEvent extends BaseEvent {
  sport: 'football';
  score?: Pair;
  minute?: number;
  halfTime?: Pair;
  /** Result set by the competition (e.g. a forfeit), not played out. */
  awarded?: boolean;
  /** Round of the competition, when the source gives one. */
  matchday?: number;
  stats?: { possession: Pair; shots: Pair; onTarget: Pair; corners: Pair };
}

export interface TennisEvent extends BaseEvent {
  sport: 'tennis';
  /** Unknown when the provider does not say; never guessed. */
  format?: 'singles' | 'doubles';
  surface?: 'hard' | 'clay' | 'grass';
  bestOf?: 3 | 5;
  /** Games per set, completed and in progress. */
  sets: readonly Pair[];
  /** Points in the current game, e.g. "40:30". */
  currentGame?: string;
  stats?: { aces: Pair; firstServePct: Pair; breakPoints: readonly [Pair, Pair] };
}

export interface BasketballEvent extends BaseEvent {
  sport: 'basketball';
  quarters: readonly Pair[];
  overtimes: readonly Pair[];
  clock?: string;
  stats?: { rebounds: Pair; assists: Pair; fgPct: Pair };
}

export type MmaMethod = 'ko_tko' | 'submission' | 'decision' | 'draw';

export interface MmaEvent extends BaseEvent {
  sport: 'mma';
  /** Unknown when the provider does not say; never guessed. */
  weightClass?: string;
  scheduledRounds?: 3 | 5;
  currentRound?: number;
  roundClock?: string;
  result?: { winner: 'home' | 'away' | null; method: MmaMethod; round: number; time: string };
  stats?: { sigStrikes: Pair; takedowns: readonly [Pair, Pair]; controlSec: Pair };
}

export type SportEvent = FootballEvent | TennisEvent | BasketballEvent | MmaEvent;

/** The model's preferred outcome (no odds source: never a value claim). */
export type PickMarket = '1' | 'X' | '2' | '1X' | 'X2' | '12' | 'NO_BET';
export type PickReason =
  | 'homeBetterForm'
  | 'awayBetterForm'
  | 'homeHigherTable'
  | 'awayHigherTable'
  | 'homeMoreGoals'
  | 'awayMoreGoals'
  | 'homeFewerConceded'
  | 'awayFewerConceded'
  | 'evenlyMatched';

export interface AiPick {
  market: PickMarket;
  reasons: readonly PickReason[];
  /** Percent computed from the 1X2 estimate; null for NO_BET. */
  probability: number | null;
}

/** Global search over the served competitions (independent of catalogue filters). */
export interface SearchCoverage {
  indexed: number;
  total: number;
  complete: boolean;
}

export interface TeamHit {
  team: Competitor;
  tournamentIds: readonly string[];
}

export interface SearchResults {
  teams: TeamHit[];
  events: SportEvent[];
  coverage: SearchCoverage;
}

export interface TeamMatches {
  team: TeamHit;
  events: SportEvent[];
  coverage: SearchCoverage;
}

export interface AiAnalysis {
  eventId: string;
  sport: Sport;
  /** partial: verified data without odds — probabilities are an estimate. */
  kind: 'ready' | 'partial' | 'insufficient' | 'unavailable';
  probabilities: readonly number[];
  reasons: readonly InsufficientReason[];
  /** When the demo snapshot was produced; null when nothing was produced. */
  generatedAt: string | null;
  /** Real analysis only. */
  /** What weighed most (model's choice); each is rendered from that team's basis numbers. */
  factors?: readonly AiFactor[];
  basis?: { home: TeamBasis; away: TeamBasis };
  /** Probabilities are an estimate (PARTIAL): labelled as such in the UI. */
  estimated?: boolean;
  /** Preferred outcome; absent in analyses made before it existed. */
  pick?: AiPick;
}
