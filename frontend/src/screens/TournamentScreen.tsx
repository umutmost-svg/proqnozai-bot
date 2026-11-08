import { useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { EventCard } from '../components/EventCard';
import { Header } from '../components/Header';
import { Logo } from '../components/Logo';
import { initialOfName } from '../data/present';
import { DataFootnote } from '../components/SourceNote';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import filters from '../components/Filters.module.css';
import type { SportEvent, TournamentCalendar } from '../data/models';
import { useAsync } from '../hooks/useAsync';
import { useHaptic } from '../hooks/useHaptic';
import { listOf } from '../i18n';
import { formatDayMonth } from '../i18n/format';
import { useFilters, useServices, useSettings } from '../state/contexts';
import { dayKeyOf, parseDayKey, weekdayOf, type DayKey } from '../time/zoned';
import s from './TournamentScreen.module.css';

/** Matches grouped by calendar day in the chosen time zone, days in order. */
export function groupByDay(events: readonly SportEvent[], timeZone: string): [DayKey, SportEvent[]][] {
  const days = new Map<DayKey, SportEvent[]>();
  for (const event of events) {
    const day = dayKeyOf(new Date(event.startsAt), timeZone);
    days.set(day, [...(days.get(day) ?? []), event]);
  }
  return [...days.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/** The first match day from today on; after the season, its last day. */
export function initialDay(days: readonly DayKey[], today: DayKey): DayKey | null {
  return days.find((d) => d >= today) ?? days[days.length - 1] ?? null;
}

function DayStrip({ days, value, onChange }: { days: readonly DayKey[]; value: DayKey; onChange(day: DayKey): void }) {
  const { t } = useSettings();
  const haptic = useHaptic();
  const weekdays = listOf(t, 'date.weekdays');
  const months = listOf(t, 'date.monthsShort');
  const selectedRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({
      block: 'nearest',
      inline: 'center',
    });
  }, [value]);
  return (
    <div className={filters.strip} role="group" aria-label={t('calendar.days')} data-testid="calendar-days">
      {days.map((day) => {
        const active = day === value;
        return (
          <button
            key={day}
            ref={active ? selectedRef : undefined}
            type="button"
            className={`${active ? filters.dateActive : filters.date} ${s.chip}`}
            aria-pressed={active}
            aria-label={formatDayMonth(t, day)}
            data-day={day}
            onClick={() => {
              haptic();
              onChange(day);
            }}
          >
            <span className={filters.weekday}>{weekdays[weekdayOf(day)]}</span>
            <span className={filters.dayNum}>{day.slice(8)}</span>
            <span className={s.monthShort}>{months[parseDayKey(day).month - 1]}</span>
          </button>
        );
      })}
    </div>
  );
}

function Calendar({ calendar }: { calendar: TournamentCalendar }) {
  const { t, timeZone } = useSettings();
  const f = useFilters();
  const groups = useMemo(() => groupByDay(calendar.events, timeZone), [calendar.events, timeZone]);
  const days = groups.map(([day]) => day);
  const [picked, setPicked] = useState<DayKey | null>(null);
  const day = picked && days.includes(picked) ? picked : initialDay(days, f.today);
  const matches = groups.find(([d]) => d === day)?.[1] ?? [];
  const rounds = [...new Set(matches.map((e) => (e.sport === 'football' ? e.matchday : undefined)))];
  const round = rounds.length === 1 ? rounds[0] : undefined;
  const { tournament, season } = calendar;

  return (
    <>
      <section className={s.head} data-testid="tournament-head">
        {
          <span className={s.emblem}>
            {tournament.logo ? (
              <Logo
                asset={tournament.logo}
                box={52}
                fallback={<span className={s.initial}>{initialOfName(tournament.name)}</span>}
              />
            ) : (
              <span className={s.initial}>{initialOfName(tournament.name)}</span>
            )}
          </span>
        }
        <div className={s.headCopy}>
          <h2 className={s.name}>{tournament.name}</h2>
          {tournament.area && <p className={s.meta}>{tournament.area}</p>}
          {season?.currentMatchday != null && (
            <p className={s.meta}>{t('calendar.currentRound', { round: season.currentMatchday })}</p>
          )}
        </div>
      </section>

      {day === null ? (
        <EmptyState title={t('calendar.emptyTitle')} text={t('calendar.emptyText')} />
      ) : (
        <>
          <DayStrip days={days} value={day} onChange={setPicked} />
          <h3 className={s.dayTitle} data-testid="calendar-day-title">
            {formatDayMonth(t, day)}
            {round !== undefined && <span className={s.round}>{` · ${t('calendar.round', { round })}`}</span>}
          </h3>
          <div className={s.list} data-testid="calendar-events">
            {matches.map((e) => (
              <EventCard key={e.id} event={e} tournament={tournament} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

export function TournamentScreen() {
  const { id = '' } = useParams();
  const { data } = useServices();
  const { t } = useSettings();
  const f = useFilters();
  const calendar = useAsync(() => data.getTournamentCalendar(id), `calendar:${id}`);
  const known = data.getTournament(id);
  const sport =
    calendar.status === 'success' && calendar.data ? calendar.data.tournament.sport : (known?.sport ?? f.filters.sport);

  return (
    <AppShell scrollKey={`/tournament/${id}`} restoreReady={calendar.status === 'success'}>
      <Header variant="screen" title={t('calendar.title')} backTo="/" />
      {calendar.status === 'loading' && (
        <LoadingState title={t('calendar.loadingTitle')} text={t('calendar.loadingText')} />
      )}
      {calendar.status === 'error' && (
        <ErrorState title={t('calendar.errorTitle')} text={t('calendar.errorText')} onRetry={calendar.retry} />
      )}
      {calendar.status === 'success' &&
        (calendar.data ? (
          <Calendar calendar={calendar.data} />
        ) : (
          <EmptyState title={t('calendar.notFoundTitle')} text={t('calendar.notFoundText')} />
        ))}
      <DataFootnote sport={sport} today={f.today} />
    </AppShell>
  );
}
