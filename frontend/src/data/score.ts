import type { BasketballEvent, MmaEvent, Pair, SportEvent, TennisEvent } from './models';

const sum = (pairs: readonly Pair[]): Pair =>
  pairs.reduce<Pair>((acc, p) => [acc[0] + p[0], acc[1] + p[1]], [0, 0]);

/** Sets won by each side; the set in progress is not counted. */
export function tennisSetsWon(e: TennisEvent): Pair {
  const completed = e.status === 'finished' ? e.sets : e.sets.slice(0, -1);
  return completed.reduce<Pair>(
    (acc, [a, b]) => (a > b ? [acc[0] + 1, acc[1]] : b > a ? [acc[0], acc[1] + 1] : acc),
    [0, 0],
  );
}

export function basketballTotal(e: BasketballEvent): Pair {
  return sum([...e.quarters, ...e.overtimes]);
}

export function currentPeriodLabel(e: BasketballEvent): string {
  if (e.overtimes.length) return `OT${e.overtimes.length > 1 ? e.overtimes.length : ''}`;
  return `Q${e.quarters.length}`;
}

export function mmaRoundLabel(e: MmaEvent): string {
  return `R${e.result?.round ?? e.currentRound ?? 1}`;
}

/** Main scoreboard value, or null when the event has not started. */
export function headlineScore(e: SportEvent): string | null {
  if (e.status === 'upcoming') return null;
  switch (e.sport) {
    case 'football':
      return e.score ? `${e.score[0]} : ${e.score[1]}` : null;
    case 'tennis': {
      const [a, b] = tennisSetsWon(e);
      return `${a} : ${b}`;
    }
    case 'basketball': {
      const [a, b] = basketballTotal(e);
      return `${a} : ${b}`;
    }
    case 'mma':
      return mmaRoundLabel(e);
  }
}

export const formatPct = (n: number) => `${n}%`;
export const formatRatio = ([a, b]: Pair) => `${a}/${b}`;
export function formatSeconds(total: number): string {
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
