import { useState } from 'react';
import { num } from './format';

export interface DayPoint { day: string; value: number }

const DAY_FMT = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', timeZone: 'UTC' });

/** "2026-11-16" (a UTC day) -> "16 нояб." */
export function dayLabel(day: string): string {
  const t = Date.parse(`${day}T00:00:00Z`);
  return Number.isNaN(t) ? day : DAY_FMT.format(t);
}

/** A scale value: integers as counts, a half step (a peak of 1 -> 0.5) with a decimal comma. */
export function scaleLabel(v: number): string {
  return Number.isInteger(v) ? num(v) : String(v).replace('.', ',');
}

/** A daily average: whole numbers from 10, one decimal below, and never a misleading "0,0" for a nonzero average. */
export function avgLabel(avg: number): string {
  if (avg === 0 || avg >= 10) return num(Math.round(avg));
  if (avg < 0.1) return '<0,1';
  return avg.toFixed(1).replace('.', ',');
}

/** The smallest 1/2/5×10^n that is >= max: the top gridline sits on a round number. */
export function niceMax(max: number): number {
  if (max <= 0) return 1;
  const base = 10 ** Math.floor(Math.log10(max));
  for (const m of [1, 2, 5, 10]) if (m * base >= max) return m * base;
  return 10 * base;
}

/** At most `count` evenly spaced indices, always including the first and the last. */
export function tickIndices(length: number, count = 6): number[] {
  if (length <= count) return Array.from({ length }, (_, i) => i);
  const step = (length - 1) / (count - 1);
  return Array.from({ length: count }, (_, i) => Math.round(i * step));
}

/** One series per day: a summary line (total, daily average, peak day), a value scale with round gridlines,
 * dated ticks and a tooltip per bar. A day without events has no bar, only the baseline (zero is not a sliver). */
export function DayBars({ points, title }: { points: DayPoint[]; title: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length === 0) return <p className="muted small">Нет данных за период.</p>;
  const total = points.reduce((s, p) => s + p.value, 0);
  const peak = points.reduce((best, p) => (p.value > best.value ? p : best), points[0]!);
  const top = niceMax(peak.value);
  const ticks = new Set(tickIndices(points.length));
  const avg = total / points.length;
  const active = hover === null ? null : points[hover]!;
  return (
    <figure className="daybars" aria-label={title}>
      <div className="daybars-summary">
        <span>Всего <b>{num(total)}</b></span>
        <span>В среднем за день <b>{avgLabel(avg)}</b></span>
        {peak.value > 0 && <span>Пик <b>{num(peak.value)}</b> · {dayLabel(peak.day)}</span>}
      </div>
      <div className="daybars-plot">
        <div className="daybars-scale" aria-hidden="true">
          <span>{scaleLabel(top)}</span><span>{scaleLabel(top / 2)}</span><span>0</span>
        </div>
        <div className="daybars-area" onMouseLeave={() => setHover(null)}>
          <div className="daybars-grid" aria-hidden="true"><i /><i /><i /></div>
          <div className="daybars-bars">
            {points.map((p, i) => (
              <div key={p.day} className={`daybars-col${hover === i ? ' on' : ''}`} onMouseEnter={() => setHover(i)}>
                {p.value > 0 && <div className="daybars-bar" style={{ height: `${(p.value / top) * 100}%` }} />}
              </div>
            ))}
          </div>
          {active && hover !== null && (
            <div className={`daybars-tip${hover < points.length * 0.15 ? ' start' : hover > points.length * 0.85 ? ' end' : ''}`}
              role="status" style={{ left: `${((hover + 0.5) / points.length) * 100}%` }}>
              <span className="muted">{dayLabel(active.day)}</span> <b>{num(active.value)}</b>
            </div>
          )}
        </div>
      </div>
      <div className="daybars-axis" aria-hidden="true">
        {points.map((p, i) => <span key={p.day}>{ticks.has(i) ? dayLabel(p.day) : ''}</span>)}
      </div>
      <table className="sr-only">
        <caption>{title}</caption>
        <tbody>{points.map((p) => <tr key={p.day}><th>{p.day}</th><td>{p.value}</td></tr>)}</tbody>
      </table>
    </figure>
  );
}
