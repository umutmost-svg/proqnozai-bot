import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { Emblem } from '../components/Emblem';
import { Header } from '../components/Header';
import { Logo } from '../components/Logo';
import { DeferredButton, EmptyState, ErrorState, LoadingState } from '../components/States';
import { SourceNote } from '../components/SourceNote';
import { StatusBadge } from '../components/StatusBadge';
import ui from '../components/ui.module.css';
import type { EventOdds, SportEvent } from '../data/models';
import { periodBreakdown, phaseText, statRows } from '../data/present';
import { headlineScore } from '../data/score';
import { useAsync } from '../hooks/useAsync';
import { LOCALES } from '../i18n';
import { formatDateTime, zoneWithOffset } from '../i18n/format';
import { usePricesLive } from './ai/Market';
import { decimal, marketText } from './ai/Model';
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
        {tournament?.logo && (
          <span className={s.logo}>
            <Logo asset={tournament.logo} box={26} />
          </span>
        )}
        {tournament ? (
          <Link
            to={`/tournament/${encodeURIComponent(tournament.id)}`}
            className={s.tournament}
            aria-label={t('tournaments.open', { name: tournament.name })}
            data-testid="event-tournament-link"
          >
            {tournament.name}
          </Link>
        ) : (
          <p className={s.tournament} />
        )}
        <StatusBadge status={event.status} demo={data.isDemo} />
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
  const { data } = useServices();
  let title = t('event.prematchTitle');
  let text = '';
  if (event.status === 'upcoming' && !data.isDemo) {
    // Real data: promise nothing the source does not supply (form, H2H, lineups).
    text = t('event.prematch.real');
  } else if (event.status === 'upcoming') {
    switch (event.sport) {
      case 'football':
        text = t('event.prematch.football');
        break;
      case 'tennis':
        text = event.surface
          ? t('event.prematch.tennis', { surface: t(`surface.${event.surface}`) })
          : t('event.prematch.noDetails');
        break;
      case 'basketball':
        text = t('event.prematch.basketball');
        break;
      case 'mma':
        text = event.scheduledRounds
          ? t('event.prematch.mma', { rounds: event.scheduledRounds })
          : t('event.prematch.noDetails');
        break;
    }
  } else if (event.status === 'live') {
    title = t('event.liveTitle');
    text = t(data.isDemo ? 'event.liveText' : 'event.liveTextReal');
  } else if (event.status !== 'finished') {
    title = t('event.statusTitle');
    text = t(`event.statusText.${event.status}`);
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
  const { data } = useServices();
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
        <EmptyState
          title={t('event.tabStats')}
          text={t(event.source ? 'event.statsNotProvided' : 'event.statsUnavailable')}
        />
      )}
      {event.sport === 'football' && <p className={ui.notice}>{t('event.lineupsNote')}</p>}
      {data.isDemo || !event.source ? (
        <p className={ui.footnote}>{t('event.demoNote')}</p>
      ) : (
        <SourceNote source={event.source} />
      )}
    </>
  );
}

/**
 * The match's current odds: every market the feed priced, with the market probability (margin removed).
 * Numbers only, never the source's name. Hidden the moment their validity ends (no stale price shown).
 */
function OddsCard({ event, odds }: { event: SportEvent; odds: EventOdds }) {
  const { t, settings, timeZone } = useSettings();
  const live = usePricesLive(odds.expires);
  if (!live) return null;
  const percent = new Intl.NumberFormat(LOCALES[settings.lang], { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  return (
    <section className={s.odds} data-testid="event-odds">
      <h3 className={s.oddsTitle}>{t('event.odds.title')}</h3>
      <div className={s.oddsHead} aria-hidden="true">
        <span />
        <span>{t('ai.value.odds')}</span>
        <span>{t('ai.value.market')}</span>
      </div>
      <ul className={s.oddsRows}>
        {odds.quotes.map((q) => (
          <li key={q.market} className={s.oddsRow} data-market={q.market}>
            <span className={s.oddsName}>{marketText(t, q.market, event.home.name, event.away.name)}</span>
            <span aria-label={`${t('ai.value.odds')} ${decimal(settings.lang, q.odds)}`}>{decimal(settings.lang, q.odds)}</span>
            <span aria-label={`${t('ai.value.market')} ${percent.format(q.marketProbability)}%`}>{percent.format(q.marketProbability)}%</span>
          </li>
        ))}
      </ul>
      <p className={s.oddsNote} data-testid="event-odds-asof">
        {t('ai.value.asOf', { time: formatDateTime(new Date(odds.fetchedAt), timeZone) })}
      </p>
    </section>
  );
}

/** How often an open match page asks again: the bot refreshes a line every 5-10 minutes. */
export const ODDS_REFRESH_MS = 60_000;

/** The odds of the last answer, refreshed while the page is open: a changed price replaces the old one. */
function OddsSection({ event }: { event: SportEvent }) {
  const { data } = useServices();
  const [odds, setOdds] = useState<EventOdds | null>(() => data.eventOdds(event.id));
  useEffect(() => {
    let alive = true;
    const every = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      data.getEvent(event.id).then(
        () => alive && setOdds(data.eventOdds(event.id)),
        () => undefined, // a failed refresh keeps the old prices only until their own deadline
      );
    }, ODDS_REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(every);
    };
  }, [data, event.id]);
  return odds ? <OddsCard event={event} odds={odds} /> : null;
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
  // Real data: the backend analyses only upcoming football, and only with its AI connected
  // (aiAvailable() is known after this event's answer) — no button that leads nowhere.
  const aiEnabled =
    data.isDemo || (!!event && event.sport === 'football' && event.status === 'upcoming' && data.aiAvailable());
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
            {aiEnabled && (
              <button type="button" role="tab" aria-selected={false} className={s.tab} onClick={openAi}>
                {t('event.tabAi')}
              </button>
            )}
          </div>
          {tab === 'overview' ? (
            <>
              <Overview event={event} />
              {event.status === 'upcoming' && <OddsSection event={event} />}
              {aiEnabled && (
                // Opens the AI screen, which only reads; creating is its own explicit button there.
                <button type="button" className={ui.primary} onClick={openAi}>
                  {t('home.openAi')}
                </button>
              )}
              {/* Favourites ship in M4. */}
              <DeferredButton label={t('event.saveMatch')} />
              {!data.isDemo && event.source && <SourceNote source={event.source} />}
            </>
          ) : (
            <Stats event={event} />
          )}
        </>
      )}
    </AppShell>
  );
}
