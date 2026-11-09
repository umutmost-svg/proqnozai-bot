import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BackendSportsDataProvider } from '../data/BackendSportsDataProvider';
import { DataError } from '../data/provider';
import { LANGS, dictionaries } from '../i18n';
import { renderApp } from './renderApp';

/** The data source's request limit (Retry-After, one automatic retry) and the tournament's name on a direct link. */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const PL = { id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org' };
const SEASON = { startDate: '2026-08-21', endDate: '2027-05-30', currentMatchday: 11 };
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const busy = (retryAfter: number | null, headers: Record<string, string> = {}) =>
  new Response(
    JSON.stringify({ error: 'source_unavailable', reason: 'rate_limited', ...(retryAfter === null ? {} : { retryAfter }) }),
    { status: 503, headers },
  );
const down = () => new Response(JSON.stringify({ error: 'source_unavailable', reason: 'timeout' }), { status: 503 });
const row = (pos: number, id: number, name: string) => ({
  position: pos, team: { id: `fd:team:${id}`, name, shortName: null, crest: null },
  played: 11, won: 7, draw: 2, lost: 2, goalsFor: 20, goalsAgainst: 10, goalDifference: 10, points: 23,
});
const TABLE = { sport: 'football', coverage: COVERAGE, tournament: PL, season: SEASON, fetchedAt: '2026-11-08T06:00:00Z', stale: false,
  blocks: [{ stage: 'REGULAR_SEASON', group: null, rows: [row(1, 57, 'Arsenal'), row(2, 65, 'Man City')] }] };
const CALENDAR = { sport: 'football', coverage: COVERAGE, tournament: PL, season: SEASON, events: [], fetchedAt: '2026-11-08T06:00:00Z',
  stale: false, ai: { status: 'connected' } };
const LIST = { sport: 'football', coverage: COVERAGE, tournaments: [PL] };

type Answer = () => Response | Promise<Response>;
function backend(over: { standings?: Answer; calendar?: Answer; tournaments?: Answer; events?: Answer; top?: Answer } = {}) {
  const fetchImpl = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (url.endsWith('/standings')) return (over.standings ?? (() => ok(TABLE)))();
    if (url.endsWith('/calendar')) return (over.calendar ?? (() => ok(CALENDAR)))();
    if (url === '/api/sports/football/tournaments') return (over.tournaments ?? (() => ok(LIST)))();
    if (url.startsWith('/api/sports/football/top')) return (over.top ?? (() => ok({ ...LIST, top: [], ai: { status: 'connected' } })))();
    if (url.startsWith('/api/sports/football/events')) return (over.events ?? (() => ok({ ...LIST, events: [] })))();
    return ok({ ...LIST, events: [] });
  });
  return { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }), fetchImpl };
}
const calls = (f: ReturnType<typeof vi.fn>, part: string) => f.mock.calls.filter(([u]) => String(u).includes(part)).length;
const never = () => new Promise<Response>(() => undefined);

describe('a busy data source', () => {
  it('reads the wait from the body, else from Retry-After; other failures carry none', async () => {
    const error = async (answer: Answer) => {
      const { data } = backend({ standings: answer });
      return data.getStandings('fd:PL').then(() => null, (e: unknown) => e as DataError);
    };
    expect((await error(() => busy(7)))?.retryAfter).toBe(7);
    expect((await error(() => busy(null, { 'Retry-After': '12' })))?.retryAfter).toBe(12);
    const invalid = await error(() => busy(-3, { 'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT' }));
    expect(invalid?.retryAfter).toBeUndefined();
    expect(invalid?.reason).toBe('source_unavailable');
    expect((await error(() => busy(99999)))?.retryAfter).toBeUndefined();
    expect((await error(down))?.retryAfter).toBeUndefined();
    const quota = () => new Response(JSON.stringify({ error: 'source_unavailable', reason: 'quota_exhausted', retryAfter: 60 }), { status: 503 });
    expect((await error(quota))?.retryAfter).toBeUndefined();
  });

  it('the table says how long to wait, asks again by itself once, and shows the table', async () => {
    let answer: Answer = () => busy(2);
    const { data, fetchImpl } = backend({ standings: () => answer() });
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    expect(await screen.findByText('Источник данных занят')).toBeInTheDocument();
    // The countdown text itself; the exact second depends on how fast the runner is.
    expect(screen.getByText(/^Слишком много запросов к источнику\. Повторим автоматически через [12] с\.$/)).toBeInTheDocument();
    answer = () => ok(TABLE);
    expect(await screen.findAllByTestId('standings-row', {}, { timeout: 5000 })).toHaveLength(2);
    expect(calls(fetchImpl, '/standings')).toBe(2);
  });

  it('only one automatic retry: then the wait is shown and the next attempt is the user’s', async () => {
    const user = userEvent.setup();
    const { fetchImpl, data } = backend({ standings: () => busy(1) });
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    await screen.findByText(/Повторим автоматически/);
    await waitFor(() => expect(calls(fetchImpl, '/standings')).toBe(2), { timeout: 5000 });
    expect(await screen.findByText(/Источник пока занят|Источник снова доступен/)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 1600));
    expect(calls(fetchImpl, '/standings')).toBe(2);
    expect(screen.getByText('Источник снова доступен — попробуйте еще раз.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    await waitFor(() => expect(calls(fetchImpl, '/standings')).toBe(3));
    await new Promise((r) => setTimeout(r, 1600));
    expect(calls(fetchImpl, '/standings')).toBe(3);
  });

  it('a long wait is only told, never waited out automatically', async () => {
    const { fetchImpl, data } = backend({ standings: () => busy(120) });
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    expect(await screen.findByText('Источник пока занят. Попробуйте еще раз через 120 с.')).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 1200));
    expect(calls(fetchImpl, '/standings')).toBe(1);
  });

  it('any other failure keeps the usual error and its manual retry', async () => {
    const { fetchImpl, data } = backend({ standings: down });
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    expect(await screen.findByText('Таблица недоступна')).toBeInTheDocument();
    expect(screen.getByText('Источник не ответил. Попробуйте еще раз чуть позже.')).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 1200));
    expect(calls(fetchImpl, '/standings')).toBe(1);
  });

  it('the calendar asks again by itself once', async () => {
    let answer: Answer = () => busy(2);
    const { data, fetchImpl } = backend({ calendar: () => answer() });
    renderApp('#/tournament/fd%3APL', { data });
    expect(await screen.findByText(/Повторим автоматически через [12] с/)).toBeInTheDocument();
    answer = () => ok(CALENDAR);
    await waitFor(() => expect(screen.queryByText('Источник данных занят')).not.toBeInTheDocument(), { timeout: 5000 });
    expect(calls(fetchImpl, '/calendar')).toBe(2);
  });

  it('the day’s matches ask again by themselves once', async () => {
    let answer: Answer = () => busy(2);
    const { data, fetchImpl } = backend({ events: () => answer() });
    renderApp('#/matches', { data });
    expect(await screen.findByText(/Повторим автоматически через [12] с/)).toBeInTheDocument();
    answer = () => ok({ ...LIST, events: [] });
    await waitFor(() => expect(screen.queryByText('Источник данных занят')).not.toBeInTheDocument(), { timeout: 5000 });
    expect(calls(fetchImpl, '/api/sports/football/events')).toBe(2);
  });

  it('the home screen’s top matches ask again by themselves once', async () => {
    let answer: Answer = () => busy(2);
    const { data, fetchImpl } = backend({ top: () => answer() });
    renderApp('#/', { data });
    expect(await screen.findByText(/Повторим автоматически через [12] с/)).toBeInTheDocument();
    answer = () => ok({ ...LIST, top: [], ai: { status: 'connected' } });
    await waitFor(() => expect(screen.queryByText('Источник данных занят')).not.toBeInTheDocument(), { timeout: 5000 });
    expect(calls(fetchImpl, '/api/sports/football/top')).toBe(2);
  });

  it('leaving for another table and coming back grants no second automatic retry', async () => {
    const { data, fetchImpl } = backend({ standings: () => busy(1) });
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    await waitFor(() => expect(calls(fetchImpl, 'fd%3APL/standings')).toBe(2), { timeout: 5000 });
    window.location.hash = '#/tournament/fd%3APD?tab=table';
    await waitFor(() => expect(calls(fetchImpl, 'fd%3APD/standings')).toBe(1));
    window.location.hash = '#/tournament/fd%3APL?tab=table';
    await waitFor(() => expect(calls(fetchImpl, 'fd%3APL/standings')).toBe(3));
    expect(await screen.findByText(/Источник пока занят|Источник снова доступен/)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 1600));
    expect(calls(fetchImpl, 'fd%3APL/standings')).toBe(3);
  });

  it('a double tap during the countdown asks once, and the timer then stays quiet', async () => {
    const { fetchImpl, data } = backend({ standings: () => busy(2) });
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    await screen.findByText(/Повторим автоматически/);
    const button = screen.getByRole('button', { name: 'Повторить' });
    button.click();
    button.click();
    await waitFor(() => expect(calls(fetchImpl, '/standings')).toBe(2));
    await new Promise((r) => setTimeout(r, 2600));
    expect(calls(fetchImpl, '/standings')).toBe(2);
  });

  it('screen readers hear the busy state once, without the ticking seconds', async () => {
    const { data } = backend({ standings: () => busy(30) });
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    const status = await screen.findByTestId('busy-status');
    expect(status).toHaveAttribute('role', 'status');
    expect(status).toHaveTextContent('Источник данных занят. Повторим запрос автоматически.');
    expect(status.textContent).not.toMatch(/\d/);
  });

  it.each(LANGS)('the wait is translated (%s)', async (lang) => {
    const { data } = backend({ standings: () => busy(30) });
    renderApp('#/tournament/fd%3APL?tab=table', { data, settings: { lang } });
    expect(await screen.findByText(dictionaries[lang]['busy.title'])).toBeInTheDocument();
    expect(document.body.textContent).toContain(dictionaries[lang]['busy.auto'].replace('{seconds}', '30'));
    expect(document.body.textContent).not.toMatch(/busy\.[a-zA-Z]|\{seconds\}/);
  });
});

describe('the tournament’s name on a direct link', () => {
  it('comes from the served list at once, while the calendar is still loading', async () => {
    const { data } = backend({ calendar: never });
    renderApp('#/tournament/fd%3APL', { data });
    expect(await screen.findByRole('heading', { level: 1, name: 'Premier League' })).toBeInTheDocument();
    expect(screen.getByTestId('tournament-head')).toHaveTextContent('Premier League');
    expect(screen.queryByRole('heading', { level: 1, name: 'Турнир' })).not.toBeInTheDocument();
  });

  it('stays when the calendar fails', async () => {
    const { data } = backend({ calendar: down });
    renderApp('#/tournament/fd%3APL', { data });
    expect(await screen.findByText('Не удалось загрузить календарь')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { level: 1, name: 'Premier League' })).toBeInTheDocument();
  });

  it('a tournament already known needs no extra request', async () => {
    const { data, fetchImpl } = backend({ calendar: never });
    await data.loadTournaments('football');
    renderApp('#/tournament/fd%3APL', { data });
    expect(await screen.findByRole('heading', { level: 1, name: 'Premier League' })).toBeInTheDocument();
    expect(fetchImpl.mock.calls.filter(([u]) => String(u) === '/api/sports/football/tournaments')).toHaveLength(1);
  });

  it('an unknown id falls back to the generic title once nothing names it', async () => {
    const { data } = backend({ calendar: () => new Response('{}', { status: 404 }) });
    renderApp('#/tournament/fd%3AXX', { data });
    expect(await screen.findByRole('heading', { level: 1, name: 'Турнир' })).toBeInTheDocument();
  });
});
