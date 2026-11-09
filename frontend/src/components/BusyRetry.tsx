import { useCallback, useEffect, useRef, useState } from 'react';
import { DataError } from '../data/provider';
import type { AsyncState } from '../hooks/useAsync';
import { useSettings } from '../state/contexts';
import { ErrorState } from './States';
import ui from './ui.module.css';

/** Waits up to this long are retried automatically (once); longer ones only say how long. */
export const AUTO_RETRY_MAX_S = 60;

export type Busy = { seconds: number; auto: boolean };

/** The server's wait (seconds) when the data source is busy; undefined for any other failure. */
export function busyWait(error: unknown): number | undefined {
  return error instanceof DataError ? error.retryAfter : undefined;
}

/**
 * The data source's request limit: counts down the server's Retry-After and then asks again by
 * itself, at most once per `key` (a table, a calendar, a day's listing). A second refusal, a
 * longer wait or a manual retry leaves the next attempt to the user, so a busy source is never
 * polled. The state lives in the screen that owns `state`, not in the error view, so the view
 * unmounting while the retry loads does not grant another automatic attempt.
 */
export function useBusyRetry(state: AsyncState<unknown> & { retry(): void }, key: string): { busy: Busy | null; retry(): void } {
  const error = state.status === 'error' ? state.error : null;
  const wait = busyWait(error);
  // Keys whose one automatic retry is used up, for as long as the screen is open (A → B → A
  // does not grant A another one).
  const [spent, setSpent] = useState<ReadonlySet<string>>(() => new Set());
  const spend = useCallback((k: string) => setSpent((prev) => (prev.has(k) ? prev : new Set(prev).add(k))), []);
  // Seconds elapsed since this very error arrived (updated by the timer only).
  const [elapsed, setElapsed] = useState<{ error: unknown; seconds: number } | null>(null);
  const auto = wait !== undefined && wait <= AUTO_RETRY_MAX_S && !spent.has(key);
  const { retry: again } = state;
  // One retry per failure, claimed synchronously: the timer and a tap in the same moment, or a
  // double tap, never ask twice.
  const retriedFor = useRef<unknown>(null);
  const retryOnce = useCallback(
    (failed: unknown) => {
      if (failed === null || retriedFor.current === failed) return;
      retriedFor.current = failed;
      spend(key);
      again();
    },
    [key, again, spend],
  );
  useEffect(() => {
    if (wait === undefined) return undefined;
    const start = Date.now();
    const tick = window.setInterval(() => setElapsed({ error, seconds: (Date.now() - start) / 1000 }), 1000);
    const retryAt = auto
      ? window.setTimeout(() => retryOnce(error), wait * 1000)
      : undefined;
    return () => {
      window.clearInterval(tick);
      window.clearTimeout(retryAt);
    };
  }, [error, wait, auto, retryOnce]);
  const passed = elapsed && elapsed.error === error ? elapsed.seconds : 0;
  const seconds = wait === undefined ? 0 : Math.max(0, Math.ceil(wait - passed));
  const retry = useCallback(() => retryOnce(error), [retryOnce, error]);
  return { busy: wait === undefined ? null : { seconds, auto }, retry };
}

/** The usual error view, or, while the source is busy, how long to wait (and the countdown). */
export function SourceErrorState({ busy, title, text, onRetry }: { busy: Busy | null; title: string; text: string; onRetry(): void }) {
  const { t } = useSettings();
  if (!busy) return <ErrorState title={title} text={text} onRetry={onRetry} />;
  const message = busy.auto
    ? t('busy.auto', { seconds: busy.seconds })
    : busy.seconds > 0
      ? t('busy.wait', { seconds: busy.seconds })
      : t('busy.ready');
  return (
    <>
      <ErrorState title={t('busy.title')} text={message} onRetry={onRetry} />
      {/* Announced once per state (not every second of the countdown). */}
      <p className={ui.srOnly} role="status" data-testid="busy-status">
        {t('busy.title')}. {busy.auto ? t('busy.autoAnnounce') : t('busy.manualAnnounce')}
      </p>
    </>
  );
}
