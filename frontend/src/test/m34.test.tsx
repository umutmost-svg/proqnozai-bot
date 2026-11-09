import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BackendSportsDataProvider } from '../data/BackendSportsDataProvider';
import { pickTop } from '../data/filter';
import type { SportEvent } from '../data/models';
import { CLOSE_MARGIN, outcomeTones, pickProbability } from '../data/probabilities';
import { reportText, supportUsername, validEventId } from '../data/support';
import { monthGrid } from '../components/CalendarSheet';
import { createTranslator, plural } from '../i18n';
import { renderApp } from './renderApp';

/** M3.4: AI redesign, preferred outcome, calendar, featured match, search, coming soon, support. */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const TOURNAMENTS = [{ id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org' }];
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false };
const side = (id: number, name: string, shortName: string | null = null) => ({ id: `fd:team:${id}`, name, shortName, crest: null });
const ev = (id: number, startsAt: string, status = 'upcoming', extra: Record<string, unknown> = {}) => ({
  id: `fd:${id}`, sport: 'football', tournamentId: 'fd:PL', startsAt, status, sourceStatus: 'X',
  home: side(354, 'Crystal Palace FC', 'Crystal Palace'), away: side(64, 'Liverpool FC', 'Liverpool'),
  score: status === 'finished' ? [1, 2] : null, halfTime: null, minute: null, awarded: false,
  stats: null, statsAvailability: 'unavailable', source, ...extra,
});
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const basisTeam = (name: string, form: string, goalsFor: number, goalsAgainst: number, pos: number | null, tablePoints: number | null) => ({
  name, matches: form.length, form,
  wins: form.split('W').length - 1, draws: form.split('D').length - 1, losses: form.split('L').length - 1,
  points: (form.split('W').length - 1) * 3 + (form.split('D').length - 1), goalsFor, goalsAgainst,
  tablePosition: pos, tablePoints,
});
const analysis = (pick: unknown, probabilities = { home: 24, draw: 24, away: 52 }) => ({
  eventId: 'fd:4', kind: 'partial', reasons: ['no_odds_source'], generatedAt: '2026-11-08T06:00:00Z', model: 'm',
  probabilities, factors: ['home_form', 'home_goals', 'home_table'],
  basis: { home: basisTeam('Crystal Palace FC', 'LLLWD', 3, 10, 19, 7), away: basisTeam('Liverpool FC', 'WWDWW', 11, 3, 2, 25) },
  estimated: true, ...(pick === undefined ? {} : { pick }),
});

function aiBackend(body: unknown) {
  const fetchImpl = vi.fn<typeof fetch>(async (input) =>
    String(input).startsWith('/api/ai/analysis')
      ? ok(body)
      : ok({ event: ev(4, '2026-11-08T15:00:00Z'), tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'connected' } }),
  );
  return new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
}

describe('probability colours', () => {
  it('marks a favourite and an outsider only beyond the close margin', () => {
    expect(CLOSE_MARGIN).toBe(8);
    expect(outcomeTones(24, 52)).toEqual(['outsider', 'draw', 'favourite']);
    expect(outcomeTones(52, 24)).toEqual(['favourite', 'draw', 'outsider']);
    expect(outcomeTones(40, 33)).toEqual(['neutral', 'draw', 'neutral']); // 7 points: too close
    expect(outcomeTones(41, 33)).toEqual(['favourite', 'draw', 'outsider']); // exactly 8
  });

  it('double chances add up the 1X2 estimate', () => {
    expect([pickProbability('1X', 24, 24, 52), pickProbability('X2', 24, 24, 52), pickProbability('12', 24, 24, 52)]).toEqual([48, 76, 76]);
    expect(pickProbability('NO_BET', 24, 24, 52)).toBeNull();
  });

  it('bars are exactly as wide as the probability, coloured by role', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(analysis({ market: 'X2', reasons: ['away_better_form'], probability: 76 })) });
    const rows = await screen.findAllByTestId('ai-outcome');
    expect(rows.map((r) => r.getAttribute('data-tone'))).toEqual(['outsider', 'draw', 'favourite']);
    expect(screen.getAllByTestId('ai-bar').map((b) => (b as HTMLElement).style.width)).toEqual(['24%', '24%', '52%']);
    expect(rows[2]).toHaveTextContent('Фаворит');
    expect(rows[0]).toHaveTextContent('Аутсайдер');
  });

  it('close teams get no favourite label', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(analysis({ market: 'NO_BET', reasons: [], probability: null }, { home: 36, draw: 30, away: 34 })) });
    const rows = await screen.findAllByTestId('ai-outcome');
    expect(rows.map((r) => r.getAttribute('data-tone'))).toEqual(['neutral', 'draw', 'neutral']);
    expect(screen.getByTestId('ai-result')).toHaveTextContent('Шансы команд близки — фаворита нет.');
    expect(screen.queryByText('Фаворит')).toBeNull();
  });
});

describe('recommended bet', () => {
  it('shows the preferred outcome with computed probability and verified reasons, in block order', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(analysis({ market: 'X2', reasons: ['away_better_form', 'away_higher_table'], probability: 76 })) });
    const pick = await screen.findByTestId('ai-pick');
    expect(pick).toHaveAttribute('data-market', 'X2');
    expect(pick).toHaveTextContent('Предпочтительный исход модели · Двойной шанс');
    expect(pick).toHaveTextContent('Liverpool не проиграет');
    expect(pick).toHaveTextContent('Расчетная вероятность: 76%');
    expect(pick).toHaveTextContent('Liverpool — 52%, Ничья — 24%: вместе 76%.');
    expect(pick).toHaveTextContent('У Liverpool 13 очков в последних матчах, у Crystal Palace — 4 очка.');
    expect(pick).toHaveTextContent('Liverpool выше в таблице: 2-е место против 19-го у Crystal Palace.');
    expect(pick).not.toHaveTextContent(/гаранти|коэффициент/);
    expect(screen.queryByTestId('ai-limitations')).toBeNull();
    const order = ['ai-result', 'ai-pick', 'ai-factors', 'ai-basis'].map((id) => screen.getByTestId(id));
    for (let i = 1; i < order.length; i++) {
      expect(order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  it('NO_BET says so without inventing a pick', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(analysis({ market: 'NO_BET', reasons: [], probability: null })) });
    const pick = await screen.findByTestId('ai-pick');
    expect(pick).toHaveTextContent('Нет достаточно надежного прогноза');
    expect(pick).toHaveTextContent('Модель не выбрала исход для рекомендации.');
    expect(pick).not.toHaveTextContent('%');
  });

  it('a v2 answer without its pick is a broken answer, never a page missing the block', async () => {
    await expect(aiBackend(analysis(undefined)).requestAnalysis('fd:4')).rejects.toMatchObject({ reason: 'bad_response' });
    await expect(aiBackend(analysis(null)).requestAnalysis('fd:4')).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it.each([
    [{ market: 'X2', reasons: ['away_better_form'], probability: 80 }],      // probability not the 1X2 sum
    [{ market: 'over_2_5', reasons: [], probability: null }],                 // market we never offer
    [{ market: 'X2', reasons: ['injuries'], probability: 76 }],               // unknown reason
    [{ market: 'X2', reasons: [], probability: 76 }],                         // a pick needs a reason
    [{ market: 'NO_BET', reasons: ['away_better_form'], probability: null }],
  ])('rejects an off-contract pick %#', async (pick) => {
    await expect(aiBackend(analysis(pick)).requestAnalysis('fd:4')).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('rejects counts that disagree with the form string', async () => {
    const body = analysis({ market: 'NO_BET', reasons: [], probability: null });
    (body.basis.home as { wins: number }).wins = 3;
    await expect(aiBackend(body).requestAnalysis('fd:4')).rejects.toMatchObject({ reason: 'bad_response' });
  });
});

describe('natural sentences', () => {
  it('uses the language plural rules', () => {
    const pt = createTranslator('pt-BR');
    expect([0, 1, 2].map((n) => plural(pt, 'pt-BR', 'ai.n.wins', n))).toEqual(['0 vitórias', '1 vitória', '2 vitórias']);
    const ru = createTranslator('ru');
    expect([1, 2, 5, 21].map((n) => plural(ru, 'ru', 'ai.n.wins', n))).toEqual(['1 победа', '2 победы', '5 побед', '21 победа']);
    const en = createTranslator('en');
    expect([1, 2].map((n) => plural(en, 'en', 'ai.n.goals', n))).toEqual(['1 goal', '2 goals']);
    const az = createTranslator('az');
    expect(plural(az, 'az', 'ai.n.wins', 3)).toBe('3 qələbə');
  });

  it('factors read as sentences, never as logs', async () => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(analysis({ market: 'NO_BET', reasons: [], probability: null })) });
    const factors = await screen.findByTestId('ai-factors');
    expect(factors).toHaveTextContent('За последние 5 матчей у Crystal Palace: 1 победа, 1 ничья и 3 поражения (4 очка).');
    expect(factors).toHaveTextContent('За последние 5 матчей Crystal Palace забил 3 гола и пропустил 10.');
    expect(factors).toHaveTextContent('Crystal Palace занимает 19-е место в таблице с 7 очками.');
    // Form chips: newest first, localized letters, the order explained.
    expect(screen.getByTestId('ai-basis')).toHaveTextContent('ППП');
    expect(screen.getByTestId('ai-basis')).toHaveTextContent('Слева самый новый матч');
    expect(screen.getByTestId('ai-basis')).toHaveTextContent('во всех турнирах');
  });

  it.each([
    ['en', 'In the last 5 matches, Crystal Palace have 1 win, 1 draw and 3 defeats (4 points).'],
    ['az', 'Crystal Palace son 5 oyunda: 1 qələbə, 1 heç-heçə və 3 məğlubiyyət (4 xal).'],
  ] as const)('%s sentences', async (lang, text) => {
    renderApp('#/event/fd%3A4/ai', { data: aiBackend(analysis({ market: 'NO_BET', reasons: [], probability: null })), settings: { lang } });
    expect(await screen.findByTestId('ai-factors')).toHaveTextContent(text);
  });
});

describe('home calendar', () => {
  it('builds Monday-first months', () => {
    const nov = monthGrid(2026, 11); // 1 Nov 2026 is a Sunday
    expect(nov[0]).toEqual([null, null, null, null, null, null, '2026-11-01']);
    expect(nov.flat().filter(Boolean)).toHaveLength(30);
    expect(monthGrid(2027, 2).flat().filter(Boolean)).toHaveLength(28);
  });

  it('opens, switches months and years, picks a day, and syncs the strip and list', async () => {
    const user = userEvent.setup();
    renderApp('#/matches');
    await screen.findByTestId('event-list');
    await user.click(screen.getByTestId('calendar-open'));
    const sheet = screen.getByTestId('calendar-sheet');
    expect(within(sheet).getByTestId('calendar-month')).toHaveTextContent('Ноябрь 2026');
    await user.click(within(sheet).getByRole('button', { name: 'Следующий месяц' }));
    expect(within(sheet).getByTestId('calendar-month')).toHaveTextContent('Декабрь 2026');
    await user.click(within(sheet).getByRole('button', { name: 'Следующий год' }));
    expect(within(sheet).getByTestId('calendar-month')).toHaveTextContent('Декабрь 2027');
    await user.click(within(sheet).getByRole('button', { name: 'Предыдущий год' }));
    await user.click(within(sheet).getByRole('button', { name: 'Предыдущий месяц' }));
    await user.click(within(sheet).getByRole('gridcell', { name: '9 ноября' }));
    expect(screen.queryByTestId('calendar-sheet')).toBeNull();
    expect(screen.getByRole('button', { name: '9 ноября', pressed: true })).toBeInTheDocument();
  });

  it('closing without choosing keeps the day; "today" returns to today', async () => {
    const user = userEvent.setup();
    renderApp('#/matches');
    await screen.findByTestId('event-list');
    await user.click(screen.getByRole('button', { name: '7 ноября' }));
    await user.click(screen.getByTestId('calendar-open'));
    await user.click(screen.getByRole('button', { name: 'Закрыть' }));
    expect(screen.getByRole('button', { name: '7 ноября', pressed: true })).toBeInTheDocument();
    await user.click(screen.getByTestId('calendar-open'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('calendar-sheet')).toBeNull();
    await user.click(screen.getByTestId('calendar-open'));
    await user.click(screen.getByRole('button', { name: 'Сегодня' }));
    expect(screen.getByRole('button', { name: '8 ноября', pressed: true })).toBeInTheDocument();
  });

  it('a day far from today moves the strip to it', async () => {
    const user = userEvent.setup();
    renderApp('#/matches');
    await screen.findByTestId('event-list');
    await user.click(screen.getByTestId('calendar-open'));
    await user.click(within(screen.getByTestId('calendar-sheet')).getByRole('gridcell', { name: '25 ноября' }));
    const strip = screen.getByTestId('date-strip');
    expect(within(strip).getByRole('button', { name: '25 ноября', pressed: true })).toBeInTheDocument();
  });

  it('real mode: dots only for days already loaded; days outside the source window are disabled', async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.startsWith('/api/sports/football/events')) return ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, events: [ev(1, '2026-11-08T15:00:00Z')] });
      if (url === '/api/sports/football/tournaments') return ok({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS });
      return new Response('{}', { status: 404 });
    });
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    const calls = fetchImpl.mock.calls.length;
    await user.click(screen.getByTestId('calendar-open'));
    const sheet = screen.getByTestId('calendar-sheet');
    expect(within(sheet).getAllByTestId('calendar-dot')).toHaveLength(1); // only 8 Nov was loaded
    expect(fetchImpl.mock.calls.length).toBe(calls); // opening the calendar asks for nothing
    for (let i = 0; i < 3; i++) await user.click(within(sheet).getByRole('button', { name: 'Следующий месяц' }));
    expect(within(sheet).getByRole('gridcell', { name: '28 февраля' })).toBeDisabled();
    // The last allowed day in the strip: later strip days cannot be chosen either.
    await user.click(within(sheet).getByRole('button', { name: 'Предыдущий месяц' })); // January 2027
    await user.click(within(sheet).getByRole('gridcell', { name: '6 января' })); // today + 59: the last allowed day
    const strip = screen.getByTestId('date-strip');
    expect(within(strip).getByRole('button', { name: '7 января' })).toBeDisabled();
    expect(within(strip).getByRole('button', { name: '5 января' })).toBeEnabled();
  });

  it('a downward swipe closes the sheet only when it is scrolled to the top', async () => {
    const user = userEvent.setup();
    renderApp('#/matches');
    await screen.findByTestId('event-list');
    await user.click(screen.getByTestId('calendar-open'));
    const sheet = screen.getByTestId('calendar-sheet');
    sheet.scrollTop = 120; // scrolled down on a short phone: the swipe scrolls, not closes
    fireEvent.touchStart(sheet, { touches: [{ clientY: 100 }] });
    fireEvent.touchMove(sheet, { touches: [{ clientY: 300 }] });
    fireEvent.touchEnd(sheet);
    expect(screen.getByTestId('calendar-sheet')).toBeInTheDocument();
    sheet.scrollTop = 0;
    fireEvent.touchStart(sheet, { touches: [{ clientY: 100 }] });
    fireEvent.touchMove(sheet, { touches: [{ clientY: 300 }] });
    fireEvent.touchEnd(sheet);
    await waitFor(() => expect(screen.queryByTestId('calendar-sheet')).toBeNull());
  });

  it('returns focus to the calendar button on close', async () => {
    const user = userEvent.setup();
    renderApp('#/matches');
    await screen.findByTestId('event-list');
    await user.click(screen.getByTestId('calendar-open'));
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('calendar-open')));
  });

  it('keeps keyboard focus inside the open calendar', async () => {
    const user = userEvent.setup();
    renderApp('#/matches');
    await screen.findByTestId('event-list');
    await user.click(screen.getByTestId('calendar-open'));
    const sheet = screen.getByTestId('calendar-sheet');
    for (let i = 0; i < 60; i++) {
      await user.tab();
      expect(sheet.contains(document.activeElement)).toBe(true);
    }
  });
});

describe('top matches of the day', () => {
  const at = (id: string, status: SportEvent['status'], tournamentId: string, startsAt: string) =>
    ({ id, sport: 'football', status, tournamentId, startsAt, home: { id: 'a', name: 'A' }, away: { id: 'b', name: 'B' }, ai: { kind: 'unavailable' } }) as SportEvent;

  it('demo order: tournament priority, live and upcoming alike (a kickoff never reorders), finished last, never a stopped match', () => {
    const rank = (id: string) => ['cl', 'pl'].indexOf(id);
    const ids = (list: SportEvent[]) => pickTop(list, rank).map((e) => e.id);
    expect(ids([at('1', 'upcoming', 'pl', '2026-11-08T10:00:00Z'), at('2', 'upcoming', 'cl', '2026-11-08T20:00:00Z')])).toEqual(['2', '1']);
    expect(ids([at('1', 'upcoming', 'cl', '2026-11-08T10:00:00Z'), at('2', 'live', 'pl', '2026-11-08T09:00:00Z')])).toEqual(['1', '2']);
    expect(ids([at('1', 'finished', 'cl', '2026-11-08T10:00:00Z'), at('2', 'postponed', 'cl', '2026-11-08T09:00:00Z'), at('3', 'upcoming', 'pl', '2026-11-08T21:00:00Z')])).toEqual(['3', '1']);
    expect(pickTop(Array.from({ length: 8 }, (_, i) => at(String(i), 'upcoming', 'pl', `2026-11-08T1${i}:00:00Z`)))).toHaveLength(5);
  });

  it('says "match of the day" only for today, the chosen date otherwise, with the real status', async () => {
    const user = userEvent.setup();
    renderApp('#/');
    expect(await screen.findByTestId('hero-title')).toHaveTextContent('Главные матчи дня');
    await user.click(screen.getByRole('button', { name: '7 ноября' }));
    await waitFor(() => expect(screen.getByTestId('hero-title')).toHaveTextContent('Главные матчи · 7 ноября'));
  });
});

describe('coming soon', () => {
  it.each(['Теннис', 'Баскетбол', 'MMA'])('real mode: %s shows a coming-soon screen and no API call', async (tab) => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      String(input).startsWith('/api/sports/football/events')
        ? ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, events: [ev(1, '2026-11-08T15:00:00Z')] })
        : String(input).startsWith('/api/sports/football/top')
          ? ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, top: [{ event: ev(1, '2026-11-08T15:00:00Z'), leaders: false }] })
          : ok({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS }),
    );
    renderApp('#/', { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }) });
    await screen.findByTestId('hero');
    const before = fetchImpl.mock.calls.length;
    await user.click(screen.getByRole('button', { name: tab }));
    expect(await screen.findByTestId('coming-soon')).toHaveTextContent('Скоро');
    expect(screen.queryByTestId('hero')).toBeNull();
    expect(fetchImpl.mock.calls.length).toBe(before);
  });
});

describe('global search (real mode)', () => {
  const searchBody = {
    sport: 'football', query: 'barca',
    teams: [{ id: 'fd:team:81', name: 'FC Barcelona', shortName: 'Barcelona', crest: '/api/crests/team/81', tournaments: ['fd:PD'] }],
    events: [ev(9, '2026-11-20T20:00:00Z', 'upcoming', { tournamentId: 'fd:PD', home: side(81, 'FC Barcelona', 'Barcelona') })],
    coverage: { indexed: ['fd:PL', 'fd:PD'], total: 10, complete: false },
    tournaments: [...TOURNAMENTS, { id: 'fd:PD', sport: 'football', name: 'LaLiga', area: 'Spain', provider: 'football-data.org' }],
    source: COVERAGE,
  };

  function searchBackend(search: Response = ok(searchBody)) {
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.startsWith('/api/sports/football/search')) return search.clone();
      if (url.startsWith('/api/sports/football/events')) return ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, events: [ev(1, '2026-11-08T15:00:00Z')] });
      return ok({ sport: 'football', coverage: COVERAGE, tournaments: TOURNAMENTS });
    });
    return { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }), fetchImpl };
  }

  it('is independent of the chosen date and tournament, debounced, and says when coverage is partial', async () => {
    const user = userEvent.setup();
    const { data, fetchImpl } = searchBackend();
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    await user.type(screen.getByPlaceholderText('Поиск матча или команды'), 'barca');
    const results = await screen.findByTestId('search-results');
    const searches = fetchImpl.mock.calls.map(([u]) => String(u)).filter((u) => u.includes('/search'));
    expect(searches).toEqual(['/api/sports/football/search?q=barca']); // one request after typing settles
    expect(within(results).getByTestId('search-team')).toHaveTextContent('Barcelona');
    expect(within(results).getByTestId('search-team')).toHaveTextContent('LaLiga');
    expect(within(results).getByTestId('search-coverage')).toHaveTextContent('Поиск идет по 2 из 10 турниров');
    // The same query can be refreshed while the index fills up.
    await user.click(within(results).getByRole('button', { name: 'Обновить результаты' }));
    await waitFor(() => expect(fetchImpl.mock.calls.filter(([u]) => String(u).includes('/search'))).toHaveLength(2));
  });

  it('an API error shows an error with retry, not "nothing found"', async () => {
    const user = userEvent.setup();
    const { data } = searchBackend(new Response('{"error":"source_unavailable","reason":"rate_limited"}', { status: 503 }));
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    await user.type(screen.getByPlaceholderText('Поиск матча или команды'), 'barca');
    expect(await screen.findByRole('button', { name: 'Повторить' })).toBeInTheDocument();
    expect(screen.queryByText('Ничего не найдено')).toBeNull();
  });

  it('a broken answer is an error, never a partial list', async () => {
    const { data } = searchBackend(ok({ ...searchBody, teams: [...searchBody.teams, { id: 'x' }] }));
    await expect(data.search('football', 'barca')).rejects.toMatchObject({ reason: 'bad_response' });
    const lying = searchBackend(ok({ ...searchBody, coverage: { indexed: ['fd:PL'], total: 10, complete: true } }));
    await expect(lying.data.search('football', 'barca')).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('the search box stops at the server limit', async () => {
    const { data } = searchBackend();
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    expect(screen.getByPlaceholderText('Поиск матча или команды')).toHaveAttribute('maxLength', '40');
  });

  it('a team page keeps postponed matches apart from played ones', async () => {
    const team = { id: 'fd:team:81', name: 'FC Barcelona', shortName: 'Barcelona', crest: null, tournaments: ['fd:PD'] };
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      ok({
        sport: 'football', team,
        events: [
          ev(1, '2026-11-20T20:00:00Z', 'upcoming', { home: side(81, 'FC Barcelona', 'Barcelona'), tournamentId: 'fd:PD' }),
          ev(2, '2026-11-02T20:00:00Z', 'postponed', { home: side(81, 'FC Barcelona', 'Barcelona'), tournamentId: 'fd:PD' }),
          ev(3, '2026-11-01T20:00:00Z', 'finished', { home: side(81, 'FC Barcelona', 'Barcelona'), tournamentId: 'fd:PD' }),
        ],
        coverage: { indexed: ['fd:PD'], total: 2, complete: false },
        tournaments: [{ id: 'fd:PD', sport: 'football', name: 'LaLiga', area: 'Spain', provider: 'football-data.org' }],
        source: COVERAGE,
      }),
    );
    renderApp('#/team/fd%3Ateam%3A81', { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }) });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Обновить результаты' })); // partial index: refresh in place
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    expect(within(await screen.findByTestId('team-played')).getAllByTestId('event-card').map((c) => c.getAttribute('data-event-id'))).toEqual(['fd:3']);
    expect(within(screen.getByTestId('team-other')).getAllByTestId('event-card').map((c) => c.getAttribute('data-event-id'))).toEqual(['fd:2']);
    expect(within(screen.getByTestId('team-upcoming')).getAllByTestId('event-card').map((c) => c.getAttribute('data-event-id'))).toEqual(['fd:1']);
  });

  it.each(['Барса', '!!', '  '])('"%s" is not sent to the server', async (q) => {
    const user = userEvent.setup();
    const { data, fetchImpl } = searchBackend();
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    await user.type(screen.getByPlaceholderText('Поиск матча или команды'), q);
    if (q.trim()) expect(await screen.findByTestId('search-hint')).toBeInTheDocument();
    if (q === 'Барса') await waitFor(() => expect(screen.getByTestId('search-hint')).toHaveTextContent('латиницей'));
    await new Promise((r) => setTimeout(r, 400));
    expect(fetchImpl.mock.calls.some(([u]) => String(u).includes('/search'))).toBe(false);
  });

  it('one letter does not search', async () => {
    const user = userEvent.setup();
    const { data, fetchImpl } = searchBackend();
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    await user.type(screen.getByPlaceholderText('Поиск матча или команды'), 'b');
    expect(await screen.findByTestId('search-hint')).toBeInTheDocument();
    expect(fetchImpl.mock.calls.some(([u]) => String(u).includes('/search'))).toBe(false);
  });
});

describe('support', () => {
  it('reads only a valid configured username', () => {
    expect(supportUsername('')).toBeNull();
    expect(supportUsername(undefined)).toBeNull();
    expect(supportUsername('@ProqnozAI_support')).toBe('ProqnozAI_support');
    expect(supportUsername('bad name')).toBeNull();
    expect(supportUsername('https://evil.example')).toBeNull();
  });

  it('builds a report without any session data', () => {
    const text = reportText(
      { topicLabel: 'AI-прогноз', description: 'Не открывается разбор', eventId: 'fd:560643', lang: 'ru', timeZone: 'Asia/Baku', mode: 'real' },
      '0.1.0',
    );
    expect(text).toContain('Match: fd:560643');
    expect(text).toContain('App: 0.1.0 · real · ru · Asia/Baku');
    expect(text).not.toMatch(/initData|hash=|cookie|token|csrf/i);
    expect(validEventId('fd:1')).toBe(true);
    expect(validEventId('drop table')).toBe(false);
  });

  it('without a configured channel the actions are disabled and nothing claims to be sent', async () => {
    const user = userEvent.setup();
    renderApp('#/profile');
    await user.click(await screen.findByRole('link', { name: /Помощь и поддержка/ }));
    expect(await screen.findByTestId('support-contact')).toHaveTextContent('Канал поддержки пока не подключен.');
    await user.type(screen.getByRole('textbox', { name: /Описание/ }), 'Счет матча не обновляется');
    expect(screen.getByTestId('support-prepare')).toBeDisabled();
    expect(screen.queryByTestId('support-status')).toBeNull();
    expect(screen.getByTestId('support-faq')).toHaveTextContent('Почему вероятность не гарантирует результат?');
  });
});

it('plural forms exist in every dictionary for every count key', () => {
  for (const lang of ['ru', 'az', 'en'] as const) {
    const t = createTranslator(lang);
    for (const key of ['ai.n.wins', 'ai.n.draws', 'ai.n.losses', 'ai.n.points', 'ai.n.goals', 'ai.n.lastMatches'] as const) {
      for (const n of [0, 1, 2, 5, 11, 21]) expect(plural(t, lang, key, n)).toContain(String(n));
    }
  }
  render(<span />);
});
