import { SPORTS, type Sport } from '../data/models';
import { useHaptic } from '../hooks/useHaptic';
import { useT } from '../state/contexts';
import s from './Filters.module.css';

export function SportTabs({ value, onChange }: { value: Sport; onChange(sport: Sport): void }) {
  const t = useT();
  const haptic = useHaptic();
  return (
    <div className={s.tabs} role="group" aria-label={t('sport.label')}>
      {SPORTS.map((sport) => (
        <button
          key={sport}
          type="button"
          className={sport === value ? s.tabActive : s.tab}
          aria-pressed={sport === value}
          onClick={() => {
            haptic();
            onChange(sport);
          }}
        >
          {t(`sport.${sport}`)}
        </button>
      ))}
    </div>
  );
}
