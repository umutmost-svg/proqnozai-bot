import type { Translate } from '../i18n';
import { basketballTotal, currentPeriodLabel, formatSeconds } from './score';
import type { Competitor, Pair, SportEvent } from './models';

/** Secondary scoreboard line: minute, set, quarter clock or round clock. */
export function phaseText(t: Translate, e: SportEvent, kickoffTime: string): string {
  if (e.status === 'upcoming') return kickoffTime;
  switch (e.sport) {
    case 'football':
      if (e.status === 'finished') return t('phase.finished');
      return t('phase.football.live', {
        minute: e.minute ?? 0,
        half: t((e.minute ?? 0) > 45 ? 'phase.football.secondHalf' : 'phase.football.firstHalf'),
      });
    case 'tennis':
      if (e.status === 'finished') return t('phase.finished');
      return t('phase.tennis.live', { set: e.sets.length, game: e.currentGame ?? '' });
    case 'basketball':
      if (e.status === 'finished') {
        return e.overtimes.length ? `${t('phase.finished')} · ${currentPeriodLabel(e)}` : t('phase.finished');
      }
      return t('phase.basketball.live', { period: currentPeriodLabel(e), clock: e.clock ?? '' });
    case 'mma':
      if (e.status === 'finished' && e.result) {
        return t('phase.mma.finished', {
          method: t(`mma.method.${e.result.method}`),
          round: e.result.round,
          time: e.result.time,
        });
      }
      return t('phase.mma.live', { clock: e.roundClock ?? '', rounds: e.scheduledRounds });
  }
}

/** Per-period breakdown for sports where the headline hides it. */
export function periodBreakdown(e: SportEvent): string | null {
  const join = (pairs: readonly Pair[]) => pairs.map(([a, b]) => `${a}:${b}`).join(' · ');
  if (e.sport === 'tennis' && e.sets.length) return join(e.sets);
  if (e.sport === 'basketball' && e.quarters.length) {
    const ot = e.overtimes.length ? ` · OT ${join(e.overtimes)}` : '';
    const [a, b] = basketballTotal(e);
    return `${join(e.quarters)}${ot} (${a}:${b})`;
  }
  return null;
}

export interface StatRow {
  label: string;
  home: string;
  away: string;
}

export function statRows(t: Translate, e: SportEvent): StatRow[] | null {
  const pct = (n: number) => `${n}%`;
  const ratio = ([a, b]: Pair) => `${a}/${b}`;
  switch (e.sport) {
    case 'football':
      if (!e.stats) return null;
      return [
        { label: t('stat.possession'), home: pct(e.stats.possession[0]), away: pct(e.stats.possession[1]) },
        { label: t('stat.shots'), home: `${e.stats.shots[0]}`, away: `${e.stats.shots[1]}` },
        { label: t('stat.onTarget'), home: `${e.stats.onTarget[0]}`, away: `${e.stats.onTarget[1]}` },
        { label: t('stat.corners'), home: `${e.stats.corners[0]}`, away: `${e.stats.corners[1]}` },
      ];
    case 'tennis':
      if (!e.stats) return null;
      return [
        { label: t('stat.aces'), home: `${e.stats.aces[0]}`, away: `${e.stats.aces[1]}` },
        { label: t('stat.firstServe'), home: pct(e.stats.firstServePct[0]), away: pct(e.stats.firstServePct[1]) },
        { label: t('stat.breakPoints'), home: ratio(e.stats.breakPoints[0]), away: ratio(e.stats.breakPoints[1]) },
      ];
    case 'basketball':
      if (!e.stats) return null;
      return [
        { label: t('stat.rebounds'), home: `${e.stats.rebounds[0]}`, away: `${e.stats.rebounds[1]}` },
        { label: t('stat.assists'), home: `${e.stats.assists[0]}`, away: `${e.stats.assists[1]}` },
        { label: t('stat.fgPct'), home: pct(e.stats.fgPct[0]), away: pct(e.stats.fgPct[1]) },
      ];
    case 'mma':
      if (!e.stats) return null;
      return [
        { label: t('stat.sigStrikes'), home: `${e.stats.sigStrikes[0]}`, away: `${e.stats.sigStrikes[1]}` },
        { label: t('stat.takedowns'), home: ratio(e.stats.takedowns[0]), away: ratio(e.stats.takedowns[1]) },
        { label: t('stat.control'), home: formatSeconds(e.stats.controlSec[0]), away: formatSeconds(e.stats.controlSec[1]) },
      ];
  }
}

/** Role line under a competitor on non-football cards. */
export function competitorRole(t: Translate, e: SportEvent, side: 'home' | 'away'): string {
  switch (e.sport) {
    case 'tennis':
      return t(e.format === 'singles' ? 'card.singles' : 'card.doubles');
    case 'basketball':
      return t(side === 'home' ? 'card.home' : 'card.away');
    case 'mma':
      return t(side === 'home' ? 'card.redCorner' : 'card.blueCorner');
    default:
      return '';
  }
}

/** Flag emoji for an athlete's country, as drawn in the Figma tennis cards. */
export function flagOf(c: Competitor): string | null {
  if (!c.country || !/^[A-Z]{2}$/.test(c.country)) return null;
  return String.fromCodePoint(...[...c.country].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65));
}

export function initialOf(c: Competitor): string {
  return c.name.trim().charAt(0).toLocaleUpperCase() || '?';
}
