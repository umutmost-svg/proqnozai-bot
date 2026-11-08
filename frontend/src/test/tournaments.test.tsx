import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BackendSportsDataProvider, remoteImage } from '../data/BackendSportsDataProvider';
import { filterEvents } from '../data/filter';
import type { FootballEvent } from '../data/models';
import { groupByDay, initialDay } from '../screens/TournamentScreen';
import { renderApp } from './renderApp';

/** M3.3: short names, crests from our backend, tournament calendars (test-only payloads). */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const PL = {
  id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org',
  emblem: '/api/crests/competition/PL',
};
const SA = { id: 'fd:SA', sport: 'football', name: 'Serie A', area: 'Italy', provider: 'football-data.org', emblem: '/api/crests/competition/SA' };
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false };
const side = (id: number, name: string, shortName: string | null, crest: string | null = `/api/crests/team/${id}`) => ({
  id: `fd:team:${id}`, name, shortName, crest,
});
const ev = (id: number, startsAt: string, status = 'upcoming', extra: Record<string, unknown> = {}) => ({
  id: `fd:${id}`, sport: 'football', tournamentId: 'fd:SA', startsAt, status, sourceStatus: 'X',
  home: side(98, 'AC Milan', 'Milan'), away: side(108, 'FC Internazionale Milano', 'Inter'),
  score: status === 'finished' ? [1, 2] : null, halfTime: null, minute: null, awarded: false, matchday: 10,
  stats: null, statsAvailability: 'unavailable', source, ...extra,
});
// 1 Nov: played; 9 Nov (Baku) — two matches of matchday 11; 22 Nov — matchday 12.
const CALENDAR_EVENTS = [
  ev(1, '2026-11-01T17:00:00Z', 'finished'),
  ev(3, '2026-11-09T19:45:00Z', 'upcoming', { matchday: 11, home: side(99, 'ACF Fiorentina', 'Fiorentina'), away: side(100, 'AS Roma', 'Roma') }),
  ev(2, '2026-11-09T14:00:00Z', 'upcoming', { matchday: 11 }),
  ev(4, '2026-11-22T14:00:00Z', 'upcoming', { matchday: 12 }),
];
const calendarBody = (events: unknown[] = CALENDAR_EVENTS) => ({
  sport: 'football', coverage: COVERAGE, tournament: SA,
  season: { startDate: '2026-08-23', endDate: '2027-05-30', currentMatchday: 11 },
  events, fetchedAt: '2026-11-08T05:59:00Z', stale: false, ai: { status: 'not_connected' },
});
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

function backend(calendar: Response = ok(calendarBody())) {
  const fetchImpl = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (url.startsWith('/api/sports/football/tournaments/')) return calendar.clone();
    if (url === '/api/sports/football/tournaments') return ok({ sport: 'football', coverage: COVERAGE, tournaments: [PL, SA] });
    if (/\/api\/sports\/\w+\/tournaments$/.test(url)) {
      return ok({ sport: 'x', coverage: { status: 'not_connected', provider: null, attribution: null }, tournaments: [] });
    }
    if (url.startsWith('/api/sports/football/events')) {
      return ok({ coverage: COVERAGE, tournaments: [PL, SA], events: [ev(9, '2026-11-08T15:00:00Z', 'upcoming', { tournamentId: 'fd:PL' })] });
    }
    if (url.startsWith('/api/sports/event/')) {
      return ok({ event: ev(9, '2026-11-08T15:00:00Z', 'upcoming', { tournamentId: 'fd:SA' }), tournament: SA, coverage: COVERAGE });
    }
    return ok({ coverage: { status: 'not_connected', provider: null, attribution: null }, tournaments: [], events: [] });
  });
  return { data: new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl }), fetchImpl };
}

describe('short names and crests', () => {
  it('shows the short name, keeps the full one for search and the crest label', async () => {
    const { data } = backend();
    const calendar = await data.getTournamentCalendar('fd:SA');
    const match = calendar!.events[0]!;
    expect(match.home).toEqual({
      id: 'fd:team:98', name: 'Milan', fullName: 'AC Milan',
      emblem: { src: '/api/crests/team/98', width: 1, height: 1, remote: true },
    });
    const query = { sport: 'football' as const, day: '2026-11-01', timeZone: 'Asia/Baku', tournamentId: 'all', query: 'internazionale' };
    expect(filterEvents([match], [], query)).toHaveLength(1); // the official name still finds it
  });

  it('without a short name the full name is shown; a crest is never a third-party URL', () => {
    expect(remoteImage('/api/crests/team/57')).toBeDefined();
    for (const bad of ['https://crests.football-data.org/57.png', '//evil.com/api/crests/team/1',
      '/api/crests/team/../../x', '/api/crests/player/1', 'javascript:alert(1)', 42, null]) {
      expect(remoteImage(bad)).toBeUndefined();
    }
  });

  it('a crest that fails to load falls back to the initial badge', async () => {
    const { data } = backend();
    renderApp('#/tournament/fd%3ASA', { data });
    const list = await screen.findByTestId('calendar-events');
    const img = within(list).getByRole('img', { name: 'AC Milan' });
    expect(img).toHaveAttribute('src', '/api/crests/team/98');
    fireEvent.error(img);
    await waitFor(() => expect(within(list).queryByRole('img', { name: 'AC Milan' })).toBeNull());
    const emblems = within(list).getAllByTestId('emblem');
    expect(emblems[0]).toHaveTextContent('M'); // Milan's initial instead of a broken image
    expect(within(list).getByRole('img', { name: 'FC Internazionale Milano' })).toBeInTheDocument();
  });
});

describe('logo failures', () => {
  it('a failed remote logo does not hide the next one in the same component', async () => {
    const { render } = await import('@testing-library/react');
    const { Logo } = await import('../components/Logo');
    const a = { src: '/api/crests/competition/PL', width: 1, height: 1, remote: true };
    const b = { src: '/api/crests/competition/SA', width: 1, height: 1, remote: true };
    const view = render(<Logo asset={a} box={30} alt="logo" fallback={<span>P</span>} />);
    fireEvent.error(view.getByAltText('logo'));
    expect(view.getByText('P')).toBeInTheDocument();
    view.rerender(<Logo asset={b} box={30} alt="logo" fallback={<span>S</span>} />);
    expect(view.getByAltText('logo')).toHaveAttribute('src', '/api/crests/competition/SA');
  });
});

describe('tournament calendar', () => {
  it('groups by day in the chosen zone and opens on the next match day', () => {
    const events = CALENDAR_EVENTS as unknown as FootballEvent[];
    const days = groupByDay(events, 'Asia/Baku');
    // 19:45Z on 9 Nov is 23:45 in Baku: still 9 Nov there; in Tokyo it is already the 10th.
    expect(days.map(([d, list]) => [d, list.length])).toEqual([['2026-11-01', 1], ['2026-11-09', 2], ['2026-11-22', 1]]);
    expect(groupByDay(events, 'Asia/Tokyo').map(([d]) => d)).toContain('2026-11-10');
    expect(initialDay(days.map(([d]) => d), '2026-11-08')).toBe('2026-11-09');
    expect(initialDay(['2026-05-01'], '2026-11-08')).toBe('2026-05-01'); // after the season: its last day
    expect(initialDay([], '2026-11-08')).toBeNull();
  });

  it('home lists tournaments; tapping one opens its calendar on the next match day', async () => {
    const user = userEvent.setup();
    const { data, fetchImpl } = backend();
    renderApp('#/', { data });
    const list = await screen.findByTestId('tournament-list');
    await user.click(within(list).getByRole('link', { name: 'Serie A: открыть календарь' }));
    expect(await screen.findByTestId('tournament-head')).toHaveTextContent('Serie A');
    expect(screen.getByTestId('tournament-head')).toHaveTextContent('Italy');
    expect(screen.getByTestId('tournament-head')).toHaveTextContent('Текущий тур: 11');
    expect(screen.getByTestId('calendar-day-title')).toHaveTextContent('9 ноября · Тур 11');
    const cards = within(screen.getByTestId('calendar-events')).getAllByTestId('event-card');
    expect(cards.map((c) => c.getAttribute('data-event-id'))).toEqual(['fd:2', 'fd:3']); // by kickoff
    expect(cards[0]).toHaveTextContent('Milan — Inter');
    expect(screen.getByTestId('source-footer')).toHaveTextContent(ATTR);
    const calls = fetchImpl.mock.calls.map(([u]) => String(u)).filter((u) => u.includes('/tournaments/'));
    expect(calls).toEqual(['/api/sports/football/tournaments/fd%3ASA/calendar']);
  });

  it('another day of the season can be picked', async () => {
    const user = userEvent.setup();
    const { data } = backend();
    renderApp('#/tournament/fd%3ASA', { data });
    const strip = await screen.findByTestId('calendar-days');
    expect(within(strip).getAllByRole('button').map((b) => b.getAttribute('data-day'))).toEqual([
      '2026-11-01', '2026-11-09', '2026-11-22',
    ]);
    await user.click(within(strip).getByRole('button', { name: '1 ноября' }));
    expect(screen.getByTestId('calendar-day-title')).toHaveTextContent('1 ноября · Тур 10');
    expect(within(screen.getByTestId('calendar-events')).getByTestId('event-card')).toHaveTextContent('1 : 2');
  });

  it('a broken calendar is an error with retry, never an empty season', async () => {
    const broken = ok(calendarBody([...CALENDAR_EVENTS, { id: 'fd:7', sport: 'football' }]));
    const { data } = backend(broken);
    renderApp('#/tournament/fd%3ASA', { data });
    expect(await screen.findByText('Не удалось загрузить календарь')).toBeInTheDocument();
    expect(screen.queryByTestId('calendar-events')).toBeNull();
  });

  it('rejects a calendar of another tournament or with foreign matches', async () => {
    const other = backend(ok({ ...calendarBody(), tournament: PL }));
    await expect(other.data.getTournamentCalendar('fd:SA')).rejects.toMatchObject({ reason: 'bad_response' });
    const foreign = backend(ok(calendarBody([ev(5, '2026-11-09T14:00:00Z', 'upcoming', { tournamentId: 'fd:PL' })])));
    await expect(foreign.data.getTournamentCalendar('fd:SA')).rejects.toMatchObject({ reason: 'bad_response' });
    const stale = backend(ok({ ...calendarBody([]), stale: true }));
    await expect(stale.data.getTournamentCalendar('fd:SA')).rejects.toMatchObject({ reason: 'source_unavailable' });
  });

  it.each([
    ['missing season', { season: undefined }],
    ['season not an object', { season: 'x' }],
    ['bad season date', { season: { startDate: '23.08.2026', endDate: null, currentMatchday: 1 } }],
    ['bad matchday', { season: { startDate: null, endDate: null, currentMatchday: -1 } }],
    ['season field missing', { season: { startDate: null, endDate: null } }],
    ['another sport', { sport: 'tennis' }],
    ['tournament of another sport', { tournament: { ...SA, sport: 'tennis' } }],
  ])('broken calendar metadata is an error (%s)', async (_, patch) => {
    const { data } = backend(ok({ ...calendarBody(), ...patch }));
    await expect(data.getTournamentCalendar('fd:SA')).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('an emblem withdrawn by the backend is dropped, not kept from earlier', async () => {
    const { data } = backend();
    await data.getTournamentCalendar('fd:SA');
    expect(data.getTournament('fd:SA')?.logo?.src).toBe('/api/crests/competition/SA');
    const later = backend(ok({ ...calendarBody(), tournament: { ...SA, emblem: null } }));
    // Same provider instance: simulate the next response through its fetch.
    (data as unknown as { fetchImpl: typeof fetch }).fetchImpl = later.fetchImpl;
    await data.getTournamentCalendar('fd:SA');
    expect(data.getTournament('fd:SA')?.logo).toBeUndefined();
  });

  it('a season with unknown dates is still valid', async () => {
    const { data } = backend(ok({ ...calendarBody(), season: { startDate: null, endDate: null, currentMatchday: null } }));
    expect((await data.getTournamentCalendar('fd:SA'))!.season).toEqual({ startDate: null, endDate: null, currentMatchday: null });
  });

  it('an unknown tournament says so', async () => {
    const { data } = backend(new Response('{"error":"not_found"}', { status: 404 }));
    renderApp('#/tournament/fd%3AXX', { data });
    expect(await screen.findByText('Турнир не найден')).toBeInTheDocument();
  });

  it('a season without matches is an empty state', async () => {
    const { data } = backend(ok(calendarBody([])));
    renderApp('#/tournament/fd%3ASA', { data });
    expect(await screen.findByText('Матчей нет')).toBeInTheDocument();
  });

  it('a tournament list with one broken entry stores nothing', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      ok({ sport: 'football', coverage: COVERAGE, tournaments: [PL, { id: 'fd:SA', sport: 'football' }] }),
    );
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
    await expect(data.loadTournaments('football')).rejects.toMatchObject({ reason: 'bad_response' });
    expect(data.listTournaments('football')).toEqual([]);
  });

  it('a day listing with a broken tournament entry is an error, not a partial catalogue', async () => {
    for (const tournaments of [[PL, { id: 'fd:SA' }], undefined]) {
      const fetchImpl = vi.fn<typeof fetch>(async () => ok({ coverage: COVERAGE, tournaments, events: [] }));
      const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
      const q = { sport: 'football' as const, day: '2026-11-08', timeZone: 'Asia/Baku', tournamentId: 'all', query: '' };
      await expect(data.listEvents(q)).rejects.toMatchObject({ reason: 'bad_response' });
      expect(data.listTournaments('football')).toEqual([]);
    }
  });

  it('a day listing whose catalogue holds another sport is an error', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      ok({ coverage: COVERAGE, tournaments: [PL, { ...SA, sport: 'tennis' }], events: [] }),
    );
    const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl });
    const q = { sport: 'football' as const, day: '2026-11-08', timeZone: 'Asia/Baku', tournamentId: 'all', query: '' };
    await expect(data.listEvents(q)).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('home says when the tournament list could not load, and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { data, fetchImpl } = backend();
    const base = fetchImpl.getMockImplementation()!;
    fetchImpl.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.startsWith('/api/sports/football/events') || (fail && url === '/api/sports/football/tournaments')) {
        return new Response('{"error":"source_unavailable"}', { status: 503 });
      }
      return base(input, init);
    });
    renderApp('#/', { data });
    const error = await screen.findByTestId('tournament-list-error');
    expect(error).toHaveTextContent('Не удалось загрузить список турниров.');
    fail = false;
    await user.click(within(error).getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByTestId('tournament-list')).toHaveTextContent('Serie A');
  });

  it('home still lists tournaments when the day listing fails', async () => {
    const user = userEvent.setup();
    const { data, fetchImpl } = backend();
    const base = fetchImpl.getMockImplementation()!;
    fetchImpl.mockImplementation(async (input, init) =>
      String(input).startsWith('/api/sports/football/events')
        ? new Response('{"error":"source_unavailable"}', { status: 503 })
        : base(input, init),
    );
    renderApp('#/', { data });
    const list = await screen.findByTestId('tournament-list');
    await user.click(within(list).getByRole('link', { name: 'Serie A: открыть календарь' }));
    expect(await screen.findByTestId('tournament-head')).toHaveTextContent('Serie A');
  });

  it('the match page links to its tournament calendar', async () => {
    const user = userEvent.setup();
    const { data } = backend();
    renderApp('#/event/fd%3A9', { data });
    const link = await screen.findByTestId('event-tournament-link');
    expect(link).toHaveTextContent('Serie A');
    await user.click(link);
    expect(await screen.findByTestId('tournament-head')).toHaveTextContent('Serie A');
  });

  it('the catalog offers the calendar of the selected tournament', async () => {
    const user = userEvent.setup();
    const { data } = backend();
    renderApp('#/matches', { data });
    await screen.findAllByTestId('event-card');
    expect(screen.queryByTestId('catalog-calendar-link')).toBeNull();
    await user.click(within(screen.getByTestId('tournament-strip')).getByRole('button', { name: /Serie A/ }));
    expect(screen.getByTestId('catalog-calendar-link')).toHaveTextContent('Календарь: Serie A');
  });

  it.each([
    ['az', 'Turnir təqvimi', 'Tur 11'],
    ['en', 'Tournament calendar', 'Matchday 11'],
  ] as const)('is translated (%s)', async (lang, title, round) => {
    const { data } = backend();
    renderApp('#/tournament/fd%3ASA', { data, settings: { lang } });
    expect(await screen.findByTestId('calendar-day-title')).toHaveTextContent(round);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(title);
  });

  it('demo mode: a demo tournament opens its own matches', async () => {
    const user = userEvent.setup();
    renderApp('#/');
    const list = await screen.findByTestId('tournament-list');
    const first = within(list).getAllByRole('link')[0]!;
    await user.click(first);
    const cards = within(await screen.findByTestId('calendar-events')).getAllByTestId('event-card');
    expect(cards.length).toBeGreaterThan(0);
  });
});
