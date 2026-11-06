/**
 * Demo fixtures. Everything here is illustrative: schedules, scores, statistics
 * and probabilities are invented and always shown with a "Demo" mark.
 * Dates are generated relative to the injected clock, never pinned to the
 * date used in the Figma mockups.
 */
import laligaLogo from '../assets/figma/t-laliga.svg';
import premierLogo from '../assets/figma/t-premier.svg';
import atpLogo from '../assets/figma/t-atp.svg';
import wtaLogo from '../assets/figma/t-wta.svg';
import nbaLogo from '../assets/figma/t-nba.svg';
import ufcLogo from '../assets/figma/t-ufc.svg';
import rmCrest from '../assets/figma/crest-real-madrid.svg';
import fcbCrest from '../assets/figma/crest-barcelona.svg';
import rmHero from '../assets/figma/team-real-madrid.svg';
import fcbHero from '../assets/figma/team-barcelona.svg';

import { addDays, dayKeyOf, zonedTimeToInstant, type DayKey } from '../time/zoned';
import type {
  BasketballEvent,
  Competitor,
  FootballEvent,
  MmaEvent,
  SportEvent,
  TennisEvent,
  Tournament,
} from './models';

/** Fixture wall-clock times are authored in Baku time. */
export const FIXTURE_TIME_ZONE = 'Asia/Baku';

export const TOURNAMENTS: readonly Tournament[] = [
  { id: 'laliga', sport: 'football', name: 'La Liga', logo: { src: laligaLogo, width: 26.496, height: 28 } },
  { id: 'premier', sport: 'football', name: 'Premier League', logo: { src: premierLogo, width: 28, height: 28 } },
  { id: 'atp', sport: 'tennis', name: 'ATP Tour', logo: { src: atpLogo, width: 27.758, height: 8.209, flipY: true } },
  { id: 'wta', sport: 'tennis', name: 'WTA Tour', logo: { src: wtaLogo, width: 26.832, height: 27.737, flipY: true } },
  { id: 'nba', sport: 'basketball', name: 'NBA', logo: { src: nbaLogo, width: 28, height: 28 } },
  { id: 'ufc', sport: 'mma', name: 'UFC', logo: { src: ufcLogo, width: 28, height: 28 } },
];

const realMadrid: Competitor = {
  id: 'real-madrid',
  name: 'Real Madrid',
  emblem: { src: rmCrest, width: 54, height: 54 },
  heroEmblem: { src: rmHero, width: 52, height: 64 },
};
const barcelona: Competitor = {
  id: 'barcelona',
  name: 'Barcelona',
  emblem: { src: fcbCrest, width: 54, height: 54 },
  heroEmblem: { src: fcbHero, width: 53, height: 53 },
};
// Fictional demo sides: no real crest exists for them, so the UI shows the
// neutral initial badge used by the Figma event frames.
const team = (id: string, name: string, country?: string): Competitor => ({ id, name, country });

type When = { dayOffset: number; time: string } | { minutesFromNow: number };

function at(when: When, today: DayKey, now: Date): string {
  if ('minutesFromNow' in when) return new Date(now.getTime() + when.minutesFromNow * 60000).toISOString();
  return zonedTimeToInstant(addDays(today, when.dayOffset), when.time, FIXTURE_TIME_ZONE).toISOString();
}

export function buildDemoEvents(now: Date): SportEvent[] {
  const today = dayKeyOf(now, FIXTURE_TIME_ZONE);
  const t = (w: When) => at(w, today, now);

  const football: FootballEvent[] = [
    {
      id: 'fb-rm-fcb', sport: 'football', tournamentId: 'laliga', featured: true,
      startsAt: t({ dayOffset: 0, time: '22:00' }), status: 'upcoming',
      home: realMadrid, away: barcelona,
      ai: { kind: 'ready', probabilities: [48, 27, 25] },
    },
    {
      id: 'fb-costa-sierra', sport: 'football', tournamentId: 'laliga',
      startsAt: t({ minutesFromNow: -70 }), status: 'live', minute: 67,
      home: team('costa-verde', 'Costa Verde CF'), away: team('sierra-alta', 'Sierra Alta'),
      score: [2, 1],
      stats: { possession: [54, 46], shots: [12, 8], onTarget: [6, 3], corners: [4, 2] },
      ai: { kind: 'ready', probabilities: [52, 26, 22] },
    },
    {
      id: 'fb-kingsford-harbour', sport: 'football', tournamentId: 'premier',
      startsAt: t({ dayOffset: 0, time: '19:30' }), status: 'upcoming',
      home: team('kingsford', 'Kingsford United'), away: team('harbour', 'Harbour City'),
      ai: { kind: 'ready', probabilities: [41, 30, 29] },
    },
    {
      id: 'fb-long-names', sport: 'football', tournamentId: 'premier',
      startsAt: t({ dayOffset: 1, time: '21:45' }), status: 'upcoming',
      home: team('wolverhampton-borough', 'Wolverhampton Borough Wanderers Athletic'),
      away: team('north-yorkshire', 'North Yorkshire Moorlands Football Club'),
      ai: { kind: 'insufficient', reasons: ['noFreshStats', 'lineupUnconfirmed', 'limitedCoverage'] },
    },
    {
      // 00:30 in Baku is still the previous calendar day in UTC and Moscow.
      id: 'fb-midnight', sport: 'football', tournamentId: 'laliga',
      startsAt: t({ dayOffset: 1, time: '00:30' }), status: 'upcoming',
      home: team('northbridge', 'Northbridge FC'), away: team('valle-oscuro', 'Valle Oscuro'),
      ai: { kind: 'ready', probabilities: [37, 31, 32] },
    },
    {
      id: 'fb-finished', sport: 'football', tournamentId: 'premier',
      startsAt: t({ dayOffset: -1, time: '21:00' }), status: 'finished',
      home: team('eastgate', 'Eastgate Town'), away: team('millbrook', 'Millbrook Rangers'),
      score: [1, 1],
      stats: { possession: [49, 51], shots: [9, 11], onTarget: [3, 4], corners: [5, 6] },
      ai: { kind: 'ready', probabilities: [35, 33, 32] },
    },
  ];

  const tennis: TennisEvent[] = [
    {
      id: 'tn-live', sport: 'tennis', tournamentId: 'atp', format: 'singles', surface: 'hard', bestOf: 3,
      startsAt: t({ minutesFromNow: -95 }), status: 'live',
      home: team('mateo-alvarez', 'Mateo Alvarez', 'ES'), away: team('luca-bernardi', 'Luca Bernardi', 'IT'),
      sets: [[6, 4], [3, 6], [2, 1]], currentGame: '40:30',
      stats: { aces: [8, 5], firstServePct: [68, 61], breakPoints: [[3, 6], [2, 5]] },
      ai: { kind: 'ready', probabilities: [58, 42] },
    },
    {
      id: 'tn-atp-upcoming', sport: 'tennis', tournamentId: 'atp', format: 'singles', surface: 'hard', bestOf: 3,
      startsAt: t({ dayOffset: 1, time: '15:00' }), status: 'upcoming',
      home: team('jonas-keller', 'Jonas Keller', 'DE'), away: team('arda-yilmaz', 'Arda Yılmaz', 'TR'),
      sets: [],
      ai: { kind: 'ready', probabilities: [55, 45] },
    },
    {
      id: 'tn-wta-upcoming', sport: 'tennis', tournamentId: 'wta', format: 'singles', surface: 'clay', bestOf: 3,
      startsAt: t({ dayOffset: 0, time: '17:00' }), status: 'upcoming',
      home: team('elena-marchetti', 'Elena Marchetti', 'IT'), away: team('sofia-novak', 'Sofia Novak', 'CZ'),
      sets: [],
      ai: { kind: 'insufficient', reasons: ['noFreshStats', 'limitedCoverage'] },
    },
    {
      id: 'tn-wta-finished', sport: 'tennis', tournamentId: 'wta', format: 'singles', surface: 'hard', bestOf: 3,
      startsAt: t({ dayOffset: -1, time: '16:00' }), status: 'finished',
      home: team('claire-dubois', 'Claire Dubois', 'FR'), away: team('hana-sato', 'Hana Sato', 'JP'),
      sets: [[6, 3], [6, 7], [7, 5]],
      stats: { aces: [4, 7], firstServePct: [64, 59], breakPoints: [[4, 9], [3, 7]] },
      ai: { kind: 'ready', probabilities: [51, 49] },
    },
  ];

  const basketball: BasketballEvent[] = [
    {
      id: 'bb-live', sport: 'basketball', tournamentId: 'nba',
      startsAt: t({ minutesFromNow: -80 }), status: 'live',
      home: team('bay-city', 'Bay City Comets'), away: team('lakeshore', 'Lakeshore Pilots'),
      quarters: [[24, 20], [22, 25], [28, 23]], overtimes: [], clock: '05:20',
      stats: { rebounds: [34, 31], assists: [21, 18], fgPct: [48, 45] },
      ai: { kind: 'ready', probabilities: [55, 45] },
    },
    {
      id: 'bb-upcoming', sport: 'basketball', tournamentId: 'nba',
      startsAt: t({ dayOffset: 2, time: '04:30' }), status: 'upcoming',
      home: team('desert-sun', 'Desert Sun'), away: team('northern-lights', 'Northern Lights'),
      quarters: [], overtimes: [],
      ai: { kind: 'ready', probabilities: [53, 47] },
    },
    {
      id: 'bb-finished-ot', sport: 'basketball', tournamentId: 'nba',
      startsAt: t({ dayOffset: -1, time: '05:00' }), status: 'finished',
      home: team('harbor-hawks', 'Harbor Hawks'), away: team('mountain-elk', 'Mountain Elk'),
      quarters: [[27, 25], [21, 24], [26, 22], [20, 23]], overtimes: [[12, 9]],
      stats: { rebounds: [45, 42], assists: [26, 22], fgPct: [46, 44] },
      ai: { kind: 'ready', probabilities: [50, 50] },
    },
  ];

  const mma: MmaEvent[] = [
    {
      id: 'mma-live', sport: 'mma', tournamentId: 'ufc', weightClass: 'lightweight', scheduledRounds: 3,
      startsAt: t({ minutesFromNow: -9 }), status: 'live', currentRound: 2, roundClock: '03:14',
      home: team('kai-moreno', 'Kai Moreno', 'BR'), away: team('elias-brandt', 'Elias Brandt', 'DE'),
      stats: { sigStrikes: [42, 31], takedowns: [[2, 3], [1, 4]], controlSec: [134, 97] },
      ai: { kind: 'ready', probabilities: [54, 46] },
    },
    {
      id: 'mma-main-event', sport: 'mma', tournamentId: 'ufc', weightClass: 'welterweight', scheduledRounds: 5,
      startsAt: t({ dayOffset: 3, time: '07:00' }), status: 'upcoming',
      home: team('tomas-varga', 'Tomás Varga', 'HU'), away: team('dmitri-orlov', 'Dmitri Orlov', 'AM'),
      ai: { kind: 'ready', probabilities: [49, 51] },
    },
    {
      id: 'mma-finished', sport: 'mma', tournamentId: 'ufc', weightClass: 'middleweight', scheduledRounds: 3,
      startsAt: t({ dayOffset: -1, time: '06:30' }), status: 'finished',
      home: team('ivan-petrak', 'Ivan Petrak', 'RS'), away: team('marcus-hale', 'Marcus Hale', 'US'),
      result: { winner: 'home', method: 'ko_tko', round: 1, time: '04:12' },
      stats: { sigStrikes: [27, 12], takedowns: [[0, 1], [1, 2]], controlSec: [18, 41] },
      ai: { kind: 'ready', probabilities: [61, 39] },
    },
  ];

  return [...football, ...tennis, ...basketball, ...mma];
}
