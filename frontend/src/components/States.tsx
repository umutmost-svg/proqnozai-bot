import type { ReactNode } from 'react';
import eventIcon from '../assets/figma/icon-event.svg';
import errorIcon from '../assets/figma/icon-error.svg';
import { useT } from '../state/contexts';
import ui from './ui.module.css';
import s from './States.module.css';

export function LoadingState({ title, text, cards = 3 }: { title?: string; text?: string; cards?: number }) {
  return (
    <div className={s.loading} role="status" aria-live="polite" aria-busy="true" data-testid="loading-state">
      {title && <p className={s.heading}>{title}</p>}
      {text && <p className={s.sub}>{text}</p>}
      {Array.from({ length: cards }, (_, i) => (
        <div key={i} className={s.skeleton} aria-hidden="true">
          <span className={s.bar} style={{ width: '62%' }} />
          <span className={s.bar} style={{ width: '82%' }} />
          <span className={s.bar} style={{ width: '48%' }} />
        </div>
      ))}
    </div>
  );
}

interface MessageProps {
  title: string;
  text?: string;
  actions?: ReactNode;
  icon?: 'event' | 'error' | 'none';
  testId?: string;
}

export function EmptyState({ title, text, actions, icon = 'event', testId = 'empty-state' }: MessageProps) {
  return (
    <>
      <div className={s.block} data-testid={testId}>
        {icon !== 'none' && (
          <span className={s.icon} aria-hidden="true">
            {icon === 'error' ? (
              <img src={errorIcon} alt="" width={23.33} height={23.33} />
            ) : (
              <img src={eventIcon} alt="" width={18} height={20} />
            )}
          </span>
        )}
        <p className={s.title}>{title}</p>
        {text && <p className={s.text}>{text}</p>}
      </div>
      {actions && <div className={s.actions}>{actions}</div>}
    </>
  );
}

export function ErrorState({ title, text, onRetry }: { title: string; text: string; onRetry(): void }) {
  const t = useT();
  return (
    <EmptyState
      icon="error"
      testId="error-state"
      title={title}
      text={text}
      actions={
        <button type="button" className={ui.primary} onClick={onRetry}>
          {t('common.retry')}
        </button>
      }
    />
  );
}

/** A button for an action that exists in the design but is not built yet. */
export function DeferredButton({ label }: { label: string }) {
  const t = useT();
  return (
    <button type="button" className={ui.deferred} disabled aria-describedby={undefined}>
      <span>{label}</span>
      <span className={ui.deferredHint}>{t('common.later')}</span>
    </button>
  );
}
