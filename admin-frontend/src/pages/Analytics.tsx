import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { Analytics as AnalyticsData, Broadcast, Overview, ValueData } from '../api';
import { fromUtcText, localTime, num } from '../format';
import { can, useLoad, useMe, usePrefs } from '../state';
import { BarList, Card, Empty, ErrorBox, Loading, Metric, Metrics, NotCollected, PageHead, PeriodSelect, Tag, tagTone } from '../ui';
import { DayBars } from '../charts';

const TABS = ['Обзор', 'Ценность и топ', 'Аудитория и гео', 'Время активности', 'Контент и лиги', 'Удержание', 'Источники'] as const;
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
        ...row.map((v, h) => <span key={`${d}-${h}`} className="cell" title={`${DAYS[d]} ${h}:00 — в среднем ${Math.round(v * 100) / 100}`} style={{ opacity: Math.max(0.06, v / max) }} />),
      ])}
    </div>
  );
}

function bestSlot(data: number[][]): string {
  let best = { d: 0, h: 0, v: -1 };
  data.forEach((row, d) => row.forEach((v, h) => { if (v > best.v) best = { d, h, v }; }));
  return best.v > 0 ? `${DAYS[best.d]}, ${String(best.h).padStart(2, '0')}:00–${String((best.h + 1) % 24).padStart(2, '0')}:00` : '—';
}

function who(name: string, username: string, id: number): string {
  return name || (username ? `@${username}` : `id ${id}`);
}

function UserCell({ u }: { u: { userId: number | null; name: string; username: string } }) {
  return u.userId === null ? <span className="muted">скрыто</span> : <Link to={`/users/${u.userId}`}>{who(u.name, u.username, u.userId)}</Link>;
}

/** User value and top users: the bot (hourly snapshot of its database) and the Mini App (its telemetry). */
function ValueTab() {
  const v = useLoad<ValueData>('/api/admin/value');
  if (v.error) return <ErrorBox error={v.error} onRetry={v.reload} />;
  if (!v.data) return <Loading rows={4} />;
  const { bot, miniapp } = v.data;
  return (
    <>
      <h2 className="card-title" style={{ margin: '4px 0 12px' }}>Бот</h2>
      {bot.source === 'unavailable' ? (
        <Card><Empty title="Снимок данных бота не читается">Файл снимка повреждён или недоступен. Следующее обновление — в течение часа.</Empty></Card>
      ) : bot.source !== 'snapshot' ? (
        <Card><Empty title="Снимок данных бота ещё не получен">Итоги из базы бота обновляются раз в час. Если бот остановлен, остаётся последний снимок.</Empty></Card>
      ) : (
        <>
          {(Date.now() - Date.parse(bot.generatedAt)) / 3600_000 > 2 && (
            <div className="banner warn" role="status" style={{ marginBottom: 14 }}>
              Снимок данных бота устарел: последнее обновление {localTime(Math.floor(Date.parse(bot.generatedAt) / 1000))}.
              Обычно он обновляется раз в час — проверьте, что бот и задание снимка на сервере работают.
            </div>
          )}
          <Metrics>
            <Metric label="Срок жизни" value={`${num(bot.value.avgLifespanDays, 1)} дн.`} caption="среднее у вернувшихся хотя бы раз"
              hint="Сколько дней в среднем проходит от регистрации до последнего визита — среди тех, кто вернулся хотя бы через день. Одноразовые визиты не учитываются." />
            <Metric label="Прогнозов на пользователя" value={num(bot.value.forecastsPerUser, 1)} caption={`за всё время, на зарегистрированного (${num(bot.value.users)})`}
              hint="Все прогнозы бота за всё время, делённые на число зарегистрированных пользователей." />
            <Metric label="Кликов на пользователя" value={num(bot.value.clicksPerUser, 2)} caption={`дошли до партнёра ${bot.value.clickConversion}% людей`}
              hint="Переходы по партнёрским ссылкам, делённые на число зарегистрированных. Процент — доля людей, перешедших хотя бы раз." />
            <Metric label="Взяли промокод" value={`${bot.value.promoConversion}%`} caption="от зарегистрированных"
              hint="Доля зарегистрированных пользователей, получивших хотя бы один промокод." />
            <Metric label="Доход на пользователя" value={bot.value.cpa && bot.value.revenuePerUser !== null ? num(bot.value.revenuePerUser, 2) : '—'}
              caption={bot.value.cpa ? `всего ${num(bot.value.revenueTotal ?? 0, 2)} · ставка ${num(bot.value.cpa, 2)} за клик` : 'ставка за клик (PARTNER_CPA) не задана'}
              hint="Оценка, а не измеренный доход: клики на пользователя × ставка за клик партнёра (PARTNER_CPA в настройках бота)." />
          </Metrics>
          <Card title="Топ пользователей бота" sub={`по числу запросов · снимок ${localTime(bot.generatedAt, true)}`}>
            <table className="table">
              <thead><tr><th>#</th><th>Пользователь</th><th>Запросов</th><th>Прогнозов</th><th>Кликов</th><th>Промокод</th><th>Последний визит</th></tr></thead>
              <tbody>{bot.topUsers.map((u, i) => (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td><UserCell u={u} /></td>
                  <td>{num(u.requests)}</td><td>{num(u.forecasts)}</td><td>{num(u.clicks)}</td><td>{u.promo ? 'да' : '—'}</td>
                  <td className="muted">{localTime(fromUtcText(u.lastActive))}</td>
                </tr>
              ))}</tbody>
            </table>
          </Card>
        </>
      )}
      <h2 className="card-title" style={{ margin: '24px 0 12px' }}>Mini App</h2>
      {miniapp.source === 'unavailable' ? (
        <Card><Empty title="Телеметрия Mini App сейчас не читается">Файл занят или недоступен; обновите страницу позже.</Empty></Card>
      ) : miniapp.source !== 'telemetry' ? (
        <Card><NotCollected what="Активность Mini App" /></Card>
      ) : (
        <>
          <Metrics>
            <Metric label="Активных пользователей" value={num(miniapp.activeUsers)} caption={`из ${num(miniapp.totalUsers)} в базе · за ${miniapp.windowDays} дней`}
              hint="Сколько разных людей открывали Mini App за последние 90 дней (с начала сбора, если он моложе). Остальные — в базе (например, перенесены из бота), но не заходили." />
            <Metric label="Срок жизни" value={`${num(miniapp.value.avgLifespanDays, 1)} дн.`} caption="среднее у вернувшихся хотя бы раз"
              hint="Дней от первого до последнего дня активности — среди тех, кто заходил в разные дни." />
            <Metric label="Прогнозов на пользователя" value={num(miniapp.value.forecastsPerUser, 1)} caption="быстрые, купоны и AI-разборы"
              hint="Выданные быстрые прогнозы, купоны и созданные AI-разборы, делённые на активных пользователей." />
            <Metric label="Кликов на пользователя" value={num(miniapp.value.clicksPerUser, 2)} caption={`дошли до партнёра ${miniapp.value.clickConversion}% людей`}
              hint="Клики по партнёрским предложениям в Mini App, делённые на активных пользователей." />
          </Metrics>
          <Card title="Топ пользователей Mini App" sub={`по выданным прогнозам, затем по активности · за ${miniapp.windowDays} дней`}>
            {miniapp.topUsers.length === 0 ? <p className="small muted">Пока никто не открывал Mini App с начала сбора.</p> : (
              <table className="table">
                <thead><tr><th>#</th><th>Пользователь</th><th>Прогнозов</th><th title="Входы в Mini App и проверки сессии">Открытий</th><th>Дней активности</th><th>Кликов</th><th>Последний день</th></tr></thead>
                <tbody>{miniapp.topUsers.map((u, i) => (
                  <tr key={i}>
                    <td>{i + 1}</td>
                    <td><UserCell u={u} /></td>
                    <td>{num(u.forecasts)}</td><td>{num(u.requests)}</td><td>{num(u.activeDays)}</td><td>{num(u.clicks)}</td>
                    <td className="muted">{u.lastDay || '—'}</td>
                  </tr>
                ))}</tbody>
              </table>
            )}
          </Card>
        </>
      )}
    </>
  );
}

function DemoOnly({ what }: { what: string }) {
  return <Card><NotCollected what={what} /></Card>;
}

/** Real figures live on another page (CRM audit B2: these tabs said "not collected" although the data exists). */
function SeeElsewhere({ title, to, page }: { title: string; to: string; page: string }) {
  return <Card><Empty title={title} action={<Link className="btn" to={to}>Открыть «{page}»</Link>}>Реальные данные собираются и показаны на странице «{page}».</Empty></Card>;
}

const PLATFORM_NAMES: Record<string, string> = {
  ios: 'iOS', android: 'Android', android_x: 'Android', tdesktop: 'Telegram Desktop', macos: 'macOS',
  web: 'Web', weba: 'Web', webk: 'Web', unigram: 'Unigram', unknown: 'Другое', other: 'Другое',
};

function countryName(code: string): string {
  try {
    return new Intl.DisplayNames(['ru'], { type: 'region' }).of(code) ?? code;
  } catch {
    return code;
  }
}

/** Platforms merged by display name (Web A/K, Android X …), largest first. */
function platformRows(rows: { name: string; count: number }[]) {
  const merged = new Map<string, number>();
  for (const r of rows) {
    const name = PLATFORM_NAMES[r.name] ?? 'Другое';
    merged.set(name, (merged.get(name) ?? 0) + r.count);
  }
  return [...merged].sort((a, b) => b[1] - a[1]).map(([label, value]) => ({ label, value }));
}

export function Analytics() {
  const { period, demo } = usePrefs();
  const navigate = useNavigate();
  const [tab, setTab] = useState<TabName>('Обзор');
  const qs = `days=${period}${demo ? '&demo=1' : ''}`;
  const a = useLoad<AnalyticsData>(`/api/admin/analytics?${qs}`);
  const ov = useLoad<Overview>(`/api/admin/overview?${qs}`);
  const me = useMe();
  // Broadcasts, users and forecasts are not for analysts and promotion managers (the API refuses them too).
  const staff = can(me.admin.role, 'read_personal');
  const promoter = me.admin.role === 'promoter';
  const bc = useLoad<{ items: Broadcast[] }>(staff ? '/api/admin/broadcasts' : null);
  const d = a.data?.demo ?? null;
  const real = a.data?.real;
  // Collected (D1): shown as measured, zero included; otherwise the pilot's demo set or "not collected".
  const edge = a.data?.edge && a.data.edge.source === 'telemetry' ? a.data.edge : null;
  const mobile = edge ? edge.platforms.filter((p) => ['ios', 'android', 'android_x'].includes(p.name)).reduce((x, p) => x + p.count, 0) : 0;
  // The share among KNOWN platforms only ("other" / "unknown" are not counted either way).
  const platformTotal = edge ? edge.platforms.reduce((x, p) => x + p.count, 0) : 0;
  const knownPlatforms = edge ? edge.platforms.filter((p) => !['other', 'unknown'].includes(p.name)).reduce((x, p) => x + p.count, 0) : 0;
  // Measured from the Mini App files the panel reads — in the pilot that is the generated demo set.
  const src = a.data?.dataset === 'demo' ? 'демо-набор пилота' : 'реальные данные';

  return (
    <>
      <PageHead title="Аналитика" subtitle="Рост аудитории, вовлечённость, география, время активности и результат рассылок"
        actions={<><PeriodSelect />{staff && <button type="button" className="btn primary" onClick={() => navigate('/broadcasts/new')}>Новая рассылка</button>}</>} />
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
                <Metric label="Пользователи всего" value={!staff ? num(real.totalUsers) : <Link className="metric-link" to="/users">{num(real.totalUsers)}</Link>} caption={`+${num(real.newUsers)} за период · ${src}`} />
                <Metric demo={Boolean(d)} hint="DAU (Daily Active Users) — сколько разных людей открыли Mini App сегодня (по UTC). Один человек считается один раз, сколько бы раз он ни заходил." label="Активные сегодня (DAU)" value={d ? num(d.activeByDay.at(-1)?.dau) : '—'} caption={d ? 'демо-телеметрия' : <Link to="/product">реальные — в «Продукте»</Link>} />
                <Metric label="Прогнозы модели" value={promoter ? num(ov.data?.forecasts.total) : <Link className="metric-link" to="/forecasts">{num(ov.data?.forecasts.total)}</Link>} caption={`сверено: ${num(ov.data?.forecasts.settled)}`} />
                <Metric demo={Boolean(d)} label="Средняя сессия" value={d ? `${Math.floor(d.summary.avgSessionS / 60)} мин ${d.summary.avgSessionS % 60} с` : '—'} caption={d ? 'демо-телеметрия' : 'не собирается'} />
              </Metrics>
              <div className="grid g2">
                <Card title="Новые пользователи по дням" sub={`Регистрации в Mini App по UTC-дням · ${src}`} action={<Tag tone="accent">{num(real.newUsers)} за период</Tag>}>
                  <DayBars title="Новые пользователи" points={real.newByDay.map((x) => ({ day: x.day, value: x.new }))} />
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
                {staff && <Card title="Рассылки" action={<Link to="/broadcasts" className="small">Все рассылки →</Link>}>
                  {bc.data && bc.data.items.length > 0 ? (
                    <table className="table">
                      <thead><tr><th>Кампания</th><th>Статус</th><th>Когда</th></tr></thead>
                      <tbody>{bc.data.items.slice(0, 4).map((b) => (
                        <tr key={b.id}><td className="strong">{b.name}</td><td><Tag tone={b.status === 'scheduled' ? 'accent' : b.status === 'draft' ? 'neutral' : b.status === 'sent' ? 'success' : 'warning'}>{{ draft: 'Черновик', scheduled: 'Запланирована', sent: 'Отправлена', cancelled: 'Отменена' }[b.status]}</Tag></td><td>{localTime(b.scheduledAt ?? b.updatedAt)}</td></tr>
                      ))}</tbody>
                    </table>
                  ) : <p className="muted small">Рассылок пока нет.</p>}
                </Card>}
                <Card title="Теги аудитории" sub="Сколько пользователей с каждым тегом">
                  {real.tags.length === 0 ? <p className="muted small">Тегов нет или нет доступа к ним.</p> : (
                    <div className="bar-list">{real.tags.slice(0, 6).map((t) => (
                      <div key={t.tag} className="row" style={{ justifyContent: 'space-between' }}>
                        {promoter ? <Tag tone={tagTone(t.tag)}>{t.tag}</Tag> : <Link to={`/users?tag=${encodeURIComponent(t.tag)}`}><Tag tone={tagTone(t.tag)}>{t.tag}</Tag></Link>}<b>{num(t.users)}</b>
                      </div>
                    ))}</div>
                  )}
                </Card>
              </div>
            </>
          )}
          {tab === 'Ценность и топ' && <ValueTab />}
          {tab === 'Аудитория и гео' && (
            <>
              {edge && (
                <Metrics>
                  <Metric label="Стран" value={num(edge.geo.length)} caption="по стране входа" />
                  <Metric label="Активных за период" value={num(edge.activeUsers)} caption="открывали Mini App" />
                  <Metric label="Мобильные" value={knownPlatforms ? `${Math.round((mobile / knownPlatforms) * 100)}%` : '—'} caption="iOS + Android, среди известных платформ" />
                </Metrics>
              )}
              {!edge && d && (
                <Metrics>
                  <Metric demo label="Стран" value={num(d.summary.countries)} caption="по стране входа" />
                  <Metric demo label="Пик активности" value={d.summary.peak} caption="Баку" />
                  <Metric demo label="Средняя сессия" value={`${Math.floor(d.summary.avgSessionS / 60)} мин`} caption="демо-телеметрия" />
                  <Metric demo label="Мобильные" value={`${Math.round(d.summary.mobileShare * 100)}%`} caption="iOS + Android" />
                </Metrics>
              )}
              <div className="grid g3">
                {edge ? (
                  <Card title="География" sub="Страна из Cloudflare при входе, IP не хранится · активные за период">
                    {edge.geo.length ? <BarList rows={edge.geo.map((g) => ({ label: countryName(g.code), value: g.count, extra: <Tag>{g.code}</Tag> }))} />
                      : <p className="small muted">Пока нет входов с определённой страной.</p>}
                  </Card>
                ) : d ? (
                  <Card title="География" sub="Страна из Cloudflare при входе, IP не хранится" action={<Tag tone="violet">ДЕМО</Tag>}>
                    <BarList rows={d.geo.map((g) => ({ label: g.name, value: g.count, extra: g.code !== '—' ? <Tag>{g.code}</Tag> : undefined }))} />
                  </Card>
                ) : <DemoOnly what="География" />}
                <Card title="Языки интерфейса" sub={`${src} · выбор пользователя или язык Telegram`}>
                  <BarList tone="violet" rows={real.languages.map((l) => ({ label: LANG_NAMES[l.lang] ?? l.lang, value: l.count }))} />
                </Card>
                {edge ? (
                  <Card title="Платформы" sub="Клиент Telegram при входе · активные за период">
                    {platformTotal ? <BarList tone="success" rows={platformRows(edge.platforms)} />
                      : <p className="small muted">Пока нет входов с известной платформой.</p>}
                  </Card>
                ) : d ? (
                  <Card title="Платформы" sub="Клиент Telegram при запуске" action={<Tag tone="violet">ДЕМО</Tag>}>
                    <BarList tone="success" rows={d.platforms.map((p) => ({ label: p.name, value: p.count }))} />
                  </Card>
                ) : <DemoOnly what="Платформы" />}
              </div>
            </>
          )}
          {tab === 'Время активности' && (
            <>
              {edge ? (
                <Card title="Когда пользователи активны" sub="Активные пользователи по дням недели и часам · Баку (UTC+4)">
                  <Heatmap data={edge.activityHeat} label="Тепловая карта активности" />
                  <div className="banner info" style={{ marginTop: 14 }}>Лучшее время для рассылки: <strong>{bestSlot(edge.activityHeat)}</strong></div>
                </Card>
              ) : d ? (
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
          ) : <SeeElsewhere title="Удержание по когортам" to="/product" page="Продукт" />)}
          {tab === 'Источники' && (d ? (
            <Card title="Источники новых пользователей" sub="Откуда пришли за период" action={<Tag tone="violet">ДЕМО</Tag>}>
              <BarList tone="warning" rows={d.sources.map((s) => ({ label: s.name, value: s.count }))} />
            </Card>
          ) : <SeeElsewhere title="Источники пользователей" to="/referrals" page="Рефералы" />)}
          <div className="banner info" style={{ marginTop: 16 }}>Из файлов Mini App ({src}): пользователи, регистрации, языки, лиги, теги{edge ? '; страна, платформа и часы активности — сбор Mini App с момента его включения' : ''}. Демо-данные помечены «ДЕМО».</div>
        </>
      )}
    </>
  );
}
