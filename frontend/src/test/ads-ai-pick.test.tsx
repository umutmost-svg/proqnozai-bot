import { screen, waitFor, within } from '@testing-library/react';
import { BackendSportsDataProvider } from '../data/BackendSportsDataProvider';
import type { AdsApi, OfferAnswer } from '../data/ads';
import type { MarketKey } from '../data/models';
import { FORECAST_V3 } from './fixtures/forecastV3';
import { renderApp } from './renderApp';

/** Ads: the partner offer sits inside the AI screen's "Recommended bet" card (placement ai_pick). */
const ATTR = 'Football data provided by the Football-Data.org API';
const COVERAGE = { status: 'connected', provider: 'football-data.org', attribution: ATTR, liveScores: 'delayed' };
const PL = { id: 'fd:PL', sport: 'football', name: 'Premier League', area: 'England', provider: 'football-data.org' };
const source = { provider: 'football-data.org', fetchedAt: '2026-11-08T05:59:00Z', sourceUpdatedAt: null, stale: false };
const side = (id: number, name: string, shortName: string | null = null) => ({ id: `fd:team:${id}`, name, shortName, crest: null });
const EVENT = {
  id: 'fd:4', sport: 'football', tournamentId: 'fd:PL', startsAt: '2026-11-08T15:00:00Z', status: 'upcoming', sourceStatus: 'TIMED',
  home: side(354, 'Crystal Palace FC', 'Crystal Palace'), away: side(64, 'Liverpool FC', 'Liverpool'),
  score: null, halfTime: null, minute: null, awarded: false, stats: null, statsAvailability: 'unavailable', source,
};
const team = (name: string, form: string, goalsFor: number, goalsAgainst: number, pos: number, tablePoints: number) => ({
  name, matches: form.length, form,
  wins: form.split('W').length - 1, draws: form.split('D').length - 1, losses: form.split('L').length - 1,
  points: (form.split('W').length - 1) * 3 + (form.split('D').length - 1), goalsFor, goalsAgainst, tablePosition: pos, tablePoints,
});
const M = FORECAST_V3.markets as Record<MarketKey, number>;

function analysis(market: string) {
  return {
    schema: 'v3', eventId: 'fd:4', kind: 'partial', reasons: ['no_odds_source'], generatedAt: '2026-11-08T06:00:00Z', model: 'm',
    probabilities: { home: M['1'], draw: M.X, away: M['2'] },
    factors: ['away_rating', 'away_form', 'home_advantage'],
    basis: { home: team('Crystal Palace FC', 'LLLWD', 3, 10, 19, 7), away: team('Liverpool FC', 'WWDWW', 11, 3, 2, 25) },
    estimated: true,
    pick: market === 'NO_BET'
      ? { market: 'NO_BET', reasons: [], probability: null }
      : { market, reasons: ['away_stronger_rating', 'away_better_form'], probability: M.X2 },
    alternatives: market === 'NO_BET' ? [] : [{ market: 'H2+1.5', probability: M['H2+1.5'] }],
    forecast: { ...FORECAST_V3, noBetReason: market === 'NO_BET' ? 'small_sample' : null },
  };
}

function backend(body: unknown) {
  const fetchImpl = vi.fn<typeof fetch>(async (input) =>
    new Response(JSON.stringify(String(input).startsWith('/api/ai/analysis')
      ? body
      : { event: EVENT, tournament: PL, coverage: COVERAGE, ai: { status: 'connected' } }), { status: 200 }));
  return new BackendSportsDataProvider({ waitForSession: async () => true, getCsrfToken: () => 'c', fetchImpl });
}

function ads(answer: OfferAnswer): AdsApi {
  return { offer: vi.fn(async () => answer), impression: vi.fn(async () => {}), click: vi.fn(async () => null), confirmAge: vi.fn(async () => true) };
}

const OFFER: OfferAnswer = {
  kind: 'offer',
  offer: {
    id: 1, variant: 'A', headline: 'Бонус партнёра', body: '', extra: '', cta: 'Забрать', disclosure: {},
    price: { market: 'X2', odds: 1.42, expires: { mono: performance.now() + 600_000, wall: Date.now() + 600_000 } },
    oddsRequired: false, impressionToken: 'tok',
  },
};

describe('partner offer in the AI recommended bet', () => {
  it('is asked for this match in the ai_pick placement and shown inside the card with its marking and price', async () => {
    const api = ads(OFFER);
    renderApp('#/event/fd%3A4/ai', { data: backend(analysis('X2')), ads: api });
    const pick = await screen.findByTestId('ai-pick');
    const offer = await within(pick).findByTestId('partner-offer');
    expect(api.offer).toHaveBeenCalledWith('ai_pick', 'fd:4');
    expect(within(offer).getByTestId('partner-disclosure')).toHaveTextContent('Реклама партнёра');
    expect(within(offer).getByTestId('partner-odds')).toHaveTextContent('1,42');
  });

  it('a NO_BET card has no offer', async () => {
    const api = ads(OFFER);
    renderApp('#/event/fd%3A4/ai', { data: backend(analysis('NO_BET')), ads: api });
    await screen.findByTestId('ai-pick');
    await waitFor(() => expect(screen.getByTestId('ai-pick')).toHaveAttribute('data-market', 'NO_BET'));
    expect(api.offer).not.toHaveBeenCalled();
    expect(screen.queryByTestId('partner-offer')).toBeNull();
  });
});
