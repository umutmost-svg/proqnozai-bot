/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The admin panel is served by its own backend process (miniapp.admin_web) from the same
// origin; in dev Vite forwards /api to it on loopback, keeping the browser Origin header.
const apiTarget = process.env.ADMIN_API_PROXY ?? 'http://127.0.0.1:8795';

export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: { '/api': { target: apiTarget, changeOrigin: false, xfwd: false } },
  },
  build: { assetsInlineLimit: 0, sourcemap: false },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
