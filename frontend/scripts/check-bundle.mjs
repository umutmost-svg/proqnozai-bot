// Fails if the production build references temporary Figma URLs or looks like it contains secrets.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const DIST = new URL('../dist/', import.meta.url).pathname;
const RULES = [
  { name: 'temporary Figma asset URL', re: /figma\.com\/api\/mcp|figma\.com\/api\/|mcp\/asset/i },
  { name: 'Telegram bot token', re: /\b\d{8,10}:[A-Za-z0-9_-]{35}\b/ },
  { name: 'Anthropic API key', re: /sk-ant-[A-Za-z0-9_-]{10,}/ },
  { name: 'test-only initData signing key', re: /AAtest-only-not-a-real-token|WebAppData/ },
  // Sports providers are called only by the backend: no provider host, key header or key variable here.
  { name: 'sports provider called from the browser', re: /api\.football-data\.org|X-Auth-Token|x-apisports-key|MINIAPP_FOOTBALL_DATA_KEY/i },
  { name: 'secret env name', re: /\b(BOT_TOKEN|TELEGRAM_TOKEN|MINIAPP_BOT_TOKEN|ANTHROPIC_API_KEY|FOOTBALL_KEY|FOOTBALL_API_KEY|MOSTBET_[A-Z_]*KEY)\b/ },
];

const files = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path);
    else files.push(path);
  }
};
walk(DIST);

let problems = 0;
for (const file of files) {
  const text = readFileSync(file, 'utf8');
  for (const rule of RULES) {
    if (rule.re.test(text)) {
      console.error(`✗ ${rule.name}: ${file.replace(DIST, 'dist/')}`);
      problems++;
    }
  }
}
console.log(`Checked ${files.length} files in dist/: ${problems ? `${problems} problem(s)` : 'no secrets or temporary Figma URLs'}`);
process.exit(problems ? 1 : 0);
