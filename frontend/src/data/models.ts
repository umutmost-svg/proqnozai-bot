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
  /** One of the main competitions: offered as a quick filter (the others are in the full list). */
  featured?: boolean;
}

/** A tournament's season schedule: every known match, grouped into days by the screen. */
export interface TournamentCalendar {
  tournament: Tournament;
  events: SportEvent[];
  season: { startDate: string | null; endDate: string | null; currentMatchday: number | null; pendingFixtures?: number } | null;
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
  | 'noOddsSource'
  /** Real analysis: the goal model has too few results of this competition. */
  | 'modelSmallSample'
  /** Real analysis: one of the teams has too few results for the goal model. */
  | 'modelTeamHistory'
  /** Real analysis: the model's expected goals fell outside the supported range. */
  | 'modelExtremeRates';

/** Verified numbers behind a real analysis (computed by the backend, not the model). */
export type AiFactor =
  | 'homeForm'
  | 'awayForm'
  | 'homeGoals'
  | 'awayGoals'
  | 'homeTable'
  | 'awayTable'
  /** Goal-model figures (rendered from the forecast, never from model text). */
  | 'homeRating'
  | 'awayRating'
  | 'homeAdvantage';

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

/** Markets of the goal model (miniapp/forecast/markets.py keys), all from one score distribution. */
export const MARKET_KEYS = [
  '1', 'X', '2', '1X', 'X2', '12',
  'O1.5', 'U1.5', 'O2.5', 'U2.5', 'O3.5', 'U3.5',
  'BTTS_Y', 'BTTS_N',
  'H1-0.5', 'H1-1.5', 'H2+0.5', 'H2+1.5',
  'HO0.5', 'HU0.5', 'HO1.5', 'HU1.5', 'AO0.5', 'AU0.5', 'AO1.5', 'AU1.5',
] as const;
export type MarketKey = (typeof MARKET_KEYS)[number];
export type MarketFamily = '1x2' | 'double' | 'total' | 'btts' | 'handicap' | 'team_total';

/** The recommended market (no odds source: never a value claim) or NO_BET. */
export type PickMarket = MarketKey | 'NO_BET';
export type PickReason =
  | 'homeBetterForm'
  | 'awayBetterForm'
  | 'homeHigherTable'
  | 'awayHigherTable'
  | 'homeMoreGoals'
  | 'awayMoreGoals'
  | 'homeFewerConceded'
  | 'awayFewerConceded'
  | 'evenlyMatched'
  | 'homeStrongerRating'
  | 'awayStrongerRating'
  | 'homeAdvantage';

export interface AiPick {
  market: PickMarket;
  reasons: readonly PickReason[];
  /** Whole percent of that market in the model's numbers; null for NO_BET. */
  probability: number | null;
}

export type NoBetReason =
  | 'limitedCompetition'
  | 'smallSample'
  | 'noQualifyingMarket'
  | 'partialHistory'
  | 'unvalidatedCompetition'
  | 'earlyForecast';

/** The goal model's figures behind a v3 analysis (M3.5). Every number is computed, none written by the AI. */
export interface ModelForecast {
  version: string;
  kind: 'poisson' | 'dixon_coles';
  /** Limited-data competition (Champions League): shown, never recommended. */
  limited: boolean;
  computedAt: string;
  /** Kickoff of the latest result the model learnt from. */
  dataUntil: string;
  /** Results of this competition in the model's window. */
  sample: number;
  teamMatches: { home: number; away: number };
  expectedGoals: { home: number; away: number };
  /** Whole percents of every market; identities checked when read (1X = 1 + X, pairs = 100…). */
  markets: Record<MarketKey, number>;
  /** Families not more accurate than the league average on the historical check: reference only. */
  unprovenFamilies: readonly MarketFamily[];
  topScores: readonly { home: number; away: number; probability: number }[];
  /** Relative to the competition average (1 = average; defence above 1 concedes more). */
  ratings: { home: { attack: number; defence: number }; away: { attack: number; defence: number } };
  homeAdvantage: number;
  leagueGoals: { home: number; away: number };
  noBetReason: NoBetReason | null;
  /** Recommendations not yet confirmed on matches forecast before kickoff: labelled as experimental. */
  experimental: boolean;
}

export interface MarketChance {
  market: MarketKey;
  probability: number;
}

/** One table of a tournament exactly as the provider orders it. */
export interface StandingRow {
  position: number;
  team: Competitor;
  played: number;
  won: number;
  draw: number;
  lost: number;
  goalsFor: number;
  goalsAgainst: number;
  goalDifference: number;
  points: number;
}

export interface StandingBlock {
  stage: string | null;
  group: string | null;
  rows: readonly StandingRow[];
}

export interface TournamentStandings {
  tournament: Tournament;
  season: TournamentCalendar['season'];
  /** Empty: the competition has no table (e.g. a knockout cup). */
  blocks: readonly StandingBlock[];
  fetchedAt: string | null;
  stale: boolean;
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
  /** v3 (M3.5): the goal model's figures; absent in older (v2) answers. */
  forecast?: ModelForecast;
  /** v3: up to three other markets that passed the selection rules. */
  alternatives?: readonly MarketChance[];
  /** Current prices of this match against the model; absent without a fresh matched line. */
  market?: MarketPrices;
}

/**
 * One market's current price against the model, computed by the backend (percents):
 * implied 100/odds, the market probability with the margin removed, the model's, their
 * difference (edge, in points), the expected value of a unit stake and the group's margin.
 */
export interface MarketQuote {
  market: MarketKey;
  odds: number;
  impliedProbability: number;
  marketProbability: number;
  modelProbability: number;
  edge: number;
  expectedValue: number;
  margin: number;
}

/**
 * When shown prices must disappear: fixed when the answer arrived from the server's "valid for"
 * seconds, on the monotonic clock (immune to device-clock changes) and on the device clock
 * (covers a monotonic clock that slept); whichever passes first.
 */
export interface PriceDeadline {
  mono: number;
  wall: number;
}

/** One outcome of the match's own market (match page): odds and the market probability without the margin. */
export interface BookQuote {
  market: MarketKey;
  odds: number;
  marketProbability: number;
  margin: number;
}

/** The match's current odds: numbers only (never the source's name), valid until `expires`. */
export interface EventOdds {
  fetchedAt: string;
  expires: PriceDeadline;
  quotes: readonly BookQuote[];
}

export interface MarketPrices {
  /** When the prices were taken. */
  fetchedAt: string;
  expires: PriceDeadline;
  quotes: readonly MarketQuote[];
}

/** One of the day's top matches (ranked by the backend from source facts only). */
export interface TopMatch {
  event: SportEvent;
  /** Both teams near the top of the competition's table. */
  leaders: boolean;
}

export type QuickMode = 'single' | 'coupon';
export type QuickPeriod = 'today' | '24h' | '3d' | '7d';
export type QuickMarket = 'any' | MarketFamily;
export type QuickRisk = 'conservative' | 'balanced' | 'elevated';

export interface QuickCriteria {
  mode: QuickMode;
  period: QuickPeriod;
  /** Empty: every tournament. */
  tournaments: readonly string[];
  market: QuickMarket;
  risk: QuickRisk;
  /** Coupon legs, 2–5 (a single is always one). */
  size: number;
}

/** Why matches of the period gave no recommendation (stable backend codes). */
export const QUICK_REASONS = [
  'limited_competition',
  'unvalidated_competition',
  'early_forecast',
  'small_sample',
  'no_qualifying_market',
  'partial_history',
  'model_small_sample',
  'model_team_history',
  'model_extreme_rates',
  'source_unavailable',
] as const;
export type QuickReason = (typeof QUICK_REASONS)[number];

export interface QuickLeg {
  event: FootballEvent;
  market: MarketKey;
  family: MarketFamily;
  /** Whole percent, as on the AI screen. */
  probability: number;
  /** The same market's frequency in this competition's window, whole percent. */
  leagueRate: number;
  expectedGoals: { home: number; away: number };
  sample: number;
  teamMatches: { home: number; away: number };
  dataUntil: string;
  /** This market's current price against the model; null without a fresh matched line. */
  price: (MarketQuote & { fetchedAt: string; expires: PriceDeadline }) | null;
}

export interface QuickPreview {
  event: SportEvent;
  probabilities: { home: number; draw: number; away: number };
  /** UTC day (YYYY-MM-DD) of the match: the only day the rules may recommend it. */
  recommendationDay: string;
}

export interface QuickAnswer {
  mode: QuickMode;
  /** unavailable: today's data could not be loaded now (retry later; never a "no bet"). */
  status: 'ok' | 'no_bet' | 'not_enough' | 'unavailable';
  legs: readonly QuickLeg[];
  size: number;
  /** Matches meeting the criteria (a coupon: how many legs they allow). */
  suitable: number;
  /**
   * Coupon: bounds of the chance that every leg wins, whole percents of the shown legs —
   * [max(0, Σ − 100·(n−1)), weakest leg]. Valid for any dependence: nothing is multiplied.
   */
  combined: readonly [low: number, high: number] | null;
  counts: { matches: number; forecasts: number };
  reasons: Partial<Record<QuickReason, number>>;
  unavailableTournaments: readonly string[];
  /** Days of the period whose matches could not be loaded now. */
  uncheckedDays: number;
  /** Kickoff of the earliest match of the period after today's UTC day (picked only on its day). */
  nextMatchAt: string | null;
  /**
   * Coupon: the product of the legs' current prices, only when every leg has one. A figure
   * computed from separate quotes, not a price offered for the combination.
   */
  couponOdds: { odds: number; fetchedAt: string; expires: PriceDeadline } | null;
  experimental: boolean;
  computedAt: string;
  /**
   * Without a pick only: the nearest later matches of the validated competitions with the model's 1/X/2
   * and the UTC day a recommendation becomes possible. Never a pick (the rules are unchanged).
   */
  preview: readonly QuickPreview[];
}
