import { useState } from 'react';
import type { Campaign, CampaignInput, Condition, Group, OddsMode, Partner, Placement, Tier, Variant } from './adsApi';
import { LANGS, adsError, adsApi, fromLocalInput, parseList, toLocalInput } from './adsApi';
import { TargetingBuilder } from './Targeting';

const KEYS = ['A', 'B', 'C'] as const;
const DAY = 86400;

export function blankCampaign(partnerId: number, now: number): CampaignInput {
  return {
    name: '', tier: 'general', priority: 0, langIn: [], placements: ['quick_pick', 'ai_pick'], countries: [],
    allowUnknownCountry: false, startsAt: now, endsAt: now + 30 * DAY, partnerId, disclosure: {}, condition: null,
    controlPct: 0, oddsMode: 'optional', caps: { perDay: 0, total: 0 },
    variants: [{ key: 'A', weight: 100, headline: '', body: '', extra: '', cta: '', url: '' }],
  };
}

export function inputOf(c: Campaign): CampaignInput {
  const { id, name, tier, priority, langIn, placements, countries, allowUnknownCountry, startsAt, endsAt, partnerId,
    disclosure, condition, controlPct, oddsMode, caps, variants } = c;
  return { id, name, tier, priority, langIn, placements, countries, allowUnknownCountry, startsAt, endsAt, partnerId,
    disclosure, condition, controlPct, oddsMode, caps, variants };
}

/** Even split of 100 % over n variants (the remainder goes to A). */
function split(n: number): number[] {
  const base = Math.floor(100 / n);
  return Array.from({ length: n }, (_, i) => base + (i === 0 ? 100 - base * n : 0));
}

/**
 * Campaign editor: offer texts and CTA per A/B/C variant, partner link (only the partner's hosts; {click_id}
 * is the one allowed placeholder), schedule (UTC), priority, placements, GEO, languages, audience, frequency
 * caps and what happens without a verified price. There is no odds field: prices come from the odds store.
 */
export function CampaignForm({ initial, partners, groups, readOnly, onSaved }: {
  initial: CampaignInput; partners: Partner[]; groups: Group[]; readOnly: boolean; onSaved: (id: number) => void;
}) {
  const [c, setC] = useState<CampaignInput>(initial);
  const [countries, setCountries] = useState(initial.countries.join(', '));
  const [conditionError, setConditionError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (p: Partial<CampaignInput>) => setC((prev) => ({ ...prev, ...p }));
  const setVariant = (i: number, p: Partial<Variant>) => set({ variants: c.variants.map((v, j) => (j === i ? { ...v, ...p } : v)) });
  const weights = c.variants.reduce((s, v) => s + v.weight, 0);
  const partner = partners.find((p) => p.id === c.partnerId);

  const setCount = (n: number) => {
    const w = split(n);
    set({
      variants: KEYS.slice(0, n).map((key, i) => ({
        ...(c.variants[i] ?? { ...c.variants[0]!, headline: '', body: '', extra: '', cta: '' }), key, weight: w[i]!,
      })),
    });
  };
  const toggle = <T,>(list: T[], item: T): T[] => (list.includes(item) ? list.filter((x) => x !== item) : [...list, item]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (readOnly || busy) return;
    if (conditionError) {
      setError(conditionError);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await adsApi.saveCampaign({ ...c, countries: parseList(countries) });
      onSaved(r.id);
    } catch (err) {
      setError(adsError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={save} aria-label="Кампания" data-testid="campaign-form">
      <fieldset disabled={readOnly} style={{ border: 0, padding: 0, margin: 0 }}>
        <section className="card section">
          <h2 className="card-title">Основное</h2>
          <div className="field">
            <label htmlFor="ads-name">Внутреннее название</label>
            <input id="ads-name" className="control" value={c.name} maxLength={120} onChange={(e) => set({ name: e.target.value })} />
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="ads-partner">Партнёр</label>
              <select id="ads-partner" className="control" value={c.partnerId} onChange={(e) => set({ partnerId: Number(e.target.value) })}>
                {partners.map((p) => <option key={p.id} value={p.id}>{p.name}{p.active ? '' : ' (выключен)'}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="ads-tier">Уровень приоритета</label>
              <select id="ads-tier" className="control" value={c.tier} onChange={(e) => set({ tier: e.target.value as Tier })}>
                <option value="individual">Индивидуальная</option>
                <option value="segment">Сегмент</option>
                <option value="general">Общая</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="ads-priority">Приоритет (0–1000)</label>
              <input id="ads-priority" className="control" type="number" min={0} max={1000} value={c.priority}
                onChange={(e) => set({ priority: Number(e.target.value) })} />
            </div>
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="ads-start">Начало (UTC)</label>
              <input id="ads-start" className="control" type="datetime-local" value={toLocalInput(c.startsAt)}
                onChange={(e) => { const v = fromLocalInput(e.target.value); if (v !== null) set({ startsAt: v }); }} />
            </div>
            <div className="field">
              <label htmlFor="ads-end">Окончание (UTC)</label>
              <input id="ads-end" className="control" type="datetime-local" value={toLocalInput(c.endsAt)}
                onChange={(e) => { const v = fromLocalInput(e.target.value); if (v !== null) set({ endsAt: v }); }} />
            </div>
          </div>
          <div className="field">
            <span className="small strong">Где показывать</span>
            <div className="row small" style={{ gap: 16 }}>
              {([['quick_pick', 'Быстрый прогноз'], ['ai_pick', 'AI-разбор: «Рекомендуемая ставка»']] as [Placement, string][]).map(([p, label]) => (
                <label key={p}><input type="checkbox" checked={c.placements.includes(p)} onChange={() => set({ placements: toggle(c.placements, p) })} /> {label}</label>
              ))}
            </div>
          </div>
        </section>

        <section className="card section">
          <h2 className="card-title">Тексты, кнопки и ссылки</h2>
          <p className="small muted">
            Коэффициент в карточке подставляет Mini App из проверенной ленты для рекомендованного исхода; в текстах цифры
            коэффициентов запрещены. Ссылка — https на доменах партнёра ({partner ? partner.hosts.join(', ') : '—'}); для
            постбэка добавьте <code>{'{click_id}'}</code>, например <code>?sub={'{click_id}'}</code>.
          </p>
          <div className="field-row">
            <div className="field">
              <label htmlFor="ads-count">Варианты A/B</label>
              <select id="ads-count" className="control" value={c.variants.length} onChange={(e) => setCount(Number(e.target.value))}>
                <option value={1}>Один вариант</option>
                <option value={2}>A / B</option>
                <option value={3}>A / B / C</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="ads-control">Контрольная группа, % (без рекламы)</label>
              <input id="ads-control" className="control" type="number" min={0} max={50} value={c.controlPct}
                onChange={(e) => set({ controlPct: Number(e.target.value) })} />
            </div>
          </div>
          {c.variants.map((v, i) => (
            <div key={v.key} className="card" style={{ marginBottom: 12 }} data-testid={`variant-${v.key}`}>
              <div className="field-row">
                <strong>Вариант {v.key}</strong>
                {c.variants.length > 1 && (
                  <label className="small">Вес, %{' '}
                    <input className="control" type="number" min={1} max={100} style={{ width: 80 }} aria-label={`Вес ${v.key}`}
                      value={v.weight} onChange={(e) => setVariant(i, { weight: Number(e.target.value) })} />
                  </label>
                )}
              </div>
              <div className="field"><label htmlFor={`h-${v.key}`}>Заголовок</label>
                <input id={`h-${v.key}`} className="control" maxLength={80} value={v.headline} onChange={(e) => setVariant(i, { headline: e.target.value })} /></div>
              <div className="field"><label htmlFor={`b-${v.key}`}>Текст</label>
                <textarea id={`b-${v.key}`} className="control" rows={3} maxLength={300} value={v.body} onChange={(e) => setVariant(i, { body: e.target.value })} /></div>
              <div className="field"><label htmlFor={`x-${v.key}`}>Дополнительная строка (условия)</label>
                <input id={`x-${v.key}`} className="control" maxLength={120} value={v.extra} onChange={(e) => setVariant(i, { extra: e.target.value })} /></div>
              <div className="field-row">
                <div className="field"><label htmlFor={`c-${v.key}`}>Текст кнопки</label>
                  <input id={`c-${v.key}`} className="control" maxLength={30} value={v.cta} onChange={(e) => setVariant(i, { cta: e.target.value })} /></div>
                <div className="field"><label htmlFor={`u-${v.key}`}>Ссылка партнёра</label>
                  <input id={`u-${v.key}`} className="control" maxLength={500} value={v.url} placeholder="https://…?sub={click_id}"
                    onChange={(e) => setVariant(i, { url: e.target.value })} /></div>
              </div>
            </div>
          ))}
          {weights !== 100 && <div className="form-error" role="alert">Сумма весов — {weights} %, нужно 100 %.</div>}
          <div className="field">
            <label htmlFor="ads-erid">Идентификатор рекламы (маркировка)</label>
            <input id="ads-erid" className="control" maxLength={64} value={c.disclosure.adId ?? ''}
              onChange={(e) => set({ disclosure: { ...c.disclosure, adId: e.target.value } })} />
          </div>
        </section>

        <section className="card section">
          <h2 className="card-title">Аудитория и частота</h2>
          <div className="field-row">
            <div className="field">
              <label htmlFor="ads-countries">Страны (пусто — все страны партнёра)</label>
              <input id="ads-countries" className="control" value={countries} placeholder="AZ, TR" onChange={(e) => setCountries(e.target.value)} />
            </div>
            <label className="small" style={{ alignSelf: 'center' }}>
              <input type="checkbox" checked={c.allowUnknownCountry} onChange={(e) => set({ allowUnknownCountry: e.target.checked })} />{' '}
              Показывать при неизвестной стране (если разрешил партнёр)
            </label>
          </div>
          <div className="field">
            <span className="small strong">Языки интерфейса (пусто — все)</span>
            <div className="row small" style={{ gap: 12 }}>
              {LANGS.map((l) => (
                <label key={l}><input type="checkbox" checked={c.langIn.includes(l)} onChange={() => set({ langIn: toggle(c.langIn, l) })} /> {l}</label>
              ))}
            </div>
          </div>
          <TargetingBuilder value={c.condition} groups={groups} onChange={(cond: Condition, err) => { setConditionError(err); if (!err) set({ condition: cond }); }} />
          <div className="field-row" style={{ marginTop: 12 }}>
            <div className="field">
              <label htmlFor="ads-cap-day">Показов в день на пользователя (0 — без ограничения)</label>
              <input id="ads-cap-day" className="control" type="number" min={0} max={100} value={c.caps.perDay}
                onChange={(e) => set({ caps: { ...c.caps, perDay: Number(e.target.value) } })} />
            </div>
            <div className="field">
              <label htmlFor="ads-cap-total">Показов всего на пользователя (за 90 дней; 0 — без ограничения)</label>
              <input id="ads-cap-total" className="control" type="number" min={0} max={1000} value={c.caps.total}
                onChange={(e) => set({ caps: { ...c.caps, total: Number(e.target.value) } })} />
            </div>
            <div className="field">
              <label htmlFor="ads-odds">Без проверенного коэффициента</label>
              <select id="ads-odds" className="control" value={c.oddsMode} onChange={(e) => set({ oddsMode: e.target.value as OddsMode })}>
                <option value="optional">Показывать без коэффициента</option>
                <option value="required">Не показывать</option>
              </select>
            </div>
          </div>
        </section>
      </fieldset>
      {error && <div className="form-error" role="alert">{error}</div>}
      {!readOnly && (
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="submit" className="btn primary" disabled={busy || weights !== 100}>{busy ? 'Сохранение…' : 'Сохранить'}</button>
        </div>
      )}
    </form>
  );
}
