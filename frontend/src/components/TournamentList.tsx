import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Sport, Tournament } from '../data/models';
import { matchesQuery } from '../data/tournaments';
import { useAsync } from '../hooks/useAsync';
import { useServices, useT } from '../state/contexts';
import { areaName, initialOfName } from '../data/present';
import { Logo } from './Logo';
import s from './TournamentList.module.css';
import ui from './ui.module.css';

/** Tournaments of the chosen sport; each opens its calendar. */
export function TournamentList({ sport }: { sport: Sport }) {
  const t = useT();
  const { data } = useServices();
  // Loaded on its own, so the calendars stay reachable even when the day's matches fail to load.
  const loaded = useAsync(() => data.loadTournaments(sport), `tournaments:${sport}`);
  const tournaments = loaded.status === 'success' ? loaded.data : data.listTournaments(sport);
  if (loaded.status === 'error' && tournaments.length === 0) {
    // Nothing to fall back on: say so and offer a retry, never a silently missing section.
    return (
      <section className={s.section} data-testid="tournament-list-error">
        <h2 className={s.title}>{t('tournaments.title')}</h2>
        <p className={s.area}>{t('tournaments.errorText')}</p>
        <button type="button" className={ui.secondary} onClick={loaded.retry}>
          {t('common.retry')}
        </button>
      </section>
    );
  }
  if (tournaments.length === 0) return null;
  return <Catalogue tournaments={tournaments} />;
}

/** Few competitions: one plain list. Many: a search field, the main ones first, the rest grouped by country. */
function Catalogue({ tournaments }: { tournaments: Tournament[] }) {
  const t = useT();
  const [query, setQuery] = useState('');
  const large = tournaments.length > 12 && tournaments.some((x) => x.featured);
  const shown = tournaments.filter((x) => matchesQuery(x, query, x.area ? areaName(t, x.area) : ''));
  const featured = large && !query ? shown.filter((x) => x.featured) : [];
  const rest = large && !query ? shown.filter((x) => !x.featured) : shown;
  const groups = new Map<string, Tournament[]>();
  if (large) {
    for (const x of rest) {
      const area = x.area ? areaName(t, x.area) : '—';
      groups.set(area, [...(groups.get(area) ?? []), x]);
    }
  }
  return (
    <section className={s.section} aria-labelledby="tournaments-title" data-testid="tournament-list">
      <h2 id="tournaments-title" className={s.title}>
        {t('tournaments.title')}
      </h2>
      {large && (
        <input
          type="search"
          className={s.search}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('tournaments.search')}
          aria-label={t('tournaments.search')}
          data-testid="tournament-search"
        />
      )}
      {large && shown.length === 0 && <p className={s.area}>{t('tournaments.notFound')}</p>}
      {featured.length > 0 && (
        <>
          <h3 className={s.group}>{t('tournaments.featured')}</h3>
          <Rows list={featured} />
        </>
      )}
      {large ? (
        [...groups.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([area, list]) => (
            <div key={area} data-testid="tournament-group">
              <h3 className={s.group}>{area}</h3>
              <Rows list={list} />
            </div>
          ))
      ) : (
        <Rows list={rest} />
      )}
    </section>
  );
}

function Rows({ list }: { list: Tournament[] }) {
  const t = useT();
  return (
    <ul className={s.list}>
      {list.map((tournament) => (
        <li key={tournament.id}>
          <Link
            to={`/tournament/${encodeURIComponent(tournament.id)}`}
            className={s.row}
            aria-label={t('tournaments.open', { name: tournament.name })}
            data-tournament-id={tournament.id}
          >
            <span className={s.logo}>
              {tournament.logo ? (
                <Logo
                  asset={tournament.logo}
                  box={30}
                  fallback={<span className={s.initial}>{initialOfName(tournament.name)}</span>}
                />
              ) : (
                <span className={s.initial}>{initialOfName(tournament.name)}</span>
              )}
            </span>
            <span className={s.copy}>
              <span className={s.name}>{tournament.name}</span>
              {tournament.area && <span className={s.area}>{areaName(t, tournament.area)}</span>}
            </span>
            <span className={s.arrow} aria-hidden="true">
              →
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
