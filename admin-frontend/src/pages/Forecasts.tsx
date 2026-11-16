import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { ForecastDetail, ForecastRow, Page, Quality } from '../api';
import { num, pct, PICKS, localTime } from '../format';
import { useLoad } from '../state';
import { Empty, ErrorBox, Loading, Metric, Metrics, Note, PageHead, Pager, SectionHead, SortHeader, Status, useSort } from '../ui';

type ListPage = Page<ForecastRow> & { tournaments: { id: string; name: string }[]; available: boolean };
interface Summary { counts: { pending: number; settled: number; void: number; total: number }; quality: Quality; available: boolean }

export function Forecasts() {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get('q') ?? '');
  const status = params.get('status') ?? '';
  const tournament = params.get('tournament') ?? '';
  const days = params.get('days') ?? 'all';
  const page = Number(params.get('page') ?? '1') || 1;
  const open = params.get('open');
  const { sort, onSort } = useSort('kickoff');

  // Patch the CURRENT address: a delayed call (search debounce) must not restore a stale copy of the
  // filters (react-router's functional form still hands over the render-time params).
  const update = (patch: Record<string, string>) => {
    const next = new URLSearchParams(window.location.search);
    for (const [k, v] of Object.entries(patch)) { if (v) next.set(k, v); else next.delete(k); }
    setParams(next, { replace: true });
  };
  useEffect(() => {
    const t = setTimeout(() => {
      if ((new URLSearchParams(window.location.search).get('q') ?? '') !== query) update({ q: query, page: '' });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const summary = useLoad<Summary>('/api/admin/forecasts/summary');
  const qs = new URLSearchParams({ q: params.get('q') ?? '', status, tournament, days, page: String(page), size: '12',
    sort: sort.field, order: sort.desc ? 'desc' : 'asc' });
  const list = useLoad<ListPage>(`/api/admin/forecasts?${qs}`);
  const s = summary.data;
  const v = s ? Object.entries(s.quality.versions).find(([name]) => name === s.quality.currentVersion) : undefined;
  const filtered = Boolean(params.get('q') || status || tournament || days !== 'all');

  return (
    <>
      <PageHead crumb="Прогнозы" title="Аналитика прогнозов" subtitle="История модели, сверка и показатели качества" />
      <div className="filters">
        <select className="control" aria-label="Период" value={days} onChange={(e) => update({ days: e.target.value === 'all' ? '' : e.target.value, page: '' })}>
          <option value="all">За всё время</option><option value="7">Последние 7 дней</option>
          <option value="30">Последние 30 дней</option><option value="90">Последние 90 дней</option>
        </select>
        <select className="control" aria-label="Статус" value={status} onChange={(e) => update({ status: e.target.value, page: '' })}>
          <option value="">Статус: все</option><option value="pending">Ожидает</option>
          <option value="settled">Сверено</option><option value="void">Аннулирован</option>
        </select>
        <select className="control" aria-label="Турнир" value={tournament} onChange={(e) => update({ tournament: e.target.value, page: '' })}>
          <option value="">Турнир: все</option>
          {list.data?.tournaments.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        <input className="control" type="search" placeholder="Поиск матча" aria-label="Поиск матча" value={query} onChange={(e) => setQuery(e.target.value)} />
        {filtered && <button type="button" className="btn link" onClick={() => { setQuery(''); setParams({}, { replace: true }); }}>Сбросить</button>}
      </div>

      {summary.error ? <ErrorBox error={summary.error} onRetry={summary.reload} /> : s && (
        <Metrics>
          <Metric label="Прогнозов модели" value={num(s.counts.total)} caption="строк в истории" />
          <Metric label="Ожидают результата" value={num(s.counts.pending)} caption="включая перенесённые" />
          <Metric label="Сверено" value={num(s.counts.settled)} caption={`аннулировано: ${num(s.counts.void)}`} />
          <Metric label="Порог качества" value={num(s.quality.minimum)} caption={`в проверке ${s.quality.currentVersion}: ${num(s.quality.currentGateForecasts)}`} />
        </Metrics>
      )}

      <div className="cols">
        <section className="section">
          <SectionHead title="История прогнозов" action={list.data && <span className="small muted">Найдено: {num(list.data.total)}</span>} />
          {list.error ? <ErrorBox error={list.error} onRetry={list.reload} /> : list.loading && !list.data ? <Loading rows={8} /> : list.data && (
            !list.data.available ? <Empty title="История прогнозов пока пуста">Файл истории появится после первого AI-разбора в Mini App.</Empty>
              : list.data.items.length === 0 ? <Empty title="Ничего не найдено">Измените фильтры.</Empty> : (
                <>
                  <table className="table" aria-busy={list.loading} style={{ opacity: list.loading ? 0.45 : 1, pointerEvents: list.loading ? 'none' : undefined }} inert={list.loading}>
                    <thead>
                      <tr>
                        <th>Матч</th>
                        <SortHeader label="Дата" field="kickoff" sort={sort} onSort={onSort} />
                        <th>Рекомендация</th>
                        <th>Итог</th>
                        <SortHeader label="Сверка" field="status" sort={sort} onSort={onSort} />
                      </tr>
                    </thead>
                    <tbody>
                      {list.data.items.map((f) => (
                        <tr key={f.key} className="clickable" onClick={() => update({ open: f.key })}>
                          <td className="strong">
                            <button type="button" className="btn link" style={{ color: 'var(--ink)', fontWeight: 500 }}
                              onClick={(e) => { e.stopPropagation(); update({ open: f.key }); }}>{f.match ?? f.eventId}</button>
                            <span className="sub">{f.tournament}</span>
                          </td>
                          <td>{localTime(f.kickoffAt)}</td>
                          <td>{PICKS[f.pick] ?? f.pick}{f.pickProbability !== null && <span className="sub">{pct(f.pickProbability, 0)}</span>}</td>
                          <td className="mono">{f.score ? `${f.score[0]}:${f.score[1]}` : '—'}</td>
                          <td><Status value={f.status} />{f.revision > 1 && <span className="sub">ревизия {f.revision}</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <Pager page={list.data.page} size={list.data.size} total={list.data.total} onPage={(p) => update({ page: String(p) })} />
                </>
              ))}
        </section>
        <aside>
          <SectionHead title="Качество модели" />
          {!v ? <Empty title="Сверенных матчей пока нет">Метрики появятся после первых сверенных матчей.</Empty> : (
            <>
              <ul className="kv">
                <li><span className="k">Версия модели</span><span className="v mono">{v[0]}</span></li>
                <li><span className="k">LogLoss 1X2</span><span className="v">{v[1].logLoss1x2.toFixed(4)}</span></li>
                <li><span className="k">Brier 1X2</span><span className="v">{v[1].brier1x2.toFixed(4)}</span></li>
                <li><span className="k">Попадание рекомендаций</span><span className="v">{pct(v[1].picks.hitRate)} <span className="small muted">из {v[1].picks.count}</span></span></li>
                <li><span className="k">Проспективная проверка</span><span className="v">{v[1].prospectiveGate.forecasts} / {v[1].prospectiveGate.minimum}</span></li>
              </ul>
              <div className="small muted" style={{ margin: '22px 0 8px' }}>LogLoss по турнирам</div>
              <ul className="kv">
                {Object.entries(v[1].byTournament).map(([name, t]) => (
                  <li key={name}><span className="k">{name} <span className="small">({t.forecasts})</span></span><span className="v" style={{ fontSize: 14 }}>{t.logLoss1x2.toFixed(3)}</span></li>
                ))}
              </ul>
              {s && s.quality.currentGateForecasts < s.quality.minimum && (
                <Note title="Предварительные значения">До {s.quality.minimum} сверенных матчей метрики не являются оценкой качества.</Note>
              )}
            </>
          )}
          <Note title="Определение метрики">Здесь прогноз модели — строка истории (одна на набор входных данных), а не просмотр
            пользователем. Просмотры учитываются отдельно после D1.</Note>
        </aside>
      </div>
      {open && <ForecastDrawer forecastKey={open} onClose={() => update({ open: '' })} />}
    </>
  );
}

function ForecastDrawer({ forecastKey, onClose }: { forecastKey: string; onClose: () => void }) {
  const { data, error, loading, reload } = useLoad<ForecastDetail>(`/api/admin/forecasts/${encodeURIComponent(forecastKey)}`);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <aside className="drawer" role="dialog" aria-label="Карточка прогноза">
      <button type="button" className="btn" onClick={onClose}>Закрыть ✕</button>
      {error ? <div style={{ marginTop: 20 }}><ErrorBox error={error} onRetry={reload} /></div> : loading || !data ? <Loading rows={8} /> : (
        <>
          <h2>{data.match ?? data.eventId}</h2>
          <div className="sub">{data.tournament} · {localTime(data.kickoffAt, true)}</div>
          <div style={{ marginTop: 14 }}><Status value={data.status} />{data.score && <span className="chip" style={{ marginLeft: 8 }}>Итог {data.score[0]}:{data.score[1]}</span>}</div>
          <div className="probs">
            {(['1', 'X', '2'] as const).map((k) => (
              <div className="prob" key={k}><div className="k">{PICKS[k]}</div><div className="v">{data.display[k] ?? '—'}%</div></div>
            ))}
          </div>
          <ul className="kv">
            <li><span className="k">Рекомендация</span><span className="v" style={{ fontSize: 14 }}>{PICKS[data.pick] ?? data.pick}{data.pickProbability !== null ? ` · ${pct(data.pickProbability, 0)}` : ''}</span></li>
            {data.noBetReason && <li><span className="k">Причина «без ставки»</span><span className="v mono">{data.noBetReason}</span></li>}
            <li><span className="k">Ожидаемые голы</span><span className="v" style={{ fontSize: 14 }}>{data.expectedGoals[0].toFixed(2)} : {data.expectedGoals[1].toFixed(2)}</span></li>
            <li><span className="k">Выборка матчей</span><span className="v" style={{ fontSize: 14 }}>{data.sample}</span></li>
            <li><span className="k">Прогноз создан</span><span className="v" style={{ fontSize: 14 }}>{localTime(data.createdAt, true)}</span></li>
            <li><span className="k">Модель</span><span className="v mono">{data.modelVersion}</span></li>
          </ul>
          <div className="small muted" style={{ margin: '24px 0 4px' }}>Журнал ревизий итога</div>
          {data.settlements.length === 0 ? <p className="small muted">Результата ещё нет.</p> : (
            <table className="table">
              <thead><tr><th>Ревизия</th><th>Итог</th><th>Статус</th><th>Записано</th></tr></thead>
              <tbody>
                {data.settlements.map((r) => (
                  <tr key={r.revision}><td>{r.revision}</td><td className="mono">{r.score ? `${r.score[0]}:${r.score[1]}` : '—'}</td>
                    <td><Status value={r.status} /></td><td>{localTime(r.recordedAt)}</td></tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="small muted mono" style={{ marginTop: 18, wordBreak: 'break-all' }}>{data.key}</p>
        </>
      )}
    </aside>
  );
}
