import { useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, errorText, type CrmProfile } from '../api';
import { num, userName, utc, utcDay } from '../format';
import { can, useLoad, useMe, usePrefs } from '../state';
import { Avatar, Card, DangerDialog, Empty, ErrorBox, Loading, Status, Tag, tagTone, Toast, Topbar } from '../ui';
import { CreditsDialog, TagDialog } from './dialogs';

/** Keyed by the id: moving to another user remounts the card, so no data or open dialog carries over. */
export function UserCardRoute() {
  const { id } = useParams();
  return <UserCard key={id} id={id ?? ''} />;
}

const TABS = ['Хронология', 'Сообщения', 'Кредиты', 'Сессии', 'Заметки'] as const;
const DOT: Record<string, string> = { registered: '★', login: '→', message: '✉', credits: '∞', note: 'З', admin: '#', ai: 'AI' };
const MSG_STATUS: Record<string, string> = { not_sent_pilot: 'не отправлено (пилот)', queued: 'в очереди', sent: 'отправлено', failed: 'ошибка' };

function UserCard({ id }: { id: string }) {
  const me = useMe();
  const { demo } = usePrefs();
  const loaded = useLoad<CrmProfile>(`/api/admin/users/${encodeURIComponent(id)}${demo ? '?demo=1' : ''}`);
  const { error, loading, reload } = loaded;
  const data = loaded.data && String(loaded.data.id) === id ? loaded.data : null;
  const [tab, setTab] = useState<typeof TABS[number]>('Хронология');
  const [dialog, setDialog] = useState<null | 'tag' | 'credits' | 'revoke' | 'block'>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const manage = can(me.admin.role, 'manage_users');
  const active = data?.activeSessions ?? 0;

  const act = async (fn: () => Promise<unknown>, done: string, reset: () => void) => {
    setBusy(true); setFormError(null);
    try { await fn(); reset(); setToast(done); reload(); } catch (e) { setFormError(errorText(e)); } finally { setBusy(false); }
  };

  return (
    <>
      <Topbar />
      <div className="crumbs" style={{ margin: '18px 0 14px' }}><Link to="/users">← Пользователи</Link>{data && <>&nbsp;&nbsp;/&nbsp;&nbsp;{userName(data)}</>}</div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={8} /> : data && (
        <>
          <section className="card hero" aria-label="Профиль">
            <Avatar name={userName(data)} large />
            <div style={{ flex: 1 }}>
              <h2>{userName(data)}
                {data.tags.map((t) => <Tag key={t} tone={tagTone(t)} onRemove={manage ? () => void act(() => api(`/api/admin/users/${data.id}/tags/remove`, { method: 'POST', body: { tag: t } }), `Тег «${t}» снят`, () => {}) : undefined}>{t}</Tag>)}
                {data.blocked ? <Tag tone="danger">Заблокирован</Tag> : active > 0 ? <Tag tone="success">Активна сессия</Tag> : null}
              </h2>
              <div className="small muted" style={{ marginTop: 6 }}>
                {data.username ? `@${data.username}` : 'без username'} · Telegram ID <span className="mono">{data.id}</span> · язык {data.lang} · с нами с {utcDay(data.createdAt)}
              </div>
            </div>
            <div className="row">
              <button type="button" className="btn primary" disabled={!manage} onClick={() => composer.current?.focus()}>Написать</button>
              <button type="button" className="btn" disabled={!manage} onClick={() => setDialog('credits')}>Кредиты</button>
              <button type="button" className="btn" disabled={!manage} onClick={() => setDialog('tag')}>Тег</button>
            </div>
          </section>
          <div className="stats-strip">
            <div className="stat"><div className="k">Последний вход</div><div className="v">{utc(data.lastLoginAt)}</div></div>
            <div className="stat"><div className="k">Активных сессий</div><div className="v">{active}</div></div>
            <div className="stat"><div className="k">Кредиты</div><div className="v" style={{ color: 'var(--demo)' }}>{data.credits.unlimitedUntil ? '∞ безлимит' : num(data.credits.balance)}</div></div>
            <div className="stat"><div className="k">Сообщений от нас</div><div className="v">{data.messages.length}</div></div>
            <div className="stat"><div className="k">AI-разборов</div><div className="v">{data.aiCalls ? data.aiCalls.length : '—'}</div></div>
            <div className="stat"><div className="k">Заметок</div><div className="v">{data.notes.length}</div></div>
          </div>
          <div className="grid card-3">
            <section className="card" style={{ padding: 0 }} aria-label="История">
              <div className="tabs inner" role="tablist">
                {TABS.map((t) => <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{t}</button>)}
              </div>
              <div style={{ padding: 20 }}>
                {tab === 'Хронология' && (data.timeline.length === 0 ? <Empty title="Событий пока нет" /> : (
                  <div className="timeline">
                    {data.timeline.map((e, i) => (
                      <div className="tl-item" key={i}>
                        <span className={`tl-dot ${e.kind}`} aria-hidden="true">{DOT[e.kind] ?? '•'}</span>
                        <div>
                          <div className="tl-title">{e.title}<span className="tl-time">{utc(e.at)}</span>{e.demo && <> <Tag tone="violet">ДЕМО</Tag></>}</div>
                          {e.kind === 'message' ? <div className="bubble-box">{e.detail}<div className="small muted" style={{ marginTop: 4 }}>{e.by} · {MSG_STATUS[e.status ?? ''] ?? e.status}</div></div>
                            : e.detail && <div className="tl-detail">{e.detail}{e.by && <span className="muted"> · {e.by}</span>}</div>}
                        </div>
                      </div>
                    ))}
                  </div>
                ))}
                {tab === 'Сообщения' && (data.messages.length === 0 ? <Empty title="Сообщений пользователю не было" /> : data.messages.map((m) => (
                  <div className="bubble-box" key={m.id}>{m.body}<div className="small muted" style={{ marginTop: 4 }}>{utc(m.at)} · {m.by} · {MSG_STATUS[m.status]}</div></div>
                )))}
                {tab === 'Кредиты' && (data.ledger.length === 0 ? <Empty title="Операций с кредитами не было" /> : (
                  <table className="table"><thead><tr><th>Время</th><th>Операция</th><th className="num">Сумма</th><th className="num">Баланс</th><th>Причина</th></tr></thead>
                    <tbody>{data.ledger.map((l) => (
                      <tr key={l.id}><td>{utc(l.at)}</td><td><Tag tone={l.kind === 'debit' ? 'danger' : l.kind.startsWith('unlimited') ? 'violet' : 'success'}>{{ grant: 'Начисление', debit: 'Списание', unlimited: 'Безлимит', unlimited_off: 'Безлимит выкл.' }[l.kind]}</Tag></td>
                        <td className="num">{l.kind.startsWith('unlimited') ? '∞' : l.delta > 0 ? `+${l.delta}` : l.delta}</td><td className="num">{l.balanceAfter}</td><td>{l.reason}<span className="sub">{l.by}</span></td></tr>
                    ))}</tbody></table>
                ))}
                {tab === 'Сессии' && (data.sessions.length === 0 ? <Empty title="Сессий нет" /> : (
                  <table className="table"><thead><tr><th>Создана (UTC)</th><th>Истекает</th><th>Отозвана</th><th>Состояние</th></tr></thead>
                    <tbody>{data.sessions.map((s, i) => (<tr key={i}><td>{utc(s.createdAt)}</td><td>{utc(s.expiresAt)}</td><td>{utc(s.revokedAt)}</td><td><Status value={s.state} /></td></tr>))}</tbody></table>
                ))}
                {tab === 'Заметки' && (data.notes.length === 0 ? <Empty title="Заметок нет" /> : data.notes.map((n) => (
                  <div className="note-card" key={n.id} style={{ marginBottom: 8 }}>{n.body}<div className="by">{n.by} · {utc(n.at)}</div></div>
                )))}
              </div>
            </section>
            <div>
              <Card title="Написать в Telegram" action={<Tag>от имени бота</Tag>}>
                <textarea ref={composer} className="control" aria-label="Текст сообщения" placeholder="Текст сообщения…" maxLength={4000}
                  value={message} onChange={(e) => setMessage(e.target.value)} disabled={!manage} />
                <div className="tags" style={{ margin: '10px 0' }}>
                  {['Здравствуйте! Мы уже разбираемся с проблемой 🙏', 'Спасибо, что пользуетесь SporBot! Для вас новый бонус.'].map((t) => (
                    <button key={t} type="button" className="tag" style={{ cursor: 'pointer' }} onClick={() => setMessage(t)} disabled={!manage}>{t.slice(0, 22)}…</button>
                  ))}
                </div>
                {formError && <div className="form-error" role="alert">{formError}</div>}
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <span className="small muted">Пилот: сохраняется, но не отправляется</span>
                  <button type="button" className="btn primary" disabled={!manage || busy || !message.trim()}
                    onClick={() => void act(() => api(`/api/admin/users/${data.id}/messages`, { method: 'POST', body: { body: message } }), 'Сообщение записано (в пилоте не отправляется)', () => setMessage(''))}>Отправить</button>
                </div>
              </Card>
              <Card title="Кредиты" action={<Tag tone="warning">выключены</Tag>}>
                <div className="kv-row"><span>Баланс</span><b style={{ color: 'var(--demo)' }}>{data.credits.unlimitedUntil ? `∞ до ${utcDay(data.credits.unlimitedUntil)}` : num(data.credits.balance)}</b></div>
                <div className="kv-row"><span>Операций</span><b>{data.ledger.length}</b></div>
                <div className="row" style={{ marginTop: 10 }}>
                  <button type="button" className="btn" disabled={!manage} onClick={() => setDialog('credits')}>Начислить / списать</button>
                </div>
                <p className="small muted" style={{ marginBottom: 0 }}>Пока кредиты выключены, операции пишутся только в журнал и не влияют на доступ к прогнозам.</p>
              </Card>
              <Card title="Заметки команды">
                {data.notes.slice(0, 2).map((n) => <div className="note-card" key={n.id} style={{ marginBottom: 8 }}>{n.body}<div className="by">{n.by} · {utc(n.at)}</div></div>)}
                <textarea className="control" style={{ minHeight: 60 }} aria-label="Новая заметка" placeholder="Новая заметка (видна только команде)" maxLength={2000}
                  value={note} onChange={(e) => setNote(e.target.value)} disabled={!manage} />
                <div className="row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
                  <button type="button" className="btn" disabled={!manage || busy || !note.trim()}
                    onClick={() => void act(() => api(`/api/admin/users/${data.id}/notes`, { method: 'POST', body: { body: note } }), 'Заметка добавлена', () => setNote(''))}>+ заметка</button>
                </div>
              </Card>
              <Card title="Доступ и безопасность">
                <div className="kv-row"><span>Активных сессий</span><b>{active}</b></div>
                {data.blocked && <div className="kv-row"><span>Заблокирован</span><b>{utc(data.blocked.at)} · {data.blocked.reason}</b></div>}
                <div className="row" style={{ marginTop: 10 }}>
                  <button type="button" className="btn" disabled={!manage || active === 0} onClick={() => setDialog('revoke')}>Отозвать сессии</button>
                  <button type="button" className={`btn ${data.blocked ? '' : 'danger'}`} disabled={!manage} onClick={() => setDialog('block')}>{data.blocked ? 'Разблокировать' : 'Заблокировать'}</button>
                </div>
                <p className="small muted" style={{ marginBottom: 0 }}>Опасные действия — с причиной и повторной 2FA. Блокировка в пилоте: флаг и выход со всех устройств.</p>
              </Card>
            </div>
          </div>
        </>
      )}
      {dialog === 'tag' && data && (
        <TagDialog count={1} onClose={() => setDialog(null)} onSave={async (t) => {
          await api(`/api/admin/users/${data.id}/tags`, { method: 'POST', body: { tag: t } }); setDialog(null); setToast(`Тег «${t}» добавлен`); reload();
        }} />
      )}
      {dialog === 'credits' && data && <CreditsDialog userIds={[data.id]} onClose={() => setDialog(null)} onDone={(t) => { setDialog(null); setToast(t); reload(); }} />}
      {dialog === 'revoke' && data && (
        <DangerDialog title="Отозвать все сессии?" confirmLabel="Отозвать сессии" onClose={() => setDialog(null)}
          lead={<>Пользователь <span className="mono">{data.id}</span> выйдет из Mini App на всех устройствах. Это действие будет записано в журнал.</>}
          onConfirm={async (input) => {
            const r = await api<{ after: { revoked: number } | null; replayed: boolean }>(`/api/admin/users/${data.id}/sessions/revoke`, { method: 'POST', body: input });
            setDialog(null); setToast(r.replayed ? 'Действие уже было выполнено ранее' : `Отозвано сессий: ${r.after?.revoked ?? 0}`); reload();
          }} />
      )}
      {dialog === 'block' && data && (
        <DangerDialog title={data.blocked ? 'Снять блокировку?' : 'Заблокировать пользователя?'} confirmLabel={data.blocked ? 'Разблокировать' : 'Заблокировать'}
          tone={data.blocked ? 'primary' : 'danger'} onClose={() => setDialog(null)}
          lead={data.blocked ? 'Пользователь снова сможет входить.' : <>Пользователь <span className="mono">{data.id}</span> будет отмечен как заблокированный и выйдет на всех устройствах.</>}
          onConfirm={async (input) => {
            await api(`/api/admin/users/${data.id}/block`, { method: 'POST', body: { ...input, blocked: !data.blocked } });
            setDialog(null); setToast(data.blocked ? 'Блокировка снята' : 'Пользователь заблокирован'); reload();
          }} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}
