import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { Emblem } from '../components/Emblem';
import { Header } from '../components/Header';
import { Logo } from '../components/Logo';
import { DeferredButton, EmptyState, ErrorState, LoadingState } from '../components/States';
import { StatusBadge } from '../components/StatusBadge';
import ui from '../components/ui.module.css';
import type { SportEvent } from '../data/models';
import { periodBreakdown, phaseText, statRows } from '../data/present';
import { headlineScore } from '../data/score';
import { useAsync } from '../hooks/useAsync';
import { formatDateTime, zoneWithOffset } from '../i18n/format';
import { useServices, useSettings } from '../state/contexts';
import { timeOf } from '../time/zoned';
import s from './EventScreen.module.css';

const hasLongWord = (name: string) => name.split(/\s+/).some((w) => w.length > 11);

export function EventHero({ event }: { event: SportEvent }) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const tournament = data.getTournament(event.tournamentId);
  const start = new Date(event.startsAt);
  const breakdown = periodBreakdown(event);
  return (
    <section className={s.hero} data-testid="event-hero" data-sport={event.sport}>
      <div className={s.competition}>
        {tournament && (
          <span className={s.logo}>
            <Logo asset={tournament.logo} box={26} />
          </span>
        )}
        <p className={s.tournament}>{tournament?.name}</p>
        <StatusBadge status={event.status} />
      </div>
      <div className={s.competitors}>
        <div className={s.competitor}>
          <Emblem competitor={event.home} size={64} crestBox={46} useHeroAsset />
          <p className={hasLongWord(event.home.name) ? s.nameLong : s.name}>{event.home.name}</p>
        </div>
        <div className={s.score}>
          <p className={s.scoreMain} data-testid="score">
            {headlineScore(event) ?? t('common.vs')}
          </p>
          <p className={s.phase}>{phaseText(t, event, timeOf(start, timeZone))}</p>
        </div>
        <div className={s.competitor}>
          <Emblem competitor={event.away} size={64} crestBox={46} useHeroAsset />
          <p className={hasLongWord(event.away.name) ? s.nameLong : s.name}>{event.away.name}</p>
        </div>
      </div>
      {breakdown && (
        <p className={s.breakdown} aria-label={t('event.breakdown')}>
          {breakdown}
        </p>
      )}
      <p className={s.when}>{`${formatDateTime(start, timeZone)} · ${zoneWithOffset(t, timeZone, start)}`}</p>
    </section>
  );
}

function Overview({ event }: { event: SportEvent }) {
  const { t } = useSettings();
  let title = t('event.prematchTitle');
  let text = '';
  if (event.status === 'upcoming') {
    switch (event.sport) {
      case 'football':
        text = t('event.prematch.football');
        break;
      case 'tennis':
        text = t('event.prematch.tennis', { surface: t(`surface.${event.surface}`) });
        break;
      case 'basketball':
        text = t('event.prematch.basketball');
        break;
      case 'mma':
        text = t('event.prematch.mma', { rounds: event.scheduledRounds });
        break;
    }
  } else if (event.status === 'live') {
    title = t('event.liveTitle');
    text = t('event.liveText');
  } else {
    title = t('event.finishedTitle');
    text =
      event.sport === 'mma' && event.result?.winner
        ? `${t('mma.winner', { name: event[event.result.winner].name })} · ${phaseText(t, event, '')}`
        : phaseText(t, event, '');
  }
  return (
    <section className={ui.section}>
      <h2 className={ui.sectionTitle}>{title}</h2>
      <p className={ui.body}>{text}</p>
    </section>
  );
}

function Stats({ event }: { event: SportEvent }) {
  const { t } = useSettings();
  const rows = statRows(t, event);
  return (
    <>
      {rows ? (
        <section className={ui.section} data-testid="stats">
          <h2 className={ui.sectionTitle}>{t('event.compare')}</h2>
          <table className={s.table}>
            <thead>
              <tr>
                <th scope="col">{t('event.indicator')}</th>
                <th scope="col">A</th>
                <th scope="col">B</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.label}>
                  <th scope="row" style={{ fontWeight: 400, padding: '8px 0', color: 'inherit' }}>
                    {r.label}
                  </th>
                  <td>{r.home}</td>
                  <td>{r.away}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : (
        <EmptyState title={t('event.tabStats')} text={t('event.statsUnavailable')} />
      )}
      {event.sport === 'football' && <p className={ui.notice}>{t('event.lineupsNote')}</p>}
      <p className={ui.footnote}>{t('event.demoNote')}</p>
    </>
  );
}

export function EventScreen() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'stats' ? 'stats' : 'overview';
  const navigate = useNavigate();
  const { data } = useServices();
  const { t } = useSettings();
  const result = useAsync(() => data.getEvent(id), `event:${id}`);
  const event = result.status === 'success' ? result.data : null;
  const openAi = () => navigate(`/event/${id}/ai`);
  const setTab = (next: 'overview' | 'stats') =>
    setParams(next === 'stats' ? { tab: 'stats' } : {}, { replace: true });

  return (
    <AppShell scrollKey={`/event/${id}`} restoreReady={result.status === 'success'} gap={16}>
      <Header variant="screen" title={event ? t(`sport.${event.sport}`) : ''} backTo="/matches" />
      {result.status === 'loading' && <LoadingState cards={2} />}
      {result.status === 'error' && (
        <ErrorState title={t('event.errorTitle')} text={t('catalog.errorText')} onRetry={result.retry} />
      )}
      {result.status === 'success' && !event && (
        <EmptyState
          title={t('event.notFoundTitle')}
          text={t('event.notFoundText')}
          actions={
            <button type="button" className={ui.primary} onClick={() => navigate('/matches')}>
              {t('event.toCatalog')}
            </button>
          }
        />
      )}
      {event && (
        <>
          <EventHero event={event} />
          <div className={s.tabs} role="tablist" aria-label={t('event.tabs')}>
            <button type="button" role="tab" aria-selected={tab === 'overview'} className={tab === 'overview' ? s.tabActive : s.tab} onClick={() => setTab('overview')}>
              {t('event.tabOverview')}
            </button>
            <button type="button" role="tab" aria-selected={tab === 'stats'} className={tab === 'stats' ? s.tabActive : s.tab} onClick={() => setTab('stats')}>
              {t('event.tabStats')}
            </button>
            <button type="button" role="tab" aria-selected={false} className={s.tab} onClick={openAi}>
              {t('event.tabAi')}
            </button>
          </div>
          {tab === 'overview' ? (
            <>
              <Overview event={event} />
              <button type="button" className={ui.primary} onClick={openAi}>
                {t('event.createAi')}
              </button>
              {/* Favourites ship in M4. */}
              <DeferredButton label={t('event.saveMatch')} />
            </>
          ) : (
            <Stats event={event} />
          )}
        </>
      )}
    </AppShell>
  );
}
