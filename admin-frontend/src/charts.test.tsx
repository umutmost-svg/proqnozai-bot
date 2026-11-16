import { describe, expect, it } from 'vitest';
import { avgLabel, dayLabel, niceMax, scaleLabel, tickIndices } from './charts';

describe('day bars helpers', () => {
  it('rounds the scale up to 1/2/5 steps', () => {
    expect([0, 1, 3, 7, 12, 48, 51, 999].map(niceMax)).toEqual([1, 1, 5, 10, 20, 50, 100, 1000]);
  });
  it('keeps the first and the last day among at most six ticks', () => {
    expect(tickIndices(4)).toEqual([0, 1, 2, 3]);
    const t = tickIndices(30);
    expect(t.length).toBe(6);
    expect(t[0]).toBe(0);
    expect(t.at(-1)).toBe(29);
  });
  it('labels half steps and tiny averages honestly', () => {
    expect(scaleLabel(0.5)).toBe('0,5');
    expect(scaleLabel(10)).toBe('10');
    expect(avgLabel(1 / 30)).toBe('<0,1');
    expect(avgLabel(0)).toBe('0');
    expect(avgLabel(2.25)).toBe('2,3');
  });
  it('formats a UTC day in Russian', () => {
    expect(dayLabel('2026-11-16')).toMatch(/^16 ноя/);
    expect(dayLabel('bad')).toBe('bad');
  });
});
