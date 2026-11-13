import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { DataFootnote } from '../components/SourceNote';
import { Header } from '../components/Header';
import { DeferredButton, EmptyState, ErrorState, LoadingState } from '../components/States';
import { DemoBanner } from '../components/StatusBadge';
import ui from '../components/ui.module.css';
import type { AiAnalysis, SportEvent, TeamBasis } from '../data/models';
import { Facts, Factors } from './ai/Explain';
import { MarketValue, quoteOf, usePricesLive } from './ai/Market';
import { Alternatives, MarketsBlock, ModelStats } from './ai/Model';
import { PickCard } from './ai/PickCard';
import { ProbabilityBlock } from './ai/ProbabilityBlock';
import { DataError } from '../data/provider';
import { useAsync } from '../hooks/useAsync';
import { useGoBack } from '../hooks/useGoBack';
import { formatDateTime } from '../i18n/format';
import { useFilters, useServices, useSettings } from '../state/contexts';
import s from './AiScreen.module.css';

function Waiting({ onBack }: { onBack(): void }) {
  const { t } = useSettings();
  const [step, setStep] = useState(1);
  useEffect(() => {
    const id = window.setTimeout(() => setStep(2), 600);
    return () => window.clearTimeout(id);
  }, []);
  return (
    <>
      <section className={s.progress} role="status" aria-live="polite" data-testid="ai-loading">
        <span className={s.orb} aria-hidden="true">
          AI
        </span>
        <p className={s.progressTitle}>{t('ai.preparing')}</p>
        <p className={s.progressText}>{t('ai.preparingText')}</p>
        <ul className={s.steps}>
          <li>✓ {t('ai.stepFound')}</li>
          <li>{step >= 2 ? '✓' : '…'} {t('ai.stepData')}</li>
          <li>… {t('ai.stepConclusions')}</li>
        </ul>
      </section>
      <button type="button" className={ui.secondary} onClick={onBack}>
        {t('ai.backToMatch')}
      </button>
      <p className={ui.notice}>{t('ai.loadingNote')}</p>
    </>
  );
}

function Result({ event, analysis }: { event: SportEvent; analysis: AiAnalysis }) {
  const { data } = useServices();
  const { t, timeZone } = useSettings();
  const labels =
    analysis.probabilities.length === 3
      ? [event.home.name, t('ai.draw'), event.away.name]
      : [event.home.name, event.away.name];
  const real = !data.isDemo;
  // The short name replaces the analysis' own team name only when both name the same club
  // (the official name matches); otherwise the snapshot's name stays with its numbers.
  const shortFor = (team: TeamBasis, c: typeof event.home) =>
    team.name === (c.fullName ?? c.name) ? { ...team, name: c.name } : team;
  const basis = analysis.basis && {
    home: shortFor(analysis.basis.home, event.home),
    away: shortFor(analysis.basis.away, event.away),
  };
  const forecast = analysis.forecast;
  // A real analysis without the goal model (cups, qualifiers, small samples): no pick, no model
  // figures; its probabilities, when present, are the market's — labelled as an estimate.
  const basisOnly = real && !forecast && analysis.kind === 'partial' && !analysis.pick;
  // Every PARTIAL marks its probabilities as an estimate (CLAUDE.md, LLM output).
  const title = !real
    ? t('ai.probabilities')
    : analysis.kind === 'partial'
      ? t('ai.probabilitiesEstimated')
      : t('ai.probabilitiesReal');
  const live = usePricesLive(real ? analysis.market?.expires : undefined);
  const prices = real && live ? analysis.market : undefined;
  // The market's estimate disappears with the prices it was computed from.
  const estimateLive = usePricesLive(analysis.estimateExpires);
  const showProbabilities = analysis.probabilities.length > 0 && (!analysis.estimateExpires || estimateLive);
  const tournament = data.getTournament(event.tournamentId);
  const generated = analysis.generatedAt && (
    <p className={s.snapshot}>
      {t(real ? 'ai.generated' : 'ai.snapshot', { date: formatDateTime(new Date(analysis.generatedAt), timeZone) })}
    </p>
  );
  return (
    <>
      {!real && <DemoBanner label={t('ai.exampleBadge')} />}
      <h2 className={s.matchTitle}>{`${event.home.name} — ${event.away.name}`}</h2>
      {forecast ? (
        <p className={s.snapshot} data-testid="ai-match-meta">
          {[tournament?.name, formatDateTime(new Date(event.startsAt), timeZone)].filter(Boolean).join(' · ')}
        </p>
      ) : (
        generated
      )}
      {basisOnly && (
        <section className={ui.section} data-testid="ai-basis-only">
          <h3 className={ui.sectionTitle}>{t('ai.basisOnlyTitle')}</h3>
          <p className={ui.body}>{t('ai.basisOnlyText')}</p>
          <ul className={s.reasons}>
            {analysis.reasons.map((r) => (
              <li key={r}>{t(`ai.reason.${r}`)}</li>
            ))}
          </ul>
        </section>
      )}
      {showProbabilities && (
        <ProbabilityBlock
          title={title}
          labels={labels}
          values={analysis.probabilities}
          sport={analysis.sport}
          badge={basisOnly ? t('ai.marketEstimate') : undefined}
        />
      )}
      {real && basis && analysis.pick && (
        <PickCard
          pick={analysis.pick}
          event={event}
          values={analysis.probabilities}
          basis={basis}
          forecast={forecast}
          quote={quoteOf(prices, analysis.pick.market)}
        />
      )}
      {prices && <MarketValue prices={prices} event={event} />}
      {real && forecast && <Alternatives items={analysis.alternatives ?? []} event={event} prices={prices} />}
      {real && forecast && <MarketsBlock forecast={forecast} event={event} prices={prices} />}
      {real && basis && analysis.factors && analysis.factors.length > 0 && (
        <Factors factors={analysis.factors} basis={basis} forecast={forecast} />
      )}
      {real && basis && forecast ? (
        <Facts basis={basis} tournament={tournament} title={t('ai.stats.title')}>
          <ModelStats forecast={forecast} event={event} />
        </Facts>
      ) : real && basis ? (
        <Facts basis={basis} tournament={tournament} />
      ) : (
        <section className={ui.section} data-testid="ai-basis">
          <h3 className={ui.sectionTitle}>{t('ai.basisTitle')}</h3>
          <p className={ui.body}>{t(`ai.basis.${analysis.sport}`)}</p>
          <p className={ui.note}>{t('ai.limitation')}</p>
        </section>
      )}
      {/* AI history ships in M4. */}
      <DeferredButton label={t('ai.saveAnalysis')} />
    </>
  );
}

/** Real events: no analysis yet, and no demo example in its place. */
function Unavailable({ onBack }: { onBack(): void }) {
  const { t } = useSettings();
  return (
    <>
      <section className={ui.section} data-testid="ai-unavailable">
        <h2 className={ui.pageHeading}>{t('ai.unavailableTitle')}</h2>
        <p className={ui.body}>{t('ai.unavailableText')}</p>
      </section>
      <button type="button" className={ui.secondary} onClick={onBack}>
        {t('ai.backToMatch')}
      </button>
    </>
  );
}

function Insufficient({ analysis, onStats, onRetry }: { analysis: AiAnalysis; onStats(): void; onRetry(): void }) {
  const { t } = useSettings();
  return (
    <>
      <section className={ui.section} data-testid="ai-insufficient">
        <h2 className={ui.pageHeading}>{t('ai.insufficientTitle')}</h2>
        <p className={ui.body}>{t('ai.insufficientText')}</p>
        <ul className={s.reasons}>
          {analysis.reasons.map((r) => (
            <li key={r}>{t(`ai.reason.${r}`)}</li>
          ))}
        </ul>
      </section>
      <button type="button" className={ui.primary} onClick={onStats}>
        {t('ai.viewStats')}
      </button>
      <button type="button" className={ui.secondary} onClick={onRetry}>
        {t('ai.checkAgain')}
      </button>
    </>
  );
}

export function AiScreen() {
  const { id = '' } = useParams();
  const { data } = useServices();
  const { t } = useSettings();
  const navigate = useNavigate();
  const goBack = useGoBack(`/event/${id}`);
  const f = useFilters();
  const event = useAsync(() => data.getEvent(id), `ai-event:${id}`);
  // Opening the screen only READS (an analysis already made, or one that costs nothing); a new
  // paid analysis starts only from the explicit "Create AI analysis" button below.
  // Tied to the match it was tapped for: moving to another match's AI screen never carries it over.
  const [createFor, setCreateFor] = useState<string | null>(null);
  const create = createFor === id;
  const read = useAsync(() => data.getCachedAnalysis(id), `ai-read:${id}`);
  const made = useAsync(() => (create ? data.requestAnalysis(id) : Promise.resolve(null)), `ai:${id}:${create}`);
  const analysis = create ? made : read;

  let body;
  if (event.status === 'success' && !event.data) {
    body = (
      <EmptyState
        title={t('event.notFoundTitle')}
        text={t('event.notFoundText')}
        actions={
          <button type="button" className={ui.primary} onClick={() => navigate('/matches')}>
            {t('event.toCatalog')}
          </button>
        }
      />
    );
  } else if (event.status === 'error') {
    body = <ErrorState title={t('event.errorTitle')} text={t('catalog.errorText')} onRetry={event.retry} />;
  } else if (analysis.status === 'error') {
    const reason = analysis.error instanceof DataError ? analysis.error.reason : undefined;
    const text =
      reason === 'limit_reached'
        ? t('ai.limitText')
        : reason === 'budget_exhausted' || reason === 'rate_limited'
          ? t('ai.busyText')
          : t('ai.errorText');
    body = (
      <>
        <section className={ui.section} data-testid="ai-error" data-reason={reason}>
          <h2 className={ui.pageHeading}>{t('ai.errorTitle')}</h2>
          <p className={ui.body}>{text}</p>
        </section>
        <button type="button" className={ui.primary} onClick={analysis.retry}>
          {t('common.retry')}
        </button>
        <button type="button" className={ui.secondary} onClick={goBack}>
          {t('ai.backToMatch')}
        </button>
      </>
    );
  } else if (analysis.status === 'loading') {
    // The staged "preparing" screen and its demo note belong to the demo only.
    body = data.isDemo ? <Waiting onBack={goBack} /> : <LoadingState cards={2} />;
  } else if (event.status === 'loading') {
    body = <LoadingState cards={2} />;
  } else if (event.data && analysis.data === null) {
    body = (
      <>
        <section className={ui.section} data-testid="ai-create-prompt">
          <h2 className={ui.pageHeading}>{t('ai.createTitle')}</h2>
          <p className={ui.body}>{t(data.isDemo ? 'ai.createTextDemo' : 'ai.createText')}</p>
        </section>
        <button type="button" className={ui.primary} onClick={() => setCreateFor(id)} data-testid="ai-create">
          {t('event.createAi')}
        </button>
        <button type="button" className={ui.secondary} onClick={goBack}>
          {t('ai.backToMatch')}
        </button>
      </>
    );
  } else if (event.data && analysis.data) {
    body =
      analysis.data.kind === 'unavailable' ? (
        <Unavailable onBack={goBack} />
      ) : analysis.data.kind === 'ready' || analysis.data.kind === 'partial' ? (
        <Result event={event.data} analysis={analysis.data} />
      ) : (
        <Insufficient
          analysis={analysis.data}
          onStats={() => navigate(`/event/${id}?tab=stats`, { replace: true })}
          // "Check again" only reads: a new paid analysis needs the create button again.
          onRetry={() => {
            setCreateFor(null);
            read.retry();
          }}
        />
      );
  }

  return (
    <AppShell scrollKey={`/event/${id}/ai`}>
      <Header variant="screen" title={t('ai.title')} backTo={`/event/${id}`} />
      {body}
      {/* The provider attribution its terms require: the app's common footer, not the analysis text. */}
      {event.status === 'success' && event.data && <DataFootnote sport={event.data.sport} today={f.today} />}
    </AppShell>
  );
}
