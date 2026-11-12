import { describe, expect, it } from 'vitest';
import { scrollChipIntoStrip } from '../components/scrollStrip';
import { seasonFinished } from '../screens/TournamentScreen';
import type { SportEvent } from '../data/models';

const ev = (startsAt: string, status: SportEvent['status']) => ({ startsAt, status }) as SportEvent;

describe('season finished (provider season dates are not trusted)', () => {
  it('only when every match is played and before today', () => {
    expect(seasonFinished([ev('2023-11-19T18:00:00Z', 'finished')], '2026-11-12', 'Asia/Baku')).toBe(true);
    expect(seasonFinished([ev('2023-11-19T18:00:00Z', 'finished'), ev('2026-11-20T18:00:00Z', 'upcoming')], '2026-11-12', 'Asia/Baku')).toBe(false);
    expect(seasonFinished([ev('2026-11-12T10:00:00Z', 'live')], '2026-11-12', 'Asia/Baku')).toBe(false);
    expect(seasonFinished([ev('2026-11-12T10:00:00Z', 'finished')], '2026-11-12', 'Asia/Baku')).toBe(false); // today still counts
    expect(seasonFinished([], '2026-11-12', 'Asia/Baku')).toBe(false);
    // a cup whose last listed match was yesterday but a final with unknown teams is still scheduled
    expect(seasonFinished([ev('2026-11-11T18:00:00Z', 'finished')], '2026-11-12', 'Asia/Baku', 1)).toBe(false);
  });
});

function strip(scrollWidth: number, clientWidth: number, chipLeft: number, chipWidth: number) {
  const parent = { offsetLeft: 0, scrollLeft: 0, scrollWidth, clientWidth } as unknown as HTMLElement;
  const chip = { parentElement: parent, offsetLeft: chipLeft, offsetWidth: chipWidth } as unknown as HTMLElement;
  return { parent, chip };
}

describe('day strip scrolling moves only the strip', () => {
  it('centres a chip, clamped to the strip', () => {
    const { parent, chip } = strip(1200, 300, 600, 60);
    scrollChipIntoStrip(chip, 'center');
    expect(parent.scrollLeft).toBe(480);
    const end = strip(1200, 300, 1140, 60);
    scrollChipIntoStrip(end.chip, 'center');
    expect(end.parent.scrollLeft).toBe(900); // never past the end: nothing left to push ancestors
  });
  it('nearest: moves only as much as needed', () => {
    const { parent, chip } = strip(1200, 300, 320, 60);
    scrollChipIntoStrip(chip, 'nearest');
    expect(parent.scrollLeft).toBe(80);
  });
  it('a missing chip is a no-op', () => {
    expect(() => scrollChipIntoStrip(null, 'center')).not.toThrow();
  });
});
