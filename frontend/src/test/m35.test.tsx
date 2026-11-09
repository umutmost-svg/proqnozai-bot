import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BackendSportsDataProvider } from '../data/BackendSportsDataProvider';
import { marketsConsistent } from '../data/markets';
import { MARKET_KEYS, type MarketKey } from '../data/models';
import { LANGS, dictionaries } from '../i18n';
import { FORECAST_V3 } from './fixtures/forecastV3';
import { renderApp } from './renderApp';

/** M3.5: goal-model analysis (markets, pick, alternatives) and tournament tables. */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const PL = { id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org' };
const CL = { id: 'fd:CL', sport: 'football', name: 'UEFA Champions League', area: 'Europe', provider: 'football-data.org' };
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false };
const side = (id: number, name: string, shortName: string | null = null) => ({ id: `fd:team:${id}`, name, shortName, crest: null });
const ev = (id: number, tournamentId = 'fd:PL') => ({
  id: `fd:${id}`, sport: 'football', tournamentId, startsAt: '2026-11-08T15:00:00Z', status: 'upcoming', sourceStatus: 'TIMED',
  home: side(354, 'Crystal Palace FC', 'Crystal Palace'), away: side(64, 'Liverpool FC', 'Liverpool'),
  score: null, halfTime: null, minute: null, awarded: false, stats: null, statsAvailability: 'unavailable', source,
});
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const team = (name: string, form: string, goalsFor: number, goalsAgainst: number, pos: number | null, tablePoints: number | null) => ({
  name, matches: form.length, form,
  wins: form.split('W').length - 1, draws: form.split('D').length - 1, losses: form.split('L').length - 1,
  points: (form.split('W').length - 1) * 3 + (form.split('D').length - 1), goalsFor, goalsAgainst, tablePosition: pos, tablePoints,
});
const M = FORECAST_V3.markets as Record<MarketKey, number>;

type Over = Record<string, unknown>;
function v3(over: Over = {}, forecast: Over = {}) {
  return {
    schema: 'v3', eventId: 'fd:4', kind: 'partial', reasons: ['no_odds_source'], generatedAt: '2026-11-08T06:00:00Z', model: 'm',
    probabilities: { home: M['1'], draw: M.X, away: M['2'] },
    factors: ['away_rating', 'away_form', 'home_advantage'],
    basis: { home: team('Crystal Palace FC', 'LLLWD', 3, 10, 19, 7), away: team('Liverpool FC', 'WWDWW', 11, 3, 2, 25) },
    estimated: true,
    pick: { market: 'X2', reasons: ['away_stronger_rating', 'away_better_form'], probability: M.X2 },
    alternatives: [{ market: 'H2+1.5', probability: M['H2+1.5'] }],
    forecast: { ...FORECAST_V3, noBetReason: null, ...forecast },
    ...over,
  };
}

function aiBackend(body: unknown, tournament = PL) {
  const fetchImpl = vi.fn<typeof fetch>(async (input) =>
    String(input).startsWith('/api/ai/analysis')
      ? ok(body)
      : ok({ event: ev(4, tournament.id), tournament, coverage: COVERAGE, ai: { status: 'connected' } }),
  );
  return new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
}

describe('v3 analysis is read strictly', () => {
  const read = async (body: unknown) => aiBackend(body).requestAnalysis('fd:4');

  it('accepts the backend shape and keeps every number the model gave', async () => {
    const a = await read(v3());
    expect(a.probabilities).toEqual([M['1'], M.X, M['2']]);
    expect(a.pick).toEqual({ market: 'X2', reasons: ['awayStrongerRating', 'awayBetterForm'], probability: M.X2 });
    expect(a.alternatives).toEqual([{ market: 'H2+1.5', probability: M['H2+1.5'] }]);
    expect(a.forecast?.markets['O2.5']).toBe(M['O2.5']);
    expect(marketsConsistent(M)).toBe(true);
  });

  it.each([
    ['1X2 that differs from the model', v3({ probabilities: { home: 20, draw: 20, away: 60 } })],
    ['a pick percent that is not the market’s', v3({ pick: { market: 'X2', reasons: [], probability: M.X2 - 1 } })],
    ['a recommended unproven market', v3({ pick: { market: 'O2.5', reasons: [], probability: M['O2.5'] } })],
    ['a pick in a limited competition', v3({}, { limited: true })],
    ['NO_BET with alternatives', v3({ pick: { market: 'NO_BET', reasons: [], probability: null } }, { noBetReason: 'no_qualifying_market' })],
    ['NO_BET without its reason', v3({ pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [] })],
    ['an alternative with the wrong percent', v3({ alternatives: [{ market: 'H2+1.5', probability: 50 }] })],
    ['the pick repeated as an alternative', v3({ alternatives: [{ market: 'X2', probability: M.X2 }] })],
    ['an unproven family as an alternative', v3({ alternatives: [{ market: 'O2.5', probability: M['O2.5'] }] })],
    ['markets that break an identity', v3({}, { markets: { ...M, '1X': M['1X'] + 1 } })],
    ['a two-way pair that is not 100', v3({}, { markets: { ...M, 'O2.5': M['O2.5'] + 1 } })],
    ['an unknown market key', v3({}, { markets: { ...M, CORNERS: 50 } })],
    ['free text', v3({ summary: 'Liverpool will win' })],
    ['an unknown schema', { ...v3(), schema: 'v9' }],
    ['a forecast without its recommendation status', v3({}, { experimental: undefined })],
  ])('rejects %s', async (_name, body) => {
    await expect(read(body)).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('accepts a points deduction below zero in the table evidence', async () => {
    const body = v3();
    body.basis.home = { ...body.basis.home, tablePoints: -2 };
    const a = await read(body);
    expect(a.basis?.home.tablePoints).toBe(-2);
  });

  it('accepts a full NO_BET with its reason and nothing else', async () => {
    const a = await read(v3({ pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [] }, { noBetReason: 'small_sample' }));
    expect(a.pick?.market).toBe('NO_BET');
    expect(a.forecast?.noBetReason).toBe('smallSample');
    const other = await read(v3({ pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [] }, { noBetReason: 'unvalidated_competition', limited: true }));
    expect(other.forecast?.noBetReason).toBe('unvalidatedCompetition');
    const early = await read(v3({ pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [] }, { noBetReason: 'early_forecast' }));
    expect(early.forecast?.noBetReason).toBe('earlyForecast');
    const partial = await read(v3({ pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [] }, { noBetReason: 'partial_history' }));
    expect(partial.forecast?.noBetReason).toBe('partialHistory');
  });

  it('keeps reading older v2 answers, without the model-only reasons and factors', async () => {
    const v2 = { ...v3(), schema: undefined, forecast: undefined, alternatives: undefined, factors: ['away_form', 'away_goals'],
      pick: { market: 'X2', reasons: ['away_better_form'], probability: M.X + M['2'] } };
    const a = await read(v2);
    expect(a.forecast).toBeUndefined();
    expect(a.pick?.market).toBe('X2');
    await expect(read({ ...v2, factors: ['away_rating', 'away_form'] })).rejects.toBeTruthy();
    await expect(read({ ...v2, pick: { ...v2.pick, reasons: ['away_stronger_rating'] } })).rejects.toBeTruthy();
  });

  it('an insufficient model answer carries no numbers', async () => {
    const a = await read({ schema: 'v3', eventId: 'fd:4', kind: 'insufficient', reasons: ['model_team_history', 'no_odds_source'],
      generatedAt: null, model: null, probabilities: null, factors: [], basis: null, estimated: false, pick: null,
      forecast: null, alternatives: [] });
    expect(a.kind).toBe('insufficient');
    expect(a.reasons).toEqual(['modelTeamHistory', 'noOddsSource']);
  });
});

describe('AI screen with the goal model', () => {
  it('shows the sections in order: the pick, alternatives, every market, factors and the numbers', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(v3()) });
    const pick = await screen.findByTestId('ai-pick');
    const order = ['ai-result', 'ai-pick', 'ai-alternatives', 'ai-markets', 'ai-factors', 'ai-basis']
      .map((id) => screen.getByTestId(id));
    for (let i = 1; i < order.length; i++) {
      expect(order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(screen.getByTestId('ai-match-meta')).toHaveTextContent('Premier League');
    expect(pick).toHaveTextContent('Рекомендуемая ставка');
    expect(pick).toHaveTextContent('Двойной шанс');
    expect(pick).toHaveTextContent('Liverpool не проиграет');
    expect(pick).toHaveTextContent(`Расчетная вероятность: ${M.X2}%`);
    expect(pick).toHaveTextContent('Liverpool сильнее по модели: атака 1,45, оборона 0,75');
    const alt = screen.getByTestId('ai-alternatives');
    expect(alt).toHaveTextContent('Фора Liverpool (+1.5)');
    expect(alt).toHaveTextContent(`${M['H2+1.5']}%`);
    const markets = screen.getByTestId('ai-markets');
    expect(within(markets).getByText('Тотал больше 2.5').parentElement).toHaveTextContent(`${M['O2.5']}%`);
    expect(screen.getByTestId('ai-scores')).toHaveTextContent('0:1');
    expect(screen.getByTestId('ai-model-stats')).toHaveTextContent('1,90');
    expect(screen.getByTestId('ai-factors')).toHaveTextContent('Домашний фактор турнира');
    // Without prices: no market block, and no boilerplate anywhere.
    expect(screen.queryByTestId('ai-market-value')).toBeNull();
    expect(screen.queryByTestId('price-line')).toBeNull();
    for (const id of ['ai-sources', 'ai-limitations', 'ai-pick-caveat', 'ai-pick-experimental', 'ai-limited', 'ai-model-source', 'ai-source']) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    expect(document.body.textContent).not.toMatch(/гаранти|Экспериментально|оценочн|football-data|goals-1|Пуассон|AI может ошибаться|не точнее среднего/);
  });

  it('the Champions League: probabilities shown, no recommendation, its reason stated once', async () => {
    const body = v3({ pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [] },
      { limited: true, noBetReason: 'limited_competition' });
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(body, CL) });
    const pick = await screen.findByTestId('ai-pick');
    expect(pick).toHaveAttribute('data-market', 'NO_BET');
    expect(pick).toHaveTextContent('В Лиге чемпионов у модели мало матчей на команду');
    expect(screen.queryByTestId('ai-limited')).toBeNull();
    expect(screen.queryByTestId('ai-alternatives')).toBeNull();
    expect(screen.queryByTestId('ai-limitations')).toBeNull();
  });

  it.each(LANGS)('renders every block in %s without a missing translation', async (lang) => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(v3(READY_OVER)), settings: { lang } });
    await screen.findByTestId('ai-market-value');
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/ai\.(pick|family|markets|stats|sources|alt|limits|value|price)\./);
    expect(text).not.toMatch(/\{\w+\}/);
    expect(screen.getByTestId('ai-market-value')).toHaveTextContent(dictionaries[lang]['ai.value.title']);
  });
});

// --- prices against the model ---------------------------------------------------------------

/** The backend's quote for one market at these odds and market probability (same rounding). */
function quote(market: MarketKey, odds: number, marketP: number, modelP = M[market]) {
  const r = (x: number) => Math.round(x * 10) / 10;
  return { market, odds, impliedProbability: r(100 / odds), marketProbability: marketP, modelProbability: modelP,
    edge: r(modelP - marketP), expectedValue: r((modelP / 100) * odds * 100 - 100), margin: 4.8 };
}
const PRICES = { fetchedAt: '2026-11-08T05:58:00Z', validFor: 840, quotes: [quote('1', 4.2, 22.6), quote('X', 3.9, 24.3), quote('2', 1.85, 53.1),
  quote('X2', 1.3, 77.4), quote('O2.5', 1.8, 53.0)] };
const READY_OVER = { kind: 'ready', reasons: [], estimated: false, market: PRICES };

describe('the model against the market', () => {
  it('accepts consistent prices only with a complete analysis', async () => {
    const a = await aiBackend(v3(READY_OVER)).requestAnalysis('fd:4');
    expect(a.kind).toBe('ready');
    expect(a.market?.quotes.map((q) => q.market)).toEqual(['1', 'X', '2', 'X2', 'O2.5']);
    expect(a.market?.fetchedAt).toBe('2026-11-08T05:58:00Z');
  });

  it.each([
    ['prices with an incomplete analysis', v3({ market: PRICES })],
    ['a complete analysis without prices', v3({ kind: 'ready', reasons: [], estimated: false })],
    ['an implied probability that is not 1/odds', v3({ ...READY_OVER, market: { ...PRICES, quotes: [{ ...quote('2', 1.85, 53.1), impliedProbability: 60 }] } })],
    ['a model figure that is not the shown percent', v3({ ...READY_OVER, market: { ...PRICES, quotes: [quote('2', 1.85, 53.1, M['2'] + 5)] } })],
    ['an expected value that does not follow', v3({ ...READY_OVER, market: { ...PRICES, quotes: [{ ...quote('2', 1.85, 53.1), expectedValue: 30 }] } })],
    ['an edge that does not follow', v3({ ...READY_OVER, market: { ...PRICES, quotes: [{ ...quote('2', 1.85, 53.1), edge: 9 }] } })],
    ['odds below 1.01', v3({ ...READY_OVER, market: { ...PRICES, quotes: [quote('2', 1.0, 53.1)] } })],
    ['a market the feed never prices', v3({ ...READY_OVER, market: { ...PRICES, quotes: [quote('H2+1.5', 1.2, 80)] } })],
    ['a repeated market', v3({ ...READY_OVER, market: { ...PRICES, quotes: [quote('2', 1.85, 53.1), quote('2', 1.85, 53.1)] } })],
    ['no time', v3({ ...READY_OVER, market: { ...PRICES, fetchedAt: '2026-11-08 05:58' } })],
    ['no quotes', v3({ ...READY_OVER, market: { ...PRICES, quotes: [] } })],
    ['no validity', v3({ ...READY_OVER, market: { ...PRICES, validFor: undefined } })],
    ['an expired validity', v3({ ...READY_OVER, market: { ...PRICES, validFor: 0 } })],
    ['a validity beyond the longest the server gives', v3({ ...READY_OVER, market: { ...PRICES, validFor: 901 } })],
  ])('rejects %s', async (_, body) => {
    await expect(aiBackend(body).requestAnalysis('fd:4')).rejects.toThrow();
  });

  it('shows the price of the pick and every priced outcome, largest model advantage first', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(v3(READY_OVER)) });
    const pick = await screen.findByTestId('ai-pick');
    const x2 = PRICES.quotes.find((q) => q.market === 'X2')!;
    expect(within(pick).getByTestId('price-line')).toHaveTextContent(
      `Коэффициент 1,30 · рынок ${String(x2.marketProbability).replace('.', ',')}% · модель ${M.X2}% · матожидание`);
    const block = screen.getByTestId('ai-market-value');
    const rows = [...block.querySelectorAll('li[data-market]')].map((r) => r.getAttribute('data-market'));
    const byEdge = [...PRICES.quotes].sort((a, b) => b.edge - a.edge).map((q) => q.market);
    expect(rows).toEqual(byEdge);
    expect(screen.getByTestId('ai-market-asof')).toHaveTextContent('Коэффициенты на');
    expect(screen.getByTestId('ai-market-asof')).toHaveTextContent('маржа на исход матча 4,8%');
    expect(within(screen.getByTestId('ai-markets')).getByText('кэф 1,80')).toBeInTheDocument();
    // Only numbers: no bookmaker is named anywhere.
    expect(document.body.textContent).not.toMatch(/mostbet|bookmaker name/i);
  });

  it('prices disappear on screen once their validity ends; the analysis stays', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(v3({ ...READY_OVER, market: { ...PRICES, validFor: 1 } })) });
    expect(await screen.findByTestId('ai-market-value')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId('ai-market-value')).toBeNull(), { timeout: 3000 });
    expect(screen.queryByTestId('price-line')).toBeNull();
    expect(within(screen.getByTestId('ai-markets')).queryByText(/кэф/)).toBeNull();
    expect(screen.getByTestId('ai-pick')).toHaveTextContent('Liverpool не проиграет');
  });

  it('the provider attribution is in the screen footer, not in the analysis', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(v3(READY_OVER)) });
    expect(await screen.findByTestId('source-footer')).toHaveTextContent(ATTR);
    expect(screen.getByTestId('ai-result')).not.toHaveTextContent(ATTR);
  });

  it('a large gap below the market is named too, never reported as agreement', async () => {
    // 1X2: the model 3.8 points under the market on the draw, 1.9 over on each win.
    const quotes = [quote('1', 4.2, M['1'] - 1.9), quote('X', 3.9, M.X + 3.8), quote('2', 1.85, M['2'] - 1.9)];
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(v3({ ...READY_OVER, market: { ...PRICES, quotes } })) });
    const verdict = await screen.findByTestId('ai-market-verdict');
    expect(verdict).toHaveTextContent('Наибольшее расхождение — Ничья');
    expect(verdict).toHaveTextContent(/\([-−]3,8 п\.п\.\)/);
  });

  it('names the largest difference, or says the model agrees with the market', async () => {
    const best = [...PRICES.quotes].sort((a, b) => b.edge - a.edge)[0]!;
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(v3(READY_OVER)) });
    const verdict = await screen.findByTestId('ai-market-verdict');
    if (best.edge >= 2) expect(verdict).toHaveTextContent('Наибольшее расхождение');
    else expect(verdict).toHaveTextContent('Модель и рынок оценивают матч одинаково');
  });
});

// --- tournament tables ------------------------------------------------------------------

const row = (pos: number, id: number, name: string, pts: number, gd: number) => ({
  position: pos, team: side(id, `${name} FC`, name), played: 11, won: 7, draw: 2, lost: 2,
  goalsFor: 20, goalsAgainst: 20 - gd, goalDifference: gd, points: pts,
});
const SEASON = { startDate: '2026-08-21', endDate: '2027-05-30', currentMatchday: 11 };
const standings = (blocks: unknown[], tournament = PL) =>
  ({ sport: 'football', coverage: COVERAGE, tournament, season: SEASON, blocks, fetchedAt: '2026-11-08T06:00:00Z', stale: false });
const LEAGUE = [{ stage: 'REGULAR_SEASON', group: 'Matchday', rows: [row(1, 57, 'Arsenal', 25, 14), row(2, 65, 'Man City', 25, 16), row(3, 64, 'Liverpool', 23, 9)] }];

function tableBackend(table: () => Response) {
  const calendar = { sport: 'football', coverage: COVERAGE, tournament: PL, season: SEASON, events: [ev(9)], fetchedAt: '2026-11-08T06:00:00Z', stale: false, ai: { status: 'connected' } };
  const fetchImpl = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (url.endsWith('/standings')) return table();
    if (url.endsWith('/calendar')) return ok(calendar);
    if (url.startsWith('/api/sports/football/teams/')) {
      return ok({ sport: 'football', team: { ...side(64, 'Liverpool FC', 'Liverpool'), tournaments: ['fd:PL'] }, events: [ev(9)],
        coverage: { indexed: ['fd:PL'], total: 1, complete: true }, tournaments: [PL], source: COVERAGE });
    }
    if (url === '/api/sports/football/tournaments') return ok({ sport: 'football', coverage: COVERAGE, tournaments: [PL] });
    return ok({ sport: 'football', coverage: COVERAGE, tournaments: [PL], events: [] });
  });
  return { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }), fetchImpl };
}
const calls = (f: ReturnType<typeof vi.fn>, suffix: string) => f.mock.calls.filter(([u]) => String(u).endsWith(suffix)).length;

describe('tournament table tab', () => {
  it('keeps the source order and numbers, shows the season, and never refetches on tab switches', async () => {
    const user = userEvent.setup();
    const { data, fetchImpl } = tableBackend(() => ok(standings(LEAGUE)));
    renderApp('#/tournament/fd%3APL', { data });
    await screen.findByTestId('calendar-events');
    await user.click(screen.getByRole('tab', { name: 'Таблица' }));
    const rows = await screen.findAllByTestId('standings-row');
    // Arsenal above Man City on equal points: the provider's tie-break, not re-sorted by goal difference.
    expect(rows.map((r) => r.getAttribute('data-team'))).toEqual(['fd:team:57', 'fd:team:65', 'fd:team:64']);
    const cells = within(rows[0]!).getAllByRole('cell').map((c) => c.textContent);
    expect([cells[0], ...cells.slice(2)]).toEqual(['1', '11', '+14', '25', '7', '2', '2', '20:6']);
    expect(cells[1]).toContain('Arsenal');
    expect(screen.getByTestId('standings')).toHaveTextContent('Сезон 2026/27');
    expect(screen.getByTestId('standings-source')).toHaveTextContent('Источник: football-data.org');
    await user.click(screen.getByRole('tab', { name: 'Матчи' }));
    await user.click(screen.getByRole('tab', { name: 'Таблица' }));
    expect(calls(fetchImpl, '/standings')).toBe(1);
    expect(calls(fetchImpl, '/calendar')).toBe(1);
  });

  it('a team row opens the team, and coming back keeps the table tab', async () => {
    const user = userEvent.setup();
    const { data } = tableBackend(() => ok(standings(LEAGUE)));
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    const rows = await screen.findAllByTestId('standings-row');
    await user.click(within(rows[2]!).getByRole('button'));
    await waitFor(() => expect(window.location.hash).toBe('#/team/fd%3Ateam%3A64'));
    window.history.back();
    await waitFor(() => expect(window.location.hash).toBe('#/tournament/fd%3APL?tab=table'));
    expect(await screen.findByRole('tab', { name: 'Таблица' })).toHaveAttribute('aria-selected', 'true');
  });

  it('the Champions League league phase is one table with its own heading', async () => {
    const blocks = [{ stage: 'LEAGUE_STAGE', group: 'League phase', rows: [row(1, 5, 'Bayern', 12, 9), row(2, 81, 'Barcelona', 12, 8)] }];
    const { data } = tableBackend(() => ok(standings(blocks, CL)));
    renderApp('#/tournament/fd%3ACL?tab=table', { data });
    const block = await screen.findByTestId('standings-block');
    expect(block).toHaveAttribute('data-stage', 'LEAGUE_STAGE');
    expect(block).toHaveTextContent('Лиговая фаза');
  });

  it('groups stay separate tables', async () => {
    const blocks = ['A', 'B'].map((g, i) => ({ stage: 'GROUP_STAGE', group: `GROUP_${g}`, rows: [row(1, 10 + i, `T${g}`, 6, 3)] }));
    const { data } = tableBackend(() => ok(standings(blocks)));
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    const found = await screen.findAllByTestId('standings-block');
    expect(found.map((b) => b.querySelector('h3')?.textContent)).toEqual(['Группа A', 'Группа B']);
  });

  it('a points deduction below zero is shown as given, not rejected', async () => {
    const deducted = [{ ...LEAGUE[0], rows: [...LEAGUE[0]!.rows, { ...row(4, 61, 'Chelsea', -2, -10), position: 4 }] }];
    const { data } = tableBackend(() => ok(standings(deducted)));
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    const rows = await screen.findAllByTestId('standings-row');
    expect(within(rows[3]!).getAllByRole('cell')[4]).toHaveTextContent('-2');
  });

  it('an unfamiliar stage keeps the source label as its heading', async () => {
    const blocks = [{ stage: 'PLAYOFFS', group: null, rows: [row(1, 1, 'A', 3, 1)] }, { stage: 'FINAL_STAGE', group: null, rows: [row(1, 2, 'B', 3, 1)] }];
    const { data } = tableBackend(() => ok(standings(blocks)));
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    const found = await screen.findAllByTestId('standings-block');
    expect(found.map((b) => b.querySelector('h3')?.textContent)).toEqual(['PLAYOFFS', 'FINAL_STAGE']);
  });

  it('a single unfamiliar stage still has its heading', async () => {
    const { data } = tableBackend(() => ok(standings([{ stage: 'PLAYOFFS', group: null, rows: [row(1, 1, 'A', 3, 1)] }])));
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    const block = await screen.findByTestId('standings-block');
    expect(block.querySelector('h3')?.textContent).toBe('PLAYOFFS');
  });

  it('a competition without a table says so instead of inventing one', async () => {
    const { data } = tableBackend(() => ok(standings([])));
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    expect(await screen.findByText('У турнира нет таблицы')).toBeInTheDocument();
  });

  it('a broken table or a source error is an error with retry, and a retry asks again', async () => {
    const user = userEvent.setup();
    let answer = () => ok(standings([{ ...LEAGUE[0], rows: [{ ...row(1, 57, 'Arsenal', 25, 14), points: null }] }]));
    const { data, fetchImpl } = tableBackend(() => answer());
    renderApp('#/tournament/fd%3APL?tab=table', { data });
    expect(await screen.findByText('Таблица недоступна')).toBeInTheDocument();
    answer = () => ok(standings(LEAGUE));
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findAllByTestId('standings-row')).toHaveLength(3);
    expect(calls(fetchImpl, '/standings')).toBe(2);
  });

  it.each(LANGS)('the table is translated (%s)', async (lang) => {
    const { data } = tableBackend(() => ok(standings(LEAGUE)));
    renderApp('#/tournament/fd%3APL?tab=table', { data, settings: { lang } });
    await screen.findAllByTestId('standings-row');
    expect(screen.getByRole('tab', { selected: true })).toHaveTextContent(dictionaries[lang]['tournament.tab.table']);
    expect(document.body.textContent).not.toMatch(/standings\.|tournament\./);
  });
});

describe('market identities', () => {
  it('every shown market set keeps 1X2 = 100, double chances and two-way pairs', () => {
    expect(MARKET_KEYS).toHaveLength(26);
    expect(marketsConsistent(M)).toBe(true);
    expect(marketsConsistent({ ...M, X: M.X + 1 })).toBe(false);
    expect(marketsConsistent({ ...M, 'H1-0.5': M['1'] + 1 })).toBe(false);
    expect(marketsConsistent({ ...M, 'O3.5': M['O2.5'] + 1, 'U3.5': 99 - M['O2.5'] })).toBe(false);
  });
});
