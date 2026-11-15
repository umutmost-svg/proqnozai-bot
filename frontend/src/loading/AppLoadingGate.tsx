/**
 * Covers the app until the first screen can be used, driven by BootTracker:
 *
 *   0 – SHOW_AFTER_MS      plain background only. A fast boot never shows the
 *                          splash (no flash of splash or of skeletons).
 *   SHOW_AFTER_MS – …      AppLoadingScreen; leaves as soon as the boot settles,
 *                          with a short fade (no minimum display time).
 *   SLOW_AFTER_MS          "taking longer than usual"; offline is said at once.
 *   FAIL_AFTER_MS          stops waiting: retry, or open the app without waiting.
 *
 * The app renders underneath from the start (inert while covered), so the first
 * screen's requests run in parallel and nothing remounts at hand-over. A failed
 * first request ends the wait: the screen shows its own error with retry.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { useAuth } from '../auth/useAuth';
import { useServices, useSettings } from '../state/contexts';
import { AppLoadingScreen, type LoadingHint, type LoadingSubject } from './AppLoadingScreen';
import s from './AppLoadingGate.module.css';

export const SHOW_AFTER_MS = 250;
export const SLOW_AFTER_MS = 4000;
export const FAIL_AFTER_MS = 12000;
export const EXIT_MS = 240;

type Mode = 'cover' | 'splash' | 'exit' | 'done';
type Wait = 'normal' | 'slow' | 'failed';

const isOffline = () => typeof navigator !== 'undefined' && navigator.onLine === false;
const isVisible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden';

export function AppLoadingGate({ children }: { children: ReactNode }) {
  const { boot, auth } = useServices();
  const { motionReduced } = useSettings();
  const { state: authState } = useAuth();
  const location = useLocation();
  // key alone is not enough: a hash changed from outside the router is a POP
  // with key 'default', the same as the first entry.
  const screenKey = `${location.key}|${location.pathname}|${location.search}`;
  const snapshot = useSyncExternalStore(boot.subscribe, boot.getSnapshot);

  const [requested, setMode] = useState<Mode>('cover');
  // Settled: leave now. From the plain cover there is nothing to fade.
  const mode: Mode = snapshot.settled
    ? requested === 'cover' ? 'done' : requested === 'splash' ? 'exit' : requested
    : requested;
  const [wait, setWait] = useState<Wait>('normal');
  const [offline, setOffline] = useState(isOffline);
  const [visible, setVisible] = useState(isVisible);
  // Bumped on retry: the wait is measured anew.
  const [epoch, setEpoch] = useState(0);
  // Waiting time counts only while visible: `waited` holds finished visible
  // stretches, `visibleSince` the current one (null while in the background).
  const waited = useRef(0);
  const visibleSince = useRef<number | null>(null);
  const elapsed = () => waited.current + (visibleSince.current === null ? 0 : Date.now() - visibleSince.current);

  useEffect(() => {
    visibleSince.current = isVisible() ? Date.now() : null;
  }, []);

  useEffect(() => {
    if (authState.status !== 'checking') boot.markSessionChecked();
  }, [authState.status, boot]);

  // Effects of the screen ran before this one: its boot requests are known.
  // Keyed by location: navigating while covered (e.g. Telegram's BackButton on a
  // deep link) runs this cleanup before the new screen's effects, so the screen
  // that is actually shown is the one waited for.
  useEffect(() => {
    boot.closeRegistration();
    return () => boot.reopenRegistration();
  }, [boot, screenKey]);

  useEffect(() => {
    if (mode === 'cover') {
      const id = window.setTimeout(() => setMode('splash'), SHOW_AFTER_MS);
      return () => window.clearTimeout(id);
    }
    if (mode === 'exit') {
      // A timer, not transitionend: with reduced motion there is no transition at all.
      const id = window.setTimeout(() => setMode('done'), EXIT_MS);
      return () => window.clearTimeout(id);
    }
    return undefined;
  }, [mode]);

  // Long waits. Measured only while the Mini App is visible: a WebView frozen in
  // the background must not come back straight into the "failed" view.
  useEffect(() => {
    if (mode !== 'splash' || snapshot.settled || !visible || wait === 'failed') return undefined;
    const spent = elapsed();
    const timers: number[] = [];
    if (wait === 'normal') timers.push(window.setTimeout(() => setWait('slow'), Math.max(0, SLOW_AFTER_MS - spent)));
    timers.push(window.setTimeout(() => setWait('failed'), Math.max(0, FAIL_AFTER_MS - spent)));
    return () => timers.forEach((id) => window.clearTimeout(id));
    // `visible` and `epoch` re-arm the timers; `elapsed` reads refs only.
  }, [mode, snapshot.settled, visible, wait, epoch]);

  const retry = useCallback(() => {
    waited.current = 0;
    visibleSince.current = isVisible() ? Date.now() : null;
    setWait('normal');
    setEpoch((e) => e + 1);
    boot.retryData();
    void auth.retry();
  }, [auth, boot]);

  useEffect(() => {
    // Back online only clears the hint. Requests in flight keep running (each is
    // bounded), and the wait limit keeps counting: an automatic restart on every
    // flap of a mobile network could postpone the failed view forever.
    const onOnline = () => setOffline(false);
    const onOffline = () => setOffline(true);
    const onVisibility = () => {
      const now = isVisible();
      if (now && visibleSince.current === null) visibleSince.current = Date.now();
      if (!now && visibleSince.current !== null) {
        waited.current += Date.now() - visibleSince.current;
        visibleSince.current = null;
      }
      setVisible(now);
    };
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  const covering = mode !== 'done';
  const hint: LoadingHint = offline ? 'offline' : wait === 'slow' ? 'slow' : 'none';
  const waitingForIdentity = snapshot.pending.includes('telegram') || snapshot.pending.includes('session');
  const subject: LoadingSubject = location.pathname.endsWith('/ai')
    ? 'analysis'
    : location.pathname.startsWith('/event/')
      ? 'event'
      : 'matches';

  return (
    <>
      <div className={s.app} inert={covering}>
        {children}
      </div>
      {covering && (
        <div className={s.overlay} data-testid="app-loading-gate" data-mode={mode}>
          {mode !== 'cover' && (
            <AppLoadingScreen
              phase={waitingForIdentity ? 'connecting' : 'data'}
              subject={subject}
              hint={hint}
              view={wait === 'failed' ? 'failed' : 'loading'}
              reducedMotion={motionReduced}
              onRetry={retry}
              onContinue={() => setMode('exit')}
            />
          )}
        </div>
      )}
    </>
  );
}
