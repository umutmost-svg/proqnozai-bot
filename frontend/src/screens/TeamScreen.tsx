import { useParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { Emblem } from '../components/Emblem';
import { EventCard } from '../components/EventCard';
import { Header } from '../components/Header';
import { DataFootnote } from '../components/SourceNote';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { useAsync } from '../hooks/useAsync';
import { useFilters, useServices, useSettings } from '../state/contexts';
import s from './TeamScreen.module.css';

/** Every indexed match of one team, across all served competitions and dates. */
export function TeamScreen() {
  const { id = '' } = useParams();
  const { data } = useServices();
  const { t } = useSettings();
  const f = useFilters();
  const team = useAsync(() => data.getTeamMatches(id), `team:${id}`);

  let body;
  if (team.status === 'loading') body = <LoadingState title={t('team.loadingTitle')} cards={2} />;
  else if (team.status === 'error') body = <ErrorState title={t('team.errorTitle')} text={t('catalog.errorText')} onRetry={team.retry} />;
  else if (!team.data) body = <EmptyState title={t('team.notFoundTitle')} text={t('team.notFoundText')} />;
  else {
    const { team: hit, events, coverage } = team.data;
    const ahead = events.filter((e) => e.status === 'live' || e.status === 'upcoming');
    // Only finished matches are "played"; postponed, suspended, cancelled or unknown ones are listed apart.
    const played = events.filter((e) => e.status === 'finished');
    const other = events.filter((e) => !['live', 'upcoming', 'finished'].includes(e.status));
    const tournaments = hit.tournamentIds.map((x) => data.getTournament(x)?.name).filter(Boolean);
    body = (
      <>
        <section className={s.head} data-testid="team-head">
          <Emblem competitor={hit.team} size={64} crestBox={52} />
          <div className={s.copy}>
            <h2 className={s.name}>{hit.team.name}</h2>
            {hit.team.fullName && <p className={s.meta}>{hit.team.fullName}</p>}
            {tournaments.length > 0 && <p className={s.meta}>{tournaments.join(' · ')}</p>}
          </div>
        </section>
        {!coverage.complete && (
          <div className={s.coverage} data-testid="team-coverage">
            <p className={s.meta}>{t('search.partial', { indexed: coverage.indexed, total: coverage.total })}</p>
            {/* More competitions may hold this team's matches once indexed: same page, fresh data. */}
            <button type="button" className={s.refresh} onClick={team.retry}>
              {t('search.refresh')}
            </button>
          </div>
        )}
        {events.length === 0 && <EmptyState title={t('team.emptyTitle')} text={t('team.emptyText')} />}
        {ahead.length > 0 && (
          <section className={s.section} data-testid="team-upcoming">
            <h3 className={s.title}>{t('team.upcoming')}</h3>
            {ahead.map((e) => (
              <EventCard key={e.id} event={e} tournament={data.getTournament(e.tournamentId)} />
            ))}
          </section>
        )}
        {other.length > 0 && (
          <section className={s.section} data-testid="team-other">
            <h3 className={s.title}>{t('team.other')}</h3>
            {other.map((e) => (
              <EventCard key={e.id} event={e} tournament={data.getTournament(e.tournamentId)} />
            ))}
          </section>
        )}
        {played.length > 0 && (
          <section className={s.section} data-testid="team-played">
            <h3 className={s.title}>{t('team.played')}</h3>
            {played.map((e) => (
              <EventCard key={e.id} event={e} tournament={data.getTournament(e.tournamentId)} />
            ))}
          </section>
        )}
      </>
    );
  }
  return (
    <AppShell scrollKey={`/team/${id}`} restoreReady={team.status === 'success'}>
      <Header variant="screen" title={t('team.title')} backTo="/matches" />
      {body}
      <DataFootnote sport="football" today={f.today} />
    </AppShell>
  );
}
