import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { Analytics as AnalyticsData, Broadcast, Overview } from '../api';
import { num, utc } from '../format';
import { useLoad, usePrefs } from '../state';
import { BarList, Bars, Card, ErrorBox, Loading, Metric, Metrics, NotCollected, PageHead, PeriodSelect, Tag, tagTone } from '../ui';

const TABS = ['Обзор', 'Аудитория и гео', 'Время активности', 'Контент и лиги', 'Удержание', 'Источники'] as const;
type TabName = typeof TABS[number];
const DAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const LANG_NAMES: Record<string, string> = { ru: 'Русский', az: 'Азербайджанский', tr: 'Турецкий', uz: 'Узбекский', en: 'Английский', 'pt-BR': 'Португальский', es: 'Испанский' };

function Heatmap({ data, label }: { data: number[][]; label: string }) {
  const max = Math.max(1, ...data.flat());
  return (
    <div className="heat" role="img" aria-label={label}>
      <span />
      {Array.from({ length: 24 }, (_, h) => <span key={h} className="hr">{h % 3 === 0 ? String(h).padStart(2, '0') : ''}</span>)}
      {data.map((row, d) => [
        <span key={`l${d}`} className="lbl">{DAYS[d]}</span>,
        ...row.map((v, h) => <span key={`${d}-${h}`} className="cell" title={`${DAYS[d]} ${h}:00 — ${v}`} style={{ opacity: Math.max(0.06, v / max) }} />),
      ])}
    </div>
  );
}

function bestSlot(data: number[][]): string {
  let best = { d: 0, h: 0, v: -1 };
  data.forEach((row, d) => row.forEach((v, h) => { if (v > best.v) best = { d, h, v }; }));
  return best.v > 0 ? `${DAYS[best.d]}, ${String(best.h).padStart(2, '0')}:00–${String((best.h + 1) % 24).padStart(2, '0')}:00` : '—';
}

function DemoOnly({ what }: { what: string }) {
  return <Card><NotCollected what={what} /></Card>;
}

export function Analytics() {
  const { period, demo } = usePrefs();
  const navigate = useNavigate();
  const [tab, setTab] = useState<TabName>('Обзор');
  const qs = `days=${period}${demo ? '&demo=1' : ''}`;
  const a = useLoad<AnalyticsData>(`/api/admin/analytics?${qs}`);
  const ov = useLoad<Overview>(`/api/admin/overview?${qs}`);
  const bc = useLoad<{ items: Broadcast[] }>('/api/admin/broadcasts');
  const d = a.data?.demo ?? null;
  const real = a.data?.real;
  // Measured from the Mini App files the panel reads — in the pilot that is the generated demo set.
  const src = a.data?.dataset === 'demo' ? 'демо-набор пилота' : 'реальные данные';

  return (
    <>
      <PageHead title="Аналитика" subtitle="Рост аудитории, вовлечённость, география, время активности и результат рассылок"
        actions={<><PeriodSelect /><button type="button" className="btn primary" onClick={() => navigate('/broadcasts/new')}>Новая рассылка</button></>} />
      <div className="tabs" role="tablist" aria-label="Разделы аналитики" style={{ marginBottom: 20 }}>
        {TABS.map((t) => (
          <a key={t} href="#" role="tab" aria-selected={tab === t} className={tab === t ? 'active' : ''}
            onClick={(e) => { e.preventDefault(); setTab(t); }}>{t}</a>
        ))}
      </div>
      {a.error ? <ErrorBox error={a.error} onRetry={a.reload} /> : !real ? <Loading rows={6} /> : (
        <>
          {tab === 'Обзор' && (
            <>
              <Metrics>
                <Metric label="Пользователи всего" value={<Link className="metric-link" to="/users">{num(real.totalUsers)}</Link>} caption={`+${num(real.newUsers)} за период · ${src}`} />
                <Metric demo={Boolean(d)} label="Активные (DAU)" value={d ? num(d.activeByDay.at(-1)?.dau) : '—'} caption={d ? 'демо-телеметрия' : 'после D1'} />
                <Metric label="Прогнозы модели" value={<Link className="metric-link" to="/forecasts">{num(ov.data?.forecasts.total)}</Link>} caption={`сверено: ${num(ov.data?.forecasts.settled)}`} />
                <Metric demo={Boolean(d)} label="Средняя сессия" value={d ? `${Math.floor(d.summary.avgSessionS / 60)} мин ${d.summary.avgSessionS % 60} с` : '—'} caption={d ? 'демо-телеметрия' : 'после D1'} />
              </Metrics>
              <div className="grid g2">
                <Card title="Новые пользователи по дням" sub={`Регистрации в Mini App · ${src}`} action={<Tag tone="accent">{num(real.newUsers)} за период</Tag>}>
                  <Bars title="Новые" values={real.newByDay.map((x) => x.new)} labels={[real.newByDay[0]?.day ?? '', real.newByDay.at(-1)?.day ?? '']} format={(v) => `${v} новых`} />
                </Card>
                <Card title="Воронка активации" action={d ? <Tag tone="violet">ДЕМО</Tag> : undefined}>
                  {d ? d.funnel.map(([label, value], i) => (
                    <div className="bar-row funnel-row" key={label}>
                      <div className="bar-head"><span>{label}</span><span className="v">{num(value)} · {Math.round((value / d.funnel[0]![1]) * 100)}%</span></div>
                      <div className="bar-track"><div className="bar-fill" style={{ width: `${(value / d.funnel[0]![1]) * 100}%`, opacity: 1 - i * 0.12 }} /></div>
                    </div>
                  )) : <p className="muted small">Воронка «бот → Mini App → первый разбор → возврат» появится после сбора событий (D1).</p>}
                </Card>
              </div>
              <div className="grid g2">
                <Card title="Рассылки" action={<Link to="/broadcasts" className="small">Все рассылки →</Link>}>
                  {bc.data && bc.data.items.length > 0 ? (
                    <table className="table">
                      <thead><tr><th>Кампания</th><th>Статус</th><th>Когда</th></tr></thead>
                      <tbody>{bc.data.items.slice(0, 4).map((b) => (
                        <tr key={b.id}><td className="strong">{b.name}</td><td><Tag tone={b.status === 'scheduled' ? 'accent' : b.status === 'draft' ? 'neutral' : b.status === 'sent' ? 'success' : 'warning'}>{{ draft: 'Черновик', scheduled: 'Запланирована', sent: 'Отправлена', cancelled: 'Отменена' }[b.status]}</Tag></td><td>{utc(b.scheduledAt ?? b.updatedAt)}</td></tr>
                      ))}</tbody>
                    </table>
                  ) : <p className="muted small">Рассылок пока нет.</p>}
                </Card>
                <Card title="Теги аудитории" sub="Сколько пользователей с каждым тегом">
                  {real.tags.length === 0 ? <p className="muted small">Тегов нет или нет доступа к ним.</p> : (
                    <div className="bar-list">{real.tags.slice(0, 6).map((t) => (
                      <div key={t.tag} className="row" style={{ justifyContent: 'space-between' }}>
                        <Link to={`/users?tag=${encodeURIComponent(t.tag)}`}><Tag tone={tagTone(t.tag)}>{t.tag}</Tag></Link><b>{num(t.users)}</b>
                      </div>
                    ))}</div>
                  )}
                </Card>
              </div>
            </>
          )}
          {tab === 'Аудитория и гео' && (
            <>
              {d && (
                <Metrics>
                  <Metric demo label="Стран" value={num(d.summary.countries)} caption="по стране входа" />
                  <Metric demo label="Пик активности" value={d.summary.peak} caption="Баку" />
                  <Metric demo label="Средняя сессия" value={`${Math.floor(d.summary.avgSessionS / 60)} мин`} caption="демо-телеметрия" />
                  <Metric demo label="Мобильные" value={`${Math.round(d.summary.mobileShare * 100)}%`} caption="iOS + Android" />
                </Metrics>
              )}
              <div className="grid g3">
                {d ? (
                  <Card title="География" sub="Страна из Cloudflare при входе, IP не хранится" action={<Tag tone="violet">ДЕМО</Tag>}>
                    <BarList rows={d.geo.map((g) => ({ label: g.name, value: g.count, extra: g.code !== '—' ? <Tag>{g.code}</Tag> : undefined }))} />
                  </Card>
                ) : <DemoOnly what="География" />}
                <Card title="Языки интерфейса" sub={`${src} · выбор пользователя или язык Telegram`}>
                  <BarList tone="violet" rows={real.languages.map((l) => ({ label: LANG_NAMES[l.lang] ?? l.lang, value: l.count }))} />
                </Card>
                {d ? (
                  <Card title="Платформы" sub="Клиент Telegram при запуске" action={<Tag tone="violet">ДЕМО</Tag>}>
                    <BarList tone="success" rows={d.platforms.map((p) => ({ label: p.name, value: p.count }))} />
                  </Card>
                ) : <DemoOnly what="Платформы" />}
              </div>
            </>
          )}
          {tab === 'Время активности' && (
            <>
              {d ? (
                <Card title="Когда пользователи активны" sub="Открытия Mini App по дням недели и часам · Баку (UTC+4)" action={<Tag tone="violet">ДЕМО</Tag>}>
                  <Heatmap data={d.activityHeat} label="Тепловая карта активности" />
                  <div className="banner info" style={{ marginTop: 14 }}>Лучшее время для рассылки: <strong>{bestSlot(d.activityHeat)}</strong></div>
                </Card>
              ) : <DemoOnly what="Активность по часам" />}
              <Card title="Когда регистрируются" sub={`${src} · регистрации за период по дням недели и часам, Баку (UTC+4)`}>
                <Heatmap data={real.registrationHeat} label="Тепловая карта регистраций" />
                <p className="small muted" style={{ marginTop: 10 }}>Пик регистраций: {bestSlot(real.registrationHeat)}</p>
              </Card>
            </>
          )}
          {tab === 'Контент и лиги' && (
            <div className="grid half">
              <Card title="Популярные лиги" sub={`${src} · прогнозы модели по турнирам`}>
                {real.leagues.length ? <BarList rows={real.leagues.map((l) => ({ label: l.name, value: l.count }))} /> : <p className="muted small">Прогнозов пока нет.</p>}
              </Card>
              {d ? (
                <Card title="Использование функций" sub="Доля активных пользователей" action={<Tag tone="violet">ДЕМО</Tag>}>
                  <BarList tone="success" rows={d.features.map((f) => ({ label: f.name, value: Math.round(f.share * 100) }))} />
                </Card>
              ) : <DemoOnly what="Использование функций" />}
            </div>
          )}
          {tab === 'Удержание' && (d ? (
            <Card title="Удержание по когортам" sub="Доля пользователей недели регистрации, вернувшихся позже" action={<Tag tone="violet">ДЕМО</Tag>}>
              <div className="cohort">
                <span className="small muted">Когорта</span><span className="small muted">Людей</span>
                {[1, 2, 3, 4, 5].map((w) => <span key={w} className="small muted">Нед {w}</span>)}
                {d.cohorts.map((c) => [
                  <b key={`${c.week}w`}>{c.week}</b>, <span key={`${c.week}n`} className="muted">{num(c.size)}</span>,
                  ...[0, 1, 2, 3, 4].map((i) => {
                    const v = c.retention[i];
                    const k = v === undefined ? 0 : Math.max(0.15, Math.min(1, (v - 15) / 40));
                    return <span key={`${c.week}${i}`} className="c" style={{ background: v === undefined ? 'var(--note)' : `rgba(37,99,235,${k})`, color: k > 0.5 ? '#fff' : 'var(--accent)' }}>{v === undefined ? '' : `${v}%`}</span>;
                  }),
                ])}
              </div>
            </Card>
          ) : <DemoOnly what="Удержание по когортам" />)}
          {tab === 'Источники' && (d ? (
            <Card title="Источники новых пользователей" sub="Откуда пришли за период" action={<Tag tone="violet">ДЕМО</Tag>}>
              <BarList tone="warning" rows={d.sources.map((s) => ({ label: s.name, value: s.count }))} />
            </Card>
          ) : <DemoOnly what="Источники пользователей" />)}
          <div className="banner info" style={{ marginTop: 16 }}>Из файлов Mini App ({src}): пользователи, регистрации, языки, лиги, теги. Остальное — после D1; демо-телеметрия помечена «ДЕМО».</div>
        </>
      )}
    </>
  );
}
