import { useNavigate } from 'react-router-dom';
import calendarCardIcon from '../assets/figma/icon-calendar-card.svg';
import { AppShell } from '../components/AppShell';
import { DateStrip } from '../components/DateStrip';
import { Header } from '../components/Header';
import { SearchLauncher } from '../components/SearchField';
import { SportTabs } from '../components/SportTabs';
import { SourceErrorState, useBusyRetry } from '../components/BusyRetry';
import { EmptyState, LoadingState } from '../components/States';
import { DataFootnote } from '../components/SourceNote';
import { NoEvents } from '../components/NoEvents';
import { ComingSoon } from '../components/ComingSoon';
import { TournamentList, useTournamentCatalogue } from '../components/TournamentList';
import { TournamentStrip } from '../components/TournamentStrip';
import { TopMatches } from '../components/TopMatches';
import type { EventStatus } from '../data/models';
import { useAsync } from '../hooks/useAsync';
import { useFilters, useServices, useSettings } from '../state/contexts';
import { useEventQuery } from './useEventQuery';
import s from './HomeScreen.module.css';

const STOPPED: ReadonlySet<EventStatus> = new Set(['postponed', 'suspended', 'cancelled']);

export function HomeScreen() {
  const { data } = useServices();
  const { t } = useSettings();
  const f = useFilters();
  const navigate = useNavigate();
  const { query, key } = useEventQuery();
  const available = data.sportAvailable(f.filters.sport);
  const catalogue = useTournamentCatalogue(f.filters.sport, available);
  // The day's top matches; only when there are none, the day itself tells "no matches"
  // from "only stopped matches" (both answered from the backend's shared cache).
  const top = useAsync(async () => {
    if (!available) return { items: [], any: false, stopped: false };
    const items = await data.getTopMatches({ ...query, query: '' });
    if (items.length) return { items, any: true, stopped: false };
    const day = await data.listEvents({ ...query, query: '' });
    // "Postponed or cancelled" only when that is what every match of the day really is.
    return { items, any: day.length > 0, stopped: day.length > 0 && day.every((e) => STOPPED.has(e.status)) };
  }, `top:${key}:${available}`);
  const topBusy = useBusyRetry(top, `top:${key}:${available}`);
  const openCatalog = () => navigate('/matches');

  return (
    <AppShell scrollKey="/" atmosphere>
      <Header variant="home" />
      <SearchLauncher value={f.filters.query} onOpen={() => navigate('/matches', { state: { focusSearch: true } })} />
      <SportTabs value={f.filters.sport} onChange={f.setSport} />
      {!available ? (
        <ComingSoon sport={f.filters.sport} onBackToFootball={() => f.setSport('football')} />
      ) : (
        <>
      <div className={s.filters}>
        <DateStrip today={f.today} value={f.day} onChange={f.setDay} />
        <TournamentStrip
          tournaments={catalogue.tournaments}
          value={f.filters.tournamentId}
          onChange={f.setTournament}
        />
      </div>

      {top.status === 'loading' && <LoadingState cards={1} text={t('home.loading')} />}
      {top.status === 'error' && (
        <SourceErrorState busy={topBusy.busy} title={t('catalog.errorTitle')} text={t('catalog.errorText')} onRetry={topBusy.retry} />
      )}
      {top.status === 'success' &&
        (top.data.items.length > 0 ? (
          <TopMatches items={top.data.items} isToday={f.day === f.today} day={f.day} />
        ) : top.data.stopped ? (
          <EmptyState title={t('home.noScheduledTitle')} text={t('home.noScheduledText')} />
        ) : top.data.any ? (
          <EmptyState title={t('top.emptyTitle')} text={t('top.emptyText')} />
        ) : (
          <NoEvents sport={f.filters.sport} title={t('home.noFeaturedTitle')} text={t('home.noFeaturedText')} />
        ))}

      <div className={s.more}>
        <button type="button" className={s.moreHead} onClick={openCatalog}>
          <span className={s.moreTitle}>{t('home.allMatches')}</span>
          <span className={s.arrow} aria-hidden="true">
            →
          </span>
        </button>
        <button type="button" className={s.schedule} onClick={openCatalog}>
          <span className={s.calendarBase}>
            <img src={calendarCardIcon} alt="" width={24} height={24} />
          </span>
          <span className={s.scheduleCopy}>
            <span className={s.scheduleTitle}>{t('home.scheduleTitle')}</span>
            <span className={s.scheduleSports}>{t('home.scheduleSports')}</span>
          </span>
        </button>
      </div>
      <TournamentList catalogue={catalogue} />
      <DataFootnote sport={f.filters.sport} today={f.today} />
        </>
      )}
    </AppShell>
  );
}
