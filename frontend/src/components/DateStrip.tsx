import { useEffect, useRef, useState } from 'react';
import { CalendarSheet } from './CalendarSheet';
import { useFilters, useServices, useSettings } from '../state/contexts';
import todayIcon from '../assets/figma/icon-today.svg';
import { listOf } from '../i18n';
import { formatDayMonth } from '../i18n/format';
import { useHaptic } from '../hooks/useHaptic';
import { addDays, parseDayKey, weekdayOf, type DayKey } from '../time/zoned';
import s from './Filters.module.css';

/** Days before and after today, as in the Figma strip (5–11 around the 8th). */
const SPAN_BEFORE = 3;
const SPAN_AFTER = 3;

export function DateStrip({ today, value, onChange }: { today: DayKey; value: DayKey; onChange(day: DayKey): void }) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const { filters } = useFilters();
  const haptic = useHaptic();
  const [calendarOpen, setCalendarOpen] = useState(false);
  const weekdays = listOf(t, 'date.weekdays');
  const months = listOf(t, 'date.monthsShort');
  // Around today; a day picked in the calendar beyond that moves the strip to it.
  const nearToday = value >= addDays(today, -SPAN_BEFORE) && value <= addDays(today, SPAN_AFTER);
  const centre = nearToday ? today : value;
  const days = Array.from({ length: SPAN_BEFORE + SPAN_AFTER + 1 }, (_, i) => addDays(centre, i - SPAN_BEFORE));
  const window = data.dateWindow();
  const range = window ? { min: addDays(today, -window.past), max: addDays(today, window.future) } : null;
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
            // Days the source cannot answer for are shown but not selectable.
            disabled={!!range && (day < range.min || day > range.max)}
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
      <button
        type="button"
        className={s.month}
        onClick={() => {
          haptic();
          setCalendarOpen(true);
        }}
        aria-label={t('calendar.open')}
        aria-haspopup="dialog"
        data-testid="calendar-open"
      >
        <img src={todayIcon} alt="" width={13.5} height={15} />
        <span>{month}</span>
      </button>
      {calendarOpen && (
      <CalendarSheet
        open
        value={value}
        today={today}
        range={range}
        markers={data.knownMatchDays(filters.sport, timeZone)}
        onSelect={onChange}
        onClose={() => setCalendarOpen(false)}
      />
      )}
    </div>
  );
}
