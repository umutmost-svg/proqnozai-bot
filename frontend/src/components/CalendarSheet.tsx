import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { listOf } from '../i18n';
import { formatDayMonth } from '../i18n/format';
import { useHaptic } from '../hooks/useHaptic';
import { useT } from '../state/contexts';
import { parseDayKey, weekdayOf, type DayKey } from '../time/zoned';
import s from './CalendarSheet.module.css';

const pad = (n: number) => String(n).padStart(2, '0');
const keyOf = (year: number, month: number, day: number): DayKey => `${year}-${pad(month)}-${pad(day)}`;
const daysIn = (year: number, month: number) => new Date(Date.UTC(year, month, 0)).getUTCDate();

/** Weeks of a month, Monday first; null fills the cells outside the month. */
export function monthGrid(year: number, month: number): (DayKey | null)[][] {
  const first = keyOf(year, month, 1);
  const cells: (DayKey | null)[] = Array.from({ length: weekdayOf(first) }, () => null);
  for (let d = 1; d <= daysIn(year, month); d++) cells.push(keyOf(year, month, d));
  while (cells.length % 7) cells.push(null);
  return Array.from({ length: cells.length / 7 }, (_, i) => cells.slice(i * 7, i * 7 + 7));
}

interface Props {
  open: boolean;
  value: DayKey;
  today: DayKey;
  /** Inclusive range of days that can be chosen (the data source's window); null = any. */
  range: { min: DayKey; max: DayKey } | null;
  /** Days already known to have matches (count > 0 draws a dot); unknown days get none. */
  markers: ReadonlyMap<DayKey, number>;
  onSelect(day: DayKey): void;
  onClose(): void;
}

/** Month calendar in a bottom sheet. Closing without choosing keeps the current day. */
export function CalendarSheet({ open, value, today, range, markers, onSelect, onClose }: Props) {
  const t = useT();
  const haptic = useHaptic();
  const start = parseDayKey(value);
  const [view, setView] = useState({ year: start.year, month: start.month });
  const sheetRef = useRef<HTMLDivElement>(null);
  const drag = useRef<number | null>(null);
  const [offset, setOffset] = useState(0);

  // Mounted only while open (see DateStrip): the view starts at the chosen day's month.
  useEffect(() => {
    if (!open) return;
    // Focus returns to the opener when the sheet closes (keyboard users keep their place).
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    sheetRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      if (e.key !== 'Tab' || !sheetRef.current) return;
      // Modal: keyboard focus cycles inside the sheet, never to the page behind it.
      const focusable = [...sheetRef.current.querySelectorAll<HTMLElement>('button:not([disabled])')];
      if (focusable.length === 0) return;
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === sheetRef.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !sheetRef.current.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      opener?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;
  const months = listOf(t, 'date.months');
  const weekdays = listOf(t, 'date.weekdays');
  const shift = (months: number) =>
    setView(({ year, month }) => {
      const index = year * 12 + (month - 1) + months;
      return { year: Math.floor(index / 12), month: (index % 12) + 1 };
    });
  const allowed = (day: DayKey) => !range || (day >= range.min && day <= range.max);
  const choose = (day: DayKey) => {
    if (!allowed(day)) return;
    haptic();
    onSelect(day);
    onClose();
  };

  // Portal: a transformed or filtered ancestor (glass cards) would otherwise trap position: fixed.
  return createPortal(
    <div className={s.backdrop} onClick={onClose} data-testid="calendar-sheet-backdrop">
      <div
        ref={sheetRef}
        className={s.sheet}
        role="dialog"
        aria-modal="true"
        aria-label={t('calendar.sheetTitle')}
        tabIndex={-1}
        style={offset ? { transform: `translateY(${offset}px)` } : undefined}
        onClick={(e) => e.stopPropagation()}
        // Swipe down to close only from the top: when the sheet itself is scrolled, the same
        // gesture just scrolls it back.
        onTouchStart={(e) => {
          drag.current = (sheetRef.current?.scrollTop ?? 0) > 0 ? null : (e.touches[0]?.clientY ?? null);
        }}
        onTouchMove={(e) => {
          if (drag.current === null) return;
          setOffset(Math.max(0, (e.touches[0]?.clientY ?? 0) - drag.current));
        }}
        onTouchEnd={() => {
          drag.current = null;
          if (offset > 90) onClose();
          else setOffset(0);
        }}
        data-testid="calendar-sheet"
      >
        <span className={s.grabber} aria-hidden="true" />
        <div className={s.head}>
          <h2 className={s.title}>{t('calendar.sheetTitle')}</h2>
          <button type="button" className={s.close} onClick={onClose} aria-label={t('calendar.close')}>
            ×
          </button>
        </div>
        <div className={s.nav}>
          <button type="button" className={s.navButton} onClick={() => shift(-12)} aria-label={t('calendar.prevYear')}>
            «
          </button>
          <button type="button" className={s.navButton} onClick={() => shift(-1)} aria-label={t('calendar.prevMonth')}>
            ‹
          </button>
          <p className={s.month} aria-live="polite" data-testid="calendar-month">
            {months[view.month - 1]} {view.year}
          </p>
          <button type="button" className={s.navButton} onClick={() => shift(1)} aria-label={t('calendar.nextMonth')}>
            ›
          </button>
          <button type="button" className={s.navButton} onClick={() => shift(12)} aria-label={t('calendar.nextYear')}>
            »
          </button>
        </div>
        <div className={s.grid} role="grid" aria-label={`${months[view.month - 1]} ${view.year}`}>
          <div className={s.row} role="row">
            {weekdays.map((w) => (
              <span key={w} className={s.weekday} role="columnheader">
                {w}
              </span>
            ))}
          </div>
          {monthGrid(view.year, view.month).map((week, i) => (
            <div key={i} className={s.row} role="row">
              {week.map((day, j) =>
                day === null ? (
                  <span key={j} className={s.empty} role="gridcell" />
                ) : (
                  <button
                    key={day}
                    type="button"
                    role="gridcell"
                    className={[s.day, day === value && s.selected, day === today && s.today].filter(Boolean).join(' ')}
                    aria-selected={day === value}
                    aria-current={day === today ? 'date' : undefined}
                    aria-label={formatDayMonth(t, day)}
                    disabled={!allowed(day)}
                    data-day={day}
                    onClick={() => choose(day)}
                  >
                    {Number(day.slice(8))}
                    {(markers.get(day) ?? 0) > 0 && <span className={s.dot} data-testid="calendar-dot" />}
                  </button>
                ),
              )}
            </div>
          ))}
        </div>
        {range && <p className={s.note}>{t('calendar.rangeNote')}</p>}
        <button type="button" className={s.todayButton} onClick={() => choose(today)}>
          {t('calendar.today')}
        </button>
      </div>
    </div>,
    document.body,
  );
}
