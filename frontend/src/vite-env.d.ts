/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "demo" (default) or "real"; see src/data/mode.ts. */
  readonly VITE_DATA_MODE?: string;
  /** Public Telegram username of the support account (no "@"); unset = support not set up yet. */
  readonly VITE_SUPPORT_TELEGRAM?: string;
}

declare const __APP_VERSION__: string;

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
