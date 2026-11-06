import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { AuthStatus } from '../auth/controller';
import { RETRYABLE } from '../auth/controller';
import { useAuth } from '../auth/useAuth';
import { AppShell } from '../components/AppShell';
import { Header } from '../components/Header';
import ui from '../components/ui.module.css';
import type { MessageKey } from '../i18n';
import { useServices, useSettings } from '../state/contexts';
import s from './ProfileScreen.module.css';

const STATUS_TEXT: Record<AuthStatus, MessageKey> = {
  checking: 'auth.checking',
  authenticated: 'auth.verified',
  outside_telegram: 'auth.outside',
  init_data_invalid: 'auth.invalid',
  init_data_expired: 'auth.initExpired',
  session_expired: 'auth.sessionExpired',
  unavailable: 'auth.unavailable',
  rate_limited: 'auth.rateLimited',
  signed_out: 'auth.signedOut',
};

/** Identity only. Whether sports data is demo is shown separately on each screen. */
function AccountStatus() {
  const { t } = useSettings();
  const { state, auth } = useAuth();
  const { telegram } = useServices();
  const [logoutFailed, setLogoutFailed] = useState(false);
  const verified = state.status === 'authenticated' ? state.session.user : null;
  // Only server-verified data is shown as the user's name; initDataUnsafe is not.
  const name = verified ? [verified.firstName, verified.lastName].filter(Boolean).join(' ') || verified.username : null;

  return (
    <section className={s.account} data-testid="account" data-auth-status={state.status}>
      <p className={s.user}>{name || t('profile.user')}</p>
      <p className={s.context}>{t('profile.context')}</p>
      <p className={verified ? s.authOk : s.authNote} role="status" aria-live="polite" data-testid="auth-status">
        {t(STATUS_TEXT[state.status])}
      </p>
      {logoutFailed && <p className={s.authNote}>{t('auth.logoutFailed')}</p>}
      {RETRYABLE.has(state.status) && (
        <button type="button" className={ui.secondary} onClick={() => void auth.retry()}>
          {t('common.retry')}
        </button>
      )}
      {state.status === 'signed_out' && telegram.available && (
        // Signing in again is explicit: reopening the Mini App brings new launch data.
        <button type="button" className={ui.secondary} onClick={() => telegram.close()}>
          {t('auth.close')}
        </button>
      )}
      {verified && (
        <button
          type="button"
          className={ui.secondary}
          onClick={async () => setLogoutFailed(!(await auth.logout()))}
        >
          {t('auth.logout')}
        </button>
      )}
    </section>
  );
}

type Row = { label: MessageKey; to?: string };

const GROUPS: { title: MessageKey; rows: Row[] }[] = [
  {
    title: 'profile.groupPersonal',
    rows: [{ label: 'profile.favorites' }, { label: 'profile.history' }, { label: 'profile.notifications' }],
  },
  {
    title: 'profile.groupSettings',
    rows: [
      { label: 'profile.language', to: '/profile/language' },
      { label: 'profile.timezone', to: '/profile/timezone' },
      { label: 'profile.reminders' },
      { label: 'profile.appearance', to: '/profile/appearance' },
      { label: 'profile.preferences' },
    ],
  },
  { title: 'profile.groupMore', rows: [{ label: 'profile.partners' }, { label: 'profile.help' }] },
];

export function ProfileScreen() {
  const { t } = useSettings();

  return (
    <AppShell scrollKey="/profile">
      <Header variant="screen" title={t('profile.title')} backTo="/" />
      <AccountStatus />
      {GROUPS.map((group) => (
        <div key={group.title} style={{ display: 'contents' }}>
          <h2 className={s.groupTitle}>{t(group.title)}</h2>
          <div className={s.group}>
            {group.rows.map((row) =>
              row.to ? (
                <Link key={row.label} to={row.to} className={s.row}>
                  <span className={s.rowLabel}>{t(row.label)}</span>
                  <span className={s.chevron} aria-hidden="true">
                    ›
                  </span>
                </Link>
              ) : (
                // Deferred sections: visible from the design, explicitly not available yet.
                <div key={row.label} className={s.rowDeferred} aria-disabled="true">
                  <span className={s.rowLabel}>{t(row.label)}</span>
                  <span className={s.later}>{t('common.later')}</span>
                </div>
              ),
            )}
          </div>
        </div>
      ))}
    </AppShell>
  );
}
