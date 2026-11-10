import { useRef, useState } from 'react';
import searchIcon from '../assets/figma/icon-search.svg';
import { ALL_TOURNAMENTS } from '../data/filter';
import { areaName } from '../data/present';
import { matchesQuery, quickPicks } from '../data/tournaments';
import type { Tournament } from '../data/models';
import { useHaptic } from '../hooks/useHaptic';
import { useT } from '../state/contexts';
import { Logo } from './Logo';
import s from './Filters.module.css';

/** Search answers shown at once: a short list, the query narrows it. */
const MAX_FOUND = 8;

export function TournamentStrip({
  tournaments,
  value,
  onChange,
}: {
  tournaments: readonly Tournament[];
  value: string;
  onChange(id: string): void;
}) {
  const t = useT();
  const haptic = useHaptic();
  const shown = quickPicks(tournaments, [value]);
  // The rest of the catalogue is found by name or country here, never listed whole in the strip.
  const searchable = shown.length < tournaments.length;
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState('');
  const area = (x: Tournament) => (x.area ? areaName(t, x.area) : '');
  const found = query.trim()
    ? tournaments
        .filter((x) => x.id !== value && matchesQuery(x, query, area(x)))
        .sort((a, b) => Number(!!b.featured) - Number(!!a.featured) || area(a).localeCompare(area(b)) || a.name.localeCompare(b.name))
        .slice(0, MAX_FOUND)
    : [];
  const toggle = useRef<HTMLButtonElement>(null);
  // Closing removes the focused field or answer: focus goes back to the button that opened the search.
  const close = () => {
    setSearching(false);
    setQuery('');
    toggle.current?.focus();
  };
  return (
    <>
      <div className={s.tournaments} role="group" aria-label={t('tournament.label')} data-testid="tournament-strip">
        {searchable && (
          <button
            ref={toggle}
            type="button"
            className={searching ? s.pillActive : s.pill}
            aria-label={t('tournaments.search')}
            aria-expanded={searching}
            data-testid="tournament-search-toggle"
            onClick={() => (searching ? close() : setSearching(true))}
          >
            <img src={searchIcon} alt="" width={18} height={18} />
          </button>
        )}
        {shown.map((tournament) => (
          <button
            key={tournament.id}
            type="button"
            className={tournament.id === value ? s.pillActive : s.pill}
            aria-pressed={tournament.id === value}
            onClick={() => {
              haptic();
              onChange(tournament.id);
            }}
          >
            {tournament.logo && (
              <span className={s.logoSlot}>
                <Logo asset={tournament.logo} box={28} />
              </span>
            )}
            {tournament.name}
          </button>
        ))}
        <button
          type="button"
          className={value === ALL_TOURNAMENTS ? s.pillActive : s.pill}
          aria-pressed={value === ALL_TOURNAMENTS}
          onClick={() => {
            haptic();
            onChange(ALL_TOURNAMENTS);
          }}
        >
          {t('tournament.all')}
        </button>
      </div>
      {searchable && searching && (
        <div className={s.tournamentSearch} data-testid="tournament-search" onKeyDown={(e) => e.key === 'Escape' && close()}>
          <label className={s.search}>
            <span className="visually-hidden">{t('tournaments.search')}</span>
            <input
              className={s.searchInput}
              type="search"
              inputMode="search"
              autoComplete="off"
              autoFocus
              maxLength={40}
              placeholder={t('tournaments.search')}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          {query.trim() && found.length === 0 && <p className={s.tournamentNone}>{t('tournaments.notFound')}</p>}
          {found.map((x) => (
            <button
              key={x.id}
              type="button"
              className={s.tournamentFound}
              onClick={() => {
                haptic();
                onChange(x.id);
                close();
              }}
            >
              <span className={s.logoSlot}>{x.logo && <Logo asset={x.logo} box={28} />}</span>
              {x.area ? `${area(x)} · ${x.name}` : x.name}
            </button>
          ))}
        </div>
      )}
    </>
  );
}
