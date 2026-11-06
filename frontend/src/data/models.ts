/**
 * Sport-specific event models. Each sport keeps its own scoring structure:
 * football — goals, tennis — sets/games, basketball — quarters (+ overtimes),
 * MMA — rounds and the method of finish.
 */

export type Sport = 'football' | 'tennis' | 'basketball' | 'mma';
export const SPORTS: readonly Sport[] = ['football', 'tennis', 'basketball', 'mma'];

export type EventStatus = 'upcoming' | 'live' | 'finished';

/** Local image asset with its intrinsic size, so aspect ratio is never distorted. */
export interface ImageAsset {
  src: string;
  width: number;
  height: number;
  /** Figma renders some sourced logos mirrored vertically; keep that exact. */
  flipY?: boolean;
}

export interface Tournament {
  id: string;
  sport: Sport;
  name: string;
  logo: ImageAsset;
}

export interface Competitor {
  id: string;
  name: string;
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
  | { kind: 'insufficient'; reasons: readonly InsufficientReason[] };

export type InsufficientReason = 'noFreshStats' | 'lineupUnconfirmed' | 'limitedCoverage';

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
}

export interface FootballEvent extends BaseEvent {
  sport: 'football';
  score?: Pair;
  minute?: number;
  stats?: { possession: Pair; shots: Pair; onTarget: Pair; corners: Pair };
}

export interface TennisEvent extends BaseEvent {
  sport: 'tennis';
  format: 'singles' | 'doubles';
  surface: 'hard' | 'clay' | 'grass';
  bestOf: 3 | 5;
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
  weightClass: string;
  scheduledRounds: 3 | 5;
  currentRound?: number;
  roundClock?: string;
  result?: { winner: 'home' | 'away' | null; method: MmaMethod; round: number; time: string };
  stats?: { sigStrikes: Pair; takedowns: readonly [Pair, Pair]; controlSec: Pair };
}

export type SportEvent = FootballEvent | TennisEvent | BasketballEvent | MmaEvent;

export interface AiAnalysis {
  eventId: string;
  sport: Sport;
  kind: 'ready' | 'insufficient';
  probabilities: readonly number[];
  reasons: readonly InsufficientReason[];
  /** When the demo snapshot was produced. */
  generatedAt: string;
}
