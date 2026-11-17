import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { api, type AdminAccount, type AdminEvent, type Role, type SettingValue } from '../api';
import { localTime } from '../format';
import { can, useLoad, useMe, usePrefs } from '../state';
import { DangerDialog, Empty, ErrorBox, Loading, Note, PageHead, PeriodSelect, ROLE_LABEL, SectionHead, Status, Toast } from '../ui';

export function SettingsTabs() {
  return (
    <nav className="tabs" aria-label="Вкладки настроек">
      <NavLink to="/settings" end>Функции</NavLink>
      <NavLink to="/settings/admins">Администраторы</NavLink>
      <NavLink to="/settings/roles">Роли и права</NavLink>
      <NavLink to="/settings/journal">Журнал действий</NavLink>
    </nav>
  );
}

// `live`: the Mini App follows the switch. The others are still only recorded (the Mini App reads them from its
// environment): shown disabled and labelled, never "saved" as if they changed something (CRM audit P1).
const FEATURES: { key: string; name: string; desc: string; live?: boolean }[] = [
  { key: 'ai.enabled', name: 'AI-разборы', desc: 'Включение и отключение новых платных запросов модели' },
  { key: 'quick.enabled', name: 'Быстрый прогноз', desc: 'Доступность действия и купонов в Mini App' },
  { key: 'credits.enabled', name: 'Кредиты за прогнозы', desc: 'Любой прогноз стоит 10 кредитов; без кредитов — экран «Мои кредиты». Выключено: прогнозы бесплатны', live: true },
];
const LIMITS: { key: string; name: string }[] = [
  { key: 'ai.per_user_daily', name: 'AI-разборов на пользователя в сутки' },
  { key: 'ai.daily_budget', name: 'Общий дневной бюджет AI-вызовов' },
];

type Pending = { key: string; value: boolean | number; label: string };

export function SettingsFeatures() {
  const me = useMe();
  const { data, error, loading, reload } = useLoad<{ settings: Record<string, SettingValue> }>('/api/admin/settings');
  const [pending, setPending] = useState<Pending | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const editable = can(me.admin.role, 'manage_settings');

  return (
    <>
      <PageHead crumb="Настройки" title="Настройки" subtitle="Функции продукта, лимиты и аудит изменений" />
      <SettingsTabs />
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : data && (
        <div className="cols" style={{ gridTemplateColumns: 'minmax(0, 1fr) 380px', gap: 60 }}>
          <section className="section">
            <SectionHead title="Управление функциями" />
            {FEATURES.map((f) => {
              const on = Boolean(data.settings[f.key]?.value);
              return (
                <div className="feature" key={f.key}>
                  <div>
                    <div className="feature-name" id={`f-${f.key}`}>{f.name}</div>
                    <div className="feature-desc">{f.desc}{!f.live && <> · <span className="muted">пока не применяется в Mini App</span></>}</div>
                  </div>
                  <button type="button" className="toggle" role="switch" aria-checked={on} aria-labelledby={`f-${f.key}`}
                    // A setting with its own permission (the credits switch: the owner's) is editable only with it.
                    disabled={!editable || !f.live || (data.settings[f.key]?.permission === 'manage_credits' && me.admin.role !== 'owner')}
                    title={data.settings[f.key]?.permission === 'manage_credits' && me.admin.role !== 'owner' ? 'Только владелец' : undefined}
                    onClick={() => setPending({ key: f.key, value: !on, label: `${f.name}: ${on ? 'выключить' : 'включить'}` })} />
                </div>
              );
            })}
            {LIMITS.map((l) => {
              const s = data.settings[l.key];
              return (
                <div className="feature" key={l.key} style={{ alignItems: 'flex-end' }}>
                  <div className="field" style={{ margin: 0, flex: 1 }}>
                    <label htmlFor={`l-${l.key}`} style={{ fontWeight: 500, fontSize: 13, color: 'var(--text)' }}>{l.name}</label>
                    <input id={`l-${l.key}`} className="control" style={{ maxWidth: 360 }} value={String(s?.value ?? '')} disabled readOnly />
                    <span className="field-hint">Пока не применяется в Mini App: лимит задаётся окружением и меняется деплоем</span>
                  </div>
                </div>
              );
            })}
            <p className="small muted" style={{ marginTop: 18 }}>
              Последнее изменение: {(() => {
                const times = Object.values(data.settings).map((s) => s.updatedAt ?? 0);
                const t = Math.max(0, ...times);
                return t ? `${localTime(t)}` : 'значения по умолчанию';
              })()}
            </p>
          </section>
          <aside>
            <SectionHead title="Подтверждение изменений" />
            <p style={{ fontSize: 14, color: 'var(--text)', lineHeight: 1.35, marginTop: 30 }}>
              Изменение настройки требует повторной 2FA и указания причины.
            </p>
            <Note title="Что применяется в Mini App">«Кредиты за прогнозы» и курсы депозитов применяются в Mini App сразу после
              сохранения. Остальные переключатели и лимиты пока не применяются и заблокированы.</Note>
            <Note title="Ограничение безопасности">Недоступность копии настроек выключает управляемые функции (закрытый режим).</Note>
            <div style={{ marginTop: 28 }}><span className="chip info">Изменение с аудитом</span></div>
          </aside>
        </div>
      )}
      {pending && (
        <DangerDialog title="Изменить настройку?" lead={<>{pending.label}. Действие будет записано в журнал.</>}
          confirmLabel="Сохранить изменение" tone="primary" onClose={() => setPending(null)}
          onConfirm={async (input) => {
            await api(`/api/admin/settings/${encodeURIComponent(pending.key)}`, { method: 'POST', body: { ...input, value: pending.value } });
            setPending(null);
            setToast('Настройка сохранена и записана в журнал');
            reload();
          }} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

const ROLE_DESC: Record<Role, string> = {
  owner: 'Полный доступ и управление администраторами',
  admin: 'Управление функциями и пользователями',
  analyst: 'Просмотр аналитики без изменений и персональных данных',
  promoter: 'Аналитика и только свои реферальные ссылки: создание, архив, статистика. Без персональных данных',
};

const ROLE_OPTIONS = (['promoter', 'analyst', 'admin', 'owner'] as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>);

/** The one-time link an invitee opens to set their password and 2FA (the token lives in the URL fragment). */
const inviteUrl = (token: string) => `${window.location.origin}/activate#${token}`;

type AdminAction =
  | { kind: 'invite' }
  | { kind: 'update'; target: AdminAccount; role?: Role; state?: 'active' | 'disabled'; label: string }
  | { kind: 'link'; target: AdminAccount }
  | { kind: 'reset'; target: AdminAccount };

export function SettingsAdmins() {
  const me = useMe();
  const { data, error, loading, reload } = useLoad<{ items: AdminAccount[] }>('/api/admin/admins');
  const [action, setAction] = useState<AdminAction | null>(null);
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Role>('promoter');
  const [link, setLink] = useState<{ email: string; url: string } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const owner = can(me.admin.role, 'manage_admins');
  const activeOwners = data?.items.filter((a) => a.role === 'owner' && a.state === 'active').length ?? 0;

  return (
    <>
      <PageHead crumb="Настройки" title="Администраторы и роли" subtitle="Персональные учётные записи, разрешения и 2FA" />
      <SettingsTabs />
      <div className="filters" style={{ justifyContent: 'flex-end' }}>
        <button type="button" className="btn" disabled={!owner} title={owner ? undefined : 'Только владелец'}
          onClick={() => { setEmail(''); setRole('promoter'); setAction({ kind: 'invite' }); }}>Добавить сотрудника</button>
      </div>
      {link && (
        <Note title={`Ссылка-приглашение для ${link.email}`}>
          <p style={{ margin: '0 0 8px' }}>Отправьте её сотруднику лично. Ссылка одноразовая и действует 72 часа; по ней он задаст пароль
            и подключит 2FA. Показывается один раз — если потеряется, выдайте новую. Его e-mail также нужно добавить в Cloudflare Access.</p>
          <div style={{ display: 'flex', gap: 8 }}>
            <input className="control mono" readOnly value={link.url} aria-label="Ссылка-приглашение" onFocus={(e) => e.target.select()} style={{ flex: 1 }} />
            <button type="button" className="btn" onClick={() => void navigator.clipboard?.writeText(link.url).then(() => setToast('Скопировано'), () => setToast('Не удалось скопировать'))}>Копировать</button>
            <button type="button" className="btn link" onClick={() => setLink(null)}>Скрыть</button>
          </div>
        </Note>
      )}
      <div className="cols" style={{ gridTemplateColumns: 'minmax(0, 1fr) 280px', gap: 40 }}>
        <section className="section">
          <SectionHead title="Учётные записи" />
          {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading /> : data && (
            data.items.length === 0 ? <Empty title="Учётные записи будут отображаться здесь" /> : (
              <table className="table">
                <thead><tr><th>Электронная почта</th><th>Роль</th><th>2FA</th><th>Состояние</th><th>Вход</th>{owner && <th>Действия</th>}</tr></thead>
                <tbody>
                  {data.items.map((a) => {
                    const lastOwner = a.role === 'owner' && a.state === 'active' && activeOwners <= 1;
                    return (
                      <tr key={a.id}>
                        <td className="strong">{a.email}{a.id === me.admin.id && <span className="sub">это вы</span>}</td>
                        <td>
                          {owner ? (
                            <select className="control" style={{ minWidth: 120, height: 30 }} aria-label={`Роль ${a.email}`} value={a.role}
                              disabled={lastOwner || a.state === 'invited'}
                              title={lastOwner ? 'Последнего владельца нельзя понизить' : undefined}
                              onChange={(e) => setAction({ kind: 'update', target: a, role: e.target.value as Role, label: `Роль ${a.email}: ${a.role} → ${e.target.value}` })}>
                              {ROLE_OPTIONS}
                            </select>
                          ) : ROLE_LABEL[a.role] ?? a.role}
                        </td>
                        <td>{a.twoFactor ? <Status value="ok" label="Включена" /> : <Status value="warning" label="Не настроена" />}</td>
                        <td><Status value={a.state} label={a.state === 'active' ? 'Активна' : undefined} /></td>
                        <td>{localTime(a.lastLoginAt)}</td>
                        {owner && (
                          <td>
                            {a.state === 'active' && (
                              <button type="button" className="btn link" disabled={lastOwner}
                                title={lastOwner ? 'Последнего владельца нельзя отключить' : undefined}
                                onClick={() => setAction({ kind: 'update', target: a, state: 'disabled', label: `Отключить ${a.email}` })}>Отключить</button>
                            )}
                            {a.state === 'active' && a.id !== me.admin.id && (
                              <button type="button" className="btn link" onClick={() => setAction({ kind: 'reset', target: a })}>Сбросить вход</button>
                            )}
                            {a.state === 'disabled' && (
                              <button type="button" className="btn link"
                                onClick={() => setAction({ kind: 'update', target: a, state: 'active', label: `Включить ${a.email}` })}>Включить</button>
                            )}
                            {a.state === 'invited' && (
                              <button type="button" className="btn link" onClick={() => setAction({ kind: 'link', target: a })}>Новая ссылка</button>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )
          )}
          <Note title="Защита последнего owner">Последнего владельца нельзя удалить, понизить или отключить. Смена роли и отключение
            сразу завершают сессии этой учётной записи.</Note>
        </section>
        <aside>
          <SectionHead title="Уровни доступа" />
          <ul className="kv">
            {(Object.keys(ROLE_DESC) as Role[]).map((r) => (
              <li key={r} style={{ display: 'block' }}>
                <div style={{ fontWeight: 600, fontSize: 15 }}>{ROLE_LABEL[r]}</div>
                <div className="small" style={{ color: 'var(--text)', marginTop: 6, fontSize: 13 }}>{ROLE_DESC[r]}</div>
              </li>
            ))}
          </ul>
        </aside>
      </div>
      {action?.kind === 'invite' && (
        <DangerDialog title="Добавить сотрудника" tone="primary" confirmLabel="Создать приглашение"
          lead="Будет создана учётная запись и одноразовая ссылка: по ней сотрудник сам задаст пароль и 2FA."
          onClose={() => setAction(null)}
          onConfirm={async (input) => {
            const r = await api<{ inviteToken?: string }>('/api/admin/admins', { method: 'POST', body: { ...input, email, role } });
            setAction(null);
            setLink(r.inviteToken ? { email, url: inviteUrl(r.inviteToken) } : null);
            setToast(r.inviteToken ? `Приглашение для ${email} создано` : 'Приглашение уже было создано — выдайте новую ссылку');
            reload();
          }}>
          <div className="field">
            <label htmlFor="inv-email">Электронная почта</label>
            <input id="inv-email" className="control" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" />
          </div>
          <div className="field">
            <label htmlFor="inv-role">Роль</label>
            <select id="inv-role" className="control" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLE_OPTIONS}
            </select>
            <p className="field-hint">{ROLE_DESC[role]}</p>
          </div>
        </DangerDialog>
      )}
      {action?.kind === 'update' && (
        <DangerDialog title="Изменить доступ?" confirmLabel="Подтвердить" lead={<>{action.label}. Действие будет записано в журнал.</>}
          onClose={() => setAction(null)}
          onConfirm={async (input) => {
            await api(`/api/admin/admins/${action.target.id}`, { method: 'POST', body: { ...input, role: action.role, state: action.state } });
            setAction(null);
            setToast('Доступ изменён');
            reload();
          }} />
      )}
      {action?.kind === 'link' && (
        <DangerDialog title="Новая ссылка-приглашение" tone="primary" confirmLabel="Выдать ссылку"
          lead={<>{action.target.email}: прежняя ссылка перестанет работать.</>}
          onClose={() => setAction(null)}
          onConfirm={async (input) => {
            const r = await api<{ inviteToken: string }>(`/api/admin/admins/${action.target.id}/invite`, { method: 'POST', body: input });
            setLink({ email: action.target.email, url: inviteUrl(r.inviteToken) });
            setAction(null);
          }} />
      )}
      {action?.kind === 'reset' && (
        <DangerDialog title="Сбросить пароль и 2FA?" confirmLabel="Сбросить вход"
          lead={<>{action.target.email}: пароль и 2FA удаляются, все сессии завершаются. Роль и права сохранятся. Вы получите новую
            одноразовую ссылку, по которой сотрудник задаст новый пароль и подключит 2FA.</>}
          onClose={() => setAction(null)}
          onConfirm={async (input) => {
            const r = await api<{ inviteToken: string }>(`/api/admin/admins/${action.target.id}/reset`, { method: 'POST', body: input });
            setLink({ email: action.target.email, url: inviteUrl(r.inviteToken) });
            setAction(null);
            reload();
          }} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

const ACTIONS: Record<string, string> = {
  'auth.login': 'Вход в панель', 'auth.locked': 'Блокировка после ошибок', 'settings.update': 'Изменение настройки',
  'admin.invite': 'Приглашение администратора', 'admin.update': 'Изменение доступа', 'admin.create_cli': 'Создание через CLI',
  'user.sessions_revoke': 'Отзыв сессий пользователя',
  'auth.totp_failed': 'Неверный код 2FA при входе', 'auth.step_up_failed': 'Неверный код 2FA или пароль при подтверждении',
  'admin.password_change': 'Смена своего пароля', 'admin.sessions_revoke_self': 'Выход на других устройствах',
  'admin.reset': 'Сброс входа сотрудника', 'admin.reset_cli': 'Сброс входа через CLI', 'admin.unlock_cli': 'Разблокировка через CLI',
  'admin.invite_link': 'Новая ссылка-приглашение', 'admin.activate': 'Активация по приглашению',
};

function describe(e: AdminEvent): string {
  if (!e.after && !e.before) return e.action === 'auth.login' || !e.targetId ? '' : `#${e.targetId}`; // hidden for analysts
  const after = e.after ?? {};
  if (e.action === 'settings.update') return `${String(after.key)}: ${JSON.stringify(e.before?.value)} → ${JSON.stringify(after.value)}`;
  if (e.action === 'admin.update') return `${e.targetId ? `#${e.targetId} ` : ''}${String(e.before?.role)}/${String(e.before?.state)} → ${String(after.role)}/${String(after.state)}`;
  if (e.action === 'admin.invite' || e.action === 'admin.create_cli') return `${String(after.email)} (${String(after.role)})`;
  if (e.action === 'user.sessions_revoke') return `пользователь ${e.targetId}${after.revoked !== undefined ? `, отозвано: ${String(after.revoked)}` : ''}`;
  return e.targetId ? `#${e.targetId}` : '';
}

export function SettingsJournal() {
  const { period } = usePrefs();
  const [adminId, setAdminId] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  const [result, setResult] = useState('');
  const admins = useLoad<{ items: AdminAccount[] }>('/api/admin/admins');
  const qs = new URLSearchParams({ days: String(period), admin: adminId, action: actionFilter, result });
  const { data, error, loading, reload } = useLoad<{ items: AdminEvent[] }>(`/api/admin/events?${qs}`);
  return (
    <>
      <PageHead crumb="Настройки" title="Журнал действий" subtitle="Кто, когда и почему изменил настройки или доступ" />
      <SettingsTabs />
      <div className="filters">
        <PeriodSelect />
        <select className="control" aria-label="Администратор" value={adminId} onChange={(e) => setAdminId(e.target.value)}>
          <option value="">Администратор: все</option>
          {admins.data?.items.map((a) => <option key={a.id} value={a.id}>{a.email}</option>)}
        </select>
        <select className="control" aria-label="Действие" value={actionFilter} onChange={(e) => setActionFilter(e.target.value)}>
          <option value="">Действие: все</option>
          <option value="settings.">Настройки</option><option value="admin.">Администраторы</option>
          <option value="user.">Пользователи</option><option value="auth.">Входы</option>
        </select>
        <select className="control" aria-label="Результат" value={result} onChange={(e) => setResult(e.target.value)}>
          <option value="">Результат: все</option><option value="done">Выполнено</option>
          <option value="requested">Запрошено</option><option value="completed">Завершено</option>
        </select>
      </div>
      <section className="section">
        <SectionHead title="История действий" action={data && <span className="small muted">Записей: {data.items.length}</span>} />
        {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading /> : data && (
          data.items.length === 0 ? (
            <Empty title="Действий пока нет">Каждая операция и соответствующая запись журнала выполняются в одной транзакции.</Empty>
          ) : (
            <table className="table">
              <thead><tr><th>Время</th><th>Администратор</th><th>Действие</th><th>Причина</th><th>Результат</th></tr></thead>
              <tbody>
                {data.items.map((e) => (
                  <tr key={e.id}>
                    <td>{localTime(e.at)}</td>
                    <td>{e.adminEmail ?? (e.action === 'admin.create_cli' ? 'CLI' : '—')}</td>
                    <td className="strong">{ACTIONS[e.action] ?? e.action}<span className="sub">{describe(e)}</span></td>
                    <td>{e.reason ?? '—'}</td>
                    <td><Status value={e.result} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}
      </section>
      <Note title="Неизменяемая история">Из панели нельзя удалить или отредактировать записи журнала: база запрещает изменение и удаление строк.</Note>
    </>
  );
}
