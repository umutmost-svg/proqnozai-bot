// Audit of every tournament page on a running stand, at iPhone widths (NOT part of CI; real data).
//   STAND_URL=https://<stand> STAND_BOT_TOKEN_FILE=<test bot token file> node scripts/stand-audit.mjs [out.json]
// Signs initData for a synthetic test user with the STAND's test bot token (HMAC), opens the app in a fake
// Telegram WebApp, then for each tournament x width x tab measures: horizontal overflow (scrollWidth vs viewport),
// elements sticking out on the left/right, emblem state (image / initials fallback), the language of names.
import { createHmac } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { chromium } from '@playwright/test';

const URL0 = process.env.STAND_URL;
const TOKEN = readFileSync(process.env.STAND_BOT_TOKEN_FILE, 'utf8').trim();
const WIDTHS = (process.env.WIDTHS ?? '320,375,390,430').split(',').map(Number);
const ONLY = process.env.ONLY ? process.env.ONLY.split(',') : null;
const out = process.argv[2] ?? 'stand-audit.json';

function initData(user) {
  const fields = { auth_date: String(Math.floor(Date.now() / 1000)), query_id: 'AAaudit', user: JSON.stringify(user) };
  const dcs = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(TOKEN).digest();
  return new URLSearchParams({ ...fields, hash: createHmac('sha256', secret).update(dcs).digest('hex') }).toString();
}

const measure = () => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.left < -1 || r.right > vw + 1) {
      // skip children of horizontally scrollable strips (they scroll by design)
      let p = el.parentElement, inScroller = false;
      while (p && p !== document.body) {
        const ps = getComputedStyle(p);
        if ((ps.overflowX === 'auto' || ps.overflowX === 'scroll') && p.scrollWidth > p.clientWidth) { inScroller = true; break; }
        p = p.parentElement;
      }
      if (inScroller) continue;
      offenders.push({ tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 60), testid: el.getAttribute('data-testid'),
        left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width), text: (el.textContent || '').trim().slice(0, 40) });
    }
  }
  // Containers shifted sideways (scrollLeft > 0) that are NOT horizontal strips meant to scroll.
  const shifted = [];
  for (const el of [document.documentElement, document.body, ...document.querySelectorAll('body *')]) {
    if (el.scrollLeft > 0) {
      const st = getComputedStyle(el);
      const strip = (st.overflowX === 'auto' || st.overflowX === 'scroll') && el.getBoundingClientRect().height < 160;
      if (!strip) shifted.push({ tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 40), scrollLeft: Math.round(el.scrollLeft), overflowX: st.overflowX });
    }
  }
  return { shifted, vw, scrollWidth: doc.scrollWidth, bodyScroll: document.body.scrollWidth, scrollX: window.scrollX, offenders: offenders.slice(0, 8), count: offenders.length };
};

const browser = await chromium.launch({ channel: 'chrome' });
const results = [];
const user = { id: 919900001, first_name: 'Audit', language_code: 'ru' };
let tournaments = null;
for (const width of WIDTHS) {
  const ctx = await browser.newContext({ viewport: { width, height: 800 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true, locale: 'ru-RU',
    timezoneId: 'Asia/Baku',
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Telegram-iOS' });
  const data = initData(user);
  await ctx.addInitScript((d) => {
    window.Telegram = { WebApp: { initData: d, initDataUnsafe: {}, ready() {}, expand() {}, platform: 'ios', version: '8.0',
      colorScheme: 'light', themeParams: {}, onEvent() {}, offEvent() {}, BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
      HapticFeedback: { impactOccurred() {}, selectionChanged() {}, notificationOccurred() {} },
      safeAreaInset: { top: 47, bottom: 34, left: 0, right: 0 }, contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 } } };
  }, data);
  const page = await ctx.newPage();
  // Launch like Telegram does: initData in the URL hash (telegram-web-app.js reads it at load).
  const launch = `tgWebAppData=${encodeURIComponent(data)}&tgWebAppVersion=8.0&tgWebAppPlatform=ios&tgWebAppThemeParams=%7B%7D`;
  await page.goto(`${URL0}/#${launch}`, { waitUntil: 'networkidle' });
  for (let i = 0; i < 20 && !(await ctx.cookies()).length; i++) await page.waitForTimeout(500);
  if (!tournaments) {
    const r = await page.request.get(`${URL0}/api/sports/football/tournaments`);
    if (!r.ok()) { console.log("tournaments status", r.status(), (await r.text()).slice(0, 120)); console.log("cookies", (await ctx.cookies()).map((c) => c.name)); process.exit(1); }
    tournaments = (await r.json()).tournaments.map((t) => ({ id: t.id, name: t.name, area: t.area, emblem: t.emblem, featured: t.featured }));
    if (ONLY) tournaments = tournaments.filter((t) => ONLY.includes(t.id));
  }
  for (const t of tournaments) {
    for (const tab of ['matches', 'table']) {
      const hash = `#/tournament/${encodeURIComponent(t.id)}${tab === 'table' ? '?tab=table' : ''}`;
      await page.goto(`${URL0}/${hash}`, { waitUntil: 'networkidle' }).catch(() => {});
      await page.waitForTimeout(600);
      const m = await page.evaluate(measure);
      const head = await page.evaluate(() => {
        const h = document.querySelector('h1, [data-testid="tournament-title"]');
        const img = document.querySelector('header img, [data-testid="tournament-emblem"] img');
        return { title: h ? h.textContent.trim() : null, emblemImg: img ? img.getAttribute('src') : null };
      });
      results.push({ width, id: t.id, name: t.name, area: t.area, tab, emblemApi: t.emblem, ...head, ...m });
      if (m.shifted.length || m.scrollWidth > width + 1) process.stdout.write(`SHIFTED ${width} ${t.id} ${tab} ${JSON.stringify(m.shifted)}\n`);
    }
  }
  await ctx.close();
}
await browser.close();
writeFileSync(out, JSON.stringify({ tournaments, results }, null, 1));
const bad = results.filter((r) => r.shifted.length || r.scrollWidth > r.width + 1);
console.log(`pages ${results.length}, with overflow ${bad.length}, tournaments ${tournaments.length}`);
