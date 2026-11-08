import { useState } from 'react';
import { AppShell } from '../components/AppShell';
import { Header } from '../components/Header';
import ui from '../components/ui.module.css';
import { resolveDataMode } from '../data/mode';
import { MAX_REPORT, REPORT_TOPICS, reportText, supportLink, supportUsername, validEventId, type ReportTopic } from '../data/support';
import { useServices, useSettings } from '../state/contexts';
import s from './SupportScreen.module.css';

const FAQ = ['what', 'data', 'ai', 'unavailable', 'guarantee'] as const;

export function SupportScreen() {
  const { t, settings, timeZone } = useSettings();
  const { telegram } = useServices();
  const username = supportUsername();
  const [topic, setTopic] = useState<ReportTopic>('app');
  const [description, setDescription] = useState('');
  const [eventId, setEventId] = useState('');
  const [state, setState] = useState<'idle' | 'copied' | 'copyFailed'>('idle');
  const [report, setReport] = useState('');
  const idOk = validEventId(eventId);
  const canPrepare = !!username && description.trim().length >= 10 && idOk;

  const prepare = async () => {
    if (!username || !canPrepare) return;
    const text = reportText({
      topicLabel: t(`support.topic.${topic}`),
      description,
      eventId,
      lang: settings.lang,
      timeZone,
      mode: resolveDataMode(import.meta.env.VITE_DATA_MODE),
    });
    setReport(text);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // No clipboard in this WebView: the text is shown for manual copying; the chat is
      // opened only by the user, after they have it.
      setState('copyFailed');
      return;
    }
    setState('copied');
    telegram.openTelegramLink(supportLink(username));
  };

  return (
    <AppShell scrollKey="/profile/support">
      <Header variant="screen" title={t('support.title')} backTo="/profile" />

      <section className={ui.section} data-testid="support-contact">
        <h2 className={ui.sectionTitle}>{t('support.contactTitle')}</h2>
        {username ? (
          <>
            <p className={ui.body}>{t('support.contactText')}</p>
            <button type="button" className={ui.primary} onClick={() => telegram.openTelegramLink(supportLink(username))}>
              {t('support.contactButton')}
            </button>
          </>
        ) : (
          <>
            <p className={ui.body}>{t('support.notConfigured')}</p>
            <button type="button" className={ui.deferred} disabled aria-disabled="true">
              {t('support.contactButton')}
              <span className={ui.deferredHint}>{t('common.later')}</span>
            </button>
          </>
        )}
      </section>

      <section className={ui.section} data-testid="support-report">
        <h2 className={ui.sectionTitle}>{t('support.reportTitle')}</h2>
        <p className={ui.note}>{t('support.reportIntro')}</p>
        <fieldset className={s.topics}>
          <legend className={s.label}>{t('support.topicLabel')}</legend>
          {REPORT_TOPICS.map((x) => (
            <label key={x} className={topic === x ? s.topicActive : s.topic}>
              <input type="radio" name="topic" value={x} checked={topic === x} onChange={() => setTopic(x)} />
              {t(`support.topic.${x}`)}
            </label>
          ))}
        </fieldset>
        <label className={s.label}>
          {t('support.descriptionLabel')}
          <textarea
            className={s.textarea}
            value={description}
            maxLength={MAX_REPORT}
            rows={5}
            placeholder={t('support.descriptionPlaceholder')}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <label className={s.label}>
          {t('support.eventIdLabel')}
          <input
            className={s.input}
            value={eventId}
            placeholder="fd:560643"
            autoComplete="off"
            onChange={(e) => setEventId(e.target.value)}
            aria-invalid={!idOk}
          />
        </label>
        {!idOk && <p className={s.error}>{t('support.eventIdInvalid')}</p>}
        <p className={ui.note}>{t('support.privacy')}</p>
        <button type="button" className={ui.primary} disabled={!canPrepare} onClick={prepare} data-testid="support-prepare">
          {t('support.prepareButton')}
        </button>
        {!username && <p className={ui.note}>{t('support.reportUnavailable')}</p>}
        {state === 'copied' && (
          <p className={s.status} role="status" data-testid="support-status">
            {t('support.copied')}
          </p>
        )}
        {state === 'copyFailed' && username && (
          <>
            <p className={s.error} role="status" data-testid="support-status">
              {t('support.copyFailed')}
            </p>
            <textarea className={s.textarea} readOnly value={report} rows={6} aria-label={t('support.reportTitle')} data-testid="support-text" />
            <button type="button" className={ui.secondary} onClick={() => telegram.openTelegramLink(supportLink(username))}>
              {t('support.contactButton')}
            </button>
          </>
        )}
      </section>

      <section className={ui.section} data-testid="support-faq">
        <h2 className={ui.sectionTitle}>{t('support.faqTitle')}</h2>
        {FAQ.map((q) => (
          <details key={q} className={s.faq}>
            <summary>{t(`support.faq.${q}.q`)}</summary>
            <p>{t(`support.faq.${q}.a`)}</p>
          </details>
        ))}
      </section>
    </AppShell>
  );
}
