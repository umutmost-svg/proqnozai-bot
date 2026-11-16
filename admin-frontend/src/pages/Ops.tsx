import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type AdminAccount, type OutboxPage, type ProductPage, type RolesPage } from '../api';
import { num, pct, localTime } from '../format';
import { useLoad, useMe, usePrefs } from '../state';
import { Card, DangerDialog, Empty, ErrorBox, Loading, Metric, Metrics, PageHead, PeriodSelect, pagePeriod, Status, Tag, Toast } from '../ui';
import { SettingsTabs } from './Settings';
import { DayBars } from '../charts';

const JOB_STATUS: Record<string, string> = {
  queued: 'в очереди', sending: 'отправляется', sent: 'отправлено', failed: 'ошибка', expired: 'просрочено', cancelled: 'отменено',
};
const ACTION: Record<string, string> = {
  'user.message': 'Личное сообщение', 'broadcast.schedule': 'Рассылка запланирована', 'broadcast.cancel': 'Рассылка отменена',
};

/** Admin -> bot queue: statuses and limits only (texts are never sent to the panel). */
export function Outbox() {
  const [status, setStatus] = useState('');
  const { data, error, loading, reload } = useLoad<OutboxPage>(`/api/admin/outbox${status ? `?status=${status}` : ''}`);
  const s = data?.summary;
  return (
    <>
      <PageHead title="Очередь отправки" subtitle="Сообщения и рассылки, которые отправляет бот: статусы, лимиты, журнал действий"
        actions={<button type="button" className="btn" onClick={reload}>Обновить</button>} />
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : data && s && (
        <>
          {!s.connected && <p className="small muted">Файл очереди ещё не создан: ни одного задания не было.</p>}
          <Metrics>
            <Metric label="В очереди" value={num(s.byStatus.queued ?? 0)} caption={s.nextDueAt ? `ближайшее: ${localTime(s.nextDueAt)}` : 'нет ожидающих'} />
            <Metric label="Отправлено" value={num(s.byStatus.sent ?? 0)} caption={`за 24 ч: ${num(s.last24h.sent ?? 0)} · последнее ${localTime(s.lastSentAt)}`} />
            <Metric label="Ошибки" value={num(s.byStatus.failed ?? 0)} caption={`за 24 ч (по последней попытке): ${num(s.last24h.failed ?? 0)}`} />
            <Metric label="Просрочено / отменено" value={`${num(s.byStatus.expired ?? 0)} / ${num(s.byStatus.cancelled ?? 0)}`} caption="не отправлялись" />
          </Metrics>
          <Card title="Лимиты отправки (бот)">
            <div className="kv-row"><span>Заданий за один проход (≈ раз в секунду)</span><b>{data.limits.batchPerRun}</b></div>
            <div className="kv-row"><span>Попыток при «retry after» от Telegram</span><b>{data.limits.maxAttempts}</b></div>
            <div className="kv-row"><span>Задание без итога дольше — ошибка, без повтора</span><b>{Math.round(data.limits.stuckS / 60)} мин</b></div>
            <div className="kv-row"><span>Не отправленное за срок — просрочено</span><b>{Math.round(data.limits.jobTtlS / 86400)} дн.</b></div>
            <div className="kv-row"><span>Тексты отправленных хранятся</span><b>{data.limits.textRetentionDays} дн.</b></div>
            <p className="small muted" style={{ marginBottom: 0 }}>Доставка не более одного раза: при неизвестном исходе сообщение не отправляется повторно.</p>
          </Card>
          <div className="chips" role="group" aria-label="Статус заданий" style={{ margin: '16px 0' }}>
            <button type="button" className="chip-btn" aria-pressed={status === ''} onClick={() => setStatus('')}>Все</button>
            {data.statuses.map((st) => (
              <button key={st} type="button" className="chip-btn" aria-pressed={status === st} onClick={() => setStatus(st)}>{JOB_STATUS[st] ?? st}</button>
            ))}
          </div>
          {data.items.length === 0 ? <Empty title="Заданий нет" /> : (
            <div className="table-card">
              <table className="table">
                <thead><tr><th>Создано</th><th>Тип</th><th>Получатель</th><th>Статус</th><th className="num">Попыток</th><th>Код ошибки</th><th>Отправлено</th></tr></thead>
                <tbody>{data.items.map((j) => (
                  <tr key={j.key}>
                    <td>{localTime(j.createdAt)}{j.notBefore > j.createdAt && <span className="sub">не раньше {localTime(j.notBefore)}</span>}</td>
                    <td>{j.kind === 'broadcast' ? <Tag tone="violet">Рассылка</Tag> : <Tag>Сообщение</Tag>}</td>
                    <td><Link to={`/users/${j.userId}`} className="mono">{j.userId}</Link></td>
                    <td><Status value={j.status} label={JOB_STATUS[j.status] ?? j.status} /></td>
                    <td className="num">{j.attempts}</td>
                    <td className="mono">{j.error ?? '—'}</td>
                    <td>{localTime(j.sentAt)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <Card title="Журнал действий с очередью">
            {data.log.length === 0 ? <Empty title="Действий не было" /> : (
              <table className="table"><tbody>{data.log.map((e) => (
                <tr key={e.id}><td>{localTime(e.at)}</td><td>{ACTION[e.action] ?? e.action}</td><td>{e.adminEmail ?? '—'}</td><td><Status value={e.result} /></td></tr>
              ))}</tbody></table>
            )}
          </Card>
        </>
      )}
    </>
  );
}

const PERMISSION_LABEL: Record<string, string> = {
  read: 'Просмотр панели и агрегатов', read_personal: 'Персональные данные пользователей', manage_users: 'Действия с пользователями, сообщения, рассылки',
  manage_settings: 'Настройки продукта', manage_admins: 'Администраторы и права', manage_ads: 'Рекламные кампании', publish_ads: 'Публикация рекламы',
  manage_partners: 'Партнёры рекламы', manage_referrals: 'Реферальные ссылки', manage_inquiry_settings: 'Срок хранения обращений',
  view_dialogs: 'Чтение обращений (отдельное право)',
};
const ROLE_LABEL: Record<string, string> = { owner: 'Владелец', admin: 'Администратор', analyst: 'Аналитик', promoter: 'Продвижение' };

/** Settings -> roles: the server's permission matrix, and per-admin grants (owner only, reason + 2FA). */
export function SettingsRoles() {
  const me = useMe();
  const roles = useLoad<RolesPage>('/api/admin/roles');
  const owner = me.admin.role === 'owner';
  const admins = useLoad<{ items: (AdminAccount & { grants: string[] })[] }>(owner ? '/api/admin/admins' : null);
  const [pending, setPending] = useState<{ id: number; email: string; permission: string; granted: boolean } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  return (
    <>
      <PageHead crumb="Настройки" title="Роли и права" subtitle="Матрица прав проверяется на сервере; отдельные права выдаёт владелец" />
      <SettingsTabs />
      {roles.error ? <ErrorBox error={roles.error} onRetry={roles.reload} /> : !roles.data ? <Loading rows={6} /> : (
        <div className="table-card">
          <table className="table">
            <thead><tr><th>Право</th>{roles.data.roles.map((r) => <th key={r}>{ROLE_LABEL[r] ?? r}</th>)}</tr></thead>
            <tbody>
              {roles.data.permissions.map((p) => (
                <tr key={p.permission}><td>{PERMISSION_LABEL[p.permission] ?? p.permission}<span className="sub mono">{p.permission}</span></td>
                  {roles.data!.roles.map((r) => <td key={r}>{p.roles.includes(r) ? '✓' : '—'}</td>)}</tr>
              ))}
              {roles.data.grants.map((g) => (
                <tr key={g.permission}><td>{PERMISSION_LABEL[g.permission] ?? g.permission}<span className="sub mono">{g.permission} · только по выдаче</span></td>
                  {roles.data!.roles.map((r) => <td key={r}>{g.roles.includes(r) ? 'по выдаче' : '—'}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {owner && roles.data && (
        <Card title="Отдельные права администраторов" sub="Выдача и отзыв — с причиной и кодом 2FA, запись в журнал">
          {admins.error ? <ErrorBox error={admins.error} onRetry={admins.reload} /> : !admins.data ? <Loading rows={3} /> : (
            <table className="table"><tbody>{admins.data.items.filter((a) => a.state === 'active').map((a) => (
              <tr key={a.id}><td>{a.email}<span className="sub">{ROLE_LABEL[a.role]}</span></td>
                {roles.data!.grants.map((g) => {
                  const has = a.grants.includes(g.permission);
                  const allowed = g.roles.includes(a.role);
                  return (
                    <td key={g.permission}>
                      <button type="button" className="btn" disabled={!allowed && !has}
                        onClick={() => setPending({ id: a.id, email: a.email, permission: g.permission, granted: !has })}>
                        {has ? `Отозвать «${PERMISSION_LABEL[g.permission] ?? g.permission}»` : `Выдать «${PERMISSION_LABEL[g.permission] ?? g.permission}»`}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}</tbody></table>
          )}
        </Card>
      )}
      {pending && (
        <DangerDialog title={pending.granted ? 'Выдать право?' : 'Отозвать право?'} confirmLabel={pending.granted ? 'Выдать' : 'Отозвать'}
          tone={pending.granted ? 'primary' : 'danger'} onClose={() => setPending(null)}
          lead={<>{pending.email}: {PERMISSION_LABEL[pending.permission] ?? pending.permission}</>}
          onConfirm={async (input) => {
            await api(`/api/admin/admins/${pending.id}/grants`, { method: 'POST', body: { ...input, permission: pending.permission, granted: pending.granted } });
            setPending(null); setToast(pending.granted ? 'Право выдано' : 'Право отозвано'); admins.reload();
          }} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

/** Product analytics: telemetry aggregates (no user ids), referral funnel. */
export function Product() {
  const period = pagePeriod(usePrefs().period, false);
  const { data, error, loading, reload } = useLoad<ProductPage>(`/api/admin/product?days=${period}`);
  const p = data?.product;
  const r = data?.referrals;
  return (
    <>
      <PageHead title="Продуктовая аналитика" subtitle="Активность, удержание, прогнозы и AI по телеметрии Mini App (UTC-дни)" actions={<PeriodSelect allowToday={false} />} />
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : data && (
        <>
          {p && p.source === 'telemetry' ? (
            <>
              <Metrics>
                <Metric label="Активные сегодня (DAU)" value={num(p.dau)} caption="разные пользователи за сегодня"
                  hint="DAU (Daily Active Users) — сколько разных людей открыли Mini App сегодня (по UTC). Один человек считается один раз, сколько бы раз он ни заходил." />
                <Metric label="Активные за неделю (WAU)" value={num(p.wau)} caption="разные пользователи за 7 дней"
                  hint="WAU (Weekly Active Users) — сколько разных людей открывали Mini App хотя бы раз за последние 7 дней." />
                <Metric label="Активные за месяц (MAU)" value={num(p.mau)} caption="разные пользователи за 30 дней"
                  hint="MAU (Monthly Active Users) — сколько разных людей открывали Mini App хотя бы раз за последние 30 дней." />
                <Metric label="Прогнозов выдано" value={num(p.totals.forecasts)} caption={`быстрые ${num(p.totals.quick)} · купоны ${num(p.totals.coupons)} · AI ${num(p.totals.aiCreated)}`} />
              </Metrics>
              <p className="small muted" style={{ margin: '4px 0 16px' }}>
                «Активный» — открыл Mini App хотя бы раз за период; каждый человек считается один раз. Отношение
                «сегодня / за месяц» показывает, как часто возвращаются: {p.mau ? `${Math.round((p.dau / p.mau) * 100)}%` : '—'} аудитории месяца заходили сегодня.
              </p>
              <div className="grid card-2">
                <Card title="Активные пользователи по дням" sub="Сколько разных людей открывали Mini App в каждый день (DAU)">
                  <DayBars title="DAU" points={p.series.map((d) => ({ day: d.day, value: d.dau }))} />
                </Card>
                <Card title="AI-разборы" sub={`создано ${num(p.totals.aiCreated)} · открыто ${num(p.totals.aiOpened)}`}>
                  <DayBars title="AI создано" points={p.series.map((d) => ({ day: d.day, value: d.aiCreated }))} />
                </Card>
              </div>
              <Card title="Удержание по когортам регистрации" sub="Доля активных на неделе N после своей регистрации — среди тех, у кого эта неделя уже закончилась">
                <table className="table">
                  <thead><tr><th>Неделя</th><th className="num">Размер</th>{p.cohorts[0]?.retention.map((_, k) => <th key={k} className="num">Н{k}</th>)}</tr></thead>
                  <tbody>{p.cohorts.map((c) => (
                    <tr key={c.week}><td>{c.week}</td><td className="num">{num(c.size)}</td>
                      {c.retention.map((v, k) => <td key={k} className="num">{v === null ? '' : pct(v, 0)}</td>)}</tr>
                  ))}</tbody>
                </table>
              </Card>
            </>
          ) : p && p.source === 'unavailable' ? (
            <Empty title="Телеметрия сейчас недоступна">Файл телеметрии занят или не читается; это не нулевая активность. Обновите страницу позже.</Empty>
          ) : (
            <Empty title="Телеметрия Mini App не собирается">DAU/WAU/MAU, удержание, прогнозы и AI считаются по <span className="mono">miniapp.telemetry.db</span>, который Mini App ведёт при <span className="mono">MINIAPP_ADS_ENABLED=true</span>.</Empty>
          )}
          <div className="grid card-2">
            <Card title="Реферальная воронка" action={<Link to="/referrals">Ссылки →</Link>}>
              {r && (
                <>
                  <div className="kv-row"><span>Запуски по ссылкам</span><b>{num(r.starts)}</b></div>
                  <div className="kv-row"><span>Дошли до Mini App</span><b>{num(r.inMiniApp)}</b></div>
                  <div className="kv-row"><span>Из них запросили прогноз</span><b>{r.converted === null ? '—' : num(r.converted)}</b></div>
                  <div className="kv-row"><span>Из них активны 7 дней</span><b>{num(r.active7d)}</b></div>
                </>
              )}
            </Card>
            <Card title="Покрытие коэффициентами">
              <p className="small muted" style={{ margin: 0 }}>Не собирается: Mini App хранит коэффициенты только в памяти. Нужна запись доли матчей с коэффициентами (отдельный шаг).</p>
            </Card>
          </div>
        </>
      )}
    </>
  );
}

/** Placeholder for the Ads section: the ads module (branch feat/ads-module, admin-frontend/src/ads/**) replaces
 * this route's element. Kept isolated so the two branches touch only this one line in App.tsx. */
export function AdsPlaceholder() {
  return (
    <>
      <PageHead title="Реклама" subtitle="Раздел подключается модулем рекламы" />
      <Empty title="Модуль рекламы ещё не подключён" />
    </>
  );
}
