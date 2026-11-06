import { useCallback, useContext, useEffect, useId, useState } from 'react';
import { ServicesContext } from '../state/contexts';

export type AsyncState<T> =
  | { status: 'loading' }
  | { status: 'success'; data: T }
  | { status: 'error'; error: unknown };

/**
 * Runs `load` whenever `key` changes; stale responses are dropped.
 * `retry` re-runs the same request without touching the inputs.
 */
export function useAsync<T>(load: () => Promise<T>, key: string): AsyncState<T> & { retry(): void } {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ key: string; value: AsyncState<T> }>({ key: '', value: { status: 'loading' } });

  useEffect(() => {
    let cancelled = false;
    load().then(
      (data) => !cancelled && setState({ key: `${key}#${attempt}`, value: { status: 'success', data } }),
      (error: unknown) => !cancelled && setState({ key: `${key}#${attempt}`, value: { status: 'error', error } }),
    );
    return () => {
      cancelled = true;
    };
    // `load` is intentionally keyed by `key`: callers pass a fresh closure every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt]);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);
  const current: AsyncState<T> = state.key === `${key}#${attempt}` ? state.value : { status: 'loading' };

  // Requests of the first screen are what the loading screen waits for (src/loading).
  const boot = useContext(ServicesContext)?.boot;
  const id = useId();
  useEffect(() => (boot ? () => boot.forgetTask(id) : undefined), [boot, id]);
  useEffect(() => {
    boot?.reportTask(id, current.status === 'loading' ? 'pending' : current.status === 'success' ? 'done' : 'failed', retry);
  }, [boot, id, current.status, retry]);

  return { ...current, retry };
}
