import { Link, useNavigate } from 'react-router-dom';
import type { Overview as OverviewData } from '../api';
import { num, usd, utc } from '../format';
import { useLoad, usePrefs } from '../state';
import { Bars, Empty, ErrorBox, Loading, Metric, Metrics, Note, PageHead, PeriodSelect, SectionHead, Status } from '../ui';

export function Overview() {
  const { period, demo } = usePrefs();
  const navigate = useNavigate();
  const { data, error, loading, reload } = useLoad<OverviewData>(`/api/admin/overview?days=${period}${demo ? '&demo=1' : ''}`);
  const t = data?.telemetry;
  const version = data ? Object.values(data.quality.versions)[0] : undefined;
  const series = t && t.source === 'demo' ? t.series : null;
  const last = series?.[series.length - 1];

  return (
    <>
      <PageHead crumb="Обзор" title="Обзор продукта" subtitle="Пользователи, прогнозы и состояние сервиса" />
      <div className="filters"><span className="spacer" /><PeriodSelect /></div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : data && (
        <>
          <Metrics>
            <Metric label="Зарегистрировано пользователей" value={<Link className="metric-link" to="/users">{num(data.users.total)}</Link>}
              caption={`+${num(data.users.new)} новых за период · из users`} />
            {t && t.source === 'demo' && last ? (
              <Metric demo label="DAU / WAU / MAU" value={`${last.dau} / ${t.wau} / ${t.mau}`} caption="демо-телеметрия" />
            ) : (
              <Metric label="DAU / WAU / MAU" value="—" caption="после запуска телеметрии (D1)" />
            )}
            <Metric label="Прогнозы в истории" value={<Link className="metric-link" to="/forecasts">{num(data.forecasts.total)}</Link>}
              caption={`${num(data.forecasts.createdInPeriod)} за период · из истории модели`} />
            <Metric label="Сверено" value={num(data.forecasts.settled)}
              caption={`до порога качества: ${num(Math.max(0, data.quality.minimum - data.quality.settled))}`} />
          </Metrics>

          <div className="cols">
            <div>
              <section className="section">
                <SectionHead title="Ближайшие прогнозы" action={<Link to="/forecasts" className="small">Все прогнозы →</Link>} />
                {data.upcoming.length === 0 ? (
                  <Empty title="Нет ожидающих матчей">Новые прогнозы появятся, когда пользователи откроют AI-разборы.</Empty>
                ) : (
                  <table className="table">
                    <thead><tr><th>Матч</th><th>Дата UTC</th><th>Статус</th></tr></thead>
                    <tbody>
                      {data.upcoming.map((f) => (
                        <tr key={f.key} className="clickable" onClick={() => navigate(`/forecasts?open=${encodeURIComponent(f.key)}`)}>
                          <td className="strong">{f.match ?? f.eventId}<span className="sub">{f.tournament}</span></td>
                          <td>{utc(f.kickoffAt)}</td>
                          <td><Status value={f.status} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
              <section className="section">
                <SectionHead title="Активность и расходы" action={series ? <span className="chip demo sm">ДЕМО</span> : undefined} />
                {series ? (
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 28, paddingTop: 18 }}>
                    <div>
                      <div className="small muted" style={{ marginBottom: 6 }}>Активные пользователи по дням</div>
                      <Bars title="DAU" values={series.map((d) => d.dau)} labels={[series[0]!.day, series[series.length - 1]!.day]} format={(v) => `${v} польз.`} />
                    </div>
                    <div>
                      <div className="small muted" style={{ marginBottom: 6 }}>Расходы AI по дням, оценка</div>
                      <Bars title="AI cost" values={series.map((d) => d.aiCostUsd)} labels={[series[0]!.day, series[series.length - 1]!.day]} format={usd} />
                    </div>
                  </div>
                ) : (
                  <Empty title="Графики появятся после D1">Активность по дням, использование функций и расходы AI сейчас не сохраняются.</Empty>
                )}
              </section>
            </div>
            <aside>
              <SectionHead title="Качество модели" />
              <ul className="kv">
                <li><span className="k">Матчей сверено</span><span className="v">{num(data.quality.settled)}</span></li>
                <li><span className="k">Минимум для оценки</span><span className="v">{num(data.quality.minimum)}</span></li>
                <li><span className="k">LogLoss / Brier</span><span className="v">{version ? `${version.logLoss1x2.toFixed(3)} / ${version.brier1x2.toFixed(3)}` : '—'}</span></li>
                <li><span className="k">Состояние сервиса</span><Link to="/system" className="small">Система и логи →</Link></li>
              </ul>
              {version && data.quality.settled < data.quality.minimum && (
                <Note title="Предварительные значения">Метрики посчитаны на {data.quality.settled} матчах — до порога {data.quality.minimum} они не являются оценкой качества.</Note>
              )}
              <Note title="О данных">Просмотры, активность и затраты начнут учитываться после D1. Пользователи и прогнозы — из файлов Mini App{' '}
                (в пилоте — демо-набор).</Note>
            </aside>
          </div>
        </>
      )}
    </>
  );
}
