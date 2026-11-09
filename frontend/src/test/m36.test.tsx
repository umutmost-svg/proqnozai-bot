import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BackendSportsDataProvider, readQuick } from '../data/BackendSportsDataProvider';
import type { QuickCriteria, SportEvent } from '../data/models';
import { DEFAULT_CRITERIA, quickRange, readCriteria } from '../data/quick';
import { monotonic, resetQuickMemory } from '../screens/QuickScreen';
import { renderApp } from './renderApp';
import { FORECAST_V3 } from './fixtures/forecastV3';

const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const TOURNAMENTS = [
  { id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org' },
  { id: 'fd:PD', sport: 'football', name: 'LaLiga', area: 'Spain', provider: 'football-data.org' },
];
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false };
const ev = (id: number, home: string, away: string, extra: Record<string, unknown> = {}) => ({
  id: `fd:${id}`,
  sport: 'football',
  tournamentId: 'fd:PL',
  startsAt: '2026-11-08T15:00:00Z',
  status: 'upcoming',
  sourceStatus: 'TIMED',
  home: { id: `fd:team:${id}1`, name: home },
  away: { id: `fd:team:${id}2`, name: away },
  score: null,
  halfTime: null,
  minute: null,
  awarded: false,
  stats: null,
  statsAvailability: 'unavailable',
  source,
  ...extra,
});
const leg = (id: number, home: string, away: string, market = '1', family = '1x2', probability = 64) => ({
  event: ev(id, home, away),
  market,
  family,
  probability,
  leagueRate: 45,
  expectedGoals: { home: 1.62, away: 0.94 },
  sample: 240,
  teamMatches: { home: 24, away: 23 },
  dataUntil: '2026-11-06T20:00:00Z',
});
const answer = (over: Record<string, unknown> = {}) => ({
  mode: 'single',
  status: 'ok',
  legs: [leg(1, 'Arsenal FC', 'Chelsea FC')],
  size: 1,
  suitable: 3,
  combined: null,
  counts: { matches: 9, forecasts: 8 },
  reasons: { early_forecast: 4 },
  unavailableTournaments: [],
  uncheckedDays: 0,
  nextMatchAt: null,
  experimental: true,
  modelVersion: 'goals-1',
  computedAt: '2026-11-08T06:00:00Z',
  ...over,
});
const SINGLE: QuickCriteria = { ...DEFAULT_CRITERIA, mode: 'single', size: 1 };
const COUPON: QuickCriteria = { ...DEFAULT_CRITERIA, mode: 'coupon', size: 2 };
const asEvent = (raw: unknown) => raw as SportEvent;
const RANGE = { from: new Date('2026-11-08T06:00:00Z'), to: new Date('2026-11-09T20:00:00Z') };

describe('quick prediction answer is read strictly', () => {
  it('accepts a consistent single and a coupon', () => {
    expect(readQuick(answer(), SINGLE, RANGE, asEvent)?.legs[0]?.market).toBe('1');
    const coupon = answer({ mode: 'coupon', size: 2, combined: [42, 64], legs: [leg(1, 'A', 'B'), leg(2, 'C', 'D', '1X', 'double', 78)] });
    expect(readQuick(coupon, COUPON, RANGE, asEvent)?.combined).toEqual([42, 64]);
  });

  it.each([
    ['another mode', answer({ mode: 'coupon' })],
    ['a result without its leg', answer({ legs: [] })],
    ['no_bet with legs', answer({ status: 'no_bet' })],
    ['a family that does not match the market', answer({ legs: [leg(1, 'A', 'B', '1', 'double')] })],
    ['an unknown reason', answer({ reasons: { lucky: 1 } })],
    ['a probability above 100', answer({ legs: [leg(1, 'A', 'B', '1', '1x2', 140)] })],
    ['a single with a combined chance', answer({ combined: 50 })],
    ['an unlabelled pick while the model is unconfirmed', answer({ experimental: false })],
    ['a leg outside the asked period', answer({ legs: [{ ...leg(1, 'A', 'B'), event: ev(1, 'A', 'B', { startsAt: '2026-11-12T15:00:00Z' }) }] })],
    ['"no pick" with suitable matches', answer({ status: 'no_bet', legs: [], suitable: 3 })],
    ['"not enough" for a single', answer({ status: 'not_enough', legs: [], suitable: 0 })],
    ['"unavailable" with enough suitable matches', answer({ status: 'unavailable', legs: [], suitable: 1 })],
    ['an impossible calendar date', answer({ computedAt: '2026-02-30T06:00:00Z' })],
    ['an impossible time with an offset', answer({ legs: [{ ...leg(1, 'A', 'B'), event: ev(1, 'A', 'B', { startsAt: '2026-11-08T25:00:00+04:00' }) }] })],
    ['a computed-at time without a zone', answer({ computedAt: '2026-11-08T06:00:00' })],
    ['a kickoff without a zone', answer({ legs: [{ ...leg(1, 'A', 'B'), event: ev(1, 'A', 'B', { startsAt: '2026-11-08T15:00:00' }) }] })],
    ['a leg on another UTC day than the pick', answer({ legs: [{ ...leg(1, 'A', 'B'), event: ev(1, 'A', 'B', { startsAt: '2026-11-09T15:00:00Z' }) }] })],
    ['a leg below the risk floor', answer({ legs: [leg(1, 'A', 'B', '1', '1x2', 57)] })],
    ['no unchecked-days count', answer({ uncheckedDays: undefined })],
    ['more forecasts than matches', answer({ counts: { matches: 1, forecasts: 2 } })],
    ['a leg with no forecast behind it', answer({ counts: { matches: 0, forecasts: 0 }, suitable: 0 })],
    ['fewer suitable matches than legs', answer({ suitable: 0 })],
  ])('rejects %s', (_, body) => {
    expect(readQuick(body, SINGLE, RANGE, asEvent)).toBeNull();
  });

  it('rejects a coupon that repeats a match or a team, or lacks the combined chance', () => {
    const same = answer({ mode: 'coupon', size: 2, combined: 30, legs: [leg(1, 'A', 'B'), leg(1, 'A', 'B', 'X2', 'double')] });
    expect(readQuick(same, COUPON, RANGE, asEvent)).toBeNull();
    const shared = leg(2, 'C', 'D');
    shared.event.home = { id: 'fd:team:11', name: 'A' };
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: 30, legs: [leg(1, 'A', 'B'), shared] }), COUPON, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, legs: [leg(1, 'A', 'B'), leg(2, 'C', 'D')] }), COUPON, RANGE, asEvent)).toBeNull();
    // Bounds other than those of the shown legs (or a multiplied single number) are broken answers.
    const two = [leg(1, 'A', 'B'), leg(2, 'C', 'D')];
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: 41, legs: two }), COUPON, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [0, 64], legs: two }), COUPON, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [28, 64], legs: two }), COUPON, RANGE, asEvent)).not.toBeNull();
    // Two exact 60.51% legs show as 61%: the true lower bound 21.02% rounds down to 21.
    const sixtyOne = [leg(1, 'A', 'B', '1', '1x2', 61), leg(2, 'C', 'D', '1', '1x2', 61)];
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [21, 61], legs: sixtyOne }), COUPON, RANGE, asEvent)).not.toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [21, 63], legs: sixtyOne }), COUPON, RANGE, asEvent)).toBeNull();
    // Shown 60% + 60%: an upper bound of 61 needs both above 60%, so the lower bound is at least 20.
    const sixty = [leg(1, 'A', 'B', '1', '1x2', 60), leg(2, 'C', 'D', '1', '1x2', 60)];
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [19, 61], legs: sixty }), COUPON, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [20, 61], legs: sixty }), COUPON, RANGE, asEvent)).not.toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [19, 60], legs: sixty }), COUPON, RANGE, asEvent)).not.toBeNull();
    // Double chance is a sum of rounded 1X2 percents: exact 66.6% may show as 66%; bounds [33, 67] are valid.
    const doubles = [leg(1, 'A', 'B', '1X', 'double', 66), leg(2, 'C', 'D', '1X', 'double', 66)];
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [33, 67], legs: doubles }), COUPON, RANGE, asEvent)).not.toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, combined: [40, 67], legs: doubles }), COUPON, RANGE, asEvent)).toBeNull();
    const elsewhere = { ...SINGLE, tournaments: ['fd:PD'] };
    expect(readQuick(answer(), elsewhere, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ market: 'double' }), { ...SINGLE, market: 'double' }, RANGE, asEvent)).toBeNull();
    // "Not enough" must really be fewer than asked.
    expect(readQuick(answer({ mode: 'coupon', size: 2, status: 'not_enough', legs: [], suitable: 2 }), COUPON, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ mode: 'coupon', size: 2, status: 'not_enough', legs: [], suitable: 1 }), COUPON, RANGE, asEvent)).not.toBeNull();
  });
});

describe('quick criteria', () => {
  it('"today" ends at the user’s local midnight, the others count hours from now', () => {
    const now = new Date('2026-11-08T21:30:00Z'); // 01:30 on 9 November in Baku
    expect(quickRange('today', now, 'Asia/Baku').to.toISOString()).toBe('2026-11-09T20:00:00.000Z');
    expect(quickRange('3d', now, 'Asia/Baku').to.toISOString()).toBe('2026-11-11T21:30:00.000Z');
    expect(quickRange('7d', now, 'UTC').from).toBe(now);
  });

  it('a stored value with unknown parts falls back field by field', () => {
    const known = new Set(['fd:PL']);
    expect(readCriteria({ mode: 'coupon', size: 9, risk: 'max', tournaments: ['fd:PL', 'fd:XX', 'fd:PL'] }, known)).toEqual({
      ...DEFAULT_CRITERIA,
      mode: 'coupon',
      tournaments: ['fd:PL'],
    });
    expect(readCriteria('garbage', known)).toEqual(DEFAULT_CRITERIA);
  });
});

function backend(quick: (url: URL) => unknown, top: unknown[] = []) {
  const urls: string[] = [];
  const fetchImpl = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    urls.push(url);
    const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
    if (url.startsWith('/api/quick/pick')) return ok(quick(new URL(url, 'http://x')));
    if (url.startsWith('/api/sports/football/top')) return ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, top });
    if (url.startsWith('/api/sports/football/events')) return ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, events: [] });
    if (url.startsWith('/api/sports/football/tournaments')) return ok({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS });
    return new Response('{}', { status: 404 });
  });
  return { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }), urls };
}

describe('quick prediction screen', () => {
  beforeEach(() => {
    resetQuickMemory();
    window.localStorage.clear();
  });

  it('is the fourth tab', async () => {
    renderApp('#/');
    const nav = await screen.findByRole('navigation', { name: /меню|навигац/i });
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual(['Главная', 'Матчи', 'Быстрый прогноз', 'Профиль']);
  });

  it('explains the UTC match-day rule for "today" too', async () => {
    const { data } = backend(() => answer());
    renderApp('#/quick', { data });
    expect(await screen.findByTestId('quick-match-day-note')).toHaveTextContent('сегодняшние по местному времени');
  });

  it('demo mode never invents the model’s numbers', async () => {
    renderApp('#/quick');
    expect(await screen.findByTestId('quick-demo')).toHaveTextContent('Подбор работает на реальных данных');
    expect(screen.queryByTestId('quick-submit')).toBeNull();
  });

  it('a single: criteria go to the backend, the result shows the bet, reasoning, risk and counts', async () => {
    const user = userEvent.setup();
    const { data, urls } = backend(() => answer({ legs: [{ ...leg(1, 'Arsenal FC', 'Chelsea FC', '1X', 'double', 84), event: ev(1, 'Arsenal FC', 'Chelsea FC', { tournamentId: 'fd:PD' }) }] }));
    renderApp('#/quick', { data });
    await user.click(await screen.findByRole('button', { name: 'Ближайшие 3 дня' }));
    expect(screen.getByText(/только по матчам текущего дня/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Двойной шанс' }));
    await user.click(screen.getByRole('button', { name: 'Консервативный' }));
    await user.click(screen.getByRole('button', { name: 'LaLiga' }));
    await user.click(screen.getByTestId('quick-submit'));
    const result = await screen.findByTestId('quick-result');
    const q = new URL(urls.find((u) => u.startsWith('/api/quick/pick'))!, 'http://x').searchParams;
    expect(Object.fromEntries(q)).toMatchObject({ mode: 'single', market: 'double', risk: 'conservative', tournaments: 'fd:PD' });
    expect(Date.parse(q.get('to')!) - Date.parse(q.get('from')!)).toBe(72 * 3600_000);
    expect(q.has('size')).toBe(false);
    expect(result).toHaveTextContent('Ординар');
    expect(within(result).getByTestId('quick-leg')).toHaveTextContent('Arsenal FC');
    expect(result).toHaveTextContent('Вероятность по модели: 84%');
    expect(result).toHaveTextContent('такой исход бывает в 45% матчей');
    expect(within(result).getByTestId('quick-experimental')).toHaveTextContent('Экспериментально');
    expect(within(result).getByTestId('quick-caveat')).toHaveTextContent('не гарантия');
    expect(within(result).getByTestId('source-note')).toHaveTextContent('Football data provided by the Football-Data.org API');
    expect(screen.getByTestId('quick-counts')).toHaveTextContent('Предстоящих матчей в периоде: 9. Подходят под условия: 3.');
    expect(screen.getByTestId('quick-counts')).toHaveTextContent('Не в день матча по UTC: 4');
    // "Change criteria" returns to the same, remembered criteria.
    await user.click(screen.getByTestId('quick-change'));
    expect(screen.getByRole('button', { name: 'Ближайшие 3 дня' })).toHaveAttribute('aria-pressed', 'true');
    expect(JSON.parse(window.localStorage.getItem('proqnozai.quick.v1')!)).toMatchObject({ period: '3d', market: 'double' });
  });

  it('a coupon shows every leg, the combined chance under its assumption, and is never padded', async () => {
    const user = userEvent.setup();
    let calls = 0;
    const { data } = backend((url) => {
      calls++;
      const size = Number(url.searchParams.get('size'));
      return size === 2
        ? answer({ mode: 'coupon', size: 2, combined: [45, 64], legs: [leg(1, 'A FC', 'B FC'), leg(2, 'C FC', 'D FC', 'HO0.5', 'team_total', 81)] })
        : answer({ mode: 'coupon', size, status: 'not_enough', legs: [], suitable: 2 });
    });
    renderApp('#/quick', { data });
    await user.click(await screen.findByRole('button', { name: 'Купон' }));
    await user.click(within(screen.getByTestId('quick-size')).getByRole('button', { name: '4' }));
    await user.click(screen.getByTestId('quick-submit'));
    expect(await screen.findByTestId('quick-not-enough')).toHaveTextContent('Нужно матчей: 4, подходят: 2');
    await user.click(screen.getByTestId('quick-change'));
    await user.click(within(screen.getByTestId('quick-size')).getByRole('button', { name: '2' }));
    await user.click(screen.getByTestId('quick-submit'));
    const result = await screen.findByTestId('quick-result');
    expect(within(result).getAllByTestId('quick-leg')).toHaveLength(2);
    expect(result).toHaveTextContent('Купон из 2 матчей');
    expect(within(result).getByTestId('quick-combined')).toHaveTextContent('от 45% до 64%');
    expect(within(result).getByTestId('quick-combined')).toHaveTextContent('Вероятности не перемножаются');
    await user.click(screen.getByTestId('quick-repeat'));
    await waitFor(() => expect(calls).toBe(3));
  });

  it('no suitable pick is an honest state; missing data never reads as "no pick"', async () => {
    const user = userEvent.setup();
    const { data } = backend(() => answer({ status: 'no_bet', legs: [], suitable: 0, reasons: { no_qualifying_market: 3 } }));
    renderApp('#/quick', { data });
    await user.click(await screen.findByTestId('quick-submit'));
    expect(await screen.findByTestId('quick-no-bet')).toHaveTextContent('Условия не ослабляются автоматически');
    const partial = { status: 'no_bet', legs: [], suitable: 0, reasons: { source_unavailable: 2 }, unavailableTournaments: ['fd:PD'] };
    expect(readQuick(answer(partial), SINGLE, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ ...partial, reasons: {} }), SINGLE, RANGE, asEvent)).toBeNull();
    const shown = readQuick(answer({ ...partial, status: 'unavailable' }), SINGLE, RANGE, asEvent);
    expect(shown?.unavailableTournaments).toEqual(['fd:PD']);
  });
});

describe('quick prediction transport', () => {
  afterEach(() => vi.useRealTimers());

  it('waits for a slow (cold-cache) pick longer than an ordinary listing', async () => {
    vi.useFakeTimers();
    const fetchImpl = vi.fn<typeof fetch>(
      () => new Promise((resolve) => setTimeout(() => resolve(new Response(JSON.stringify(answer()), { status: 200 })), 25_000)),
    );
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
    const pending = data.quickPick(SINGLE, new Date('2026-11-08T06:00:00Z'), 'Asia/Baku');
    await vi.advanceTimersByTimeAsync(25_000);
    await expect(pending).resolves.toMatchObject({ status: 'ok' });
  });
});

describe('AI screen in real mode', () => {
  function aiBackend(cached: unknown | null, ai: 'connected' | 'not_connected' = 'connected') {
    const calls: string[] = [];
    const fetchImpl = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      const ok = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
      if (url.startsWith('/api/ai/analysis')) {
        const id = method === 'GET' ? new URL(url, 'http://x').searchParams.get('event_id') : JSON.parse(String(init?.body)).event_id;
        calls.push(`${method} ${id}`);
        if (method === 'GET') return cached ? ok(cached) : ok({ error: 'ai_not_created' }, 404);
        return ok({ error: 'ai_unavailable', reason: 'limit_reached' }, 429);
      }
      const event = /^\/api\/sports\/event\/fd%3A(\d+)$/.exec(url) ?? /^\/api\/sports\/event\/fd:(\d+)$/.exec(url);
      if (event) return ok({ event: ev(Number(event[1]), `Home${event[1]} FC`, `Away${event[1]} FC`), tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: ai } });
      return ok({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS, top: [], events: [], ai: { status: ai } });
    });
    return { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl, getCsrfToken: () => 'csrf' }), calls };
  }

  it('opening the screen only reads; the paid request goes out on the explicit button only', async () => {
    const user = userEvent.setup();
    const { data, calls } = aiBackend(null);
    renderApp('#/event/fd%3A1/ai', { data });
    expect(await screen.findByTestId('ai-create')).toHaveTextContent('Создать AI-разбор');
    expect(screen.getByTestId('ai-create-prompt')).toHaveTextContent('расходует ваш дневной лимит AI');
    expect(calls).toEqual(['GET fd:1']);
    await user.click(screen.getByTestId('ai-create'));
    await screen.findByTestId('ai-error');
    expect(calls).toEqual(['GET fd:1', 'POST fd:1']);
  });

  it('"check again" after a free "insufficient" answer only reads; creating needs the button again', async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    const insufficient = { schema: 'v3', eventId: 'fd:1', kind: 'insufficient', reasons: ['no_verified_form', 'no_odds_source'],
      generatedAt: null, model: null, probabilities: null, factors: [], basis: null, estimated: false, pick: null, forecast: null, alternatives: [] };
    const fetchImpl = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      const ok = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
      if (url.startsWith('/api/ai/analysis')) {
        calls.push(init?.method ?? 'GET');
        return (init?.method ?? 'GET') === 'GET' ? ok({ error: 'ai_not_created' }, 404) : ok(insufficient);
      }
      if (url.startsWith('/api/sports/event/')) return ok({ event: ev(1, 'A FC', 'B FC'), tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'connected' } });
      return ok({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS, ai: { status: 'connected' } });
    });
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl, getCsrfToken: () => 'csrf' });
    renderApp('#/event/fd%3A1/ai', { data });
    await user.click(await screen.findByTestId('ai-create'));
    await screen.findByTestId('ai-insufficient');
    expect(calls).toEqual(['GET', 'POST']);
    await user.click(screen.getByRole('button', { name: 'Проверить еще раз' }));
    expect(await screen.findByTestId('ai-create')).toBeInTheDocument();
    expect(calls).toEqual(['GET', 'POST', 'GET']);
  });

  it('a slow (cold) read is waited for, not cut at the ordinary listing timeout', async () => {
    vi.useFakeTimers();
    try {
      const fetchImpl = vi.fn<typeof fetch>(async (input) => {
        const url = String(input);
        if (url.startsWith('/api/ai/analysis')) {
          return new Promise<Response>((resolve) => setTimeout(() => resolve(new Response('{"error":"ai_not_created"}', { status: 404 })), 25_000));
        }
        return new Response(JSON.stringify({ event: ev(1, 'A FC', 'B FC'), tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'connected' } }), { status: 200 });
      });
      const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
      const pending = data.getCachedAnalysis('fd:1');
      await vi.advanceTimersByTimeAsync(25_000);
      await expect(pending).resolves.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a tap for one match never creates another match’s analysis', async () => {
    const user = userEvent.setup();
    const { data, calls } = aiBackend(null);
    renderApp('#/event/fd%3A1/ai', { data });
    await user.click(await screen.findByTestId('ai-create'));
    await screen.findByTestId('ai-error');
    window.location.hash = '#/event/fd%3A2/ai'; // another match, same app
    await waitFor(() => expect(calls).toContain('GET fd:2'));
    expect(await screen.findByTestId('ai-create')).toBeInTheDocument();
    window.location.hash = '#/event/fd%3A1/ai'; // and back: reading again, not creating again
    await waitFor(() => expect(calls.filter((c) => c === 'GET fd:1')).toHaveLength(2));
    expect(await screen.findByTestId('ai-create')).toBeInTheDocument();
    expect(calls.filter((c) => c.startsWith('POST'))).toEqual(['POST fd:1']);
  });

  it('an analysis made earlier is shown even while the AI is not connected, with no request to create', async () => {
    const made = {
      schema: 'v3', eventId: 'fd:1', kind: 'partial', reasons: ['no_odds_source'], generatedAt: '2026-11-08T05:00:00Z', model: 'm',
      probabilities: { home: FORECAST_V3.markets['1'], draw: FORECAST_V3.markets.X, away: FORECAST_V3.markets['2'] },
      factors: ['home_form', 'away_form'], estimated: true,
      basis: {
        home: { name: 'Home1 FC', matches: 5, form: 'WWDWL', wins: 3, draws: 1, losses: 1, points: 10, goalsFor: 8, goalsAgainst: 5, tablePosition: null, tablePoints: null },
        away: { name: 'Away1 FC', matches: 5, form: 'LDLWL', wins: 1, draws: 1, losses: 3, points: 4, goalsFor: 4, goalsAgainst: 9, tablePosition: null, tablePoints: null },
      },
      pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [],
      forecast: { ...FORECAST_V3, noBetReason: 'no_qualifying_market' },
    };
    const { data, calls } = aiBackend(made, 'not_connected');
    renderApp('#/event/fd%3A1/ai', { data });
    expect(await screen.findByTestId('ai-result')).toBeInTheDocument();
    expect(calls).toEqual(['GET fd:1']);
    expect(screen.queryByTestId('ai-create')).toBeNull();
  });

  it('with the AI not connected and nothing made, the screen says it is unavailable', async () => {
    const { data, calls } = aiBackend(null, 'not_connected');
    renderApp('#/event/fd%3A1/ai', { data });
    await waitFor(() => expect(calls).toEqual(['GET fd:1']));
    expect(screen.queryByTestId('ai-create')).toBeNull();
  });
});

describe('quick prediction errors', () => {
  beforeEach(() => {
    resetQuickMemory();
    window.localStorage.clear();
  });

  function failing(status: number, body: unknown, tournaments = true) {
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.startsWith('/api/quick/pick')) return new Response(JSON.stringify(body), { status });
      if (url.startsWith('/api/sports/football/tournaments') && !tournaments) return new Response('{}', { status: 503 });
      return new Response(JSON.stringify({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS, top: [], events: [] }), { status: 200 });
    });
    return new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
  }

  it.each([
    [429, { error: 'rate_limited' }, 'Слишком много подборов'],
    [503, { error: 'source_unavailable', reason: 'rate_limited' }, 'Источник данных недоступен'],
    [401, { error: 'unauthenticated' }, 'Сессия истекла. Закройте и откройте Mini App заново.'],
    [400, { error: 'bad_range' }, 'Часы устройства расходятся с сервером'],
  ])('HTTP %s is "try later", not a connection problem', async (status, body, title) => {
    const user = userEvent.setup();
    renderApp('#/quick', { data: failing(status, body) });
    await user.click(await screen.findByTestId('quick-submit'));
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.queryByText('Проверьте соединение и попробуйте ещё раз.')).toBeNull();
  });

  it('a remembered competition the catalogue no longer has is dropped and never sent', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem('proqnozai.quick.v1', JSON.stringify({ tournaments: ['fd:XX', 'fd:PD'] }));
    const { data, urls } = backend(() => answer({ legs: [{ ...leg(1, 'A FC', 'B FC'), event: ev(1, 'A FC', 'B FC', { tournamentId: 'fd:PD' }) }] }));
    renderApp('#/quick', { data });
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem('proqnozai.quick.v1')!).tournaments).toEqual(['fd:PD']));
    await user.click(screen.getByTestId('quick-submit'));
    await screen.findByTestId('quick-result');
    expect(new URL(urls.find((u) => u.startsWith('/api/quick/pick'))!, 'http://x').searchParams.get('tournaments')).toBe('fd:PD');
  });

  it('with nothing to pick it says when the next match is and offers other dates, nothing loosened', async () => {
    const user = userEvent.setup();
    const periods: string[] = [];
    const { data } = backend((url) => {
      periods.push(String((Date.parse(url.searchParams.get('to')!) - Date.parse(url.searchParams.get('from')!)) / 3600e3));
      return answer({ status: 'no_bet', legs: [], suitable: 0, counts: { matches: 2, forecasts: 0 }, reasons: { early_forecast: 2 }, nextMatchAt: '2026-11-10T21:30:00Z' });
    });
    renderApp('#/quick', { data });
    await user.click(await screen.findByRole('button', { name: 'Ближайшие 3 дня' }));
    await user.click(screen.getByTestId('quick-submit'));
    // Local and UTC days differ here: 01:30 on 11 November in Baku is still 10 November in UTC.
    expect(await screen.findByTestId('quick-next-match')).toHaveTextContent('Ближайший матч в выбранном периоде — 11 ноября · 01:30');
    expect(screen.getByTestId('quick-next-match')).toHaveTextContent('только в день матча по UTC — 10 ноября, и только если исход пройдёт правила модели');
    await user.click(screen.getByTestId('quick-try-week'));
    await waitFor(() => expect(periods.at(-1)).toBe('168'));
    expect(await screen.findByTestId('quick-next-steps')).toBeInTheDocument();
    expect(screen.queryByTestId('quick-try-week')).toBeNull(); // already the longest period
    expect(JSON.parse(window.localStorage.getItem('proqnozai.quick.v1')!).period).toBe('7d');
    await user.click(screen.getByTestId('quick-open-calendar'));
    await waitFor(() => expect(window.location.hash).toBe('#/matches'));
  });

  it('a next-match time outside the asked period is a broken answer', () => {
    expect(readQuick(answer({ status: 'no_bet', legs: [], suitable: 0, nextMatchAt: '2026-11-12T20:00:00Z' }), SINGLE, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ status: 'no_bet', legs: [], suitable: 0, nextMatchAt: '2026-11-31T15:00:00Z' }), SINGLE, RANGE, asEvent)).toBeNull();
    // An offset form of a valid instant is fine.
    expect(readQuick(answer({ status: 'no_bet', legs: [], suitable: 0, nextMatchAt: '2026-11-09T19:00:00+04:00' }), SINGLE, RANGE, asEvent)).not.toBeNull();
    expect(readQuick(answer({ status: 'no_bet', legs: [], suitable: 0, nextMatchAt: '2026-11-09T15:00:00Z' }), SINGLE, RANGE, asEvent)).not.toBeNull();
  });

  it('a next-match time on the pick’s own UTC day is a broken answer', () => {
    expect(readQuick(answer({ status: 'no_bet', legs: [], suitable: 0, nextMatchAt: '2026-11-08T20:00:00Z' }), SINGLE, RANGE, asEvent)).toBeNull();
    expect(readQuick(answer({ status: 'no_bet', legs: [], suitable: 0, nextMatchAt: '2026-11-09T20:00:00' }), SINGLE, RANGE, asEvent)).toBeNull();
  });

  it('a failed competition list says so and offers a retry', async () => {
    renderApp('#/quick', { data: failing(200, answer(), false) });
    const error = await screen.findByTestId('quick-tournaments-error');
    expect(error).toHaveTextContent('Список турниров не загрузился.');
    expect(within(error).getByRole('button', { name: 'Повторить' })).toBeInTheDocument();
  });
});

describe('quick prediction freshness', () => {
  beforeEach(() => {
    resetQuickMemory();
    window.localStorage.clear();
  });

  it('missing data is a retryable state, never "no pick"', async () => {
    const user = userEvent.setup();
    const { data } = backend(() => answer({ status: 'unavailable', legs: [], suitable: 0, counts: { matches: 5, forecasts: 0 }, reasons: { source_unavailable: 5 }, uncheckedDays: 2 }));
    renderApp('#/quick', { data });
    await user.click(await screen.findByTestId('quick-submit'));
    expect(await screen.findByTestId('quick-unavailable-state')).toHaveTextContent('Это не «нет прогноза»');
    expect(screen.getByTestId('quick-unchecked')).toHaveTextContent('Не удалось проверить дней периода: 2');
    expect(screen.queryByTestId('quick-no-bet')).toBeNull();
  });

  it('a pick left loading starts again from the criteria, not an endless spinner', async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      String(input).startsWith('/api/quick/pick')
        ? new Promise<Response>(() => {})
        : new Response(JSON.stringify({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS, top: [], events: [] }), { status: 200 }),
    );
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
    const first = renderApp('#/quick', { data });
    await user.click(await screen.findByTestId('quick-submit'));
    expect(await screen.findByTestId('loading-state')).toBeInTheDocument();
    first.unmount();
    renderApp('#/quick', { data });
    expect(await screen.findByTestId('quick-submit')).toBeInTheDocument();
  });

  it('a slow device clock does not keep a pick past kickoff (the server’s time counts)', async () => {
    const user = userEvent.setup();
    // The device says 05:30; the server computed the pick at 06:00, when the match kicked off.
    const now = { ...leg(1, 'A FC', 'B FC'), event: ev(1, 'A FC', 'B FC', { startsAt: '2026-11-08T06:00:00Z' }) };
    const { data } = backend(() => answer({ legs: [now], computedAt: '2026-11-08T06:00:00Z' }));
    renderApp('#/quick', { data, clockAt: '2026-11-08T05:30:00Z' });
    await user.click(await screen.findByTestId('quick-submit'));
    expect(await screen.findByTestId('quick-expired')).toBeInTheDocument();
    expect(screen.queryByTestId('quick-result')).toBeNull();
  });

  it('time spent delivering a slow answer counts too', async () => {
    const user = userEvent.setup();
    let now = new Date('2026-11-08T05:30:00Z'); // device 30 min slow
    const clock = { now: () => new Date(now.getTime()) };
    const soon = { ...leg(1, 'A FC', 'B FC'), event: ev(1, 'A FC', 'B FC', { startsAt: '2026-11-08T06:00:30Z' }) };
    const body = answer({ legs: [soon], computedAt: '2026-11-08T06:00:00Z' });
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.startsWith('/api/quick/pick')) {
        now = new Date('2026-11-08T05:30:40Z'); // the answer arrives 40 s later
        return new Response(JSON.stringify(body), { status: 200 });
      }
      return new Response(JSON.stringify({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS, top: [], events: [] }), { status: 200 });
    });
    renderApp('#/quick', { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }), clock });
    await user.click(await screen.findByTestId('quick-submit'));
    expect(await screen.findByTestId('quick-expired')).toBeInTheDocument();
  });

  it('the kickoff timer expires the pick even if the device clock was set back meanwhile', async () => {
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let now = new Date('2026-11-08T06:00:00Z');
      const clock = { now: () => new Date(now.getTime()) };
      const soon = { ...leg(1, 'A FC', 'B FC'), event: ev(1, 'A FC', 'B FC', { startsAt: '2026-11-08T06:00:05Z' }) };
      const { data } = backend(() => answer({ legs: [soon], computedAt: '2026-11-08T06:00:00Z' }));
      renderApp('#/quick', { data, clock });
      await user.click(await screen.findByTestId('quick-submit'));
      expect(await screen.findByTestId('quick-result')).toBeInTheDocument();
      now = new Date('2026-11-08T05:00:00Z'); // the device clock jumps an hour back
      await vi.advanceTimersByTimeAsync(6_000);
      expect(await screen.findByTestId('quick-expired')).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a pick kept while away expires on the monotonic clock even if the device clock went back', async () => {
    const user = userEvent.setup();
    let mono = 1_000;
    const spy = vi.spyOn(monotonic, 'now').mockImplementation(() => mono);
    try {
      const soon = { ...leg(1, 'A FC', 'B FC'), event: ev(1, 'A FC', 'B FC', { startsAt: '2026-11-08T06:10:00Z' }) };
      const { data } = backend(() => answer({ legs: [soon], computedAt: '2026-11-08T06:00:00Z' }));
      const first = renderApp('#/quick', { data });
      await user.click(await screen.findByTestId('quick-submit'));
      expect(await screen.findByTestId('quick-result')).toBeInTheDocument();
      first.unmount();
      mono += 11 * 60_000; // 11 minutes pass; the device clock was set back to 05:00
      renderApp('#/quick', { data, clockAt: '2026-11-08T05:00:00Z' });
      expect(await screen.findByTestId('quick-expired')).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it('an old "nothing found" answer is not shown again after a while', async () => {
    const user = userEvent.setup();
    let mono = 5_000;
    const spy = vi.spyOn(monotonic, 'now').mockImplementation(() => mono);
    try {
      const { data } = backend(() => answer({ status: 'no_bet', legs: [], suitable: 0 }));
      const first = renderApp('#/quick', { data });
      await user.click(await screen.findByTestId('quick-submit'));
      expect(await screen.findByTestId('quick-no-bet')).toBeInTheDocument();
      expect(screen.getByTestId('quick-counts')).toHaveTextContent('Расчёт: 08.11.2026');
      first.unmount();
      mono += 5 * 60_000; // back within 10 minutes: still shown
      const second = renderApp('#/quick', { data });
      expect(await screen.findByTestId('quick-no-bet')).toBeInTheDocument();
      second.unmount();
      mono += 6 * 60_000; // later: the criteria again, ready for a new pick
      renderApp('#/quick', { data });
      expect(await screen.findByTestId('quick-submit')).toBeInTheDocument();
      expect(screen.queryByTestId('quick-no-bet')).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });

  it('a "no matches" answer shows when it was computed and goes after ten minutes on screen', async () => {
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let mono = 1_000;
    const spy = vi.spyOn(monotonic, 'now').mockImplementation(() => mono);
    try {
      const { data } = backend(() => answer({ status: 'no_bet', legs: [], suitable: 0, counts: { matches: 0, forecasts: 0 }, reasons: {} }));
      renderApp('#/quick', { data });
      await user.click(await screen.findByTestId('quick-submit'));
      expect(await screen.findByTestId('quick-empty')).toBeInTheDocument();
      expect(screen.getByText(/Расчёт: 08\.11\.2026/)).toBeInTheDocument();
      mono += 10 * 60_000 + 1_000;
      await vi.advanceTimersByTimeAsync(10 * 60_000 + 1_000);
      expect(await screen.findByTestId('quick-submit')).toBeInTheDocument();
      expect(screen.queryByTestId('quick-empty')).toBeNull();
    } finally {
      spy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('a kept pick is dropped once its match has kicked off', async () => {
    const user = userEvent.setup();
    // TEST_NOW is 06:00 UTC; this leg starts at 06:00:01.
    const soon = { ...leg(1, 'A FC', 'B FC'), event: ev(1, 'A FC', 'B FC', { startsAt: '2026-11-08T06:00:01Z' }) };
    const { data } = backend(() => answer({ legs: [soon] }));
    const first = renderApp('#/quick', { data });
    await user.click(await screen.findByTestId('quick-submit'));
    expect(await screen.findByTestId('quick-result')).toBeInTheDocument();
    first.unmount();
    renderApp('#/quick', { data, clockAt: '2026-11-08T06:00:02Z' });
    // Decided before the first paint: the stale card is never rendered, not even briefly.
    expect(screen.queryByTestId('quick-result')).toBeNull();
    expect(await screen.findByTestId('quick-expired')).toHaveTextContent('матчей уже начался');
    expect(screen.queryByTestId('quick-result')).toBeNull();
  });
});

describe('top matches of the day', () => {
  it('shows up to five cards of the chosen day, with the leaders mark, and opens a match', async () => {
    const user = userEvent.setup();
    const top = [
      { event: ev(1, 'Arsenal FC', 'Chelsea FC'), leaders: true },
      { event: ev(2, 'Everton FC', 'Fulham FC', { status: 'live', score: [1, 0], minute: 30 }), leaders: false },
    ];
    const { data } = backend(() => answer(), top);
    renderApp('#/', { data });
    const cards = await screen.findAllByTestId('top-card');
    expect(cards.map((c) => c.getAttribute('data-event'))).toEqual(['fd:1', 'fd:2']);
    expect(within(cards[0]!).getByTestId('top-leaders')).toHaveTextContent('Встреча лидеров');
    expect(within(cards[1]!).queryByTestId('top-leaders')).toBeNull();
    expect(cards[1]).toHaveTextContent('1 : 0');
    await user.click(within(cards[0]!).getByRole('button', { name: /Открыть матч/ }));
    await waitFor(() => expect(window.location.hash).toBe('#/event/fd:1'));
  });

  it('a postponed match in the answer makes it broken, never a top card', async () => {
    const { data } = backend(() => answer(), [{ event: ev(1, 'A FC', 'B FC', { status: 'postponed' }), leaders: false }]);
    renderApp('#/', { data });
    expect(await screen.findByText('Расписание не загрузилось')).toBeInTheDocument();
    expect(screen.queryByTestId('top-card')).toBeNull();
  });

  it('cards served from the cache after a failed refresh say so', async () => {
    const stale = { ...source, stale: true };
    const { data } = backend(() => answer(), [{ event: ev(1, 'A FC', 'B FC', { source: stale }), leaders: false }]);
    renderApp('#/', { data });
    expect(await screen.findByTestId('top-stale')).toHaveTextContent('показаны из кеша');
  });

  it('a day whose matches have no confirmed status says so, not "postponed"', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
      if (url.startsWith('/api/sports/football/top')) return ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, top: [] });
      if (url.startsWith('/api/sports/football/events')) {
        return ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, events: [ev(1, 'A FC', 'B FC', { status: 'weird', sourceStatus: 'WEIRD' })] });
      }
      return ok({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS });
    });
    renderApp('#/', { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }) });
    expect(await screen.findByText('Главных матчей нет')).toBeInTheDocument();
    expect(screen.queryByText('Матчей по расписанию нет')).toBeNull();
  });

  it('a top match without a time zone is a broken answer', async () => {
    const { data } = backend(() => answer(), [{ event: ev(1, 'A FC', 'B FC', { startsAt: '2026-11-08T15:00:00' }), leaders: false }]);
    renderApp('#/', { data });
    expect(await screen.findByText('Расписание не загрузилось')).toBeInTheDocument();
    expect(screen.queryByTestId('top-card')).toBeNull();
  });

  it('an answer with another day’s match is broken, never shown', async () => {
    const { data } = backend(() => answer(), [{ event: ev(1, 'A FC', 'B FC', { startsAt: '2026-11-10T15:00:00Z' }), leaders: false }]);
    renderApp('#/', { data });
    expect(await screen.findByText('Расписание не загрузилось')).toBeInTheDocument();
    expect(screen.queryByTestId('top-card')).toBeNull();
  });
});

describe('local day', () => {
  it('"today" moves on at local midnight while the app stays open', async () => {
    let now = new Date('2026-11-08T19:59:59Z'); // 23:59:59 in Baku
    const clock = { now: () => new Date(now.getTime()) };
    renderApp('#/', { clock });
    const strip = await screen.findByTestId('date-strip');
    expect(within(strip).getByRole('button', { name: /8 ноября/ })).toHaveAttribute('aria-pressed', 'true');
    now = new Date('2026-11-08T20:00:05Z');
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(within(screen.getByTestId('date-strip')).getByRole('button', { name: /9 ноября/ })).toHaveAttribute('aria-pressed', 'true'));
  });
});
