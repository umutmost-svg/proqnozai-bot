import { pickFeatured } from '../data/filter';
import { useNavigate } from 'react-router-dom';
import ambientSrc from '../assets/figma/ambient-light.svg';
import sparkIcon from '../assets/figma/icon-spark.svg';
import arrowIcon from '../assets/figma/icon-arrow.svg';
import calendarCardIcon from '../assets/figma/icon-calendar-card.svg';
import { AppShell } from '../components/AppShell';
import { DateStrip } from '../components/DateStrip';
import { Emblem } from '../components/Emblem';
import { Header } from '../components/Header';
import { SearchLauncher } from '../components/SearchField';
import { SportTabs } from '../components/SportTabs';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { DataFootnote } from '../components/SourceNote';
import { NoEvents } from '../components/NoEvents';
import { DemoChip, StatusBadge } from '../components/StatusBadge';
import { ComingSoon } from '../components/ComingSoon';
import { TournamentList } from '../components/TournamentList';
import { TournamentStrip } from '../components/TournamentStrip';
import type { SportEvent } from '../data/models';
import { headlineScore } from '../data/score';
import { formatDayMonth, zoneWithOffset } from '../i18n/format';
import { useAsync } from '../hooks/useAsync';
import { useFilters, useServices, useSettings } from '../state/contexts';
import { dayKeyOf, timeOf } from '../time/zoned';
import { useEventQuery } from './useEventQuery';
import s from './HomeScreen.module.css';

function Hero({ event, isToday, day }: { event: SportEvent; isToday: boolean; day: string }) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const navigate = useNavigate();
  const showStatus = event.status === 'live' || event.status === 'finished';
  const start = new Date(event.startsAt);
  const score = headlineScore(event);
  return (
    <section className={s.hero} data-testid="hero">
      <div className={s.ambient} aria-hidden="true">
        <img src={ambientSrc} alt="" width={260} height={170} />
      </div>
      <div className={s.heroHead}>
        {/* "Match of the day" only for today; another chosen day says which day it is. */}
        <h2 className={s.heroTitle} data-testid="hero-title">
          {isToday ? t('home.matchOfDay') : t('home.matchOfDate', { date: formatDayMonth(t, day) })}
        </h2>
        {data.isDemo && <DemoChip />}
        {!data.isDemo && showStatus && <StatusBadge status={event.status} demo={false} />}
      </div>
      {/* Locale-independent upper case: CSS uppercase would turn "Liga" into "LİGA" in AZ. */}
      <p className={s.league}>{data.getTournament(event.tournamentId)?.name.toUpperCase()}</p>
      <div className={s.teams}>
        <div className={s.team}>
          <Emblem competitor={event.home} size={72} crestBox={64} variant="hero" useHeroAsset />
          <p className={s.teamName}>{event.home.name}</p>
        </div>
        <span className={s.vs}>{score ?? t('common.vs')}</span>
        <div className={s.team}>
          <Emblem competitor={event.away} size={72} crestBox={64} variant="hero" useHeroAsset />
          <p className={s.teamName}>{event.away.name}</p>
        </div>
      </div>
      <div className={s.kickoff}>
        <p className={s.kickoffMain}>
          {`${formatDayMonth(t, dayKeyOf(start, timeZone))} · ${timeOf(start, timeZone)}`}
        </p>
        <p className={s.kickoffZone}>{zoneWithOffset(t, timeZone, start)}</p>
      </div>
      {/* Real data: no AI analysis yet, so the hero opens the match instead of promising one. */}
      <button
        type="button"
        className={s.cta}
        onClick={() => navigate(data.isDemo ? `/event/${event.id}/ai` : `/event/${event.id}`)}
      >
        {data.isDemo && <img src={sparkIcon} alt="" width={17} height={17} />}
        {t(data.isDemo ? 'home.openAi' : 'home.openMatch')}
        <img src={arrowIcon} alt="" width={17} height={17} />
      </button>
    </section>
  );
}

export function HomeScreen() {
  const { data } = useServices();
  const { t } = useSettings();
  const f = useFilters();
  const navigate = useNavigate();
  const { query, key } = useEventQuery();
  // The day's events, so the home can tell "no matches" from "only stopped matches".
  const available = data.sportAvailable(f.filters.sport);
  const day = useAsync(
    () => (available ? data.listEvents({ ...query, query: '' }) : Promise.resolve([])),
    `featured:${key}:${available}`,
  );
  // Tournament order of the catalogue = priority for the match of the day.
  const order = data.listTournaments(f.filters.sport).map((x) => x.id);
  const rank = (id: string) => (order.indexOf(id) === -1 ? order.length : order.indexOf(id));
  const featured = day.status === 'success' ? pickFeatured(day.data, rank) : null;
  const openCatalog = () => navigate('/matches');

  return (
    <AppShell scrollKey="/" atmosphere>
      <Header variant="home" />
      <SearchLauncher value={f.filters.query} onOpen={() => navigate('/matches', { state: { focusSearch: true } })} />
      <SportTabs value={f.filters.sport} onChange={f.setSport} />
      {!available ? (
        <ComingSoon sport={f.filters.sport} onBackToFootball={() => f.setSport('football')} />
      ) : (
        <>
      <div className={s.filters}>
        <DateStrip today={f.today} value={f.day} onChange={f.setDay} />
        <TournamentStrip
          tournaments={data.listTournaments(f.filters.sport)}
          value={f.filters.tournamentId}
          onChange={f.setTournament}
        />
      </div>

      {day.status === 'loading' && <LoadingState cards={1} text={t('home.loading')} />}
      {day.status === 'error' && (
        <ErrorState title={t('catalog.errorTitle')} text={t('catalog.errorText')} onRetry={day.retry} />
      )}
      {day.status === 'success' &&
        (featured ? (
          <Hero key={featured.id} event={featured} isToday={f.day === f.today} day={f.day} />
        ) : day.data.length > 0 ? (
          <EmptyState title={t('home.noScheduledTitle')} text={t('home.noScheduledText')} />
        ) : (
          <NoEvents sport={f.filters.sport} title={t('home.noFeaturedTitle')} text={t('home.noFeaturedText')} />
        ))}

      <div className={s.more}>
        <button type="button" className={s.moreHead} onClick={openCatalog}>
          <span className={s.moreTitle}>{t('home.allMatches')}</span>
          <span className={s.arrow} aria-hidden="true">
            →
          </span>
        </button>
        <button type="button" className={s.schedule} onClick={openCatalog}>
          <span className={s.calendarBase}>
            <img src={calendarCardIcon} alt="" width={24} height={24} />
          </span>
          <span className={s.scheduleCopy}>
            <span className={s.scheduleTitle}>{t('home.scheduleTitle')}</span>
            <span className={s.scheduleSports}>{t('home.scheduleSports')}</span>
          </span>
        </button>
      </div>
      <TournamentList sport={f.filters.sport} />
      <DataFootnote sport={f.filters.sport} today={f.today} />
        </>
      )}
    </AppShell>
  );
}
