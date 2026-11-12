import { useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { EventCard } from '../components/EventCard';
import { Header } from '../components/Header';
import { Logo } from '../components/Logo';
import { scrollChipIntoStrip } from '../components/scrollStrip';
import { areaName, initialOfName } from '../data/present';
import { DataFootnote } from '../components/SourceNote';
import { SourceErrorState, useBusyRetry } from '../components/BusyRetry';
import { EmptyState, LoadingState } from '../components/States';
import filters from '../components/Filters.module.css';
import type { SportEvent, Tournament, TournamentCalendar } from '../data/models';
import { useAsync } from '../hooks/useAsync';
import { useHaptic } from '../hooks/useHaptic';
import { listOf } from '../i18n';
import { formatDayMonth } from '../i18n/format';
import { useFilters, useServices, useSettings } from '../state/contexts';
import { dayKeyOf, parseDayKey, weekdayOf, type DayKey } from '../time/zoned';
import { seasonLabel, StandingsTab } from './Standings';
import s from './TournamentScreen.module.css';
import tabs from './EventScreen.module.css';

/** Matches grouped by calendar day in the chosen time zone, days in order. */
export function groupByDay(events: readonly SportEvent[], timeZone: string): [DayKey, SportEvent[]][] {
  const days = new Map<DayKey, SportEvent[]>();
  for (const event of events) {
    const day = dayKeyOf(new Date(event.startsAt), timeZone);
    days.set(day, [...(days.get(day) ?? []), event]);
  }
  return [...days.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/** The season is over: it has matches and none of them is still to be played or in play (by status, and
 *  the last one is before today). The provider's season dates alone are not trusted: they often miss. */
export function seasonFinished(events: readonly SportEvent[], today: DayKey, timeZone: string): boolean {
  if (!events.length || events.some((e) => e.status === 'upcoming' || e.status === 'live')) return false;
  return events.every((e) => dayKeyOf(new Date(e.startsAt), timeZone) < today);
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
    scrollChipIntoStrip(selectedRef.current, 'center');
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

function Head({ tournament, season, finished }: { tournament: Tournament; season: TournamentCalendar['season']; finished: boolean }) {
  const { t } = useSettings();
  return (
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
          {tournament.area && <p className={s.meta}>{areaName(t, tournament.area)}</p>}
          {/* The provider's "current" season may have ended long ago (cups, qualifiers): then no "current
              matchday", but the season it is, so its dates are never taken for this season's. */}
          {finished && seasonLabel(season) ? (
            <p className={s.meta} data-testid="season-finished">{t('calendar.seasonFinished', { season: seasonLabel(season)! })}</p>
          ) : (
            season?.currentMatchday != null && <p className={s.meta}>{t('calendar.currentRound', { round: season.currentMatchday })}</p>
          )}
        </div>
      </section>
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
  const { tournament } = calendar;

  return (
    <>

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

type TabName = 'matches' | 'table';

export function TournamentScreen() {
  const { id = '' } = useParams();
  const { data } = useServices();
  const { t, timeZone } = useSettings();
  const f = useFilters();
  const [params, setParams] = useSearchParams();
  // The tab lives in the URL: coming back from a team or a match restores it.
  const tab: TabName = params.get('tab') === 'table' ? 'table' : 'matches';
  const [tableOpened, setTableOpened] = useState(tab === 'table');
  const calendar = useAsync(() => data.getTournamentCalendar(id), `calendar:${id}`);
  const calendarBusy = useBusyRetry(calendar, `calendar:${id}`);
  const known = data.getTournament(id);
  // A direct link arrives before any listing: the served list (the backend's configuration, no
  // provider request) names the tournament at once, without waiting for its calendar.
  const named = useAsync(
    () =>
      known
        ? Promise.resolve(known)
        : // Like the calendar request: an unknown id is looked up among the football tournaments.
          data.loadTournaments('football').then(
            () => data.getTournament(id) ?? null,
            () => null,
          ),
    `tournament-name:${id}`,
  );
  const listed = known ?? (named.status === 'success' ? (named.data ?? undefined) : undefined);
  const sport =
    calendar.status === 'success' && calendar.data ? calendar.data.tournament.sport : (listed?.sport ?? f.filters.sport);
  const tournament = calendar.status === 'success' && calendar.data ? calendar.data.tournament : listed;
  // The league's own name; the generic word only once it is clear no tournament has this id.
  const lookupDone = named.status !== 'loading' && calendar.status !== 'loading';
  const title = tournament?.name ?? (lookupDone ? t('tournament.title') : '');
  const choose = (next: TabName) => {
    if (next === 'table') setTableOpened(true);
    setParams(next === 'table' ? { tab: 'table' } : {}, { replace: true });
  };
  const provider = data.coverage(sport).provider;

  return (
    <AppShell scrollKey={`/tournament/${id}?${tab}`} restoreReady={calendar.status !== 'loading'}>
      <Header variant="screen" title={title} backTo="/" />
      {tournament && (
        <Head
          tournament={tournament}
          season={calendar.status === 'success' ? (calendar.data?.season ?? null) : null}
          finished={calendar.status === 'success' && seasonFinished(calendar.data?.events ?? [], f.today, timeZone)}
        />
      )}
      {(tournament || calendar.status !== 'success' || calendar.data) && (
        <div className={tabs.tabs} role="tablist" aria-label={t('tournament.tabs')} data-testid="tournament-tabs">
          {(['matches', 'table'] as const).map((name) => (
            <button
              key={name}
              type="button"
              role="tab"
              aria-selected={tab === name}
              className={tab === name ? tabs.tabActive : tabs.tab}
              data-tab={name}
              onClick={() => choose(name)}
            >
              {t(`tournament.tab.${name}`)}
            </button>
          ))}
        </div>
      )}
      {/* Both tabs stay mounted once opened: switching never refetches nor resets the chosen day. */}
      <div hidden={tab !== 'matches'} className={s.panel} role="tabpanel" data-testid="tab-matches">
        {calendar.status === 'loading' && (
          <LoadingState title={t('calendar.loadingTitle')} text={t('calendar.loadingText')} />
        )}
        {calendar.status === 'error' && (
          <SourceErrorState
            busy={calendarBusy.busy}
            title={t('calendar.errorTitle')}
            text={t('calendar.errorText')}
            onRetry={calendarBusy.retry}
          />
        )}
        {calendar.status === 'success' &&
          (calendar.data ? (
            <Calendar calendar={calendar.data} />
          ) : (
            <EmptyState title={t('calendar.notFoundTitle')} text={t('calendar.notFoundText')} />
          ))}
      </div>
      {(tableOpened || tab === 'table') && (
        <div hidden={tab !== 'table'} className={s.panel} role="tabpanel" data-testid="tab-table">
          <StandingsTab id={id} provider={provider} />
        </div>
      )}
      <DataFootnote sport={sport} today={f.today} />
    </AppShell>
  );
}
