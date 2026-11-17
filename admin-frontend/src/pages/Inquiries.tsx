import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ApiError, api, errorText, JOB_ERROR, newIdempotencyKey, type InquiriesPage, type InquiryThread } from '../api';
import { localTime } from '../format';
import { can, useLoad, useMe } from '../state';
import { Card, DangerDialog, Empty, ErrorBox, Loading, PageHead, Tag, Toast } from '../ui';

const STATUS: Record<string, string> = { open: 'Открыто', closed: 'Закрыто' };
const DELIVERY: Record<string, string> = { queued: 'в очереди', sending: 'отправляется', sent: 'доставлено', failed: 'ошибка', expired: 'просрочено', cancelled: 'отменено' };

export function Inquiries() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const status = params.get('status') ?? '';
  const selected = params.get('user');
  const list = useLoad<InquiriesPage>(`/api/admin/inquiries${status ? `?status=${status}` : ''}`);
  const [toast, setToast] = useState<string | null>(null);
  const [retention, setRetention] = useState(false);
  const denied = list.error instanceof ApiError && list.error.code === 'dialogs_not_allowed';
  const pick = (patch: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) { if (v) next.set(k, v); else next.delete(k); }
    setParams(next, { replace: true });
  };

  return (
    <>
      <PageHead title="Обращения" subtitle="Входящие сообщения боту и история переписки — только с момента включения сбора"
        actions={me.admin.role === 'owner' && list.data ? <button type="button" className="btn" onClick={() => setRetention(true)}>Срок хранения: {list.data.retentionDays} дн.</button> : undefined} />
      {denied ? (
        <Empty title="Нет доступа к обращениям">Чтение переписки требует отдельного права «Диалоги», которое выдаёт владелец (Настройки → Роли и права).</Empty>
      ) : list.error ? <ErrorBox error={list.error} onRetry={list.reload} /> : !list.data ? <Loading rows={6} /> : !list.data.collected ? (
        <Empty title="Обращения не собираются">Бот сохраняет новые входящие сообщения только при <span className="mono">BOT_INQUIRIES_ENABLED=true</span>, после одноразового уведомления пользователю. Старые сообщения не собираются.</Empty>
      ) : (
        <div className="grid card-3">
          <section className="card" style={{ padding: 0 }} aria-label="Список обращений">
            <div className="chips" role="group" aria-label="Статус" style={{ padding: 12 }}>
              {[['', 'Все'], ['open', 'Открытые'], ['closed', 'Закрытые']].map(([k, label]) => (
                <button key={k} type="button" className="chip-btn" aria-pressed={status === k} onClick={() => pick({ status: k! })}>{label}</button>
              ))}
            </div>
            {list.data.items.length === 0 ? <Empty title="Обращений нет" /> : (
              <ul className="thread-list" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                {list.data.items.map((t) => (
                  <li key={t.userId}>
                    <button type="button" className={`thread-item${selected === String(t.userId) ? ' selected' : ''}`} style={{ width: '100%', textAlign: 'left', padding: '10px 14px' }}
                      aria-current={selected === String(t.userId)} onClick={() => pick({ user: String(t.userId) })}>
                      <b>{t.name ?? t.userId}</b>{t.username && <span className="muted"> @{t.username}</span>}
                      <Tag tone={t.status === 'open' ? 'accent' : 'neutral'}>{STATUS[t.status]}</Tag>
                      <div className="small muted">{localTime(t.lastAt)} · {t.count} сообщ.</div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
          {selected ? <Thread key={selected} userId={selected} onChanged={() => list.reload()} onToast={setToast} /> : <Empty title="Выберите обращение" />}
        </div>
      )}
      {retention && list.data && (
        <RetentionDialog current={list.data.retentionDays} onClose={() => setRetention(false)}
          onDone={(t) => { setRetention(false); setToast(t); list.reload(); }} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

function Thread({ userId, onChanged, onToast }: { userId: string; onChanged: () => void; onToast: (t: string) => void }) {
  const me = useMe();
  const manage = can(me.admin.role, 'manage_users');
  const { data, error, loading, reload } = useLoad<InquiryThread>(`/api/admin/inquiries/${encodeURIComponent(userId)}`);
  const [reply, setReply] = useState('');
  const [key, setKey] = useState(() => newIdempotencyKey());
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true); setFormError(null);
    try { await fn(); onToast(done); reload(); onChanged(); } catch (e) { setFormError(errorText(e)); } finally { setBusy(false); }
  };
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (loading && !data) return <Loading rows={6} />;
  if (!data) return null;
  const name = [data.user?.firstName, data.user?.lastName].filter(Boolean).join(' ') || userId;
  return (
    <Card title={<Link to={`/users/${userId}`}>{name}</Link>} sub={data.noticeAt ? `Уведомление о хранении показано ${localTime(data.noticeAt)}` : undefined}
      action={manage && (
        <button type="button" className="btn" disabled={busy} onClick={() => void act(() => api(`/api/admin/inquiries/${userId}/status`, { method: 'POST', body: { status: data.status === 'open' ? 'closed' : 'open' } }), data.status === 'open' ? 'Обращение закрыто' : 'Обращение открыто')}>
          {data.status === 'open' ? 'Закрыть' : 'Открыть снова'}
        </button>
      )}>
      {(data.truncated?.inbound || data.truncated?.outbound) && (
        <p className="small muted">Показаны последние сообщения: {data.truncated.inbound ? 'входящих — 200' : ''}{data.truncated.inbound && data.truncated.outbound ? ', ' : ''}{data.truncated.outbound ? 'наших — 50' : ''}. Более ранние хранятся до истечения срока.</p>
      )}
      <div className="timeline" aria-label="История переписки">
        {data.history.map((h) => (
          <div key={h.id} className={`bubble-box ${h.from === 'admin' ? 'out' : 'in'}`} style={{ marginLeft: h.from === 'admin' ? 40 : 0, marginRight: h.from === 'user' ? 40 : 0 }}>
            {h.text}
            <div className="small muted" style={{ marginTop: 4 }}>{localTime(h.at)} · {h.from === 'user' ? 'пользователь' : `${h.by ?? 'команда'} · ${DELIVERY[h.status ?? ''] ?? h.status ?? ''}${h.status === 'failed' && h.error ? `: ${JOB_ERROR[h.error] ?? h.error}` : ''}`}</div>
          </div>
        ))}
      </div>
      {manage && (
        <>
          <textarea className="control" aria-label="Ответ" placeholder="Ответ уйдёт от имени бота через очередь" maxLength={4000} value={reply}
            onChange={(e) => { setReply(e.target.value); setKey(newIdempotencyKey()); }} style={{ marginTop: 12 }} />
          {formError && <div className="form-error" role="alert">{formError}</div>}
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
            <button type="button" className="btn primary" disabled={busy || !reply.trim()}
              onClick={() => void act(async () => { await api(`/api/admin/users/${userId}/messages`, { method: 'POST', body: { body: reply, idempotencyKey: `msg-${key}` } }); setReply(''); setKey(newIdempotencyKey()); }, 'Ответ поставлен в очередь бота')}>Ответить</button>
          </div>
        </>
      )}
    </Card>
  );
}

function RetentionDialog({ current, onClose, onDone }: { current: number; onClose: () => void; onDone: (t: string) => void }) {
  const [days, setDays] = useState(String(current));
  return (
    <DangerDialog title="Срок хранения обращений" confirmLabel="Сохранить" tone="primary" onClose={onClose}
      lead="Сообщения старше срока удаляются автоматически (админкой и ботом); при уменьшении срока — сразу."
      onConfirm={async (input) => {
        const r = await api<{ removed?: number; replayed: boolean }>('/api/admin/inquiries-settings', { method: 'POST', body: { ...input, retentionDays: Number(days) } });
        onDone(r.replayed ? 'Уже сохранено ранее' : `Срок: ${days} дн. Удалено сообщений: ${r.removed ?? 0}`);
      }}>
      <div className="field" style={{ marginBottom: 20 }}>
        <label htmlFor="ret-days" className="sm">Дней (1–365)</label>
        <input id="ret-days" className="control" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ''))} />
      </div>
    </DangerDialog>
  );
}
