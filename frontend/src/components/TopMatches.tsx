import { useNavigate } from 'react-router-dom';
import ambientSrc from '../assets/figma/ambient-light.svg';
import arrowIcon from '../assets/figma/icon-arrow.svg';
import type { TopMatch } from '../data/models';
import { headlineScore } from '../data/score';
import { formatDayMonth, zoneWithOffset } from '../i18n/format';
import { useServices, useSettings } from '../state/contexts';
import { dayKeyOf, timeOf } from '../time/zoned';
import { Emblem } from './Emblem';
import { DemoChip, StatusBadge } from './StatusBadge';
import s from './TopMatches.module.css';

function TopCard({ item, only }: { item: TopMatch; only: boolean }) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const navigate = useNavigate();
  const { event } = item;
  const start = new Date(event.startsAt);
  const score = headlineScore(event);
  const tournament = data.getTournament(event.tournamentId)?.name ?? '';
  const when = `${formatDayMonth(t, dayKeyOf(start, timeZone))} · ${timeOf(start, timeZone)}`;
  const showStatus = !data.isDemo && (event.status === 'live' || event.status === 'finished');
  return (
    <article
      className={`${s.hero} ${only ? s.only : ''}`}
      data-testid="top-card"
      data-event={event.id}
      aria-label={t('top.cardLabel', { home: event.home.name, away: event.away.name, tournament, when })}
    >
      <div className={s.ambient} aria-hidden="true">
        <img src={ambientSrc} alt="" width={260} height={170} />
      </div>
      <div className={s.heroHead}>
        {/* Locale-independent upper case: CSS uppercase would turn "Liga" into "LİGA" in AZ. */}
        <p className={s.league}>{tournament.toUpperCase()}</p>
        {showStatus && <StatusBadge status={event.status} demo={false} />}
      </div>
      {item.leaders && (
        <div className={s.chips}>
          <span className={s.chip} data-testid="top-leaders">
            {t('top.leaders')}
          </span>
        </div>
      )}
      <div className={s.teams}>
        <div className={s.team}>
          <Emblem competitor={event.home} size={64} crestBox={56} variant="hero" useHeroAsset />
          <p className={s.teamName}>{event.home.name}</p>
        </div>
        <span className={s.vs}>{score ?? t('common.vs')}</span>
        <div className={s.team}>
          <Emblem competitor={event.away} size={64} crestBox={56} variant="hero" useHeroAsset />
          <p className={s.teamName}>{event.away.name}</p>
        </div>
      </div>
      <div className={s.kickoff}>
        <p className={s.kickoffMain}>{when}</p>
        <p className={s.kickoffZone}>{zoneWithOffset(t, timeZone, start)}</p>
      </div>
      <button type="button" className={s.cta} onClick={() => navigate(`/event/${event.id}`)}>
        {t('home.openMatch')}
        <img src={arrowIcon} alt="" width={17} height={17} />
      </button>
    </article>
  );
}

/**
 * "Top matches of the day": 1–5 cards of the chosen local day, in the provider's ranking
 * order (stable: a live score never reorders them). A swipeable row, never auto-scrolled.
 */
export function TopMatches({ items, isToday, day }: { items: readonly TopMatch[]; isToday: boolean; day: string }) {
  const { t } = useSettings();
  const { data } = useServices();
  return (
    <section className={s.top} data-testid="hero">
      <div className={s.topHead}>
        <h2 className={s.heroTitle} data-testid="hero-title">
          {isToday ? t('top.title') : t('top.titleDate', { date: formatDayMonth(t, day) })}
        </h2>
        {data.isDemo && <DemoChip />}
      </div>
      {items.some((x) => x.event.source?.stale) && (
        <p className={s.stale} data-testid="top-stale">
          {t('top.stale')}
        </p>
      )}
      <div className={s.scroller} role="region" aria-label={t('top.carousel')} tabIndex={0} data-testid="top-scroller">
        {items.map((item) => (
          <TopCard key={item.event.id} item={item} only={items.length === 1} />
        ))}
      </div>
    </section>
  );
}
