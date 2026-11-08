import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BackendSportsDataProvider } from '../data/BackendSportsDataProvider';
import { renderApp } from './renderApp';

/** The app in real mode against a fake backend: shape of /api/sports, test-only payloads. */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const TOURNAMENTS = [
  { id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org' },
  { id: 'fd:PD', sport: 'football', name: 'LaLiga', area: 'Spain', provider: 'football-data.org' },
];
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false };
// Provider's own update time (09:30 Baku) is earlier than our fetch (09:59 Baku).
const sourceWithUpdate = { ...source, sourceUpdatedAt: '2026-11-08T05:30:00Z' };
const ev = (id: number, status: string, home: string, away: string, extra: Record<string, unknown> = {}) => ({
  id: `fd:${id}`,
  sport: 'football',
  tournamentId: 'fd:PL',
  startsAt: '2026-11-08T15:00:00Z',
  status,
  sourceStatus: 'X',
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
const LIVE = ev(1, 'live', 'Arsenal FC', 'Chelsea FC', { score: [2, 1], minute: 67, startsAt: '2026-11-08T13:00:00Z' });
const POSTPONED = ev(2, 'postponed', 'Everton FC', 'Fulham FC');

function backend(overrides: { football?: Response; detail?: Response } = {}) {
  const fetchImpl = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
    if (url.startsWith('/api/sports/football/events')) {
      return (overrides.football ?? ok({ coverage: COVERAGE, tournaments: TOURNAMENTS, events: [LIVE, POSTPONED] })).clone();
    }
    if (url.startsWith('/api/sports/event/')) {
      if (overrides.detail) return overrides.detail.clone();
      const id = decodeURIComponent(url.split('/').pop()!);
      const found = [LIVE, POSTPONED].find((e) => e.id === id);
      return found
        ? ok({ event: found, tournament: TOURNAMENTS[0], coverage: COVERAGE })
        : new Response('{"error":"not_found"}', { status: 404 });
    }
    if (/\/api\/sports\/(tennis|basketball|mma)\/events/.test(url)) {
      return ok({ coverage: { status: 'not_connected', provider: null, attribution: null }, tournaments: [], events: [] });
    }
    return new Response('{}', { status: 404 });
  });
  const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
  return { data, fetchImpl };
}

describe('real mode', () => {
  it('home shows a real event without demo marks, with the source attribution', async () => {
    const { data } = backend();
    renderApp('#/', { data });
    const hero = await screen.findByTestId('hero');
    expect(within(hero).getByText('Arsenal FC')).toBeInTheDocument();
    expect(within(hero).queryByText('ДЕМО')).toBeNull();
    expect(screen.queryByTestId('demo-footer')).toBeNull();
    expect(screen.getByTestId('source-footer')).toHaveTextContent(ATTR);
    expect(screen.queryByText('Real Madrid')).toBeNull(); // no demo fixture leaks in
  });

  it('catalog lists real events; a postponed match is never shown as upcoming', async () => {
    const { data } = backend();
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    const cards = screen.getAllByTestId('event-card');
    expect(cards.map((c) => c.getAttribute('data-event-id'))).toEqual(['fd:1', 'fd:2']);
    expect(cards[1]).toHaveTextContent('Перенесен');
    expect(cards[1]).not.toHaveTextContent('Скоро');
    expect(cards[1]!.textContent!.match(/Перенесен/g)).toHaveLength(1); // the status once, not "Перенесен · Перенесен"
    expect(cards[0]).toHaveTextContent('2 : 1');
    // Tournament pills come from the backend, with the licensed local logo where we have one.
    expect(within(screen.getByTestId('tournament-strip')).getByRole('button', { name: /LaLiga/ })).toBeInTheDocument();
  });

  it('a sport without a source says so and shows no demo matches', async () => {
    const user = userEvent.setup();
    const { data } = backend();
    renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    await user.click(screen.getByRole('button', { name: 'Теннис' }));
    expect(await screen.findByTestId('not-connected')).toHaveTextContent('Источник данных не подключен');
    expect(screen.queryAllByTestId('event-card')).toHaveLength(0);
  });

  it('event page: real status, no invented statistics, source with time and attribution', async () => {
    const user = userEvent.setup();
    const { data } = backend();
    renderApp('#/event/fd%3A2', { data });
    const hero = await screen.findByTestId('event-hero');
    expect(hero).toHaveTextContent('Перенесен');
    expect(hero).not.toHaveTextContent('ДЕМО');
    expect(screen.getByText(/Матч перенесен/)).toBeInTheDocument();
    expect(screen.getByTestId('source-note')).toHaveTextContent('football-data.org');
    expect(screen.getByTestId('source-note')).toHaveTextContent(ATTR);
    await user.click(screen.getByRole('tab', { name: 'Статистика' }));
    expect(screen.getByText('Источник не предоставляет статистику этого матча.')).toBeInTheDocument();
    expect(screen.queryByText(/Демо интерфейса/)).toBeNull();
  });

  it('live match without a reported minute says "in play", never 0\'', async () => {
    const live = ev(3, 'live', 'Leeds United', 'Burnley FC', { score: [0, 0] });
    const { data } = backend({
      detail: new Response(JSON.stringify({ event: live, tournament: TOURNAMENTS[0], coverage: COVERAGE }), { status: 200 }),
    });
    renderApp('#/event/fd%3A3', { data });
    const hero = await screen.findByTestId('event-hero');
    expect(hero).toHaveTextContent('Идет матч');
    expect(hero).not.toHaveTextContent("0'");
  });

  it('AI screen: no demo analysis for a real match', async () => {
    const { data } = backend();
    renderApp('#/event/fd%3A1/ai', { data });
    expect(await screen.findByTestId('ai-unavailable')).toHaveTextContent('AI-разбор пока не подключен');
    expect(screen.queryByTestId('ai-result')).toBeNull();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it('AI connected: an upcoming match offers the analysis; the result is a labelled estimate on verified numbers', async () => {
    const user = userEvent.setup();
    const upcoming = ev(4, 'upcoming', 'Arsenal FC', 'Chelsea FC');
    const team = (name: string, form: string, points: number, position: number) => ({
      name, matches: form.length, form, points, goalsFor: 9, goalsAgainst: 2, tablePosition: position, tablePoints: 25,
    });
    const analysis = {
      eventId: 'fd:4', kind: 'partial', reasons: ['no_odds_source'], generatedAt: '2026-11-08T06:00:00Z', model: 'm',
      probabilities: { home: 52, draw: 26, away: 22 },
      factors: ['home_form', 'away_table'],
      basis: { home: team('Arsenal FC', 'WWDWW', 13, 2), away: team('Chelsea FC', 'DDDDD', 5, 9) }, estimated: true,
    };
    const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      String(input).startsWith('/api/ai/analysis')
        ? ok(analysis)
        : ok({ event: upcoming, tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'connected' } }),
    );
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
    renderApp('#/event/fd%3A4', { data });
    await user.click(await screen.findByRole('button', { name: 'Создать AI-разбор' }));
    const result = await screen.findByTestId('ai-result');
    expect(screen.getByTestId('ai-probabilities-title')).toHaveTextContent('Оценка вероятностей');
    expect(result).toHaveTextContent('52%');
    const factors = screen.getByTestId('ai-factors');
    expect(factors).toHaveTextContent('Arsenal FC: форма WWDWW, очки 13 (матчей: 5)');
    expect(factors).toHaveTextContent('Chelsea FC: 9-е место в таблице, очки 25');
    expect(screen.getByTestId('ai-basis')).toHaveTextContent('форма WWDWW, очки 13, голы 9:2 (матчей: 5)');
    expect(screen.getByTestId('ai-basis')).toHaveTextContent('2-е место, очки 25');
    expect(screen.queryByText('ДЕМО')).toBeNull();
    expect(screen.queryByText(/иллюстративные/)).toBeNull();
    const credit = within(screen.getByTestId('ai-basis')).getByTestId('ai-source');
    expect(credit).toHaveTextContent('Источник: football-data.org');
    expect(credit).toHaveTextContent(ATTR);
    expect(screen.getByTestId('ai-basis')).toHaveTextContent('и таблица из football-data.org');
  });

  it('AI without the table: the limitation does not claim the table was used', async () => {
    const upcoming = ev(7, 'upcoming', 'Arsenal FC', 'Chelsea FC');
    const team = (name: string) => ({ name, matches: 5, form: 'WWDWW', points: 13, goalsFor: 9, goalsAgainst: 2, tablePosition: null, tablePoints: null });
    const analysis = {
      eventId: 'fd:7', kind: 'partial', reasons: ['no_odds_source'], generatedAt: '2026-11-08T06:00:00Z', model: 'm',
      probabilities: { home: 45, draw: 30, away: 25 }, factors: ['home_form', 'away_form'],
      basis: { home: team('Arsenal FC'), away: team('Chelsea FC') }, estimated: true,
    };
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      String(input).startsWith('/api/ai/analysis')
        ? new Response(JSON.stringify(analysis), { status: 200 })
        : new Response(JSON.stringify({ event: upcoming, tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'connected' } }), { status: 200 }),
    );
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
    renderApp('#/event/fd%3A7/ai', { data });
    const basis = await screen.findByTestId('ai-basis');
    expect(basis).toHaveTextContent('таблица сейчас недоступна');
    expect(basis).not.toHaveTextContent('и таблица из football-data.org');
  });

  it('AI not connected: a real match offers no AI button', async () => {
    const upcoming = ev(5, 'upcoming', 'Leeds United', 'Burnley FC');
    const { data } = backend({
      detail: new Response(JSON.stringify({ event: upcoming, tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'not_connected' } }), { status: 200 }),
    });
    renderApp('#/event/fd%3A5', { data });
    await screen.findByTestId('event-hero');
    expect(screen.queryByRole('button', { name: 'Создать AI-разбор' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'AI-разбор' })).toBeNull();
  });

  it('AI daily limit: says so instead of a generic failure', async () => {
    const upcoming = ev(6, 'upcoming', 'Leeds United', 'Burnley FC');
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      String(input).startsWith('/api/ai/analysis')
        ? new Response(JSON.stringify({ error: 'ai_unavailable', reason: 'limit_reached' }), { status: 429 })
        : new Response(JSON.stringify({ event: upcoming, tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'connected' } }), { status: 200 }),
    );
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
    renderApp('#/event/fd%3A6/ai', { data });
    expect(await screen.findByTestId('ai-error')).toHaveTextContent('Дневной лимит AI-разборов исчерпан');
  });

  it('a source outage is an error state with retry, not demo data', async () => {
    const user = userEvent.setup();
    const outage = new Response(JSON.stringify({ error: 'source_unavailable', reason: 'rate_limited' }), { status: 503 });
    const { data, fetchImpl } = backend({ football: outage });
    renderApp('#/matches', { data });
    expect(await screen.findByRole('button', { name: 'Повторить' })).toBeInTheDocument();
    expect(screen.queryAllByTestId('event-card')).toHaveLength(0);
    const before = fetchImpl.mock.calls.length;
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    await waitFor(() => expect(fetchImpl.mock.calls.length).toBeGreaterThan(before));
  });

  it('live real match: no "demo score" claim; upcoming: promises nothing the source lacks', async () => {
    const { data } = backend();
    const live = renderApp('#/event/fd%3A1', { data });
    await screen.findByTestId('event-hero');
    expect(screen.getByText(/по данным источника/)).toBeInTheDocument();
    expect(screen.queryByText(/Демо-счет/)).toBeNull();
    live.unmount();

    const upcoming = ev(4, 'upcoming', 'Brentford FC', 'Brighton FC');
    const { data: data2 } = backend({
      detail: new Response(JSON.stringify({ event: upcoming, tournament: TOURNAMENTS[0], coverage: COVERAGE }), { status: 200 }),
    });
    renderApp('#/event/fd%3A4', { data: data2 });
    await screen.findByTestId('event-hero');
    expect(screen.getByText(/только если источник их опубликует/)).toBeInTheDocument();
    expect(screen.queryByText(/очные встречи/)).toBeNull();
  });

  it('AI screen in real mode never shows the demo "preparing" stage', async () => {
    const { data } = backend();
    renderApp('#/event/fd%3A1/ai', { data });
    expect(screen.queryByTestId('ai-loading')).toBeNull();
    expect(screen.queryByText(/Демонстрация/)).toBeNull();
    expect(await screen.findByTestId('ai-unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/Демонстрация/)).toBeNull();
  });

  it('profile outside Telegram in real mode does not promise demo data', async () => {
    const { data } = backend();
    renderApp('#/profile', { data, initData: null });
    const status = await screen.findByTestId('auth-status');
    await waitFor(() => expect(status).toHaveTextContent('откройте Mini App в Telegram'));
    expect(status).not.toHaveTextContent(/демо/i);
  });

  it('a day with only stopped matches has no "match of the day"', async () => {
    const stopped = [ev(5, 'postponed', 'A FC', 'B FC'), ev(6, 'cancelled', 'C FC', 'D FC'), ev(7, 'suspended', 'E FC', 'F FC', { score: [1, 0] })];
    const { data } = backend({
      football: new Response(JSON.stringify({ coverage: COVERAGE, tournaments: TOURNAMENTS, events: stopped }), { status: 200 }),
    });
    renderApp('#/', { data });
    expect(await screen.findByText('Матчей по расписанию нет')).toBeInTheDocument();
    expect(screen.queryByTestId('hero')).toBeNull();
  });

  it('real mode offers no AI analysis controls until the feature exists', async () => {
    const { data } = backend();
    const home = renderApp('#/', { data });
    const hero = await screen.findByTestId('hero');
    expect(within(hero).queryByText('Открыть AI-разбор')).toBeNull();
    expect(within(hero).getByRole('button', { name: /Открыть матч/ })).toBeInTheDocument();
    home.unmount();

    const catalog = renderApp('#/matches', { data });
    await screen.findByTestId('event-list');
    expect(screen.queryByText(/Открыть AI-разбор/)).toBeNull();
    catalog.unmount();

    renderApp('#/event/fd%3A2', { data });
    await screen.findByTestId('event-hero');
    expect(screen.queryByRole('tab', { name: 'AI-разбор' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Создать AI-разбор' })).toBeNull();
  });

  it('"updated" shows the provider\'s own update time when it gives one', async () => {
    const withUpdate = ev(8, 'upcoming', 'Wolves', 'Brentford', { source: sourceWithUpdate });
    const { data } = backend({
      detail: new Response(JSON.stringify({ event: withUpdate, tournament: TOURNAMENTS[0], coverage: COVERAGE }), { status: 200 }),
    });
    renderApp('#/event/fd%3A8', { data });
    const note = await screen.findByTestId('source-note');
    expect(note).toHaveTextContent('09:30');
    expect(note).not.toHaveTextContent('09:59');
  });

  it('an invalid provider update time falls back to the fetch time instead of crashing', async () => {
    const broken = ev(9, 'upcoming', 'Burnley FC', 'Leeds United', { source: { ...source, sourceUpdatedAt: 'not-a-date' } });
    const { data } = backend({
      detail: new Response(JSON.stringify({ event: broken, tournament: TOURNAMENTS[0], coverage: COVERAGE }), { status: 200 }),
    });
    renderApp('#/event/fd%3A9', { data });
    expect(await screen.findByTestId('source-note')).toHaveTextContent('09:59');
  });
});

