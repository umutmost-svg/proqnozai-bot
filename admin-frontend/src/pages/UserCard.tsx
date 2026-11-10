import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, type UserProfile } from '../api';
import { usd, userName, utc, utcDay } from '../format';
import { can, useLoad, useMe, usePrefs } from '../state';
import { DangerDialog, Empty, ErrorBox, Loading, Note, PageHead, SectionHead, Status, Toast } from '../ui';

export function UserCard() {
  const { id } = useParams();
  const navigate = useNavigate();
  const me = useMe();
  const { demo } = usePrefs();
  const { data, error, loading, reload } = useLoad<UserProfile>(`/api/admin/users/${encodeURIComponent(id ?? '')}${demo ? '?demo=1' : ''}`);
  const [confirm, setConfirm] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const canRevoke = can(me.admin.role, 'manage_users');
  const active = data?.sessions.filter((s) => s.state === 'active').length ?? 0;

  return (
    <>
      <PageHead crumb="Пользователи" title="Карточка пользователя" subtitle="Данные аккаунта и безопасные действия" />
      <button type="button" className="btn" style={{ minWidth: 140, marginBottom: 40 }} onClick={() => navigate(-1)}>← К списку</button>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : data && (
        <>
          <SectionHead title="Профиль пользователя" />
          <div className="profile">
            <div className="cell"><div className="k">Telegram ID</div><div className="v mono">{data.id}</div></div>
            <div className="cell"><div className="k">Дата регистрации</div><div className="v">{utcDay(data.createdAt)}</div></div>
            <div className="cell"><div className="k">Имя и username</div><div className="v">{userName(data)}{data.username ? ` · @${data.username}` : ''}</div></div>
            <div className="cell"><div className="k">Язык интерфейса</div><div className="v">{data.lang}</div></div>
            <div className="cell"><div className="k">Последний вход</div><div className="v">{utc(data.lastLoginAt)}</div></div>
            <div className="cell"><div className="k">Активность за 30 дней</div><div className="v">— <span className="small muted">после D1</span></div></div>
            <div className="cell"><div className="k">Кредиты</div><div className="v">∞ <span className="chip sm">выключено</span></div></div>
            <div className="cell"><div className="k">Активные сессии</div><div className="v">{active}</div></div>
          </div>

          <div className="cols">
            <div>
              <section className="section">
                <SectionHead title="Сессии и безопасность" />
                <p style={{ fontSize: 14, color: 'var(--text)', margin: '18px 0' }}>
                  Отзыв сессий — опасное действие: потребуются повторная 2FA и причина.
                </p>
                <button type="button" className="btn" disabled={!canRevoke || active === 0} onClick={() => setConfirm(true)}
                  title={!canRevoke ? 'Недоступно для роли аналитика' : active === 0 ? 'Нет активных сессий' : undefined}>
                  Отозвать сессии
                </button>
                {data.sessions.length === 0 ? <Empty title="Сессий нет" /> : (
                  <table className="table" style={{ marginTop: 18 }}>
                    <thead><tr><th>Создана (UTC)</th><th>Истекает</th><th>Отозвана</th><th>Состояние</th></tr></thead>
                    <tbody>
                      {data.sessions.map((s, i) => (
                        <tr key={i}>
                          <td>{utc(s.createdAt)}</td><td>{utc(s.expiresAt)}</td><td>{utc(s.revokedAt)}</td>
                          <td><Status value={s.state} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
              <section className="section">
                <SectionHead title="AI-вызовы пользователя" action={data.aiCalls ? <span className="chip demo sm">ДЕМО</span> : undefined} />
                {data.aiCalls === null ? (
                  <Empty title="Журнал AI-вызовов появится после D1">Время, исход и стоимость — без содержимого запросов.</Empty>
                ) : data.aiCalls.length === 0 ? <Empty title="Вызовов за 30 дней нет" /> : (
                  <table className="table">
                    <thead><tr><th>Время UTC</th><th>Модель</th><th className="num">Стоимость</th><th>Результат</th></tr></thead>
                    <tbody>
                      {data.aiCalls.map((c) => (
                        <tr key={c.id}><td>{utc(c.at)}</td><td>{c.model}</td><td className="num">{usd(c.costUsd)}</td>
                          <td><Status value={c.result} label={c.result === 'delivered' || c.result === 'not_delivered' ? undefined : c.result} /></td></tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
              <section className="section">
                <SectionHead title="Действия администраторов" />
                {data.events.length === 0 ? <Empty title="Действий по этому пользователю не было" /> : (
                  <table className="table">
                    <thead><tr><th>Время UTC</th><th>Администратор</th><th>Действие</th><th>Причина</th><th>Результат</th></tr></thead>
                    <tbody>
                      {data.events.map((e) => (
                        <tr key={e.id}><td>{utc(e.at)}</td><td>{e.adminEmail ?? '—'}</td><td>Отзыв сессий{e.after ? ` (${String(e.after.revoked)})` : ''}</td>
                          <td>{e.reason ?? '—'}</td><td><Status value={e.result} /></td></tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
            </div>
            <aside>
              <Note title="Доступ к действию">Разрешения зависят от роли администратора. Все действия попадают в журнал.</Note>
            </aside>
          </div>
        </>
      )}
      {confirm && data && (
        <DangerDialog
          title="Отозвать все сессии?"
          lead="Пользователь выйдет из Mini App на всех устройствах. Это действие будет записано в журнал."
          confirmLabel="Отозвать сессии"
          onClose={() => setConfirm(false)}
          onConfirm={async (input) => {
            const result = await api<{ after: { revoked: number } | null; replayed: boolean }>(
              `/api/admin/users/${data.id}/sessions/revoke`, { method: 'POST', body: input });
            setConfirm(false);
            setToast(result.replayed ? 'Действие уже было выполнено ранее' : `Отозвано сессий: ${result.after?.revoked ?? 0}`);
            reload();
          }}
        />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}
