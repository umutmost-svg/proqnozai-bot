import { readFileSync } from 'node:fs';
import type { Page, Route } from '@playwright/test';
import { FORECAST_V3 } from '../src/test/fixtures/forecastV3';

/**
 * Answers for every /api call of the real-mode build. Synthetic by default (shapes made
 * by the backend's own code, see src/test/fixtures); M35_FIXTURES=<file> replays answers
 * the real backend produced offline from saved provider data (screenshots only).
 */
type Fixtures = Record<string, unknown>;

const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const PL = { id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org', emblem: null };
const CL = { id: 'fd:CL', sport: 'football', name: 'UEFA Champions League', area: 'Europe', provider: 'football-data.org', emblem: null };
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false, attribution: ATTR };
const side = (id: number, name: string, shortName: string) => ({ id: `fd:team:${id}`, name, shortName, crest: null });
const TEAMS = [side(354, 'Crystal Palace FC', 'Crystal Palace'), side(64, 'Liverpool FC', 'Liverpool'), side(57, 'Arsenal FC', 'Arsenal'),
  side(65, 'Manchester City FC', 'Man City'), side(61, 'Chelsea FC', 'Chelsea'), side(73, 'Tottenham Hotspur FC', 'Tottenham')];
const ev = (id: number, home: number, away: number, startsAt: string, tournamentId = 'fd:PL') => ({
  id: `fd:${id}`, sport: 'football', tournamentId, startsAt, status: 'upcoming', sourceStatus: 'TIMED', matchday: 11,
  home: TEAMS[home], away: TEAMS[away], score: null, halfTime: null, minute: null, awarded: false, stats: null,
  statsAvailability: 'unavailable', source,
});
// Kickoffs in the future: an analysis exists only for upcoming matches (the backend refuses after kickoff).
const EVENTS = [ev(4, 0, 1, '2099-11-21T15:00:00Z'), ev(5, 2, 3, '2099-11-21T17:30:00Z'), ev(6, 4, 5, '2099-11-22T14:00:00Z')];
const SEASON = { startDate: '2026-08-21', endDate: '2027-05-30', currentMatchday: 11 };
const row = (pos: number, t: number, pts: number, gd: number) => ({
  position: pos, team: TEAMS[t], played: 11, won: Math.floor(pts / 3), draw: pts % 3, lost: 11 - Math.floor(pts / 3) - (pts % 3),
  goalsFor: 20 + gd, goalsAgainst: 20, goalDifference: gd, points: pts,
});
const basisTeam = (name: string, form: string, goalsFor: number, goalsAgainst: number, pos: number, tablePoints: number) => ({
  name, matches: form.length, form, wins: form.split('W').length - 1, draws: form.split('D').length - 1,
  losses: form.split('L').length - 1, points: (form.split('W').length - 1) * 3 + (form.split('D').length - 1),
  goalsFor, goalsAgainst, tablePosition: pos, tablePoints,
});
const M = FORECAST_V3.markets;

function synthetic(): Fixtures {
  const calendar = { sport: 'football', coverage: COVERAGE, tournament: PL, season: SEASON, events: EVENTS,
    fetchedAt: '2026-11-08T06:00:00Z', stale: false, ai: { status: 'connected' } };
  const table = (tournament: typeof PL, blocks: unknown[]) => ({ sport: 'football', coverage: COVERAGE, tournament, season: SEASON,
    blocks, fetchedAt: '2026-11-08T06:00:00Z', stale: false });
  // Prices as the backend computes them (rounded to 0.1): with them the analysis is complete.
  const r1 = (x: number) => Math.round(x * 10) / 10;
  const quote = (market: keyof typeof M, odds: number, marketP: number) => ({ market, odds, impliedProbability: r1(100 / odds),
    marketProbability: marketP, modelProbability: M[market], edge: r1(M[market] - marketP),
    expectedValue: r1((M[market] / 100) * odds * 100 - 100), margin: 4.8 });
  const market = { fetchedAt: '2099-11-21T05:58:00Z', validFor: 840, quotes: [quote('1', 4.2, 22.6), quote('X', 3.9, 24.3), quote('2', 1.85, 53.1),
    quote('1X', 2.1, 46.9), quote('X2', 1.3, 77.4), quote('12', 1.32, 75.7), quote('O2.5', 1.8, 53), quote('U2.5', 2.05, 47),
    quote('BTTS_Y', 1.75, 54.6), quote('BTTS_N', 2.1, 45.4)] };
  const analysis = {
    schema: 'v3', eventId: 'fd:4', kind: 'ready', reasons: [], market, generatedAt: '2026-11-08T06:00:00Z', model: 'm',
    probabilities: { home: M['1'], draw: M.X, away: M['2'] }, factors: ['away_rating', 'away_form', 'home_advantage'],
    basis: { home: basisTeam('Crystal Palace FC', 'LLLWD', 3, 10, 19, 7), away: basisTeam('Liverpool FC', 'WWDWW', 11, 3, 2, 25) },
    estimated: false, pick: { market: 'X2', reasons: ['away_stronger_rating', 'away_better_form'], probability: M.X2 },
    alternatives: [{ market: 'H2+1.5', probability: M['H2+1.5'] }], forecast: FORECAST_V3,
  };
  return {
    '/api/sports/coverage': { sports: { football: { ...COVERAGE, tournaments: [PL, CL] } }, ai: { status: 'connected' } },
    '/api/sports/football/tournaments': { sport: 'football', coverage: COVERAGE, tournaments: [PL, CL] },
    '/api/sports/football/tournaments/fd:PL/calendar': calendar,
    '/api/sports/football/tournaments/fd:PL/standings': table(PL, [{ stage: 'REGULAR_SEASON', group: 'Matchday',
      rows: [row(1, 2, 25, 14), row(2, 3, 25, 16), row(3, 1, 23, 9), row(4, 4, 20, 5), row(5, 5, 17, 1), row(6, 0, 7, -12)] }]),
    '/api/sports/football/tournaments/fd:CL/standings': table(CL, [{ stage: 'LEAGUE_STAGE', group: 'League phase',
      rows: [row(1, 1, 12, 9), row(2, 2, 12, 8), row(3, 3, 10, 6)] }]),
    '/api/sports/event/fd:4': { event: EVENTS[0], tournament: PL, coverage: COVERAGE, ai: { status: 'connected' } },
    'POST /api/ai/analysis fd:4': analysis,
    // A match already under way: the backend answers "not upcoming" and nothing else.
    '/api/sports/event/fd:7': { event: { ...ev(7, 1, 2, '2020-01-01T15:00:00Z'), status: 'live' }, tournament: PL, coverage: COVERAGE,
      ai: { status: 'connected' } },
    'POST /api/ai/analysis fd:7': { schema: 'v3', eventId: 'fd:7', kind: 'insufficient', reasons: ['not_upcoming'], generatedAt: null,
      model: null, probabilities: null, factors: [], basis: null, estimated: false, pick: null, forecast: null, alternatives: [] },
  };
}

export function fixtures(): Fixtures {
  const file = process.env.M35_FIXTURES;
  return file ? (JSON.parse(readFileSync(file, 'utf8')) as Fixtures) : synthetic();
}

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/** Installs a fake Telegram client and answers /api from `data`; returns the list of /api paths asked. */
export async function mockApi(page: Page, data: Fixtures, lang = 'ru'): Promise<string[]> {
  const asked: string[] = [];
  const created = new Set<string>();
  await page.route('https://telegram.org/**', (r) => r.abort());
  await page.addInitScript((l) => {
    localStorage.setItem('sporai.settings.v1', JSON.stringify({ lang: l, timeZone: 'auto', reduceMotion: true, haptics: true }));
    (window as unknown as { Telegram: unknown }).Telegram = {
      WebApp: { initData: 'query_id=x&user=%7B%7D&auth_date=1&hash=x', initDataUnsafe: {}, isVersionAtLeast: () => false,
        ready: () => {}, close: () => {}, expand: () => {} },
    };
  }, lang);
  const session = { user: { id: 42, first_name: 'Test', last_name: null, username: null, lang },
    session: { expires_at: '2099-01-01T00:00:00Z', csrf_token: 'csrf' } };
  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = decodeURIComponent(url.pathname);
    asked.push(`${req.method()} ${path}`);
    if (path === '/api/me') return json(route, { error: 'unauthenticated' }, 401);
    if (path === '/api/auth/session') return json(route, session);
    if (path === '/api/me/language') return json(route, { ok: true });
    if (req.method() === 'GET' && path === '/api/ai/analysis') {
      // Read-only, like the backend: an answer that costs nothing (insufficient) is given at once,
      // a real analysis only after it was created (the screen's explicit button).
      const id = url.searchParams.get('event_id') ?? '';
      const made = data[`POST /api/ai/analysis ${id}`] as { kind?: string } | undefined;
      if (made && (made.kind === 'insufficient' || created.has(id))) return json(route, made);
      return json(route, { error: 'ai_not_created' }, 404);
    }
    if (req.method() === 'POST' && path === '/api/ai/analysis') {
      const id = (req.postDataJSON() as { event_id: string }).event_id;
      if (data[`POST /api/ai/analysis ${id}`]) created.add(id);
      return json(route, data[`POST /api/ai/analysis ${id}`] ?? { error: 'not_found' }, data[`POST /api/ai/analysis ${id}`] ? 200 : 404);
    }
    if (path.startsWith('/api/crests/')) return route.fulfill({ status: 404, body: '' });
    const teamMatches = /^\/api\/sports\/football\/teams\/(.+)\/matches$/.exec(path);
    if (teamMatches) return json(route, teamPage(data, teamMatches[1]!));
    if (path === '/api/sports/football/events') return json(route, dayEvents(data));
    if (path === '/api/sports/football/top') return json(route, topOfDay(data, url.searchParams));
    if (path === '/api/quick/pick') return json(route, quickAnswer(data, url.searchParams));
    if (path === '/api/sports/football/search') {
      return json(route, { sport: 'football', query: url.searchParams.get('q'), teams: [], events: [],
        coverage: { indexed: [], total: 0, complete: true }, tournaments: [], source: COVERAGE });
    }
    if (/^\/api\/sports\/(tennis|basketball|mma)\/tournaments$/.test(path)) {
      return json(route, { sport: path.split('/')[3], coverage: { status: 'not_connected', provider: null, attribution: null }, tournaments: [] });
    }
    if (path in data) return json(route, data[path]);
    const event = /^\/api\/sports\/event\/(.+)$/.exec(path);
    if (event) {
      const cal = calendars(data).find((c) => c.events.some((e) => e.id === event[1]));
      const found = cal?.events.find((e) => e.id === event[1]);
      if (cal && found) return json(route, { event: found, tournament: cal.tournament, coverage: COVERAGE, ai: { status: 'connected' } });
    }
    return json(route, { error: 'not_found' }, 404);
  });
  return asked;
}

type Calendar = { events: { id: string; home: { id: string }; away: { id: string } }[]; tournament: unknown; coverage: unknown };

function calendars(data: Fixtures): Calendar[] {
  return Object.entries(data).filter(([k]) => k.endsWith('/calendar')).map(([, v]) => v as Calendar);
}

function dayEvents(data: Fixtures) {
  const all = calendars(data).flatMap((c) => c.events).slice(0, 6);
  return { sport: 'football', coverage: COVERAGE, events: all, tournaments: calendars(data).map((c) => c.tournament),
    fetchedAt: '2026-11-08T06:00:00Z', stale: false, ai: { status: 'connected' } };
}

function teamPage(data: Fixtures, teamId: string) {
  const cal = calendars(data);
  const events = cal.flatMap((c) => c.events).filter((e) => e.home.id === teamId || e.away.id === teamId);
  const team = events[0] ? (events[0].home.id === teamId ? events[0].home : events[0].away) : { id: teamId, name: teamId, shortName: null, crest: null };
  return { sport: 'football', team: { ...team, tournaments: ['fd:PL'] }, events, tournaments: cal.map((c) => c.tournament),
    coverage: { indexed: ['fd:PL'], total: 1, complete: true }, source: COVERAGE };
}

/** Recorded top matches (real data), else the day listing's matches inside the asked range. */
function topOfDay(data: Fixtures, q: URLSearchParams) {
  if (data['/api/sports/football/top']) return data['/api/sports/football/top'];
  const from = Date.parse(q.get('from')!);
  const to = Date.parse(q.get('to')!);
  const day = dayEvents(data);
  const top = (day.events as unknown as { startsAt: string }[])
    .filter((e) => Date.parse(e.startsAt) >= from && Date.parse(e.startsAt) < to)
    .slice(0, 5)
    .map((event, i) => ({ event, leaders: i === 0 }));
  return { sport: 'football', coverage: COVERAGE, top, tournaments: day.tournaments, fetchedAt: day.fetchedAt, stale: false, ai: day.ai };
}

// Picks exist only on the match's UTC day: the third leg is another match of 21 November;
// EVENTS[2] (22 November) is counted as "not on match day" and never offered.
const QUICK_LEGS = [
  { event: EVENTS[0], market: 'X2', family: 'double', probability: M.X2 },
  { event: EVENTS[1], market: '1', family: '1x2', probability: 62 },
  { event: ev(8, 4, 5, '2099-11-21T19:45:00Z'), market: 'HO0.5', family: 'team_total', probability: 81 },
];
const RISK_FLOOR: Record<string, number> = { conservative: 70, balanced: 60, elevated: 55 };

/** Recorded quick answers (real data, "<mode>" / "coupon:<size>"), else a synthetic one of the asked shape. */
function quickAnswer(data: Fixtures, q: URLSearchParams) {
  const mode = q.get('mode')!;
  const size = mode === 'coupon' ? Number(q.get('size')) : 1;
  const recorded = data[`GET /api/quick/pick ${mode === 'coupon' ? `coupon:${size}` : 'single'}`];
  if (recorded) return recorded;
  const market = q.get('market') ?? 'any';
  const only = q.get('tournaments')?.split(',');
  const floor = RISK_FLOOR[q.get('risk') ?? 'balanced'] ?? 60;
  const fit = QUICK_LEGS.filter((l) => (market === 'any' || l.family === market) && l.probability >= floor
    && (!only || only.includes(l.event.tournamentId))).map((l) => ({
    ...l, leagueRate: 46, expectedGoals: { home: 1.21, away: 1.64 }, sample: 230, teamMatches: { home: 22, away: 23 },
    dataUntil: '2026-11-06T20:00:00Z', price: null,
  }));
  const ok = fit.length >= size;
  return {
    mode, status: ok ? 'ok' : mode === 'coupon' ? 'not_enough' : 'no_bet', legs: ok ? fit.slice(0, size) : [], size,
    suitable: fit.length, combined: ok && mode === 'coupon' ? bounds(fit.slice(0, size).map((l) => l.probability)) : null,
    counts: { matches: 4, forecasts: 3 }, reasons: { early_forecast: 1, ...(fit.length < 3 ? { no_qualifying_market: 3 - fit.length } : {}) },
    unavailableTournaments: [], uncheckedDays: 0,
    // The next-day match only when the asked period reaches it.
    nextMatchAt: Date.parse(q.get('to')!) > Date.parse('2099-11-22T14:00:00Z') ? '2099-11-22T14:00:00Z' : null,
    couponOdds: null, experimental: true, modelVersion: 'goals-1', computedAt: '2099-11-21T06:00:00Z',
  };
}

function bounds(p: number[]): [number, number] {
  return [Math.max(0, p.reduce((a, x) => a + x, 0) - 100 * (p.length - 1)), Math.min(...p)];
}
