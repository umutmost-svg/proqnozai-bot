import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AdsApi, OfferAnswer } from '../data/ads';
import { AuthController } from '../auth/controller';
import { BootTracker } from '../loading/boot';
import { AppProviders } from '../state/AppProviders';
import type { Services } from '../state/contexts';
import { createTelegramAdapter } from '../telegram/telegram';
import { offlineAuthApi } from '../test/renderApp';
import { DemoDataProvider } from '../data/DemoDataProvider';
import { STORAGE_KEY } from '../settings/settings';
import { fixedClock } from '../time/clock';
import { PartnerOffer } from './PartnerOffer';

const OFFER: OfferAnswer = {
  kind: 'offer',
  offer: {
    id: 3,
    variant: 'B',
    headline: '<b>Бонус</b>',
    body: 'Текст',
    extra: 'Условия',
    cta: 'Забрать',
    disclosure: { advertiser: 'Partner LLC', adId: 'ERID-1', ageLabel: '18+' },
    price: null,
    oddsRequired: false,
    impressionToken: 'tok',
  },
};

function priced(required: boolean, validMs: number): OfferAnswer {
  if (OFFER.kind !== 'offer') throw new Error('fixture');
  const expires = { mono: performance.now() + validMs, wall: Date.now() + validMs };
  return { kind: 'offer', offer: { ...OFFER.offer, price: { market: '1X', odds: 1.85, expires }, oddsRequired: required } };
}

function setup(ads: AdsApi | undefined) {
  const clock = fixedClock('2026-11-08T06:00:00Z');
  const telegram = createTelegramAdapter(undefined);
  const openLink = vi.spyOn(telegram, 'openLink').mockImplementation(() => {});
  const services: Services = {
    clock,
    data: new DemoDataProvider(clock, { latencyMs: 0 }),
    telegram,
    auth: new AuthController(offlineAuthApi, () => null),
    scrollMemory: new Map(),
    boot: new BootTracker(),
    ads,
  };
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ lang: 'ru', timeZone: 'Asia/Baku', reduceMotion: false, haptics: false }));
  render(
    <AppProviders services={services}>
      <PartnerOffer placement="quick_pick" eventId="fd:1" />
    </AppProviders>,
  );
  return { openLink };
}

function fakeAds(answer: OfferAnswer, link: string | null = 'https://p.example.com/promo'): AdsApi & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    offer: vi.fn(async () => {
      calls.push('offer');
      return answer;
    }),
    impression: vi.fn(async () => {
      calls.push('impression');
    }),
    click: vi.fn(async () => {
      calls.push('click');
      return link;
    }),
    confirmAge: vi.fn(async () => {
      calls.push('age');
      return true;
    }),
  };
}

describe('PartnerOffer', () => {
  it('renders nothing without the ads service (demo) or without an offer', async () => {
    setup(undefined);
    expect(screen.queryByTestId('partner-offer')).toBeNull();
    const ads = fakeAds({ kind: 'none' });
    setup(ads);
    await waitFor(() => expect(ads.offer).toHaveBeenCalled());
    expect(screen.queryByTestId('partner-offer')).toBeNull();
  });

  it('shows texts as text with the disclosure, counts one impression and opens the link from the click', async () => {
    const ads = fakeAds(OFFER);
    const { openLink } = setup(ads);
    const card = await screen.findByTestId('partner-offer');
    expect(card).toHaveTextContent('<b>Бонус</b>'); // never interpreted as HTML
    expect(card.querySelector('b')).toBeNull();
    expect(screen.getByTestId('partner-disclosure')).toHaveTextContent('Реклама партнёра · Partner LLC · 18+');
    expect(card).toHaveTextContent('ID рекламы: ERID-1');
    await waitFor(() => expect(ads.impression).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByTestId('partner-cta'));
    await waitFor(() => expect(openLink).toHaveBeenCalledWith('https://p.example.com/promo'));
  });

  it('shows the verified price while it is valid and never one of its own', async () => {
    setup(fakeAds(OFFER));
    await screen.findByTestId('partner-offer');
    expect(screen.queryByTestId('partner-odds')).toBeNull();
    setup(fakeAds(priced(false, 60_000)));
    expect(await screen.findByTestId('partner-odds')).toHaveTextContent('Коэффициент: 1,85');
  });

  it('an offer that requires a price disappears once the price expires and is never counted; an optional one stays', async () => {
    const hidden = fakeAds(priced(true, -1));
    setup(hidden);
    await waitFor(() => expect(hidden.offer).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByTestId('partner-offer')).toBeNull();
    expect(hidden.impression).not.toHaveBeenCalled();
    setup(fakeAds(priced(false, -1)));
    expect(await screen.findByTestId('partner-offer')).toBeInTheDocument();
    expect(screen.queryByTestId('partner-odds')).toBeNull();
  });

  it('an offer that is gone says so instead of opening anything', async () => {
    const ads = fakeAds(OFFER, null);
    const { openLink } = setup(ads);
    await userEvent.click(await screen.findByTestId('partner-cta'));
    expect(await screen.findByText('Предложение больше недоступно')).toBeInTheDocument();
    expect(openLink).not.toHaveBeenCalled();
  });

  it('asks for the partner’s age once, then loads the offer', async () => {
    const ads = fakeAds({ kind: 'ageGate', min: 18 });
    setup(ads);
    const button = await screen.findByTestId('partner-age-confirm');
    expect(button).toHaveTextContent('Мне есть 18 лет');
    (ads.offer as ReturnType<typeof vi.fn>).mockResolvedValueOnce(OFFER);
    await userEvent.click(button);
    expect(ads.confirmAge).toHaveBeenCalledWith(18);
    expect(await screen.findByTestId('partner-offer')).toBeInTheDocument();
  });
});
