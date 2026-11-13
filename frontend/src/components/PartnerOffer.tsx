import { useEffect, useRef, useState } from 'react';
import type { AdsApi, OfferAnswer, Placement } from '../data/ads';
import { useServices, useSettings } from '../state/contexts';
import { usePricesLive } from '../screens/ai/Market';
import { decimal } from '../screens/ai/Model';
import ui from './ui.module.css';
import s from './PartnerOffer.module.css';

/**
 * A partner's offer inside the recommended bet — quick pick and the AI screen's card (docs/miniapp/
 * ads-architecture.md §7). The outcome comes from the model; the price, when there is one, from the
 * verified odds store for exactly that market (sent by the server, hidden once it expires). This block
 * adds the partner's texts, its CTA and the ad marking. Texts are rendered as text (never HTML). Without
 * an offer nothing is shown.
 */
export function PartnerOffer({ placement, eventId }: { placement: Placement; eventId: string }) {
  const { ads } = useServices();
  if (!ads) return null;
  return <OfferBlock ads={ads} placement={placement} eventId={eventId} />;
}

function OfferBlock({ ads, placement, eventId }: { ads: AdsApi; placement: Placement; eventId: string }) {
  const { t, settings } = useSettings();
  const { telegram } = useServices();
  const [answer, setAnswer] = useState<OfferAnswer>({ kind: 'none' });
  const [gone, setGone] = useState(false);
  const [busy, setBusy] = useState(false);
  const counted = useRef<string | null>(null);
  const [round, setRound] = useState(0);

  useEffect(() => {
    let live = true;
    ads.offer(placement, eventId).then((a) => {
      if (!live) return;
      setGone(false);
      setAnswer(a);
    });
    return () => {
      live = false;
    };
  }, [ads, placement, eventId, round]);

  const priced = usePricesLive(answer.kind === 'offer' ? answer.offer.price?.expires : undefined);
  // A campaign shown only with a price disappears with it (the server also refuses its click then); an offer that
  // is not on screen is never counted as an impression (nor against the user's frequency cap).
  const visible = answer.kind === 'offer' && (!answer.offer.oddsRequired || priced);
  const token = visible ? answer.offer.impressionToken : null;
  useEffect(() => {
    // Counted once per shown offer (the server also keeps one per user, campaign, variant, match and day).
    if (token && counted.current !== token) {
      counted.current = token;
      void ads.impression(token);
    }
  }, [ads, token]);

  if (answer.kind === 'none') return null;

  if (answer.kind === 'ageGate') {
    const confirm = async () => {
      setBusy(true);
      const ok = await ads.confirmAge(answer.min);
      setBusy(false);
      if (ok) setRound((n) => n + 1);
    };
    return (
      <section className={s.offer} data-testid="partner-age-gate" aria-label={t('ads.label')}>
        <p className={s.label}>{t('ads.label')}</p>
        <p className={s.body}>{t('ads.ageGate.text', { min: answer.min })}</p>
        <button type="button" className={ui.secondary} onClick={confirm} disabled={busy} data-testid="partner-age-confirm">
          {t('ads.ageGate.confirm', { min: answer.min })}
        </button>
      </section>
    );
  }

  const { offer } = answer;
  if (!visible) return null;
  const open = async () => {
    setBusy(true);
    const url = await ads.click(offer.impressionToken);
    setBusy(false);
    if (url) telegram.openLink(url);
    else setGone(true);
  };
  const disclosure = [offer.disclosure.advertiser, offer.disclosure.ageLabel].filter(Boolean).join(' · ');
  return (
    <section className={s.offer} data-testid="partner-offer" data-variant={offer.variant} aria-label={t('ads.label')}>
      <p className={s.label} data-testid="partner-disclosure">
        {t('ads.label')}
        {disclosure ? ` · ${disclosure}` : ''}
      </p>
      <p className={s.headline}>{offer.headline}</p>
      {offer.price && priced && (
        <p className={s.odds} data-testid="partner-odds">
          {t('ads.odds', { odds: decimal(settings.lang, offer.price.odds) })}
        </p>
      )}
      {offer.body && <p className={s.body}>{offer.body}</p>}
      {offer.extra && <p className={s.extra}>{offer.extra}</p>}
      {gone ? (
        <p className={s.gone} role="status">{t('ads.gone')}</p>
      ) : (
        <button type="button" className={`${ui.primary} ${s.cta}`} onClick={open} disabled={busy} data-testid="partner-cta">
          {offer.cta}
        </button>
      )}
      {offer.disclosure.adId && <p className={s.adId}>{t('ads.adId', { id: offer.disclosure.adId })}</p>}
    </section>
  );
}
