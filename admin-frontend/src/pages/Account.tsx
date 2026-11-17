import { useState } from 'react';
import { api, errorText, ApiError } from '../api';
import { useMe } from '../state';
import { Card, Note, PageHead, Toast } from '../ui';

/** Own account: change the password (current password + 2FA; other sessions end) and sign out elsewhere. */
export function Account() {
  const me = useMe();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const mismatch = repeat.length > 0 && next !== repeat;
  const valid = current.length > 0 && next.length >= 12 && next === repeat && /^\d{6}$/.test(code);

  const change = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true); setError(null);
    try {
      const r = await api<{ revokedSessions: number }>('/api/admin/auth/password', { method: 'POST', body: { current, new: next, code } });
      setCurrent(''); setNext(''); setRepeat(''); setCode('');
      setToast(r.revokedSessions ? `Пароль изменён. Завершено других сессий: ${r.revokedSessions}` : 'Пароль изменён');
    } catch (err) {
      setError(errorText(err));
      if (err instanceof ApiError && (err.code === 'invalid_code' || err.code === 'invalid_password')) setCode('');
    } finally {
      setBusy(false);
    }
  };

  const signOutElsewhere = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api<{ revoked: number }>('/api/admin/auth/sessions/revoke-others', { method: 'POST', body: {} });
      setToast(r.revoked ? `Завершено других сессий: ${r.revoked}` : 'Других активных сессий нет');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead crumb="Учётная запись" title="Безопасность" subtitle={me.admin.email} />
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="cols" style={{ gridTemplateColumns: 'minmax(0, 520px) minmax(0, 1fr)', gap: 40 }}>
        <Card title="Сменить пароль">
          <form onSubmit={change}>
            <div className="field"><label htmlFor="pw-current">Текущий пароль</label>
              <input id="pw-current" className="control" type="password" autoComplete="current-password" value={current}
                onChange={(e) => setCurrent(e.target.value)} /></div>
            <div className="field"><label htmlFor="pw-new">Новый пароль</label>
              <input id="pw-new" className="control" type="password" autoComplete="new-password" maxLength={200} value={next}
                onChange={(e) => setNext(e.target.value)} />
              <span className="field-hint">От 12 символов.</span></div>
            <div className="field"><label htmlFor="pw-repeat">Повторите новый пароль</label>
              <input id="pw-repeat" className="control" type="password" autoComplete="new-password" maxLength={200} value={repeat}
                onChange={(e) => setRepeat(e.target.value)} />
              {mismatch && <span className="field-hint" style={{ color: 'var(--danger, #d33)' }}>Пароли не совпадают</span>}</div>
            <div className="field"><label htmlFor="pw-code">Код двухфакторной аутентификации</label>
              <input id="pw-code" className="control" inputMode="numeric" autoComplete="one-time-code" placeholder="6 цифр" maxLength={6}
                value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} /></div>
            <button type="submit" className="btn primary" disabled={!valid || busy}>{busy ? 'Выполняется…' : 'Сменить пароль'}</button>
          </form>
        </Card>
        <div>
          <Card title="Другие устройства">
            <p className="small" style={{ marginTop: 0 }}>Завершает все сессии этой учётной записи, кроме текущей. После смены пароля
              это происходит автоматически.</p>
            <button type="button" className="btn" disabled={busy} onClick={() => void signOutElsewhere()}>Выйти на всех других устройствах</button>
          </Card>
          <Note title="Потеряли телефон с 2FA?">Попросите владельца сбросить вход: он выдаст новую ссылку, по которой вы зададите
            новый пароль и подключите 2FA заново.</Note>
        </div>
      </div>
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}
