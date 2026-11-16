import { useMemo, useState } from 'react';
import type { AiCall, DaySeries } from '../api';
import { num, usd, localTime } from '../format';
import { useLoad, usePrefs } from '../state';
import { DayBars } from '../charts';
import {
  Empty, ErrorBox, Loading, Metric, Metrics, NotCollected, Note, PageHead, Pager, PeriodSelect, SectionHead, SortHeader,
  Status, useSort,
} from '../ui';

type TokenDay = { day: string; model: string; calls: number; input: number; output: number; cacheRead: number; cacheWrite: number; costUsd: number | null };
type AiData =
  | { source: 'not_collected' }
  | { source: 'demo'; calls: AiCall[] }
  | { source: 'tokens'; days: TokenDay[]; totals: { calls: number; costUsd: number | null; perCallUsd: number | null } };

/** Real cost accounting: billed tokens per day and model, priced at list rates (counts only, no content). */
function AiTokenCosts({ data }: { data: Extract<AiData, { source: 'tokens' }> }) {
  const t = data.totals;
  const sum = (k: 'input' | 'output' | 'cacheRead') => data.days.reduce((s, d) => s + d[k], 0);
  return (
    <>
      <PageHead crumb="AI и расходы" title="AI и расходы" subtitle="Реальные токены AI-разборов по дням и стоимость по прайсу Anthropic" actions={<PeriodSelect />} />
      <Metrics>
        <Metric label="Вызовов AI" value={num(t.calls)} caption="за период" />
        <Metric label="Стоимость" value={t.costUsd === null ? '—' : usd(t.costUsd)} caption="по прайс-листу" />
        <Metric label="Один разбор" value={t.perCallUsd === null ? '—' : usd(t.perCallUsd)} caption="в среднем" />
        <Metric label="Токены" value={`${num(sum('input'))} / ${num(sum('output'))}`} caption={`вход / выход · из кэша ${num(sum('cacheRead'))}`} />
      </Metrics>
      {data.days.length === 0 ? <Empty title="Вызовов за период не было">Учёт токенов ведётся с 16.11.2026.</Empty> : (
        <div className="table-card">
          <table className="table">
            <thead><tr><th>День (UTC)</th><th>Модель</th><th className="num">Вызовов</th><th className="num">Вход</th><th className="num">Выход</th><th className="num">Стоимость</th><th className="num">За вызов</th></tr></thead>
            <tbody>{[...data.days].reverse().map((d) => (
              <tr key={`${d.day}-${d.model}`}>
                <td>{d.day}</td><td className="mono">{d.model}</td><td className="num">{num(d.calls)}</td>
                <td className="num">{num(d.input)}</td><td className="num">{num(d.output)}</td>
                <td className="num">{d.costUsd === null ? '—' : usd(d.costUsd)}</td>
                <td className="num">{d.costUsd === null || !d.calls ? '—' : usd(d.costUsd / d.calls)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}

const RESULT_LABEL: Record<string, string> = {
  delivered: 'Доставлен', not_delivered: 'Не доставлен', upstream_timeout: 'Таймаут', rate_limited: '429',
  invalid_answer: 'Ответ не прошёл проверку', refusal: 'Отказ модели', upstream_error: 'Ошибка провайдера',
};

function sortRows<T>(rows: T[], field: keyof T, desc: boolean): T[] {
  return [...rows].sort((a, b) => {
    const x = a[field] ?? -Infinity;
    const y = b[field] ?? -Infinity;
    const r = x < y ? -1 : x > y ? 1 : 0;
    return desc ? -r : r;
  });
}

export function AiCosts() {
  const { period, demo } = usePrefs();
  const { data, error, loading, reload } = useLoad<AiData>(`/api/admin/ai?days=${period}${demo ? '&demo=1' : ''}`);
  const [model, setModel] = useState('');
  const [result, setResult] = useState('');
  const [page, setPage] = useState(1);
  const { sort, onSort } = useSort('at');
  const calls = data && data.source === 'demo' ? data.calls : null;
  const filtered = useMemo(() => {
    if (!calls) return [];
    const rows = calls.filter((c) => (!model || c.model === model) &&
      (!result || (result === 'ok' ? c.result === 'delivered' : c.result !== 'delivered')));
    return sortRows(rows, sort.field as keyof AiCall, sort.desc);
  }, [calls, model, result, sort]);
  const known = filtered.filter((c) => c.costUsd !== null);
  const cost = known.reduce((s, c) => s + (c.costUsd ?? 0), 0);
  const unknown = filtered.length - known.length;
  const tokensIn = known.reduce((s, c) => s + (c.inputTokens ?? 0), 0);
  const tokensOut = known.reduce((s, c) => s + (c.outputTokens ?? 0), 0);
  const errors = filtered.filter((c) => !['delivered', 'not_delivered'].includes(c.result)).length;
  const latencies = filtered.map((c) => c.latencyMs).sort((a, b) => a - b);
  const p95 = latencies.length ? latencies[Math.min(latencies.length - 1, Math.floor(latencies.length * 0.95))] : null;
  const models = calls ? [...new Set(calls.map((c) => c.model))] : [];
  const size = 15;
  const shownPage = Math.min(page, Math.max(1, Math.ceil(filtered.length / size)));

  if (data && data.source === 'tokens') return <AiTokenCosts data={data} />;
  return (
    <>
      <PageHead crumb="AI и расходы" title="AI и расходы" subtitle="Себестоимость вызовов, токены и ошибки" />
      <div className="filters">
        <PeriodSelect />
        <select className="control" aria-label="Модель" value={model} onChange={(e) => { setModel(e.target.value); setPage(1); }} disabled={!calls}>
          <option value="">Модель: все</option>
          {models.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <select className="control" aria-label="Статус" value={result} onChange={(e) => { setResult(e.target.value); setPage(1); }} disabled={!calls}>
          <option value="">Статус: все</option><option value="ok">Доставлен</option><option value="fail">Ошибки и недоставленные</option>
        </select>
      </div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : (
        <>
          <Metrics>
            <Metric demo={Boolean(calls)} label="AI-вызовов" value={calls ? num(filtered.length) : '—'} caption={calls ? `p95 задержки: ${p95 ? (p95 / 1000).toFixed(1) : '—'} с` : 'журнал после D1'} />
            <Metric demo={Boolean(calls)} label="Токенов вход / выход" value={calls ? `${num(tokensIn / 1000)}k / ${num(tokensOut / 1000)}k` : '—'} caption="usage из ответа API" />
            <Metric demo={Boolean(calls)} label="Стоимость, оценка" value={calls ? usd(cost) : '—'}
              caption={calls ? (unknown ? `без ${unknown} вызовов с неизвестной стоимостью` : 'по тарифу модели') : 'по тарифу модели'} />
            <Metric demo={Boolean(calls)} label="Ошибки и 429" value={calls ? num(errors) : '—'} caption={calls ? 'по кодам — в журнале' : 'история после D1'} />
          </Metrics>
          <section className="section">
            <SectionHead title="Журнал AI-вызовов" action={calls ? <span className="chip demo sm">ДЕМО</span> : undefined} />
            {!calls ? <NotCollected what="История AI-вызовов" /> : filtered.length === 0 ? <Empty title="Нет вызовов под фильтр" /> : (
              <>
                <table className="table">
                  <thead>
                    <tr>
                      <SortHeader label="Дата и время" field="at" sort={sort} onSort={onSort} />
                      <th>Операция</th><th>Модель</th>
                      <SortHeader label="Токены" field="inputTokens" sort={sort} onSort={onSort} className="num" />
                      <SortHeader label="Стоимость" field="costUsd" sort={sort} onSort={onSort} className="num" />
                      <SortHeader label="Задержка" field="latencyMs" sort={sort} onSort={onSort} className="num" />
                      <th>Результат</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.slice((shownPage - 1) * size, shownPage * size).map((c) => (
                      <tr key={c.id}>
                        <td>{localTime(c.at)}</td><td>{c.operation}</td><td className="mono">{c.model}</td>
                        <td className="num">{c.inputTokens === null ? '—' : `${num(c.inputTokens)} / ${num(c.outputTokens)}`}</td>
                        <td className="num">{c.costUsd === null ? <span title="Стоимость неизвестна: нет usage">н/д</span> : usd(c.costUsd)}</td>
                        <td className="num">{(c.latencyMs / 1000).toFixed(1)} с</td>
                        <td><Status value={c.result === 'delivered' ? 'delivered' : c.result === 'not_delivered' ? 'not_delivered' : 'error'} label={RESULT_LABEL[c.result] ?? c.result} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Pager page={shownPage} size={size} total={filtered.length} onPage={setPage} />
              </>
            )}
          </section>
          <Note title="Откуда берётся стоимость">Стоимость — расчётная: фактический usage из ответа × версия таблицы цен в коде.
            Это не счёт провайдера. Вызовы без usage (таймаут, сеть) не суммируются и показываются отдельно.</Note>
        </>
      )}
    </>
  );
}

type ApiData = { source: 'not_collected' } | {
  source: 'demo'; series: DaySeries[];
  providers: { provider: string; dailyLimit: number | null; perMinuteLimit: number | null; usedLastMinute: number | null;
    attempts: number; rateLimited: number; errors: number; lastOkAt: number | null; status: string }[];
};

export function SportsApi() {
  const { period, demo } = usePrefs();
  const { data, error, loading, reload } = useLoad<ApiData>(`/api/admin/sports-api?days=${period}${demo ? '&demo=1' : ''}`);
  const d = data && data.source === 'demo' ? data : null;
  const total = d?.series.reduce((s, x) => s + x.apiAttempts, 0);
  const r429 = d?.series.reduce((s, x) => s + x.api429, 0);
  const errs = d?.series.reduce((s, x) => s + x.apiErrors, 0);
  const lastOk = d?.providers[0]?.lastOkAt ?? null;
  return (
    <>
      <PageHead crumb="Спортивные API" title="Спортивные API" subtitle="Квоты, запросы, ответы 429 и источники" />
      <div className="filters"><PeriodSelect /></div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : (
        <>
          <Metrics>
            <Metric demo={Boolean(d)} label="Запросов к API" value={d ? num(total) : '—'} caption={d ? 'все попытки, включая повторы' : 'после D1'} />
            <Metric demo={Boolean(d)} label="Ответов 429" value={d ? num(r429) : '—'} caption={d ? 'от провайдера' : 'после D1'} />
            <Metric demo={Boolean(d)} label="Ошибки" value={d ? num(errs) : '—'} caption={d ? 'сеть и 5xx' : 'после D1'} />
            <Metric demo={Boolean(d)} label="Последний успех" value={d ? localTime(lastOk).split(' · ')[1] ?? '—' : '—'} caption={d ? localTime(lastOk) : 'из оперативного состояния'} />
          </Metrics>
          <section className="section">
            <SectionHead title="Источники спортивных данных" action={d ? <span className="chip demo sm">ДЕМО</span> : undefined} />
            {!d ? <NotCollected what="Состояние провайдеров" /> : (
              <table className="table">
                <thead><tr><th>Провайдер</th><th className="num">Лимит в минуту</th><th className="num">Использовано</th><th className="num">429</th><th>Последний ответ</th><th>Состояние</th></tr></thead>
                <tbody>
                  {d.providers.map((p) => (
                    <tr key={p.provider}>
                      <td className="strong">{p.provider}</td>
                      <td className="num">{p.perMinuteLimit ?? '—'}</td>
                      <td className="num">{p.usedLastMinute ?? '—'}</td>
                      <td className="num">{p.status === 'disabled' ? '—' : p.rateLimited}</td>
                      <td>{localTime(p.lastOkAt)}</td>
                      <td>{p.status === 'disabled' ? <span className="chip">Отключено до production</span> : <Status value={p.status} />}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          {d && (
            <section className="section">
              <SectionHead title="Запросы по дням" action={<span className="chip demo sm">ДЕМО</span>} />
              <div style={{ paddingTop: 18 }}>
                <DayBars title="Запросы" points={d.series.map((x) => ({ day: x.day, value: x.apiAttempts }))} />
              </div>
            </section>
          )}
          <Note title="Важное ограничение">До D1 счётчики запросов живут только в памяти Mini App и обнуляются при перезапуске процесса.</Note>
        </>
      )}
    </>
  );
}

type SystemData = {
  health: string; build: string | null;
  databases: Record<string, { ok: boolean; bytes: number | null; walBytes: number | null }>;
} & ({ source: 'not_collected' } | {
  source: 'demo';
  events: { at: number; component: string; level: string; code: string; count: number; requestId: string }[];
  tasks: { name: string; lastOkAt: number; lastError: string | null; state: string }[];
});

export function System() {
  const { demo } = usePrefs();
  const { data, error, loading, reload } = useLoad<SystemData>(`/api/admin/system${demo ? '?demo=1' : ''}`);
  const [level, setLevel] = useState('');
  const [component, setComponent] = useState('');
  const events = data && data.source === 'demo' ? data.events : null;
  const rows = (events ?? []).filter((e) => (!level || e.level === level) && (!component || e.component === component));
  const kb = (b: number | null) => (b === null ? '—' : `${num(b / 1024)} КБ`);
  return (
    <>
      <PageHead crumb="Система и логи" title="Система и логи" subtitle="Состояние процессов, фоновые задачи и ошибки" />
      <div className="filters">
        <select className="control" aria-label="Уровень" value={level} onChange={(e) => setLevel(e.target.value)} disabled={!events}>
          <option value="">Уровень: все</option><option value="error">Ошибки</option><option value="warning">Предупреждения</option>
        </select>
        <select className="control" aria-label="Компонент" value={component} onChange={(e) => setComponent(e.target.value)} disabled={!events}>
          <option value="">Компонент: все</option>
          {['ai', 'sports', 'settlement', 'auth', 'quick'].map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <button type="button" className="btn" onClick={reload}>Обновить</button>
      </div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : data && (
        <>
          <Metrics>
            <Metric label="Health" value={<Status value={data.health} />} caption="проверка miniapp.db" />
            <Metric label="Версия сборки" value="—" caption="после D7" />
            <Metric demo={Boolean(events)} label="Фоновые задания" value={data.source === 'demo' ? `${data.tasks.filter((t) => t.state === 'ok').length} / ${data.tasks.length}` : '—'} caption={events ? 'в норме / всего' : 'после D1'} />
            <Metric demo={Boolean(events)} label="Ошибки" value={events ? num(events.filter((e) => e.level === 'error').reduce((s, e) => s + e.count, 0)) : '—'} caption={events ? 'за 3 дня' : 'история после D1'} />
          </Metrics>
          <div className="cols">
            <section className="section">
              <SectionHead title="События и ошибки" action={events ? <span className="chip demo sm">ДЕМО</span> : undefined} />
              {!events ? <NotCollected what="Журнал событий" /> : rows.length === 0 ? <Empty title="Нет событий под фильтр" /> : (
                <table className="table">
                  <thead><tr><th>Время</th><th>Компонент</th><th>Событие</th><th className="num">Кол-во</th><th>Request ID</th></tr></thead>
                  <tbody>
                    {rows.slice(0, 20).map((e, i) => (
                      <tr key={i}>
                        <td>{localTime(e.at)}</td><td>{e.component}</td>
                        <td><Status value={e.level} label={e.code} /></td>
                        <td className="num">{e.count}</td><td className="mono">{e.requestId}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
            <aside>
              <SectionHead title="Базы данных" />
              <ul className="kv">
                {Object.entries(data.databases).map(([name, db]) => (
                  <li key={name}><span className="k">{name}</span><span className="v" style={{ fontSize: 13 }}>{db.ok ? `${kb(db.bytes)} · WAL ${kb(db.walBytes)}` : <Status value="unavailable" />}</span></li>
                ))}
              </ul>
              {data.source === 'demo' && (
                <>
                  <div className="small muted" style={{ margin: '22px 0 4px' }}>Фоновые задачи <span className="chip demo sm">ДЕМО</span></div>
                  <ul className="kv">
                    {data.tasks.map((t) => (
                      <li key={t.name}><span className="k">{t.name}<span className="sub small">успех: {localTime(t.lastOkAt)}</span></span><Status value={t.state} /></li>
                    ))}
                  </ul>
                </>
              )}
              <Note title="Статус системы">Здоровье приложения и история ошибок — разные источники; не объединяем их в фиктивный статус.</Note>
            </aside>
          </div>
        </>
      )}
    </>
  );
}
