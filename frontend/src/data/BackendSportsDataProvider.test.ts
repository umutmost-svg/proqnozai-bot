import { AI_TIMEOUT_MS, BackendSportsDataProvider, DATA_TIMEOUT_MS } from './BackendSportsDataProvider';
import { resolveDataMode } from './mode';
import type { FootballEvent, MmaEvent, TennisEvent } from './models';
import { DataError } from './provider';
import type { EventQuery } from './filter';

/** Test payloads in the backend's shape (they imitate real provider data, served nowhere). */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: '2026-11-08T05:58:00Z', stale: false };
const team = (id: number, name: string) => ({ id: `fd:team:${id}`, name, shortName: null });
const football = (id: number, status: string, extra: Record<string, unknown> = {}) => ({
  id: `fd:${id}`,
  sport: 'football',
  tournamentId: 'fd:PL',
  startsAt: '2026-11-08T15:00:00Z',
  status,
  sourceStatus: 'X',
  home: team(57, 'Arsenal FC'),
  away: team(61, 'Chelsea FC'),
  score: null,
  halfTime: null,
  minute: null,
  awarded: false,
  stats: null,
  statsAvailability: 'unavailable',
  source,
  ...extra,
});
const TOURNAMENTS = [
  { id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org' },
  { id: 'fd:PD', sport: 'football', name: 'LaLiga', area: 'Spain', provider: 'football-data.org' },
];

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const query = (over: Partial<EventQuery> = {}): EventQuery => ({
  sport: 'football',
  day: '2026-11-08',
  timeZone: 'Asia/Baku',
  tournamentId: 'all',
  query: '',
  ...over,
});

function provider(responses: Response[], session = true, csrf: string | null = 'csrf-test') {
  const fetchImpl = vi.fn<typeof fetch>(async () => {
    const next = responses.length > 1 ? responses.shift()! : responses[0]!;
    return next.clone();
  });
  const data = new BackendSportsDataProvider({ waitForSession: async () => session, getCsrfToken: () => csrf, fetchImpl });
  return { data, fetchImpl };
}

describe('BackendSportsDataProvider: requests', () => {
  it('asks for the calendar day of the chosen time zone, as UTC instants', async () => {
    const { data, fetchImpl } = provider([json(200, { coverage: COVERAGE, events: [], tournaments: TOURNAMENTS })]);
    await data.listEvents(query({ tournamentId: 'fd:PD' }));
    const url = new URL(String(fetchImpl.mock.calls[0]![0]), 'http://x');
    expect(url.pathname).toBe('/api/sports/football/events');
    // 8 Nov in Baku (UTC+4) = 7 Nov 20:00Z .. 8 Nov 20:00Z.
    expect(url.searchParams.get('from')).toBe('2026-11-07T20:00:00.000Z');
    expect(url.searchParams.get('to')).toBe('2026-11-08T20:00:00.000Z');
    expect(url.searchParams.get('tournament')).toBe('fd:PD');
    const init = fetchImpl.mock.calls[0]![1]!;
    expect(init.credentials).toBe('same-origin');
    expect(JSON.stringify(init.headers)).not.toMatch(/auth-token|apisports/i);
  });

  it('encodes the event id in the path', async () => {
    const { data, fetchImpl } = provider([json(404, { error: 'not_found' })]);
    expect(await data.getEvent('fd:1/../x')).toBeNull();
    expect(String(fetchImpl.mock.calls[0]![0])).toBe('/api/sports/event/fd%3A1%2F..%2Fx');
  });

  it('without a session makes no request and fails clearly', async () => {
    const { data, fetchImpl } = provider([json(200, {})], false);
    await expect(data.listEvents(query())).rejects.toMatchObject({ reason: 'unauthenticated' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('BackendSportsDataProvider: real data, never demo', () => {
  it('maps real football events with their statuses, score, minute and source', async () => {
    const { data } = provider([
      json(200, {
        coverage: COVERAGE,
        tournaments: TOURNAMENTS,
        events: [
          football(1, 'live', { score: [2, 1], halfTime: [1, 1], minute: 67 }),
          football(2, 'postponed', { startsAt: '2026-11-08T12:00:00Z' }),
          football(3, 'finished', { score: [3, 0], awarded: true, startsAt: '2026-11-08T10:00:00Z' }),
          football(4, 'some-new-state', { startsAt: '2026-11-08T11:00:00Z' }),
        ],
      }),
    ]);
    const events = (await data.listEvents(query())) as FootballEvent[];
    expect(events.map((e) => [e.id, e.status])).toEqual([
      ['fd:1', 'live'],
      ['fd:2', 'postponed'],
      ['fd:3', 'finished'],
      ['fd:4', 'unknown'],
    ]);
    const live = events[0]!;
    expect(live.score).toEqual([2, 1]);
    expect(live.minute).toBe(67);
    expect(live.stats).toBeUndefined();
    expect(live.ai).toEqual({ kind: 'unavailable' });
    expect(live.home.emblem).toBeUndefined(); // no third-party crests
    expect(live.source).toEqual({
      provider: 'football-data.org',
      fetchedAt: '2026-11-08T05:59:00Z',
      sourceUpdatedAt: '2026-11-08T05:58:00Z',
      stale: false,
      attribution: ATTR,
      delayed: true,
    });
    expect(events[2]!.awarded).toBe(true);
    expect(events.some((e) => ['fb-rm-fcb', 'real-madrid'].includes(e.id))).toBe(false);
  });

  it('keeps a missing minute missing instead of 0', async () => {
    const { data } = provider([json(200, { coverage: COVERAGE, tournaments: TOURNAMENTS, events: [football(1, 'live', { score: [0, 0] })] })]);
    const [e] = (await data.listEvents(query())) as FootballEvent[];
    expect(e!.minute).toBeUndefined();
  });

  it('staleness is per event: a fresh event in a partly stale response is not marked', async () => {
    const stale = { ...source, stale: true };
    const { data } = provider([
      json(200, {
        coverage: COVERAGE,
        tournaments: TOURNAMENTS,
        events: [football(1, 'upcoming', { source: stale }), football(2, 'upcoming', { startsAt: '2026-11-08T16:00:00Z' })],
        stale: true,
      }),
    ]);
    const events = await data.listEvents(query());
    expect(events.map((e) => [e.id, e.source?.stale])).toEqual([
      ['fd:1', true],
      ['fd:2', false],
    ]);
  });

  it('tournaments come from the backend; only licensed local logos are attached', async () => {
    const { data } = provider([json(200, { coverage: COVERAGE, tournaments: [...TOURNAMENTS, { id: 'fd:SA', sport: 'football', name: 'Serie A' }], events: [] })]);
    expect(data.listTournaments('football')).toEqual([]);
    await data.listEvents(query());
    const list = data.listTournaments('football');
    expect(list.map((t) => t.id)).toEqual(['fd:PL', 'fd:PD', 'fd:SA']);
    expect(list.find((t) => t.id === 'fd:SA')!.logo).toBeUndefined();
    for (const t of list) expect(t.logo?.src ?? '').not.toMatch(/^https?:/);
  });

  it('a sport without a source is reported as not connected, with no events', async () => {
    const { data } = provider([json(200, { coverage: { status: 'not_connected', provider: null }, tournaments: [], events: [] })]);
    expect(await data.listEvents(query({ sport: 'tennis' }))).toEqual([]);
    expect(data.coverage('tennis')).toEqual({ status: 'not_connected', provider: null, attribution: null });
    expect(data.coverage('mma').status).toBe('unknown');
  });

  it('other sports keep their own score shape and never get guessed attributes', async () => {
    const tennis = {
      ...football(9, 'live'),
      sport: 'tennis',
      tournamentId: 'x:atp',
      sets: [[6, 4], [3, 2]],
      currentGame: '40:30',
      format: null,
      surface: null,
      bestOf: null,
    };
    const mma = { ...football(10, 'upcoming'), sport: 'mma', tournamentId: 'x:ufc', weightClass: null, scheduledRounds: null, result: null };
    const t = provider([json(200, { coverage: COVERAGE, tournaments: [], events: [tennis] })]);
    const [te] = (await t.data.listEvents(query({ sport: 'tennis' }))) as TennisEvent[];
    expect(te!.sets).toEqual([[6, 4], [3, 2]]);
    expect([te!.format, te!.surface, te!.bestOf]).toEqual([undefined, undefined, undefined]);
    const m = provider([json(200, { coverage: COVERAGE, tournaments: [], events: [mma] })]);
    const [me] = (await m.data.listEvents(query({ sport: 'mma' }))) as MmaEvent[];
    expect([me!.weightClass, me!.scheduledRounds, me!.result]).toEqual([undefined, undefined, undefined]);
  });

  it('requests no AI and returns "unavailable" while the backend AI is not connected', async () => {
    const { data, fetchImpl } = provider([
      json(200, { event: football(1, 'upcoming'), tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'not_connected' } }),
    ]);
    const analysis = await data.requestAnalysis('fd:1');
    expect(analysis).toMatchObject({ kind: 'unavailable', probabilities: [], reasons: [], generatedAt: null });
    expect(data.aiAvailable()).toBe(false);
    expect(fetchImpl.mock.calls.every(([u]) => String(u).startsWith('/api/sports/event/'))).toBe(true);
  });
});

const EVENT_AI = { event: football(1, 'upcoming'), tournament: TOURNAMENTS[0], coverage: COVERAGE, ai: { status: 'connected' } };
const counts = (form: string) => ({
  wins: form.split('W').length - 1,
  draws: form.split('D').length - 1,
  losses: form.split('L').length - 1,
});
const basisTeam = (name: string, form: string, points: number) => ({
  name,
  matches: form.length,
  form,
  ...counts(form),
  points,
  goalsFor: 9,
  goalsAgainst: 2,
  goalsForPerMatch: 1.8,
  goalsAgainstPerMatch: 0.4,
  venues: 'HAHAH',
  tablePosition: 2,
  tablePoints: 25,
  tablePlayed: 11,
});
const PARTIAL = {
  eventId: 'fd:1',
  kind: 'partial',
  reasons: ['no_odds_source'],
  generatedAt: '2026-11-08T06:00:00Z',
  model: 'claude-opus-5-5',
  probabilities: { home: 52, draw: 26, away: 22 },
  factors: ['home_form', 'away_table'],
  basis: { home: basisTeam('Arsenal FC', 'WWDWW', 13), away: basisTeam('Chelsea FC', 'DDDDD', 5) },
  estimated: true, pick: { market: 'NO_BET', reasons: [], probability: null },
};

describe('BackendSportsDataProvider: AI analysis', () => {
  it('posts the event with the CSRF token and reads a PARTIAL estimate', async () => {
    const { data, fetchImpl } = provider([json(200, EVENT_AI), json(200, PARTIAL)]);
    const a = await data.requestAnalysis('fd:1');
    expect(data.aiAvailable()).toBe(true);
    const [url, init] = fetchImpl.mock.calls[1]!;
    expect(url).toBe('/api/ai/analysis');
    expect(init?.method).toBe('POST');
    expect((init?.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-test');
    expect(JSON.parse(String(init?.body))).toEqual({ event_id: 'fd:1' });
    expect(a).toMatchObject({
      kind: 'partial',
      estimated: true,
      probabilities: [52, 26, 22],
      reasons: ['noOddsSource'],
      factors: ['homeForm', 'awayTable'],
      basis: { home: { form: 'WWDWW', points: 13, tablePosition: 2 }, away: { form: 'DDDDD' } },
    });
  });

  it('reads INSUFFICIENT with its reasons and no probabilities', async () => {
    const body = { ...PARTIAL, kind: 'insufficient', reasons: ['no_verified_form', 'no_odds_source'], probabilities: null, factors: [], generatedAt: null, estimated: false };
    const { data } = provider([json(200, EVENT_AI), json(200, body)]);
    expect(await data.requestAnalysis('fd:1')).toMatchObject({
      kind: 'insufficient',
      probabilities: [],
      reasons: ['noVerifiedForm', 'noOddsSource'],
    });
  });

  it('rejects an off-contract analysis instead of repairing it', async () => {
    const bad = [
      { ...PARTIAL, probabilities: { home: 50, draw: 30, away: 30 } },
      { ...PARTIAL, reasons: ['something_else'] },
      { ...PARTIAL, eventId: 'fd:2' },
      { ...PARTIAL, summary: 'Chelsea won 4 of 5' }, // no free text from the model
      { ...PARTIAL, factors: ['home_form', 'injuries'] },
      { ...PARTIAL, factors: ['home_form', 'home_form'] },
      { ...PARTIAL, basis: { home: basisTeam('A', 'WWW', 9), away: { ...basisTeam('B', 'WWW', 9), tablePosition: null } } }, // away_table without a row
      { ...PARTIAL, basis: null },
      { ...PARTIAL, basis: { home: basisTeam('A', 'WWX', 6), away: basisTeam('B', 'WWW', 9) } },
      { ...PARTIAL, basis: { home: { ...basisTeam('A', 'WWW', 9), matches: 5 }, away: basisTeam('B', 'WWW', 9) } },
      { ...PARTIAL, kind: 'example' },
      { ...PARTIAL, factors: ['home_form'] },
      { ...PARTIAL, basis: { home: { ...basisTeam('A', 'WWW', 9), tablePosition: '2' }, away: basisTeam('B', 'WWW', 9) } },
      { ...PARTIAL, kind: 'insufficient', reasons: ['no_verified_form'] }, // insufficient must carry no probabilities
    ];
    for (const body of bad) {
      const { data } = provider([json(200, EVENT_AI), json(200, body)]);
      await expect(data.requestAnalysis('fd:1')).rejects.toMatchObject({ operation: 'ai', reason: 'bad_response' });
    }
  });

  it('maps the limit, a named AI failure and an unknown one to safe reasons', async () => {
    const cases: [Response, string][] = [
      [json(429, { error: 'ai_unavailable', reason: 'limit_reached' }), 'limit_reached'],
      [json(503, { error: 'ai_unavailable', reason: 'budget_exhausted' }), 'budget_exhausted'],
      [json(503, { error: 'ai_unavailable', reason: '<script>' }), 'source_unavailable'],
      [json(503, { error: 'source_unavailable', reason: 'timeout' }), 'source_unavailable'],
    ];
    for (const [response, reason] of cases) {
      const { data } = provider([json(200, EVENT_AI), response]);
      await expect(data.requestAnalysis('fd:1')).rejects.toMatchObject({ operation: 'ai', reason });
    }
  });

  it('waits for the model longer than for data, but not forever', async () => {
    vi.useFakeTimers();
    try {
      const fetchImpl = vi.fn<typeof fetch>((input) =>
        String(input).startsWith('/api/ai/') ? new Promise<Response>(() => {}) : Promise.resolve(json(200, EVENT_AI)),
      );
      const data = new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
      const pending = data.requestAnalysis('fd:1');
      const outcome = pending.then(() => 'done', (e: DataError) => e.reason);
      await vi.advanceTimersByTimeAsync(DATA_TIMEOUT_MS + 1);
      expect(await Promise.race([outcome, Promise.resolve('waiting')])).toBe('waiting');
      await vi.advanceTimersByTimeAsync(AI_TIMEOUT_MS);
      expect(await outcome).toBe('network');
    } finally {
      vi.useRealTimers();
    }
  });

  it('sends nothing paid without a CSRF token', async () => {
    const { data, fetchImpl } = provider([json(200, EVENT_AI), json(200, PARTIAL)], true, null);
    await expect(data.requestAnalysis('fd:1')).rejects.toMatchObject({ reason: 'unauthenticated' });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});

describe('BackendSportsDataProvider: provenance is required', () => {
  it('a real event that does not say where it came from makes the answer an error', async () => {
    for (const bad of [football(1, 'upcoming', { source: null }), football(2, 'upcoming', { source: { provider: 'x' } })]) {
      const { data } = provider([json(200, { coverage: COVERAGE, tournaments: TOURNAMENTS, events: [bad, football(3, 'upcoming')] })]);
      await expect(data.listEvents(query())).rejects.toMatchObject({ reason: 'bad_response' });
    }
  });

  it('an empty day served from stale cache is "source unavailable", not "no matches"', async () => {
    const { data } = provider([json(200, { coverage: COVERAGE, tournaments: TOURNAMENTS, events: [], stale: true })]);
    await expect(data.listEvents(query())).rejects.toMatchObject({ reason: 'source_unavailable' });
  });

  it('an empty coverage block does not drop the footer attribution', async () => {
    const { data } = provider([json(200, { coverage: {}, tournaments: TOURNAMENTS, events: [football(1, 'upcoming')] })]);
    await data.listEvents(query());
    expect(data.coverage('football').attribution).toBe(ATTR);
  });

  it('a detail without coverage still carries the required attribution', async () => {
    const { data } = provider([json(200, { event: football(1, 'live', { score: [0, 0], minute: 45 }) })]);
    const event = await data.getEvent('fd:1');
    expect(event?.source?.attribution).toBe(ATTR);
  });

  it('a detail answer for another event id is an error', async () => {
    const { data } = provider([json(200, { event: football(2, 'live'), coverage: COVERAGE })]);
    await expect(data.getEvent('fd:1')).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('a malformed event detail is an error, not "not found"', async () => {
    const { data } = provider([json(200, { event: { ...football(1, 'live'), source: null }, coverage: COVERAGE })]);
    await expect(data.getEvent('fd:1')).rejects.toMatchObject({ reason: 'bad_response' });
  });
});

describe('BackendSportsDataProvider: malformed answers and attribution', () => {
  it.each([{}, { coverage: COVERAGE }, { coverage: COVERAGE, events: 'x' }, { events: [] }])(
    'a success response without events/coverage is an error, not an empty day (%#)',
    async (body) => {
      const { data } = provider([json(200, body)]);
      await expect(data.listEvents(query())).rejects.toMatchObject({ reason: 'bad_response' });
    },
  );

  it('an event of another sport in the list is an error, not a silent omission', async () => {
    const tennis = { ...football(2, 'upcoming'), sport: 'tennis', sets: [] };
    const { data } = provider([json(200, { coverage: COVERAGE, tournaments: TOURNAMENTS, events: [football(1, 'upcoming'), tennis] })]);
    await expect(data.listEvents(query())).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('one broken event among valid ones makes the answer an error, not a partial day', async () => {
    const { data } = provider([json(200, { coverage: COVERAGE, tournaments: TOURNAMENTS, events: [football(1, 'upcoming'), { id: 'x' }] })]);
    await expect(data.listEvents(query())).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('a non-empty list with only broken events is an error, not an empty day', async () => {
    const { data } = provider([json(200, { coverage: COVERAGE, tournaments: TOURNAMENTS, events: [{ id: 'x' }, { ...football(2, 'live'), source: null }] })]);
    await expect(data.listEvents(query())).rejects.toMatchObject({ reason: 'bad_response' });
  });

  it('the football-data.org attribution is shown even if the backend omits it', async () => {
    const { data } = provider([
      json(200, { coverage: { status: 'connected', provider: 'football-data.org' }, tournaments: TOURNAMENTS, events: [football(1, 'upcoming')] }),
    ]);
    const [e] = await data.listEvents(query());
    expect(e!.source?.attribution).toBe(ATTR);
    expect(data.coverage('football').attribution).toBe(ATTR);
  });
});

describe('BackendSportsDataProvider: failures stay failures', () => {
  it.each([
    [json(401, { error: 'unauthenticated' }), 'unauthenticated'],
    [json(503, { error: 'source_unavailable', reason: 'rate_limited' }), 'source_unavailable'],
    [json(500, {}), 'bad_response'],
    [new Response('<html>', { status: 200 }), 'bad_response'],
  ])('maps %# to a DataError, never to demo data', async (response, reason) => {
    const { data } = provider([response]);
    const error = await data.listEvents(query()).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(DataError);
    expect((error as DataError).reason).toBe(reason);
  });

  it('a network failure is an error', async () => {
    const data = new BackendSportsDataProvider({
      waitForSession: async () => true,
      fetchImpl: async () => {
        throw new TypeError('Failed to fetch');
      },
    });
    await expect(data.getEvent('fd:1')).rejects.toMatchObject({ reason: 'network' });
  });

  it('a hanging request ends by the timeout', async () => {
    vi.useFakeTimers();
    try {
      const data = new BackendSportsDataProvider({ waitForSession: async () => true, fetchImpl: () => new Promise(() => {}) });
      const pending = data.listEvents(query()).catch((e: unknown) => e);
      await vi.advanceTimersByTimeAsync(DATA_TIMEOUT_MS);
      expect(await pending).toMatchObject({ reason: 'network' });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('data mode', () => {
  it('defaults to demo, accepts real, rejects anything else', () => {
    expect(resolveDataMode(undefined)).toBe('demo');
    expect(resolveDataMode('')).toBe('demo');
    expect(resolveDataMode('demo')).toBe('demo');
    expect(resolveDataMode('real')).toBe('real');
    expect(() => resolveDataMode('Real')).toThrow();
    expect(() => resolveDataMode('live')).toThrow();
  });
});
