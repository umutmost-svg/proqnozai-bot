import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError, type MbLink, type UserRow } from '../api';
import { localTime } from '../format';
import { useLoad, useMe } from '../state';
import { DangerDialog, Empty, ErrorBox, Loading, PageHead, Toast } from '../ui';

type Item = MbLink & { user: UserRow | null };
const STATES = [['pending', 'На проверке'], ['approved', 'Подтверждены'], ['rejected', 'Отклонены']] as const;

/** MB IDs users entered in the Mini App: the owner approves them personally (docs/miniapp/credits.md). */
export function MbQueue() {
  const me = useMe();
  const owner = me.admin.role === 'owner';
  const [state, setState] = useState<string>('pending');
  const { data, error, loading, reload } = useLoad<{ items: Item[]; counts: Record<string, number> }>(`/api/admin/mb?state=${state}`);
  const [busy, setBusy] = useState<number | null>(null);
  const [rejecting, setRejecting] = useState<Item | null>(null);
  const [approving, setApproving] = useState<Item | null>(null);
  const [reason, setReason] = useState('');
  const [toast, setToast] = useState<string | null>(null);

  /** An approval needs a fresh 2FA code (the dialog shows a wrong one and keeps itself open). */
  const approve = async (item: Item, code: string) => {
    try {
      await api(`/api/admin/mb/${item.userId}/decide`, {
        method: 'POST', body: { decision: 'approve', mbId: item.mbId, submission: item.submission, code },
      });
    } catch (err) {
      if (err instanceof ApiError && (err.code === 'mb_stale' || err.code === 'mb_id_taken')) {
        setApproving(null);
        setToast(err.code === 'mb_stale' ? 'Пользователь уже сменил MB ID — обновите список' : 'Этот MB ID уже у другого пользователя');
        reload();
        return;
      }
      throw err;
    }
    setApproving(null);
    setToast(`MB ID ${item.mbId} подтверждён`);
    reload();
  };

  const decide = async (item: Item, approve: boolean, why?: string) => {
    setBusy(item.userId);
    try {
      await api(`/api/admin/mb/${item.userId}/decide`, {
        method: 'POST',
        body: { decision: approve ? 'approve' : 'reject', mbId: item.mbId, submission: item.submission, reason: why ?? null },
      });
      setToast(approve ? `MB ID ${item.mbId} подтверждён` : `MB ID ${item.mbId} отклонён`);
      setRejecting(null);
      setReason('');
      reload();
    } catch (err) {
      const code = err instanceof Error ? err.message : '';
      setToast(code.includes('mb_stale') ? 'Пользователь уже сменил MB ID — обновите список' : code.includes('mb_id_taken') ? 'Этот MB ID уже у другого пользователя' : 'Не удалось сохранить решение');
      reload();
    } finally {
      setBusy(null);
    }
  };

  const who = (i: Item) => i.user ? [i.user.firstName, i.user.lastName].filter(Boolean).join(' ') || (i.user.username ? `@${i.user.username}` : String(i.userId)) : String(i.userId);

  return (
    <>
      <PageHead title="MB ID" subtitle="Пользователи вводят MB ID в Mini App; подтверждает владелец. Решение применяется сразу" />
      <div className="chips" role="group" aria-label="Статус" style={{ marginBottom: 16 }}>
        {STATES.map(([k, l]) => (
          <button key={k} type="button" className="chip-btn" aria-pressed={state === k} onClick={() => setState(k)}>
            {l}{data ? ` (${data.counts[k] ?? 0})` : ''}
          </button>
        ))}
      </div>
      {!owner && <p className="small muted">Подтверждать и отклонять MB ID может только владелец.</p>}
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={4} /> : data && (
        data.items.length === 0 ? <Empty title={state === 'pending' ? 'Заявок на проверку нет' : 'Пусто'} /> : (
          <div className="table-card">
            <table className="table">
              <thead><tr><th>Пользователь</th><th>MB ID</th><th>Отправлен</th>{state !== 'pending' && <th>Решение</th>}{state === 'pending' && owner && <th>Действия</th>}</tr></thead>
              <tbody>{data.items.map((i) => (
                <tr key={i.userId}>
                  <td><Link to={`/users/${i.userId}`}>{who(i)}</Link><span className="sub mono">{i.userId}</span></td>
                  <td className="mono strong">{i.mbId}</td>
                  <td>{localTime(i.submittedAt)}</td>
                  {state !== 'pending' && <td>{localTime(i.decidedAt)}{i.reason && <span className="sub">{i.reason}</span>}</td>}
                  {state === 'pending' && owner && (
                    <td>
                      <button type="button" className="btn primary" disabled={busy === i.userId} onClick={() => setApproving(i)}>Подтвердить</button>{' '}
                      <button type="button" className="btn" disabled={busy === i.userId} onClick={() => { setRejecting(i); setReason(''); }}>Отклонить</button>
                    </td>
                  )}
                </tr>
              ))}</tbody>
            </table>
          </div>
        )
      )}
      {rejecting && (
        // A modal, in view whatever the table's length (CRM audit B2: it opened below up to 500 rows).
        <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && busy === null) setRejecting(null); }}>
          <form className="modal" role="dialog" aria-modal="true" aria-labelledby="mb-reject-title"
            onSubmit={(e) => { e.preventDefault(); if (reason.trim().length >= 3 && busy === null) void decide(rejecting, false, reason.trim()); }}>
            <h2 id="mb-reject-title">Отклонить MB ID {rejecting.mbId}</h2>
            <div className="field">
              <label htmlFor="mb-reason">Причина (её увидит пользователь)</label>
              <input id="mb-reason" className="control" maxLength={200} value={reason} autoFocus onChange={(e) => setReason(e.target.value)} />
            </div>
            <div className="modal-actions">
              <button type="button" className="btn" disabled={busy !== null} onClick={() => setRejecting(null)}>Отмена</button>
              <button type="submit" className="btn danger" disabled={reason.trim().length < 3 || busy !== null}>Отклонить</button>
            </div>
          </form>
        </div>
      )}
      {approving && (
        <DangerDialog title="Подтвердить MB ID?" tone="primary" confirmLabel="Подтвердить" noReason
          lead={<>MB ID <b className="mono">{approving.mbId}</b> станет подтверждённым у пользователя {who(approving)}. Нужен код 2FA.</>}
          onClose={() => setApproving(null)} onConfirm={({ code }) => approve(approving, code)} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}
