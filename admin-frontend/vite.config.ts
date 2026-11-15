/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The admin panel is served by its own backend process (miniapp.admin_web) from the same origin:
// build with `npm run build` and open the backend's address. There is no dev-server proxy on purpose —
// the backend accepts only its own Host/Origin (127.0.0.1:<port>).

export default defineConfig({
  plugins: [react()],
  base: '/',
  server: { host: '127.0.0.1', port: 5174, strictPort: true },
  build: { assetsInlineLimit: 0, sourcemap: false },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
