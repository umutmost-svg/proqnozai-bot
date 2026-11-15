import { defineConfig } from '@playwright/test';

/**
 * Real-mode build (VITE_DATA_MODE=real) with the whole /api answered inside the
 * browser by the spec (page.route): the real data provider, parsing and screens run,
 * but no request reaches football-data.org, Anthropic or any backend.
 */
const PORT = 4175;

export default defineConfig({
  testDir: './e2e-real',
  outputDir: './test-results/real',
  timeout: 45_000,
  fullyParallel: true,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: 'chrome',
    timezoneId: 'Asia/Baku',
    locale: 'ru-RU',
  },
  webServer: {
    command: `tsc -b && vite build --outDir dist-real && vite preview --outDir dist-real --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: false,
    timeout: 180_000,
    env: { VITE_DATA_MODE: 'real' },
  },
});
