import { Link } from 'react-router-dom';
import type { Sport, TeamHit } from '../data/models';

import { useAsync } from '../hooks/useAsync';
import { useDebounced } from '../hooks/useDebounced';
import { useServices, useSettings } from '../state/contexts';
import { Emblem } from './Emblem';
import { EventCard } from './EventCard';
import { EmptyState, ErrorState, LoadingState } from './States';
import s from './GlobalSearch.module.css';

export const MIN_SEARCH = 2;

/** The server's normalisation (miniapp/sports/search.py normalize): Latin letters and digits only. */
export function searchKey(text: string): string {
  return text
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .toLowerCase()
    .replace(/ø/g, 'o')
    .replace(/ß/g, 'ss')
    .replace(/ı/g, 'i')
    .replace(/[^0-9a-z]+/g, ' ')
    .trim();
}

function TeamRow({ hit }: { hit: TeamHit }) {
  const { t } = useSettings();
  const { data } = useServices();
  const tournaments = hit.tournamentIds.map((id) => data.getTournament(id)?.name).filter(Boolean);
  return (
    <Link
      to={`/team/${encodeURIComponent(hit.team.id)}`}
      className={s.team}
      data-testid="search-team"
      data-team-id={hit.team.id}
      aria-label={t('search.openTeam', { name: hit.team.name })}
    >
      <Emblem competitor={hit.team} size={44} crestBox={36} />
      <span className={s.teamCopy}>
        <span className={s.teamName}>{hit.team.name}</span>
        {tournaments.length > 0 && <span className={s.teamMeta}>{tournaments.join(' · ')}</span>}
      </span>
      <span className={s.arrow} aria-hidden="true">
        →
      </span>
    </Link>
  );
}

/**
 * Global search: every served competition and date, independent of the catalogue's
 * date and tournament filters (which it neither reads nor changes).
 */
export function GlobalSearch({ sport, query }: { sport: Sport; query: string }) {
  const { data } = useServices();
  const { t } = useSettings();
  const settled = useDebounced(query.trim());
  const ready = searchKey(settled).length >= MIN_SEARCH;
  // Letters in another script (e.g. "Барса") can never match the Latin team names: say so
  // instead of sending a query the server rejects.
  const otherScript = !ready && /\p{L}/u.test(settled);
  const results = useAsync(
    () => (ready ? data.search(sport, settled) : Promise.resolve(null)),
    `search:${sport}:${settled}`,
  );

  if (!ready) {
    return (
      <p className={s.hint} data-testid="search-hint">
        {t(otherScript && settled.length >= MIN_SEARCH ? 'search.latinHint' : 'search.minLength')}
      </p>
    );
  }
  if (results.status === 'loading' || settled !== query.trim()) {
    return <LoadingState title={t('search.loadingTitle')} text={t('search.loadingText')} cards={2} />;
  }
  if (results.status === 'error') {
    return <ErrorState title={t('catalog.searchErrorTitle')} text={t('catalog.searchErrorText')} onRetry={results.retry} />;
  }
  const r = results.data;
  if (!r) return null;
  // While the index is still filling, the same query can find more later: offer a refresh
  // (the same query string would not re-run by itself).
  const partial = !r.coverage.complete && (
    <div className={s.coverage} data-testid="search-coverage">
      <p>{t('search.partial', { indexed: r.coverage.indexed, total: r.coverage.total })}</p>
      <button type="button" className={s.refresh} onClick={results.retry}>
        {t('search.refresh')}
      </button>
    </div>
  );
  if (r.teams.length === 0 && r.events.length === 0) {
    return (
      <>
        <EmptyState title={t('catalog.searchEmptyTitle')} text={t('search.emptyText')} />
        {partial}
      </>
    );
  }
  return (
    <div className={s.results} data-testid="search-results">
      {partial}
      {r.teams.length > 0 && (
        <section className={s.section}>
          <h2 className={s.title}>{t('search.teams')}</h2>
          {r.teams.map((hit) => (
            <TeamRow key={hit.team.id} hit={hit} />
          ))}
        </section>
      )}
      {r.events.length > 0 && (
        <section className={s.section}>
          <h2 className={s.title}>{t('search.matches')}</h2>
          {r.events.map((e) => (
            <EventCard key={e.id} event={e} tournament={data.getTournament(e.tournamentId)} />
          ))}
        </section>
      )}
    </div>
  );
}
