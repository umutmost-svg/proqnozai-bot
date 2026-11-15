/**
 * "Signed out in this launch" marker. Survives a WebView reload (sessionStorage),
 * so a reload after logout does not start the bootstrap again.
 *
 * Bound to the launch: only a SHA-256 fingerprint of the launch initData is
 * stored, never the initData itself. A new launch (Mini App reopened) brings new
 * initData, does not match, and signs in normally, so the block is never permanent.
 * The server burns the same launch on logout; this marker only avoids asking.
 */
export interface SignedOutMarker {
  matches(initData: string): Promise<boolean>;
  remember(initData: string): Promise<void>;
}

const KEY = 'sporai.signedOutLaunch';

async function fingerprint(initData: string): Promise<string | null> {
  try {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(initData));
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
  } catch {
    return null; // no WebCrypto: fall back to the in-memory flag and the server-side burn
  }
}

export function createSessionStorageMarker(storage: () => Storage = () => window.sessionStorage): SignedOutMarker {
  return {
    async matches(initData) {
      const fp = await fingerprint(initData);
      try {
        return fp !== null && storage().getItem(KEY) === fp;
      } catch {
        return false;
      }
    },
    async remember(initData) {
      const fp = await fingerprint(initData);
      if (fp === null) return;
      try {
        storage().setItem(KEY, fp);
      } catch {
        // Storage disabled: the in-memory flag and the server-side burn still apply.
      }
    },
  };
}

/** For pages without persistence (tests, outside Telegram). */
export const noSignedOutMarker: SignedOutMarker = {
  matches: async () => false,
  remember: async () => {},
};
