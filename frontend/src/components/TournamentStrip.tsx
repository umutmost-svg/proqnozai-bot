import { ALL_TOURNAMENTS } from '../data/filter';
import { quickPicks } from '../data/tournaments';
import { areaName } from '../data/present';
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
  const shown = quickPicks(tournaments, [value]);
  const visible = new Set(shown.map((x) => x.id));
  // The other competitions stay one tap away: sorted by country, chosen from a compact list.
  const others = tournaments
    .filter((x) => !visible.has(x.id))
    .sort((a, b) => (a.area ? areaName(t, a.area) : '').localeCompare(b.area ? areaName(t, b.area) : '') || a.name.localeCompare(b.name));
  return (
    <div className={s.tournaments} role="group" aria-label={t('tournament.label')} data-testid="tournament-strip">
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
      {others.length > 0 && (
        <select
          className={s.pill}
          aria-label={t('tournament.more')}
          value=""
          onChange={(e) => {
            if (!e.target.value) return;
            haptic();
            onChange(e.target.value);
          }}
          data-testid="tournament-more"
        >
          <option value="">{t('tournament.more')}</option>
          {others.map((x) => (
            <option key={x.id} value={x.id}>
              {x.area ? `${areaName(t, x.area)} · ${x.name}` : x.name}
            </option>
          ))}
        </select>
      )}
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
