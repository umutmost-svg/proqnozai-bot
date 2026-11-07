import { Link } from 'react-router-dom';
import type { SportEvent, Tournament } from '../data/models';
import { competitorRole, flagOf, phaseText } from '../data/present';
import { headlineScore } from '../data/score';
import { formatDateTime } from '../i18n/format';
import { useServices, useSettings } from '../state/contexts';
import { timeOf } from '../time/zoned';
import { Emblem } from './Emblem';
import s from './EventCard.module.css';

interface Props {
  event: SportEvent;
  tournament?: Tournament;
}

function StatusText({ event }: { event: SportEvent }) {
  const { t, timeZone } = useSettings();
  const status = t(`status.${event.status}`);
  // Upcoming shows only its label; a stopped or unknown state is its own phase — once.
  if (event.status !== 'live' && event.status !== 'finished') return <>{status}</>;
  const phase = phaseText(t, event, timeOf(new Date(event.startsAt), timeZone));
  return (
    <>
      <span className={event.status === 'live' ? s.live : undefined}>{status}</span>
      {` · ${phase}`}
    </>
  );
}

export function EventCard({ event, tournament }: Props) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const when = formatDateTime(new Date(event.startsAt), timeZone);
  const score = headlineScore(event);
  const to = `/event/${event.id}`;

  if (event.sport === 'football') {
    return (
      <Link to={to} className={s.card} data-testid="event-card" data-event-id={event.id}>
        <p className={s.meta}>{`${(tournament?.name ?? '').toUpperCase()}  ·  ${when}`}</p>
        <div className={s.crests}>
          {(['home', 'away'] as const).map((side, i) => (
            <div key={side} className={s.team} style={{ order: i * 2 }}>
              <Emblem competitor={event[side]} size={58} crestBox={54} variant="plain" />
              <p className={s.teamName}>{event[side].name}</p>
            </div>
          ))}
          <p className={s.center} style={{ order: 1 }}>
            {score ?? t('common.vs')}
          </p>
        </div>
        <p className={s.title}>{`${event.home.name} — ${event.away.name}`}</p>
        <p className={s.footer}>
          <StatusText event={event} />
          {data.isDemo ? `   ·   ${t('card.openAi')}  →` : '   →'}
        </p>
      </Link>
    );
  }

  return (
    <Link to={to} className={s.compact} data-testid="event-card" data-event-id={event.id}>
      <p className={s.compactHeader}>
        {tournament?.name} · <StatusText event={event} />
      </p>
      <div className={s.versus}>
        {(['home', 'away'] as const).map((side, i) => {
          const flag = flagOf(event[side]);
          return (
            <div key={side} className={s.side} style={{ order: i * 2 }}>
              {flag && (
                <span className={s.flag} role="img" aria-label={event[side].country}>
                  {flag}
                </span>
              )}
              <p className={s.sideName}>{event[side].name}</p>
              <p className={s.role}>{competitorRole(t, event, side)}</p>
            </div>
          );
        })}
        <p className={s.vs} style={{ order: 1 }}>
          {score ?? t('common.vs')}
        </p>
      </div>
      <p className={s.footer}>{t('card.dateTime', { value: when })}</p>
    </Link>
  );
}
