import type { AuthController } from './controller';

/** Resolves once the sign-in check has settled: true when there is a session. */
export function waitForSession(auth: AuthController): Promise<boolean> {
  return new Promise((resolve) => {
    const settle = () => {
      const { status } = auth.getState();
      if (status === 'checking') return false;
      resolve(status === 'authenticated');
      return true;
    };
    if (settle()) return;
    const unsubscribe = auth.subscribe(() => {
      if (settle()) unsubscribe();
    });
  });
}
