import { createAdsApi, readOffer, safeLink } from './ads';

const offer = {
  id: 7,
  variant: 'A',
  headline: 'Бонус',
  body: 'Текст',
  extra: '',
  cta: 'Забрать',
  disclosure: { advertiser: 'Partner LLC', adId: '', ageLabel: '18+' },
  impressionToken: 'tok.sig',
};

const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));

describe('partner offers client', () => {
  it('reads an offer strictly and never takes a link from it', () => {
    const answer = readOffer({ offer: { ...offer, url: 'https://evil.example/' } });
    expect(answer).toEqual({
      kind: 'offer',
      offer: { ...offer, disclosure: { advertiser: 'Partner LLC', ageLabel: '18+' }, price: null, oddsRequired: false },
    });
    expect(JSON.stringify(answer)).not.toContain('evil');
    expect(readOffer({ offer: { ...offer, headline: 'x'.repeat(81) } })).toEqual({ kind: 'none' });
    expect(readOffer({ offer: { ...offer, cta: 5 } })).toEqual({ kind: 'none' });
    expect(readOffer({ offer: null })).toEqual({ kind: 'none' });
    expect(readOffer({ offer: null, ageGate: { min: 18 } })).toEqual({ kind: 'ageGate', min: 18 });
    expect(readOffer('nope')).toEqual({ kind: 'none' });
  });

  it('reads the verified price with its own deadline, and refuses a malformed or missing required one', () => {
    const sent = { mono: 1_000, wall: 2_000 };
    const odds = { market: 'X2', odds: 1.85, fetchedAt: '2026-11-08T06:00:00Z', validFor: 600 };
    const answer = readOffer({ offer: { ...offer, odds, oddsRequired: true } }, sent);
    expect(answer.kind === 'offer' && answer.offer.price).toEqual({ market: 'X2', odds: 1.85, expires: { mono: 601_000, wall: 602_000 } });
    for (const bad of [{ ...odds, odds: 0.5 }, { ...odds, validFor: 901 }, { ...odds, market: 7 }, 'x']) {
      expect(readOffer({ offer: { ...offer, odds: bad } }, sent)).toEqual({ kind: 'none' });
    }
    expect(readOffer({ offer: { ...offer, odds: null, oddsRequired: true } }, sent)).toEqual({ kind: 'none' });
  });

  it('opens only https links without user info', () => {
    expect(safeLink('https://p.example.com/x?y=1')).toBe('https://p.example.com/x?y=1');
    expect(safeLink('http://p.example.com/')).toBeNull();
    expect(safeLink('javascript:alert(1)')).toBeNull();
    expect(safeLink('https://u:p@p.example.com/')).toBeNull();
    expect(safeLink(42)).toBeNull();
  });

  it('a backend without ads (404) switches the client off after one request', async () => {
    const fetchImpl = vi.fn(() => json(404, { error: 'not_found' }));
    const api = createAdsApi({ getCsrfToken: () => 'csrf', fetchImpl: fetchImpl as unknown as typeof fetch });
    expect(await api.offer('quick_pick', 'fd:1')).toEqual({ kind: 'none' });
    expect(await api.offer('quick_pick', 'fd:2')).toEqual({ kind: 'none' });
    expect(await api.click('tok')).toBeNull();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('concurrent first calls to a backend without ads still cost one request', async () => {
    const fetchImpl = vi.fn(() => json(404, { error: 'not_found' }));
    const api = createAdsApi({ getCsrfToken: () => 'csrf', fetchImpl: fetchImpl as unknown as typeof fetch });
    const answers = await Promise.all([api.offer('quick_pick', 'fd:1'), api.offer('quick_pick', 'fd:2'), api.impression('t')]);
    expect(answers.slice(0, 2)).toEqual([{ kind: 'none' }, { kind: 'none' }]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('sends CSRF with every write and returns the link from the click', async () => {
    const calls: [string, RequestInit][] = [];
    const fetchImpl = vi.fn((url: string, init: RequestInit) => {
      calls.push([url, init]);
      if (url.startsWith('/api/ads/offer')) return json(200, { offer });
      if (url === '/api/ads/click') return json(200, { url: 'https://p.example.com/promo' });
      return json(200, { ok: true });
    });
    const api = createAdsApi({ getCsrfToken: () => 'csrf-1', fetchImpl: fetchImpl as unknown as typeof fetch });
    expect((await api.offer('quick_pick', 'fd:1')).kind).toBe('offer');
    expect(calls[0]![0]).toBe('/api/ads/offer?placement=quick_pick&event_id=fd%3A1');
    await api.impression('tok.sig');
    expect(await api.click('tok.sig')).toBe('https://p.example.com/promo');
    expect(await api.confirmAge(18)).toBe(true);
    for (const [, init] of calls.slice(1)) {
      expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-1');
      expect(init.method).toBe('POST');
    }
    expect(JSON.parse(calls[1]![1].body as string)).toEqual({ impressionToken: 'tok.sig' });
    expect(JSON.parse(calls[3]![1].body as string)).toEqual({ min: 18 });
  });

  it('a gone offer or a bad link yields no link', async () => {
    const gone = createAdsApi({ getCsrfToken: () => 'c', fetchImpl: (() => json(410, { error: 'offer_gone' })) as unknown as typeof fetch });
    expect(await gone.click('tok')).toBeNull();
    const bad = createAdsApi({ getCsrfToken: () => 'c', fetchImpl: (() => json(200, { url: 'http://x.example/' })) as unknown as typeof fetch });
    expect(await bad.click('tok')).toBeNull();
    const noSession = createAdsApi({ getCsrfToken: () => null, fetchImpl: vi.fn() as unknown as typeof fetch });
    expect(await noSession.click('tok')).toBeNull();
  });
});
