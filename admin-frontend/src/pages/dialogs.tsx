import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, errorText } from '../api';
import { DangerDialog } from '../ui';

/** Simple (non-dangerous) modal with one text field: tags. Audited, no 2FA. */
export function TagDialog({ count, onSave, onClose }: { count: number; onSave: (tag: string) => Promise<void>; onClose: () => void }) {
  const [tag, setTag] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!tag.trim() || busy) return;
    setBusy(true);
    try { await onSave(tag.trim()); } catch (err) { setError(errorText(err)); setBusy(false); }
  };
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="tag-title" onSubmit={submit} style={{ width: 480 }}>
        <h2 id="tag-title">Добавить тег</h2>
        <p className="lead">{count === 1 ? 'Тег появится в карточке и фильтрах.' : `Тег получат выбранные пользователи: ${count}.`} Действие записывается в журнал.</p>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="field">
          <label htmlFor="tag-input">Тег</label>
          <input id="tag-input" ref={ref} className="control" maxLength={32} value={tag} placeholder="Например: VIP" onChange={(e) => setTag(e.target.value)} />
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>Отмена</button>
          <button type="submit" className="btn primary" disabled={!tag.trim() || busy}>Добавить</button>
        </div>
      </form>
    </div>
  );
}

/** Credits change for one or many users: grant, debit or unlimited, with reason + 2FA. */
export function CreditsDialog({ userIds, onDone, onClose, initialKind = 'grant', groupName }: {
  userIds: number[]; onDone: (text: string) => void; onClose: () => void; initialKind?: 'grant' | 'debit' | 'unlimited';
  /** The segment the ids came from: named in the title, so a stale selection cannot pass unnoticed. */
  groupName?: string;
}) {
  const [kind, setKind] = useState<'grant' | 'debit' | 'unlimited'>(initialKind);
  const [amount, setAmount] = useState('10');
  const [days, setDays] = useState('30');
  return (
    <DangerDialog
      title={userIds.length === 1 && !groupName ? 'Кредиты пользователя' : `Кредиты: ${groupName ? `сегмент «${groupName}», ` : ''}${userIds.length} пользователей`}
      lead="Кредиты выключены: операция записывается в журнал, но не влияет на доступ к прогнозам."
      confirmLabel="Провести операцию" tone="primary" onClose={onClose}
      onConfirm={async (input) => {
        const body = { ...input, userIds, kind, amount: Number(amount), days: kind === 'unlimited' ? Number(days) : 0 };
        await api('/api/admin/credits/change', { method: 'POST', body });
        onDone(kind === 'unlimited' ? (Number(days) ? `Безлимит на ${days} дн. включён` : 'Безлимит выключен')
          : `${kind === 'grant' ? 'Начислено' : 'Списано'} ${amount} × ${userIds.length}`);
      }}>
      <div className="field-row" style={{ gridTemplateColumns: '1fr 1fr', marginBottom: 20 }}>
        <div className="field">
          <label htmlFor="cr-kind" className="sm">Операция</label>
          <select id="cr-kind" className="control" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            <option value="grant">Начислить</option><option value="debit">Списать</option><option value="unlimited">Безлимит</option>
          </select>
        </div>
        {kind === 'unlimited' ? (
          <div className="field">
            <label htmlFor="cr-days" className="sm">Дней (0 — выключить)</label>
            <input id="cr-days" className="control" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ''))} />
          </div>
        ) : (
          <div className="field">
            <label htmlFor="cr-amount" className="sm">Сумма</label>
            <input id="cr-amount" className="control" inputMode="numeric" value={amount} onChange={(e) => setAmount(e.target.value.replace(/\D/g, ''))} />
          </div>
        )}
      </div>
    </DangerDialog>
  );
}
