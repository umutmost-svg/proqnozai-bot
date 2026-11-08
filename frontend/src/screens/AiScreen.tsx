import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { Header } from '../components/Header';
import { DeferredButton, EmptyState, ErrorState, LoadingState } from '../components/States';
import { DemoBanner } from '../components/StatusBadge';
import ui from '../components/ui.module.css';
import type { AiAnalysis, AiFactor, SportEvent, TeamBasis } from '../data/models';
import { DataError } from '../data/provider';
import { useAsync } from '../hooks/useAsync';
import { useGoBack } from '../hooks/useGoBack';
import { formatDateTime } from '../i18n/format';
import { useServices, useSettings } from '../state/contexts';
import s from './AiScreen.module.css';

/** Darker bar for the leading outcome, as in the Figma probability block. */
const FILL_ALPHA = [0.55, 0.45, 0.35];

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

/** A factor the model picked, as a sentence built only from that team's verified numbers. */
function factorText(t: ReturnType<typeof useSettings>['t'], factor: AiFactor, basis: { home: TeamBasis; away: TeamBasis }): string {
  const team = factor.startsWith('home') ? basis.home : basis.away;
  const params = {
    team: team.name,
    form: team.form,
    matches: team.matches,
    points: team.points,
    goalsFor: team.goalsFor,
    goalsAgainst: team.goalsAgainst,
    position: team.tablePosition ?? 0,
    tablePoints: team.tablePoints ?? 0,
  };
  if (factor.endsWith('Form')) return t('ai.factor.form', params);
  if (factor.endsWith('Goals')) return t('ai.factor.goals', params);
  return t('ai.factor.table', params);
}

/** One team's verified numbers (computed by the backend in Python, not by the model). */
function BasisRow({ team }: { team: TeamBasis }) {
  const { t } = useSettings();
  return (
    <li>
      <strong>{team.name}</strong>
      {': '}
      {t('ai.basisForm', {
        form: team.form,
        matches: team.matches,
        points: team.points,
        goalsFor: team.goalsFor,
        goalsAgainst: team.goalsAgainst,
      })}
      {team.tablePosition !== null &&
        team.tablePoints !== null &&
        ` · ${t('ai.basisTable', { position: team.tablePosition, points: team.tablePoints })}`}
    </li>
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
  const title = !real ? t('ai.probabilities') : analysis.estimated ? t('ai.probabilitiesEstimated') : t('ai.probabilitiesReal');
  return (
    <>
      {!real && <DemoBanner label={t('ai.exampleBadge')} />}
      <h2 className={s.matchTitle}>{`${event.home.name} — ${event.away.name}`}</h2>
      {analysis.generatedAt && (
        <p className={s.snapshot}>
          {t(real ? 'ai.generated' : 'ai.snapshot', { date: formatDateTime(new Date(analysis.generatedAt), timeZone) })}
        </p>
      )}
      <section className={ui.section} data-testid="ai-result" data-sport={analysis.sport}>
        <h3 className={ui.sectionTitle} data-testid="ai-probabilities-title">
          {title}
        </h3>
        {analysis.probabilities.map((p, i) => (
          <div key={labels[i]} className={s.outcome}>
            <div className={s.outcomeLabel}>
              <span className={s.outcomeName}>{labels[i]}</span>
              <span className={s.outcomeValue}>{p}%</span>
            </div>
            <div className={s.track} role="presentation">
              <div className={s.fill} style={{ width: `${p}%`, background: `rgba(25, 35, 28, ${FILL_ALPHA[i] ?? 0.35})` }} />
            </div>
          </div>
        ))}
      </section>
      {real && analysis.basis && analysis.factors && analysis.factors.length > 0 && (
        <section className={ui.section} data-testid="ai-factors">
          <h3 className={ui.sectionTitle}>{t('ai.factorsTitle')}</h3>
          <ul className={s.reasons}>
            {analysis.factors.map((f) => (
              <li key={f}>{factorText(t, f, analysis.basis!)}</li>
            ))}
          </ul>
        </section>
      )}
      <section className={ui.section} data-testid="ai-basis">
        <h3 className={ui.sectionTitle}>{t('ai.basisTitle')}</h3>
        {real && analysis.basis ? (
          <>
            <ul className={s.reasons}>
              <BasisRow team={analysis.basis.home} />
              <BasisRow team={analysis.basis.away} />
            </ul>
            <p className={ui.note}>
              {t(
                analysis.basis.home.tablePosition === null && analysis.basis.away.tablePosition === null
                  ? 'ai.realLimitationNoTable'
                  : 'ai.realLimitation',
              )}
            </p>
            {/* The figures are the provider's: its required attribution goes with them. No
                "updated" time here — the form and table are fetched apart from the fixture;
                when the analysis was made is shown at the top. */}
            {event.source && (
              <p className={ui.footnote} data-testid="ai-source">
                {t('ai.basisSource', { provider: event.source.provider })}
                {event.source.attribution && (
                  <>
                    <br />
                    {event.source.attribution}
                  </>
                )}
              </p>
            )}
          </>
        ) : (
          <>
            <p className={ui.body}>{t(`ai.basis.${analysis.sport}`)}</p>
            <p className={ui.note}>{t('ai.limitation')}</p>
          </>
        )}
      </section>
      {/* AI history ships in M4. */}
      <DeferredButton label={t('ai.saveAnalysis')} />
      <p className={ui.notice}>{t('ai.disclaimer')}</p>
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
  const event = useAsync(() => data.getEvent(id), `ai-event:${id}`);
  const analysis = useAsync(() => data.requestAnalysis(id), `ai:${id}`);

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
  } else if (event.data) {
    body =
      analysis.data.kind === 'unavailable' ? (
        <Unavailable onBack={goBack} />
      ) : analysis.data.kind === 'ready' || analysis.data.kind === 'partial' ? (
        <Result event={event.data} analysis={analysis.data} />
      ) : (
        <Insufficient
          analysis={analysis.data}
          onStats={() => navigate(`/event/${id}?tab=stats`, { replace: true })}
          onRetry={analysis.retry}
        />
      );
  }

  return (
    <AppShell scrollKey={`/event/${id}/ai`}>
      <Header variant="screen" title={t('ai.title')} backTo={`/event/${id}`} />
      {body}
    </AppShell>
  );
}
