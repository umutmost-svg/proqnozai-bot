import { useCallback, useEffect, useState } from 'react';
import { AppShell } from '../components/AppShell';
import { Header } from '../components/Header';
import { ErrorState, LoadingState } from '../components/States';
import ui from '../components/ui.module.css';
import { DataError, type CreditsInfo } from '../data/provider';
import { formatDateTime } from '../i18n/format';
import type { MessageKey } from '../i18n/ru';
import { useServices, useSettings } from '../state/contexts';
import form from './SupportScreen.module.css';
import s from './CreditsScreen.module.css';

const MB_ID = /^[A-Za-z0-9_-]{1,32}$/;
const KIND_LABEL: Record<string, MessageKey> = {
  welcome: 'credits.kind.welcome',
  monthly: 'credits.kind.monthly',
  spend: 'credits.kind.spend',
  grant: 'credits.kind.grant',
  debit: 'credits.kind.debit',
  unlimited: 'credits.kind.unlimited',
};
const MB_ERRORS = new Set(['invalid_mb_id', 'mb_taken', 'mb_locked']);

/** The user's credits (docs/miniapp/credits.md): balance, how they come, history, and the MB ID to verify. */
export function CreditsScreen() {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const [state, setState] = useState<{ status: 'loading' } | { status: 'error' } | { status: 'ready'; info: CreditsInfo | null }>({
    status: 'loading',
  });
  const [mbId, setMbId] = useState('');
  const [sending, setSending] = useState(false);
  const [mbError, setMbError] = useState<string | null>(null);

  const load = useCallback(() => {
    data.getCredits().then(
      (info) => setState({ status: 'ready', info }),
      () => setState({ status: 'error' }),
    );
  }, [data]);
  useEffect(load, [load]);

  const at = (seconds: number) => formatDateTime(new Date(seconds * 1000), timeZone);

  const submit = async () => {
    const value = mbId.trim();
    if (!MB_ID.test(value)) {
      setMbError('invalid_mb_id');
      return;
    }
    setSending(true);
    setMbError(null);
    try {
      const mb = await data.submitMbId(value);
      setState((prev) => (prev.status === 'ready' && prev.info ? { status: 'ready', info: { ...prev.info, mb } } : prev));
      setMbId('');
    } catch (error) {
      const reason = error instanceof DataError ? error.reason : undefined;
      setMbError(reason && MB_ERRORS.has(reason) ? reason : 'other');
    } finally {
      setSending(false);
    }
  };

  let body;
  if (state.status === 'loading') body = <LoadingState cards={2} />;
  else if (state.status === 'error') {
    body = <ErrorState title={t('credits.title')} text={t('catalog.errorText')} onRetry={() => { setState({ status: 'loading' }); load(); }} />;
  } else if (!state.info) {
    body = <p className={ui.body}>{t('credits.unavailable')}</p>;
  } else {
    const info = state.info;
    const mb = info.mb;
    body = (
      <>
        <section className={ui.section} data-testid="credits-balance">
          <h2 className={ui.sectionTitle}>{t('credits.balance')}</h2>
          <p className={s.balance}>{info.balance}</p>
          <p className={ui.body}>{t('credits.cost', { n: info.cost })}</p>
          {!info.enabled && <p className={ui.note}>{t('credits.off')}</p>}
          {info.unlimitedUntil && <p className={ui.note}>{t('credits.unlimited', { date: at(info.unlimitedUntil) })}</p>}
          <p className={ui.footnote}>{t('credits.monthly', { n: info.monthly.amount })}</p>
          {info.monthly.nextAt && <p className={ui.footnote}>{t('credits.monthlyNext', { n: info.monthly.amount, date: at(info.monthly.nextAt) })}</p>}
        </section>

        <section className={ui.section} data-testid="credits-mb">
          <h2 className={ui.sectionTitle}>{t('credits.mbTitle')}</h2>
          <p className={ui.body}>{t('credits.mbText')}</p>
          {mb && (
            <p className={s.mbState} data-state={mb.state}>
              <span className={s.mono}>{mb.id}</span> ·{' '}
              {t(mb.state === 'approved' ? 'credits.mbApproved' : mb.state === 'rejected' ? 'credits.mbRejected' : 'credits.mbPending')}
            </p>
          )}
          {mb?.state === 'rejected' && mb.reason && <p className={ui.footnote}>{t('credits.mbReason', { reason: mb.reason })}</p>}
          {mb?.state !== 'approved' && (
            <>
              <label className={form.label}>
                {t('credits.mbPlaceholder')}
                <input
                  className={form.input}
                  value={mbId}
                  maxLength={32}
                  autoComplete="off"
                  inputMode="text"
                  onChange={(e) => {
                    setMbId(e.target.value);
                    setMbError(null);
                  }}
                />
              </label>
              {mbError && (
                <p className={form.error} role="status" data-testid="credits-mb-error">
                  {t(`credits.mbError.${mbError}` as MessageKey)}
                </p>
              )}
              <button type="button" className={ui.primary} disabled={sending || !mbId.trim()} onClick={() => void submit()}>
                {t('credits.mbSubmit')}
              </button>
            </>
          )}
        </section>

        <section className={ui.section} data-testid="credits-history">
          <h2 className={ui.sectionTitle}>{t('credits.history')}</h2>
          {info.history.length === 0 ? (
            <p className={ui.body}>{t('credits.historyEmpty')}</p>
          ) : (
            <ul className={s.history}>
              {info.history.map((h, i) => (
                <li key={`${h.at}-${i}`} className={s.entry}>
                  <span>
                    {KIND_LABEL[h.kind] ? t(KIND_LABEL[h.kind]!) : h.kind}
                    <span className={s.when}>{at(h.at)}</span>
                  </span>
                  <b className={h.delta < 0 ? s.minus : s.plus}>{h.delta > 0 ? `+${h.delta}` : h.delta === 0 ? '—' : h.delta}</b>
                </li>
              ))}
            </ul>
          )}
        </section>
      </>
    );
  }

  return (
    <AppShell scrollKey="/profile/credits">
      <Header variant="screen" title={t('credits.title')} backTo="/profile" />
      {body}
    </AppShell>
  );
}
