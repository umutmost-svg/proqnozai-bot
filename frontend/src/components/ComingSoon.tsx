import type { Sport } from '../data/models';
import { useT } from '../state/contexts';
import s from './ComingSoon.module.css';

/** Line icons drawn for this screen (no third-party artwork). */
function SportIcon({ sport }: { sport: Sport }) {
  const common = { width: 44, height: 44, viewBox: '0 0 48 48', fill: 'none', stroke: 'currentColor', strokeWidth: 2.4, strokeLinecap: 'round' as const };
  if (sport === 'tennis') {
    return (
      <svg {...common} aria-hidden="true">
        <circle cx="24" cy="24" r="17" />
        <path d="M10.5 13.5c6 4 6 17 0 21M37.5 13.5c-6 4-6 17 0 21" />
      </svg>
    );
  }
  if (sport === 'basketball') {
    return (
      <svg {...common} aria-hidden="true">
        <circle cx="24" cy="24" r="17" />
        <path d="M7 24h34M24 7v34M12 12c5 5 5 19 0 24M36 12c-5 5-5 19 0 24" />
      </svg>
    );
  }
  return (
    <svg {...common} aria-hidden="true">
      <path d="M15 22v-6a5 5 0 0 1 10 0v2a5 5 0 0 1 10 0v10c0 7-5 12-11 12h-1c-6 0-9-4-9-9v-9z" />
      <path d="M15 26h12M25 18v8" />
    </svg>
  );
}

/**
 * A sport without a connected source: an honest "coming soon" — no demo matches, no
 * statistics, no AI, no launch date we cannot promise, and no request to any API.
 */
export function ComingSoon({ sport, onBackToFootball }: { sport: Exclude<Sport, 'football'> | Sport; onBackToFootball(): void }) {
  const t = useT();
  return (
    <section className={s.card} data-testid="coming-soon" data-sport={sport}>
      <span className={s.icon}>
        <SportIcon sport={sport} />
      </span>
      <p className={s.sport}>{t(`sport.${sport}`)}</p>
      <h2 className={s.title}>{t('soon.title')}</h2>
      {sport !== 'football' && <p className={s.text}>{t(`soon.text.${sport}`)}</p>}
      <p className={s.note}>{t('soon.note')}</p>
      <button type="button" className={s.button} onClick={onBackToFootball}>
        {t('soon.toFootball')}
      </button>
    </section>
  );
}
