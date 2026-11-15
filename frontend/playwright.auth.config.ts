import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineConfig } from '@playwright/test';

/**
 * Auth end-to-end in the stand topology: the Mini App backend itself serves the
 * built frontend (MINIAPP_STATIC_DIR) and /api on ONE origin, with its CSP and
 * security headers, exactly as in the container. Temp SQLite, test-only bot
 * token: initData is signed inside the test, never in the app.
 */
export const E2E_BOT_TOKEN = '7000000001:AAtest-only-not-a-real-token';
const WEB_PORT = 4174;
const dbDir = process.env.SPORAI_E2E_DB_DIR ?? mkdtempSync(join(tmpdir(), 'sporai-auth-e2e-'));
process.env.SPORAI_E2E_DB_DIR = dbDir;

export default defineConfig({
  testDir: './e2e-auth',
  outputDir: './test-results/auth',
  timeout: 30_000,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    channel: 'chrome',
    timezoneId: 'Asia/Baku',
    locale: 'ru-RU',
  },
  webServer: [
    {
      command:
        'npm --prefix frontend run build && miniapp/.venv/bin/python -m uvicorn --factory miniapp.asgi:create_app_from_env ' +
        `--host 127.0.0.1 --port ${WEB_PORT} --no-proxy-headers`,
      cwd: '..',
      url: `http://127.0.0.1:${WEB_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        MINIAPP_ENV: 'development',
        MINIAPP_BOT_TOKEN: E2E_BOT_TOKEN,
        MINIAPP_DB_PATH: join(dbDir, 'miniapp-e2e.db'),
        MINIAPP_TRUSTED_ORIGINS: `http://127.0.0.1:${WEB_PORT}`,
        MINIAPP_STATIC_DIR: 'frontend/dist',
        MINIAPP_CSP_MODE: 'enforce',
      },
    },
  ],
});
