import { useState } from 'react';
import { download } from '../api';
import { num, pct } from '../format';
import { useLoad } from '../state';
import { Empty, ErrorBox, Loading } from '../ui';
import type { AdsPeriod, AdsReport, Campaign } from './adsApi';
import { PERIODS, adsApi, adsError } from './adsApi';

const PERIOD_LABEL: Record<AdsPeriod, string> = {
  1: 'Сегодня', 7: '7 дней', 30: '30 дней', 90: '90 дней', 180: '180 дней', 365: '12 месяцев', 395: '13 месяцев',
};
const SEGMENT_LABEL: Record<string, string> = { new: 'Новые (до 7 дней)', active: 'Активные (3+ дня из 7)', other: 'Остальные', unknown: 'Не определено' };

/** Ads analytics: impressions, unique reach, clicks, CTR (with the 95 % interval of the unique CTR), control
 * group, confirmed conversions (partner report by MB id) and partner postbacks; by variant, segment, country, day. */
export function AdsAnalyticsView({ manage }: { manage: boolean }) {
  const [days, setDays] = useState<AdsPeriod>(30);
  const [campaign, setCampaign] = useState<number | null>(null);
  const campaigns = useLoad<{ items: Campaign[] }>('/api/admin/ads/campaigns');
  const report = useLoad<AdsReport>(`/api/admin/ads/analytics?days=${days}${campaign ? `&campaign=${campaign}` : ''}`);
  const [csvError, setCsvError] = useState<string | null>(null);
  const exportCsv = () => {
    setCsvError(null);
    download(adsApi.csvUrl(days, campaign), `ads-${days}d${campaign ? `-${campaign}` : ''}.csv`)
      .catch((e: unknown) => setCsvError(adsError(e)));
  };
  return (
    <>
      <div className="filters row" style={{ gap: 8, marginBottom: 12 }}>
        <select className="control" aria-label="Период" value={days} onChange={(e) => setDays(Number(e.target.value) as AdsPeriod)}>
          {PERIODS.map((p) => <option key={p} value={p}>{PERIOD_LABEL[p]}</option>)}
        </select>
        <select className="control" aria-label="Кампания" value={campaign ?? ''} onChange={(e) => setCampaign(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Все кампании</option>
          {(campaigns.data?.items ?? []).map((c) => <option key={c.id} value={c.id}>#{c.id} {c.name}</option>)}
        </select>
        <button type="button" className="btn" onClick={exportCsv}>CSV</button>
      </div>
      {csvError && <p className="error" role="alert">{csvError}</p>}
      {report.error ? <ErrorBox error={report.error} onRetry={report.reload} /> : !report.data ? <Loading /> : (
        <ReportView report={report.data} />
      )}
      {campaign !== null && manage && <Conversions campaign={campaign} onDone={report.reload} />}
    </>
  );
}

export function ReportView({ report }: { report: AdsReport }) {
  if (report.source === 'not_collected') {
    return <Empty title="Рекламная телеметрия пока не собирается">Она появится, когда в Mini App включат рекламу (MINIAPP_ADS_ENABLED).</Empty>;
  }
  return (
    <>
      {report.source === 'unavailable' && (
        <div className="banner warn" role="status">Телеметрия сейчас недоступна: цифры неполные, это не «ноль».</div>
      )}
      {report.aggregated && (
        <div className="banner" role="status" data-testid="ads-aggregated">
          Период длиннее 90 дней считается по дневным агрегатам: показы, клики и конверсии точные, охват и уникальный CTR
          за такой период не хранятся.
        </div>
      )}
      {report.variants.length === 0 ? <Empty title="Показов за период нет" /> : (
        <div className="card table-card">
          <table className="table" data-testid="ads-variants">
            <thead>
              <tr>
                <th>Кампания</th><th>Вариант</th><th className="num">Показы</th><th className="num">Охват</th><th className="num">Клики</th>
                <th className="num">CTR</th><th className="num">Уник. CTR (95 %)</th><th className="num">Контроль</th>
                <th className="num">Конверсии (отчёт)</th><th className="num">Конверсии (постбэк)</th>
              </tr>
            </thead>
            <tbody>
              {report.variants.map((v) => (
                <tr key={`${v.campaignId}:${v.variant}`}>
                  <td>{v.campaign ?? `#${v.campaignId}`}</td>
                  <td>{v.variant === 'control' ? 'контроль' : v.variant === 'unattributed' ? 'без клика' : v.variant}</td>
                  <td className="num">{num(v.impressions)}</td>
                  <td className="num">{v.reach === null ? '—' : num(v.reach)}</td>
                  <td className="num">{num(v.clicks)}</td>
                  <td className="num">{v.ctr === null ? '—' : pct(v.ctr)}</td>
                  <td className="num">
                    {v.uniqueCtr === null ? '—' : pct(v.uniqueCtr)}
                    {v.uniqueCtrCi && <div className="small muted">{pct(v.uniqueCtrCi[0])}–{pct(v.uniqueCtrCi[1])}</div>}
                  </td>
                  <td className="num">{num(v.control)}</td>
                  <td className="num">{num(v.conversions)}</td>
                  <td className="num">{num(v.partnerConversions)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {report.days.length > 0 && (
        <section className="card" style={{ marginTop: 16 }}>
          <h2 className="card-title">По дням (UTC)</h2>
          <table className="table" data-testid="ads-days">
            <thead><tr><th>День</th><th className="num">Показы</th><th className="num">Клики</th><th className="num">CTR</th><th className="num">Контроль</th></tr></thead>
            <tbody>
              {report.days.map((d) => (
                <tr key={d.day}>
                  <td>{d.day}</td><td className="num">{num(d.impressions)}</td><td className="num">{num(d.clicks)}</td>
                  <td className="num">{d.impressions ? pct(d.clicks / d.impressions) : '—'}</td><td className="num">{num(d.control)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      <div className="grid card-3" style={{ marginTop: 16 }}>
        <section className="card">
          <h2 className="card-title">По сегментам</h2>
          <table className="table" data-testid="ads-segments">
            <thead><tr><th>Сегмент</th><th className="num">Показы</th><th className="num">Клики</th><th className="num">CTR</th></tr></thead>
            <tbody>
              {report.segments.map((s) => (
                <tr key={s.segment}>
                  <td>{SEGMENT_LABEL[s.segment] ?? s.segment}</td><td className="num">{num(s.impressions)}</td><td className="num">{num(s.clicks)}</td>
                  <td className="num">{s.impressions ? pct(s.clicks / s.impressions) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="card">
          <h2 className="card-title">По странам</h2>
          <table className="table">
            <thead><tr><th>Страна</th><th className="num">Показы</th><th className="num">Клики</th></tr></thead>
            <tbody>
              {report.countries.slice(0, 15).map((c) => (
                <tr key={c.country}><td>{c.country === 'unknown' ? 'неизвестна' : c.country}</td><td className="num">{num(c.impressions)}</td><td className="num">{num(c.clicks)}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="card">
          <h2 className="card-title">Постбэки партнёров</h2>
          {report.postbacks.length === 0 ? <p className="small muted">Нет конверсий от партнёров за период.</p> : (
            <table className="table" data-testid="ads-postbacks">
              <thead><tr><th>Кампания · вариант</th><th>Событие</th><th className="num">Всего</th><th className="num">Польз.</th></tr></thead>
              <tbody>
                {report.postbacks.map((p) => (
                  <tr key={`${p.campaignId}:${p.variant}:${p.event}`}>
                    <td>#{p.campaignId} · {p.variant}</td><td>{p.event}</td><td className="num">{num(p.count)}</td><td className="num">{num(p.users)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </>
  );
}

/** Confirmed conversions from the partner's report: MB ids, up to 400 per upload, attributed to the last click. */
function Conversions({ campaign, onDone }: { campaign: number; onDone: () => void }) {
  const [text, setText] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const ids = [...new Set(text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean))];
    try {
      const r = await adsApi.conversions(campaign, ids);
      setMessage(`Из отчёта: ${r.reported}, найдено пользователей: ${r.matched}, новых конверсий: ${r.added}, с кликом: ${r.attributed}.`);
      setText('');
      onDone();
    } catch (err) {
      setMessage(adsError(err));
    }
  };
  return (
    <form className="card section" style={{ marginTop: 16 }} onSubmit={submit} aria-label="Конверсии из отчёта партнёра">
      <h2 className="card-title">Конверсии из отчёта партнёра</h2>
      <div className="field"><label htmlFor="cv-ids">ID MB из отчёта (до 400 за раз)</label>
        <textarea id="cv-ids" className="control" rows={3} value={text} onChange={(e) => setText(e.target.value)} /></div>
      {message && <div className="banner" role="status">{message}</div>}
      <div className="row" style={{ justifyContent: 'flex-end' }}><button type="submit" className="btn" disabled={!text.trim()}>Загрузить</button></div>
    </form>
  );
}
