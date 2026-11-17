/**
 * Partner offers (docs/miniapp/ads-architecture.md §6). The server chooses the offer for the signed-in
 * user; the client gets texts and an impression token, never a link. The link comes back only from a
 * click, from the server's current snapshot, and is opened through Telegram (https only).
 *
 * The backend registers /api/ads only with MINIAPP_ADS_ENABLED=true: a 404 switches this client off for
 * the rest of the page's life, so a disabled backend costs one request.
 *
 * A price in an offer is never the advertiser's: the server sends the verified odds store's current price
 * of the market it recommended (or none), with the seconds it may be shown; the card hides it after that.
 */
import type { PriceDeadline } from './models';

export type Placement = 'quick_pick' | 'ai_pick';

export interface PartnerOffer {
  id: number;
  variant: string;
  headline: string;
  body: string;
  extra: string;
  cta: string;
  disclosure: { advertiser?: string; adId?: string; ageLabel?: string };
  /** The verified price of the recommended market, until `expires`; null = shown without a price. */
  price: { market: string; odds: number; expires: PriceDeadline } | null;
  /** The campaign is shown only with a price: once the price expires, the whole offer goes. */
  oddsRequired: boolean;
  impressionToken: string;
}

export type OfferAnswer = { kind: 'none' } | { kind: 'offer'; offer: PartnerOffer } | { kind: 'ageGate'; min: number };

export interface AdsApi {
  offer(placement: Placement, eventId: string): Promise<OfferAnswer>;
  impression(token: string): Promise<void>;
  /** The link to open, or null when the offer is gone (paused, ended) or the request failed. */
  click(token: string): Promise<string | null>;
  /** The user's own statement "I am `min` or older" for the partner's age gate. */
  confirmAge(min: number): Promise<boolean>;
}

const TIMEOUT_MS = 8_000;
const MAX_VALID_FOR_S = 3600; // the server never serves a price for longer (miniapp/odds/store.py MAX_VALID_FOR_S)
const NONE: OfferAnswer = { kind: 'none' };

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);
const text = (v: unknown, max: number): string | null => (typeof v === 'string' && v.length <= max ? v : null);

function readPrice(v: unknown, sent: PriceDeadline): PartnerOffer['price'] | undefined {
  if (v === null || v === undefined) return null;
  if (!isObj(v)) return undefined;
  const market = text(v.market, 12);
  const { odds, validFor } = v;
  if (!market || typeof odds !== 'number' || !Number.isFinite(odds) || odds < 1.01 || odds > 100) return undefined;
  if (typeof validFor !== 'number' || !Number.isInteger(validFor) || validFor < 1 || validFor > MAX_VALID_FOR_S) return undefined;
  return { market, odds, expires: { mono: sent.mono + validFor * 1000, wall: sent.wall + validFor * 1000 } };
}

/** Strict reading of the server's offer: anything unexpected means no offer (never a half-filled card). */
export function readOffer(body: unknown, sent: PriceDeadline = { mono: performance.now(), wall: Date.now() }): OfferAnswer {
  if (!isObj(body)) return NONE;
  const gate = body.ageGate;
  if (body.offer === null && isObj(gate) && typeof gate.min === 'number' && Number.isInteger(gate.min) && gate.min > 0) {
    return { kind: 'ageGate', min: gate.min };
  }
  const o = body.offer;
  if (!isObj(o) || typeof o.id !== 'number' || !Number.isInteger(o.id)) return NONE;
  const headline = text(o.headline, 80);
  const cta = text(o.cta, 30);
  const variant = text(o.variant, 1);
  const token = text(o.impressionToken, 400);
  const bodyText = text(o.body, 300);
  const extra = text(o.extra, 120);
  if (!headline || !cta || !variant || !token || bodyText === null || extra === null) return NONE;
  const price = readPrice(o.odds, sent);
  const oddsRequired = o.oddsRequired === true;
  // A malformed price is no offer at all; a required one that is missing is no offer either.
  if (price === undefined || (oddsRequired && price === null)) return NONE;
  const d = isObj(o.disclosure) ? o.disclosure : {};
  const disclosure: PartnerOffer['disclosure'] = {};
  for (const [key, max] of [['advertiser', 120], ['adId', 64], ['ageLabel', 8]] as const) {
    const value = text(d[key], max);
    if (value) disclosure[key] = value;
  }
  return {
    kind: 'offer',
    offer: { id: o.id, variant, headline, body: bodyText, extra, cta, disclosure, price, oddsRequired, impressionToken: token },
  };
}

/** Only an https link without user info; anything else is never opened. */
export function safeLink(v: unknown): string | null {
  if (typeof v !== 'string' || v.length > 500) return null;
  try {
    const url = new URL(v);
    return url.protocol === 'https:' && !url.username && !url.password ? url.toString() : null;
  } catch {
    return null;
  }
}

export function createAdsApi(options: {
  getCsrfToken(): string | null;
  /** Called when the server answers 401: the session ended (AuthController.sessionLost). */
  onUnauthenticated?(): void;
  fetchImpl?: typeof fetch;
  base?: string;
}): AdsApi {
  const fetchImpl = options.fetchImpl ?? ((...a: Parameters<typeof fetch>) => fetch(...a));
  const base = options.base ?? '/api';
  let disabled = false;
  // Until the backend has answered once, requests wait for the first one: concurrent cards on a backend
  // without ads still cost a single request.
  let probe: Promise<unknown> | null = null;
  let probed = false;

  async function call(path: string, init: RequestInit): Promise<{ status: number; body: unknown } | null> {
    if (!probed) {
      if (probe) {
        await probe;
      } else {
        const first = exchange(path, init);
        probe = first;
        const result = await first;
        probed = true;
        return result;
      }
    }
    return exchange(path, init);
  }

  async function exchange(path: string, init: RequestInit): Promise<{ status: number; body: unknown } | null> {
    if (disabled) return null;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = setTimeout(() => controller?.abort(), TIMEOUT_MS);
    try {
      const response = await fetchImpl(`${base}${path}`, {
        credentials: 'same-origin',
        cache: 'no-store',
        ...init,
        signal: controller?.signal,
        headers: { Accept: 'application/json', ...init.headers },
      });
      if (response.status === 404) {
        disabled = true; // the feature is off on this backend
        return null;
      }
      if (response.status === 401) options.onUnauthenticated?.();
      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      return { status: response.status, body };
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  function post(path: string, payload: Json) {
    const csrf = options.getCsrfToken();
    if (!csrf) return Promise.resolve(null);
    return call(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(payload),
    });
  }

  return {
    async offer(placement, eventId) {
      const query = new URLSearchParams({ placement, event_id: eventId }).toString();
      // The price's validity counts from when the request left, not from when the answer arrived.
      const sent = { mono: performance.now(), wall: Date.now() };
      const r = await call(`/ads/offer?${query}`, { method: 'GET' });
      return r && r.status === 200 ? readOffer(r.body, sent) : NONE;
    },
    async impression(token) {
      await post('/ads/impression', { impressionToken: token });
    },
    async click(token) {
      const r = await post('/ads/click', { impressionToken: token });
      return r && r.status === 200 && isObj(r.body) ? safeLink(r.body.url) : null;
    },
    async confirmAge(min) {
      const r = await post('/ads/age', { min });
      return !!r && r.status === 200;
    },
  };
}
