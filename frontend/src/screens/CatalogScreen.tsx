import { NoEvents } from '../components/NoEvents';
import { DataFootnote } from '../components/SourceNote';
import { ComingSoon } from '../components/ComingSoon';
import { GlobalSearch } from '../components/GlobalSearch';
import { useEffect, useRef } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { DateStrip } from '../components/DateStrip';
import { EventCard } from '../components/EventCard';
import { Header } from '../components/Header';
import { SearchField } from '../components/SearchField';
import { SportTabs } from '../components/SportTabs';
import { SourceErrorState, useBusyRetry } from '../components/BusyRetry';
import { LoadingState } from '../components/States';
import { TournamentStrip } from '../components/TournamentStrip';
import ui from '../components/ui.module.css';
import { ALL_TOURNAMENTS } from '../data/filter';
import { useAsync } from '../hooks/useAsync';
import { formatDayMonth } from '../i18n/format';
import { useFilters, useServices, useSettings } from '../state/contexts';
import { useEventQuery } from './useEventQuery';
import s from './CatalogScreen.module.css';

export function CatalogScreen() {
  const { data } = useServices();
  const { t } = useSettings();
  const f = useFilters();
  const location = useLocation();
  const searchRef = useRef<HTMLInputElement>(null);
  const datesRef = useRef<HTMLDivElement>(null);
  const { query, key } = useEventQuery();
  const available = data.sportAvailable(f.filters.sport);
  const searching = f.filters.query.trim().length > 0;
  // A sport without a source, or a global search, needs no day listing at all.
  const events = useAsync(
    () => (available && !searching ? data.listEvents(query) : Promise.resolve([])),
    `events:${key}:${available}:${searching}`,
  );
  const eventsBusy = useBusyRetry(events, `events:${key}:${available}:${searching}`);
  const tournaments = data.listTournaments(f.filters.sport);
  const tournamentName = (id: string) => data.getTournament(id)?.name;
  const filtersAreDefault = f.filters.day === null && f.filters.tournamentId === ALL_TOURNAMENTS;

  useEffect(() => {
    if ((location.state as { focusSearch?: boolean } | null)?.focusSearch) searchRef.current?.focus();
  }, [location.state]);

  const focusDates = () => {
    datesRef.current?.scrollIntoView?.({ block: 'start' });
    datesRef.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus();
  };

  const summary = [
    formatDayMonth(t, f.day),
    f.filters.tournamentId === ALL_TOURNAMENTS ? t('tournament.all') : tournamentName(f.filters.tournamentId),
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <AppShell scrollKey="/matches" restoreReady={events.status === 'success'}>
      <Header variant="brand" backTo="/" />
      <SearchField ref={searchRef} value={f.filters.query} onChange={f.setQuery} />
      <SportTabs value={f.filters.sport} onChange={f.setSport} />
      {!available ? (
        <ComingSoon sport={f.filters.sport} onBackToFootball={() => f.setSport('football')} />
      ) : searching ? (
        <GlobalSearch sport={f.filters.sport} query={f.filters.query} />
      ) : (
        <>
      <div className={s.filters} ref={datesRef}>
        <DateStrip today={f.today} value={f.day} onChange={f.setDay} />
        <TournamentStrip tournaments={tournaments} value={f.filters.tournamentId} onChange={f.setTournament} />
      </div>
      {f.filters.tournamentId !== ALL_TOURNAMENTS && data.getTournament(f.filters.tournamentId) && (
        <Link
          to={`/tournament/${encodeURIComponent(f.filters.tournamentId)}`}
          className={s.calendarLink}
          data-testid="catalog-calendar-link"
        >
          {t('calendar.openFor', { name: tournamentName(f.filters.tournamentId) ?? '' })}
          <span aria-hidden="true">→</span>
        </Link>
      )}
      <div className={s.titleRow}>
        <h2 className={s.title}>{t('catalog.title')}</h2>
        <button type="button" className={s.reset} onClick={f.reset} disabled={filtersAreDefault}>
          {t('catalog.reset')}
        </button>
      </div>

      {events.status === 'loading' && (
        <LoadingState title={t('catalog.loadingTitle')} text={t('catalog.loadingText')} />
      )}

      {events.status === 'error' && (
        <SourceErrorState
          busy={eventsBusy.busy}
          title={t('catalog.errorTitle')}
          text={t('catalog.errorText')}
          onRetry={eventsBusy.retry}
        />
      )}

      {events.status === 'success' && events.data.length === 0 && (
        <>
          <p className={s.summary}>{summary}</p>
          <NoEvents
            sport={f.filters.sport}
            title={t('catalog.emptyTitle')}
            text={t('catalog.emptyText')}
            actions={
              <>
                <button type="button" className={ui.primary} onClick={focusDates}>
                  {t('catalog.pickDate')}
                </button>
                <button type="button" className={ui.secondary} onClick={f.reset}>
                  {t('catalog.resetFilters')}
                </button>
              </>
            }
          />
        </>
      )}

      {events.status === 'success' && events.data.length > 0 && (
        <div className={s.list} data-testid="event-list">
          <p className="visually-hidden" aria-live="polite">
            {t('catalog.results', { count: events.data.length })}
          </p>
          {events.data.map((e) => (
            <EventCard key={e.id} event={e} tournament={data.getTournament(e.tournamentId)} />
          ))}
        </div>
      )}

        </>
      )}
      {available && <DataFootnote sport={f.filters.sport} today={f.today} />}
    </AppShell>
  );
}
