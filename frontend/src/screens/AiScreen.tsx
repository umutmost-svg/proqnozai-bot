import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { Header } from '../components/Header';
import { DeferredButton, EmptyState, ErrorState, LoadingState } from '../components/States';
import { DemoBanner } from '../components/StatusBadge';
import ui from '../components/ui.module.css';
import type { AiAnalysis, SportEvent } from '../data/models';
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

function Result({ event, analysis }: { event: SportEvent; analysis: AiAnalysis }) {
  const { t, timeZone } = useSettings();
  const labels =
    analysis.probabilities.length === 3
      ? [event.home.name, t('ai.draw'), event.away.name]
      : [event.home.name, event.away.name];
  return (
    <>
      <DemoBanner label={t('ai.exampleBadge')} />
      <h2 className={s.matchTitle}>{`${event.home.name} — ${event.away.name}`}</h2>
      <p className={s.snapshot}>{t('ai.snapshot', { date: formatDateTime(new Date(analysis.generatedAt), timeZone) })}</p>
      <section className={ui.section} data-testid="ai-result" data-sport={analysis.sport}>
        <h3 className={ui.sectionTitle}>{t('ai.probabilities')}</h3>
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
      <section className={ui.section}>
        <h3 className={ui.sectionTitle}>{t('ai.basisTitle')}</h3>
        <p className={ui.body}>{t(`ai.basis.${analysis.sport}`)}</p>
        <p className={ui.note}>{t('ai.limitation')}</p>
      </section>
      {/* AI history ships in M4. */}
      <DeferredButton label={t('ai.saveAnalysis')} />
      <p className={ui.notice}>{t('ai.disclaimer')}</p>
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
    body = (
      <>
        <section className={ui.section} data-testid="ai-error">
          <h2 className={ui.pageHeading}>{t('ai.errorTitle')}</h2>
          <p className={ui.body}>{t('ai.errorText')}</p>
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
    body = <Waiting onBack={goBack} />;
  } else if (event.status === 'loading') {
    body = <LoadingState cards={2} />;
  } else if (event.data) {
    body =
      analysis.data.kind === 'ready' ? (
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
