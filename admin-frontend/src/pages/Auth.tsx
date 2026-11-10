import { useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent } from 'react';
import { api, ApiError, errorText } from '../api';
import { useAuth } from '../state';

function Frame({ children, foot }: { children: React.ReactNode; foot: string }) {
  return (
    <div className="auth">
      <div className="auth-brand">
        <div className="brand-name">SporBot</div>
        <div className="brand-sub">Панель администратора</div>
      </div>
      <div className="auth-card">{children}</div>
      <div className="auth-foot">{foot}</div>
    </div>
  );
}

export function LoginFlow() {
  const [stage, setStage] = useState<'password' | 'totp'>('password');
  const [notice, setNotice] = useState<string | null>(null);
  if (stage === 'totp') {
    return <TwoFactor onBack={(message) => { setNotice(message); setStage('password'); }} />;
  }
  return <Login notice={notice} onNext={() => { setNotice(null); setStage('totp'); }} />;
}

function Login({ onNext, notice }: { onNext: () => void; notice: string | null }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(notice);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api('/api/admin/auth/login', { method: 'POST', body: { email, password } });
      onNext();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };
  return (
    <Frame foot="Локальный пилот: доступен только с этого компьютера (127.0.0.1). Вход не связан с Telegram.">
      <form onSubmit={submit} noValidate>
        <h1>Вход в систему</h1>
        <p className="lead">Используйте личную учётную запись администратора</p>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="field">
          <label htmlFor="email">Электронная почта</label>
          <input id="email" className="control" type="email" autoComplete="username" placeholder="name@example.com"
            value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        </div>
        <div className="field">
          <label htmlFor="password">Пароль</label>
          <input id="password" className="control" type="password" autoComplete="current-password"
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <p className="field-hint" style={{ margin: '12px 0 44px', maxWidth: 320 }}>
          После пароля потребуется код двухфакторной аутентификации.
        </p>
        <button type="submit" className="btn primary block" disabled={busy || !email || !password}>
          {busy ? 'Проверяем…' : 'Продолжить'}
        </button>
      </form>
    </Frame>
  );
}

function TwoFactor({ onBack }: { onBack: (message: string | null) => void }) {
  const { refresh } = useAuth();
  const [digits, setDigits] = useState<string[]>(Array(6).fill(''));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  const code = digits.join('');

  const setAt = (i: number, value: string) => {
    const d = value.replace(/\D/g, '').slice(-1);
    setDigits((prev) => prev.map((x, j) => (j === i ? d : x)));
    if (d && i < 5) refs.current[i + 1]?.focus();
  };
  const onKey = (i: number, e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Backspace' && !digits[i] && i > 0) refs.current[i - 1]?.focus();
  };
  const onPaste = (e: ClipboardEvent<HTMLInputElement>) => {
    const text = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, 6);
    if (text.length > 1) {
      e.preventDefault();
      setDigits(Array.from({ length: 6 }, (_, i) => text[i] ?? ''));
      refs.current[Math.min(text.length, 5)]?.focus();
    }
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (code.length !== 6) return;
    setBusy(true);
    setError(null);
    try {
      await api('/api/admin/auth/totp', { method: 'POST', body: { code } });
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && (err.code === 'expired' || err.code === 'locked')) {
        onBack(errorText(err));
        return;
      }
      setError(errorText(err));
      setDigits(Array(6).fill(''));
      refs.current[0]?.focus();
      setBusy(false);
    }
  };
  return (
    <Frame foot="Обязательная 2FA для каждой учётной записи">
      <form onSubmit={submit}>
        <h1>Подтверждение входа</h1>
        <p className="lead">Введите шестизначный код из приложения аутентификации.</p>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="otp" role="group" aria-label="Код из 6 цифр">
          {digits.map((d, i) => (
            <input key={i} ref={(el) => { refs.current[i] = el; }} value={d} inputMode="numeric"
              autoComplete={i === 0 ? 'one-time-code' : 'off'} aria-label={`Цифра ${i + 1}`} maxLength={1}
              autoFocus={i === 0} onChange={(e) => setAt(i, e.target.value)} onKeyDown={(e) => onKey(i, e)}
              onPaste={onPaste} />
          ))}
        </div>
        <button type="submit" className="btn primary block" disabled={busy || code.length !== 6}>
          {busy ? 'Проверяем…' : 'Подтвердить'}
        </button>
        <p style={{ marginTop: 28 }}>
          <button type="button" className="btn link" onClick={() => onBack(null)}>← Назад к вводу пароля</button>
        </p>
      </form>
    </Frame>
  );
}
