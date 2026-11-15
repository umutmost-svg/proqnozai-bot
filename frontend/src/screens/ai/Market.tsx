import { useEffect, useState } from 'react';
import type { MarketKey, MarketPrices, MarketQuote, PriceDeadline, SportEvent } from '../../data/models';
import { LOCALES, type Lang, type Translate } from '../../i18n';
import { formatDateTime } from '../../i18n/format';
import { useSettings } from '../../state/contexts';
import s from './Ai.module.css';
import { decimal, marketText } from './Model';

/** A difference worth naming: smaller gaps are within the noise of either estimate. */
export const NOTABLE_EDGE = 2;

function oneDecimal(lang: Lang, value: number, signed = false): string {
  return new Intl.NumberFormat(LOCALES[lang], {
    maximumFractionDigits: 1,
    minimumFractionDigits: 0,
    ...(signed ? { signDisplay: 'exceptZero' as const } : {}),
  }).format(value);
}

export function pricesExpired(expires: PriceDeadline): boolean {
  return performance.now() >= expires.mono || Date.now() >= expires.wall;
}

/**
 * Whether prices with this deadline may still be shown: false from the moment either clock
 * passes it — checked when it is due, every 15 s, and whenever the app comes back to the front.
 */
export function usePricesLive(expires: PriceDeadline | undefined): boolean {
  const [expiredOf, setExpiredOf] = useState<PriceDeadline | null>(null);
  useEffect(() => {
    if (!expires) return undefined;
    const check = () => {
      if (pricesExpired(expires)) setExpiredOf(expires);
    };
    const due = window.setTimeout(check, Math.max(0, expires.mono - performance.now()) + 20);
    const every = window.setInterval(check, 15_000);
    document.addEventListener('visibilitychange', check);
    return () => {
      window.clearTimeout(due);
      window.clearInterval(every);
      document.removeEventListener('visibilitychange', check);
    };
  }, [expires]);
  // Also checked while rendering: a deadline already past never paints its prices even once.
  return !!expires && expiredOf !== expires && !pricesExpired(expires);
}

export function quoteOf(prices: MarketPrices | undefined, market: MarketKey | 'NO_BET'): MarketQuote | undefined {
  return market === 'NO_BET' ? undefined : prices?.quotes.find((q) => q.market === market);
}

/** "Odds 1.80 · market 51% · model 60% · expected value +8%" — every number from the backend. */
export function priceLine(t: Translate, lang: Lang, q: MarketQuote): string {
  return t('ai.price.line', {
    odds: decimal(lang, q.odds),
    market: oneDecimal(lang, q.marketProbability),
    model: oneDecimal(lang, q.modelProbability),
    ev: oneDecimal(lang, q.expectedValue, true),
  });
}

export function PriceLine({ quote }: { quote: MarketQuote }) {
  const { t, settings } = useSettings();
  return (
    <p className={s.priceLine} data-testid="price-line" data-positive={quote.expectedValue > 0 || undefined}>
      {priceLine(t, settings.lang, quote)}
    </p>
  );
}

/**
 * The model against the market: every priced outcome with its odds, the market probability
 * with the margin removed, the model's, and the expected value at that price — ordered by the
 * size of the model's advantage. The verdict names the largest difference, or says there is none.
 */
export function MarketValue({ prices, event }: { prices: MarketPrices; event: SportEvent }) {
  const { t, settings, timeZone } = useSettings();
  const lang = settings.lang;
  const home = event.home.name;
  const away = event.away.name;
  const rows = [...prices.quotes].sort((a, b) => b.edge - a.edge || a.market.localeCompare(b.market));
  // The verdict names the largest difference either way (the model above or below the market).
  const best = [...prices.quotes].sort((a, b) => Math.abs(b.edge) - Math.abs(a.edge) || a.market.localeCompare(b.market))[0];
  const margin = prices.quotes.find((q) => q.market === '1')?.margin;
  return (
    <section className={s.card} data-testid="ai-market-value">
      <h3 className={s.cardTitle}>{t('ai.value.title')}</h3>
      <p className={s.body} data-testid="ai-market-verdict">
        {best && Math.abs(best.edge) >= NOTABLE_EDGE
          ? t('ai.value.best', {
              outcome: marketText(t, best.market, home, away),
              model: oneDecimal(lang, best.modelProbability),
              market: oneDecimal(lang, best.marketProbability),
              edge: oneDecimal(lang, best.edge, true),
            })
          : t('ai.value.none')}
      </p>
      {/* One row per outcome: its name across the card, the four numbers under it (fits 360 px). */}
      <div className={s.valueHead} aria-hidden="true">
        <span>{t('ai.value.odds')}</span>
        <span>{t('ai.value.market')}</span>
        <span>{t('ai.value.model')}</span>
        <span>{t('ai.value.ev')}</span>
      </div>
      <ul className={s.valueRows}>
        {rows.map((q) => (
          <li key={q.market} className={s.valueRow} data-market={q.market} data-positive={q.expectedValue > 0 || undefined}>
            <span className={s.valueName}>{marketText(t, q.market, home, away)}</span>
            <span aria-label={`${t('ai.value.odds')} ${decimal(lang, q.odds)}`}>{decimal(lang, q.odds)}</span>
            <span aria-label={`${t('ai.value.market')} ${oneDecimal(lang, q.marketProbability)}%`}>{oneDecimal(lang, q.marketProbability)}%</span>
            <span aria-label={`${t('ai.value.model')} ${oneDecimal(lang, q.modelProbability)}%`}>{oneDecimal(lang, q.modelProbability)}%</span>
            <span className={s.valueEv} aria-label={`${t('ai.value.ev')} ${oneDecimal(lang, q.expectedValue, true)}%`}>
              {oneDecimal(lang, q.expectedValue, true)}%
            </span>
          </li>
        ))}
      </ul>
      <p className={s.hint} data-testid="ai-market-asof">
        {[
          t('ai.value.asOf', { time: formatDateTime(new Date(prices.fetchedAt), timeZone) }),
          margin !== undefined ? t('ai.value.margin', { value: oneDecimal(lang, margin) }) : null,
        ]
          .filter(Boolean)
          .join(' · ')}
      </p>
    </section>
  );
}
