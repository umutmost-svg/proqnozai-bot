/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';

const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string };

// Same-origin topology: the page and /api share one origin. Locally Vite
// forwards /api to the Mini App backend (uvicorn, loopback only); the browser
// Origin header is passed through unchanged so the backend's check applies.
const apiTarget = process.env.MINIAPP_API_PROXY ?? 'http://127.0.0.1:8000';
const apiProxy = { '/api': { target: apiTarget, changeOrigin: false, xfwd: false } };

export default defineConfig({
  plugins: [react()],
  // App version for support reports (public, not a secret).
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  // Relative base: the Mini App can be served from any sub-path.
  base: './',
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy: apiProxy },
  // Preview proxies /api only when asked (auth e2e); the M1 layout e2e runs without a backend.
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    // `{}` matters: preview otherwise inherits server.proxy.
    proxy: process.env.MINIAPP_API_PROXY ? apiProxy : {},
  },
  build: {
    // Keep SVG emblems as files instead of inlining them into JS.
    assetsInlineLimit: 0,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: { modules: { classNameStrategy: 'non-scoped' } },
  },
});
