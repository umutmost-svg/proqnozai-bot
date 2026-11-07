/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "demo" (default) or "real"; see src/data/mode.ts. */
  readonly VITE_DATA_MODE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
