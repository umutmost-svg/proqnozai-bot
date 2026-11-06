import { useEffect, useRef } from 'react';
import todayIcon from '../assets/figma/icon-today.svg';
import { listOf } from '../i18n';
import { formatDayMonth } from '../i18n/format';
import { useHaptic } from '../hooks/useHaptic';
import { useT } from '../state/contexts';
import { addDays, parseDayKey, weekdayOf, type DayKey } from '../time/zoned';
import s from './Filters.module.css';

/** Days before and after today, as in the Figma strip (5–11 around the 8th). */
const SPAN_BEFORE = 3;
const SPAN_AFTER = 3;

export function DateStrip({ today, value, onChange }: { today: DayKey; value: DayKey; onChange(day: DayKey): void }) {
  const t = useT();
  const haptic = useHaptic();
  const weekdays = listOf(t, 'date.weekdays');
  const months = listOf(t, 'date.monthsShort');
  const days = Array.from({ length: SPAN_BEFORE + SPAN_AFTER + 1 }, (_, i) => addDays(today, i - SPAN_BEFORE));
  const selectedRef = useRef<HTMLButtonElement>(null);
  const month = months[parseDayKey(value).month - 1];

  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [value]);

  return (
    <div className={s.strip} role="group" aria-label={t('date.label')} data-testid="date-strip">
      {days.map((day) => {
        const active = day === value;
        return (
          <button
            key={day}
            ref={active ? selectedRef : undefined}
            type="button"
            className={active ? s.dateActive : s.date}
            aria-pressed={active}
            aria-label={formatDayMonth(t, day)}
            data-day={day}
            onClick={() => {
              haptic();
              onChange(day);
            }}
          >
            <span className={s.weekday}>{weekdays[weekdayOf(day)]}</span>
            <span className={s.dayNum}>{day.slice(8)}</span>
          </button>
        );
      })}
      {/* Month calendar ("UX / calendar") is deferred: shown as a label, not a control. */}
      <span className={s.month} title={t('date.monthHint')}>
        <img src={todayIcon} alt="" width={13.5} height={15} />
        <span>{month}</span>
      </span>
    </div>
  );
}
