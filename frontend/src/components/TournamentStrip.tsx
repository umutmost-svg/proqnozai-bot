import { ALL_TOURNAMENTS } from '../data/filter';
import type { Tournament } from '../data/models';
import { useHaptic } from '../hooks/useHaptic';
import { useT } from '../state/contexts';
import { Logo } from './Logo';
import s from './Filters.module.css';

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
  return (
    <div className={s.tournaments} role="group" aria-label={t('tournament.label')} data-testid="tournament-strip">
      {tournaments.map((tournament) => (
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
          <span className={s.logoSlot}>
            <Logo asset={tournament.logo} box={28} />
          </span>
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
  );
}
