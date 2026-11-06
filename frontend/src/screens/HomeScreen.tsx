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
import { DemoChip } from '../components/StatusBadge';
import { TournamentStrip } from '../components/TournamentStrip';
import ui from '../components/ui.module.css';
import type { SportEvent } from '../data/models';
import { headlineScore } from '../data/score';
import { formatDayMonth, formatLongDate, zoneLabel, zoneWithOffset } from '../i18n/format';
import { useAsync } from '../hooks/useAsync';
import { useFilters, useServices, useSettings } from '../state/contexts';
import { dayKeyOf, timeOf } from '../time/zoned';
import { useEventQuery } from './useEventQuery';
import s from './HomeScreen.module.css';

function Hero({ event }: { event: SportEvent }) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const navigate = useNavigate();
  const start = new Date(event.startsAt);
  const score = headlineScore(event);
  return (
    <section className={s.hero} data-testid="hero">
      <div className={s.ambient} aria-hidden="true">
        <img src={ambientSrc} alt="" width={260} height={170} />
      </div>
      <div className={s.heroHead}>
        <h2 className={s.heroTitle}>{t('home.matchOfDay')}</h2>
        <DemoChip />
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
      <button type="button" className={s.cta} onClick={() => navigate(`/event/${event.id}/ai`)}>
        <img src={sparkIcon} alt="" width={17} height={17} />
        {t('home.openAi')}
        <img src={arrowIcon} alt="" width={17} height={17} />
      </button>
    </section>
  );
}

export function HomeScreen() {
  const { data } = useServices();
  const { t, timeZone } = useSettings();
  const f = useFilters();
  const navigate = useNavigate();
  const { query, key } = useEventQuery();
  const featured = useAsync(() => data.getFeatured(query), `featured:${key}`);
  const openCatalog = () => navigate('/matches');

  return (
    <AppShell scrollKey="/" atmosphere>
      <Header variant="home" />
      <SearchLauncher value={f.filters.query} onOpen={() => navigate('/matches', { state: { focusSearch: true } })} />
      <SportTabs value={f.filters.sport} onChange={f.setSport} />
      <div className={s.filters}>
        <DateStrip today={f.today} value={f.day} onChange={f.setDay} />
        <TournamentStrip
          tournaments={data.listTournaments(f.filters.sport)}
          value={f.filters.tournamentId}
          onChange={f.setTournament}
        />
      </div>

      {featured.status === 'loading' && <LoadingState cards={1} text={t('home.loading')} />}
      {featured.status === 'error' && (
        <ErrorState title={t('catalog.errorTitle')} text={t('catalog.errorText')} onRetry={featured.retry} />
      )}
      {featured.status === 'success' &&
        (featured.data ? (
          <Hero key={featured.data.id} event={featured.data} />
        ) : (
          <EmptyState title={t('home.noFeaturedTitle')} text={t('home.noFeaturedText')} />
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
      <p className={ui.footnote} data-testid="demo-footer">
        {t('footer.demo', { date: formatLongDate(t, f.today), zone: zoneLabel(t, timeZone) })}
      </p>
    </AppShell>
  );
}
