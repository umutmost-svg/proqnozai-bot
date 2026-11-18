import { screen } from '@testing-library/react';
import { BackendSportsDataProvider, readLean } from '../data/BackendSportsDataProvider';
import type { MarketKey } from '../data/models';
import { FORECAST_V3 } from './fixtures/forecastV3';
import { renderApp } from './renderApp';

/** The model's forecast on every match with probabilities ("lean"), separate from the strict pick. */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const UNL = { id: 'fd:UNL', sport: 'football', name: 'UEFA Nations League', area: 'Europe', provider: 'football-data.org' };
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false };
const side = (id: number, name: string) => ({ id: `fd:team:${id}`, name, shortName: null, crest: null });
const ev = {
  id: 'fd:4', sport: 'football', tournamentId: 'fd:UNL', startsAt: '2026-11-08T15:00:00Z', status: 'upcoming', sourceStatus: 'TIMED',
  home: side(1, 'Spain'), away: side(2, 'Italy'),
  score: null, halfTime: null, minute: null, awarded: false, stats: null, statsAvailability: 'unavailable', source,
};
const team = (name: string) => ({ name, matches: 5, form: 'WWDLW', wins: 3, draws: 1, losses: 1, points: 10, goalsFor: 8, goalsAgainst: 4,
  tablePosition: null, tablePoints: null });
const M = FORECAST_V3.markets as Record<MarketKey, number>;
const top = (['1', 'X', '2'] as const).reduce((a, k) => (M[k] > M[a] ? k : a), '1' as '1' | 'X' | '2');

function noBet(lean: unknown, over: Record<string, unknown> = {}, forecast: Record<string, unknown> = {}) {
  return {
    schema: 'v3', eventId: 'fd:4', kind: 'partial', reasons: ['no_odds_source'], generatedAt: '2026-11-08T06:00:00Z', model: 'm',
    probabilities: { home: M['1'], draw: M.X, away: M['2'] }, factors: ['home_form', 'away_form'],
    basis: { home: team('Spain'), away: team('Italy') }, estimated: true,
    pick: { market: 'NO_BET', reasons: [], probability: null }, alternatives: [],
    forecast: { ...FORECAST_V3, noBetReason: 'unvalidated_competition', gateEligible: false, ...forecast },
    lean, ...over,
  };
}
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
function backend(body: unknown) {
  const fetchImpl = vi.fn<typeof fetch>(async (input) =>
    String(input).startsWith('/api/ai/analysis') ? ok(body) : ok({ event: ev, tournament: UNL, coverage: COVERAGE, ai: { status: 'connected' } }));
  return new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
}
const LEAN = { market: top, probability: M[top], confidence: 'estimate' };

describe('the model forecast is read strictly', () => {
  const read = async (body: unknown) => backend(body).requestAnalysis('fd:4');

  it('accepts the top outcome with its percent next to a NO_BET', async () => {
    const a = await read(noBet(LEAN));
    expect(a.lean).toEqual(LEAN);
    expect(a.pick?.market).toBe('NO_BET');
  });

  it('older answers without it still read', async () => {
    expect((await read(noBet(undefined))).lean).toBeUndefined();
  });

  it.each([
    ['a percent that is not the shown one', noBet({ ...LEAN, probability: M[top] - 1 })],
    ['an outcome that is not the top one', noBet({ ...LEAN, market: top === '2' ? '1' : '2', probability: M[top === '2' ? '1' : '2'] })],
    ['model confidence outside the validated population', noBet({ ...LEAN, confidence: 'model' })],
    ['an unknown market', noBet({ ...LEAN, market: 'O2.5' })],
    ['an inherited key as the market', noBet({ ...LEAN, market: 'constructor' })],
    ['null next to shown probabilities', noBet(null)],
  ])('rejects %s', async (_name, body) => {
    await expect(read(body)).rejects.toMatchObject({ reason: 'bad_response' });
  });
});

describe('ties and confidence (readLean)', () => {
  it('a tied top two is their double chance with the summed percent; a single outcome is then wrong', () => {
    expect(readLean({ market: '12', probability: 80, confidence: 'estimate' }, [40, 20, 40], false))
      .toEqual({ market: '12', probability: 80, confidence: 'estimate' });
    expect(readLean({ market: '1X', probability: 76, confidence: 'estimate' }, [38, 38, 24], false))
      .toEqual({ market: '1X', probability: 76, confidence: 'estimate' });
    expect(readLean({ market: '1', probability: 40, confidence: 'estimate' }, [40, 20, 40], false)).toBeNull();
    expect(readLean({ market: '12', probability: 79, confidence: 'estimate' }, [40, 20, 40], false)).toBeNull();
  });

  it('model confidence only where allowed (READY in the validated population)', () => {
    const lean = { market: '1', probability: 54, confidence: 'model' };
    expect(readLean(lean, [54, 26, 20], true)).toEqual(lean);
    expect(readLean(lean, [54, 26, 20], false)).toBeNull();
  });

  it('absent is an older answer, null next to probabilities is a violation', () => {
    expect(readLean(undefined, [54, 26, 20], false)).toBeUndefined();
    expect(readLean(null, [54, 26, 20], false)).toBeNull();
    expect(readLean({ market: 'constructor', probability: 54, confidence: 'estimate' }, [54, 26, 20], false)).toBeNull();
  });
});

describe('AI screen', () => {
  it('without the model the recommended bet is the market\'s most likely outcome, marked as from odds', async () => {
    const body = {
      schema: 'v3', eventId: 'fd:4', kind: 'partial', reasons: ['model_team_history'], generatedAt: '2026-11-08T06:00:00Z', model: 'm',
      probabilities: { home: 21, draw: 25, away: 54 }, factors: ['away_form', 'home_form'],
      basis: { home: team('Spain'), away: team('Italy') }, estimated: true, pick: null, forecast: null, alternatives: [], market: null,
      estimateFrom: { fetchedAt: new Date().toISOString(), validFor: 840 },
      lean: { market: '2', probability: 54, confidence: 'estimate' },
    };
    renderApp('#/event/fd%3A4/ai', { data: backend(body) });
    const lean = await screen.findByTestId('ai-pick');
    expect(lean).toHaveTextContent('Рекомендуемая ставка');
    expect(lean).toHaveTextContent('По коэффициентам');
    expect(lean).toHaveAttribute('data-market', '2');
  });

  it('a NO_BET recommendation shows the most likely outcome instead of an empty card, marked as an estimate', async () => {
    renderApp('#/event/fd%3A4/ai', { data: backend(noBet(LEAN)) });
    const pick = await screen.findByTestId('ai-pick');
    expect(pick).toHaveTextContent('Рекомендуемая ставка');
    expect(pick).toHaveTextContent('Оценка');
    expect(pick).toHaveTextContent(`${M[top]}%`);
    expect(pick).toHaveAttribute('data-market', top);
    expect(pick).not.toHaveTextContent('Нет достаточно надежного прогноза');
    expect(screen.queryByTestId('ai-lean')).toBeNull();      // no separate card
    expect(screen.getAllByTestId('ai-pick')).toHaveLength(1);
  });

  it('a strict pick is shown as before, the lean does not replace it', async () => {
    const strict = noBet(LEAN, { pick: { market: 'X2', reasons: [], probability: M.X2 },
      alternatives: [] }, { noBetReason: null, limited: false });
    renderApp('#/event/fd%3A4/ai', { data: backend(strict) });
    const pick = await screen.findByTestId('ai-pick');
    expect(pick).toHaveAttribute('data-market', 'X2');
    expect(pick).not.toHaveAttribute('data-lean');
  });
});

describe('the validated-model lean card', () => {
  it('says no market passed the selection, without claiming more', async () => {
    vi.resetModules();
    const { createTranslator } = await import('../i18n');
    vi.doMock('../state/contexts', () => ({ useSettings: () => ({ t: createTranslator('ru'), settings: { lang: 'ru' } }) }));
    const { LeanPick } = await import('../screens/ai/PickCard');
    const { render } = await import('@testing-library/react');
    render(<LeanPick lean={{ market: '1', probability: 54, confidence: 'model' }} event={ev as never} />);
    const pick = screen.getByTestId('ai-pick');
    expect(pick).toHaveTextContent('Рекомендуемая ставка');
    expect(pick).toHaveTextContent('Проверенная модель');
    expect(pick).toHaveTextContent('Ни один рынок не прошёл строгий отбор');
    expect(pick).not.toHaveTextContent('уверенность');
    vi.doUnmock('../state/contexts');
  });
});

describe('a near-certain favourite has no lean (selection rule 6, min odds 1.30)', () => {
  it('null is read as "no lean" only when the favourite is above 1/1.30', () => {
    expect(readLean(null, [80, 12, 8], false)).toBeUndefined();
    expect(readLean(null, [40, 20, 40], false)).toBeUndefined();   // a tied pair summing to 80 %
    expect(readLean(null, [60, 25, 15], false)).toBeNull();        // an open match must carry its lean
  });
});
