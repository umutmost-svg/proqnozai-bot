import { useEffect, useSyncExternalStore } from 'react';
import { useServices } from '../state/contexts';

export function useAuth() {
  const { auth } = useServices();
  return { state: useSyncExternalStore(auth.subscribe, auth.getState), auth };
}

/** Starts the one-time bootstrap; safe under StrictMode double effects. */
export function useAuthBootstrap() {
  const { auth } = useServices();
  useEffect(() => {
    void auth.start();
  }, [auth]);
}
