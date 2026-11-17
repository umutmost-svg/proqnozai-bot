import { createContext, useContext, useEffect, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent } from 'react';
import { api, ApiError, errorText } from '../api';
import { useAuth } from '../state';

// Before sign-in the mode is not known yet: the local pilot is served on loopback names only.
const LOOPBACK = new Set(['127.0.0.1', 'localhost']);

// The session ended while a page was open: the same sign-in, in a dialog over the page (its unsaved text stays).
const EmbeddedContext = createContext(false);

function Frame({ children, foot }: { children: React.ReactNode; foot: string }) {
  if (useContext(EmbeddedContext)) {
    return (
      <div className="overlay" style={{ zIndex: 40 }}>  {/* above any dialog of the page underneath */}
        <div className="modal" role="dialog" aria-modal="true" aria-label="Сессия истекла">
          <div className="form-error" role="status" style={{ marginBottom: 12 }}>Сессия истекла. Войдите снова — открытая страница и
            несохранённый текст останутся.</div>
          {children}
        </div>
      </div>
    );
  }
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

export function LoginFlow({ embedded = false }: { embedded?: boolean }) {
  const [stage, setStage] = useState<'password' | 'totp'>('password');
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <EmbeddedContext.Provider value={embedded}>
      {stage === 'totp'
        ? <TwoFactor onBack={(message) => { setNotice(message); setStage('password'); }} />
        : <Login notice={notice} onNext={() => { setNotice(null); setStage('totp'); }} />}
    </EmbeddedContext.Provider>
  );
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
    <Frame foot={LOOPBACK.has(window.location.hostname)
      ? 'Локальный пилот: доступен только с этого компьютера (127.0.0.1). Вход не связан с Telegram.'
      : 'Вход только для администраторов: пароль и код 2FA. Вход не связан с Telegram.'}>
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

interface InviteInfo { email: string; role: string; secret: string; otpauth: string }

const INVITE_ROLE: Record<string, string> = { owner: 'Владелец', admin: 'Администратор', analyst: 'Аналитик', promoter: 'Менеджер по продвижению' };

/** /activate#<token>: an invited colleague sets their password and connects 2FA (the token never leaves the fragment
 * except in a POST body). */
export function Activate() {
  const [token] = useState(() => window.location.hash.replace(/^#/, ''));
  const [info, setInfo] = useState<InviteInfo | null>(null);
  const [state, setState] = useState<'loading' | 'invalid' | 'form' | 'done'>(token ? 'loading' : 'invalid');
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!token) return;
    api<InviteInfo>('/api/admin/auth/invite', { method: 'POST', body: { token } })
      .then((r) => { setInfo(r); setState('form'); }, () => setState('invalid'));
  }, [token]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== repeat) { setError('Пароли не совпадают'); return; }
    setBusy(true);
    setError(null);
    try {
      await api('/api/admin/auth/invite/accept', { method: 'POST', body: { token, password, code } });
      window.history.replaceState(null, '', '/activate');
      setState('done');
    } catch (err) {
      if (err instanceof ApiError && err.code === 'invite_invalid') { setState('invalid'); return; }
      setError(err instanceof ApiError && err.code === 'weak_password' ? 'Пароль — не короче 12 символов'
        : err instanceof ApiError && err.code === 'invalid_code' ? 'Код не подошёл: проверьте, что ключ добавлен в приложение, и введите свежий код'
          : errorText(err));
      setCode('');
      setBusy(false);
    }
  };
  if (state === 'loading') return <Frame foot="Приглашение"><p className="lead">Проверяем ссылку…</p></Frame>;
  if (state === 'invalid') {
    return (
      <Frame foot="Приглашение">
        <h1>Ссылка недействительна</h1>
        <p className="lead">Она уже использована, устарела (72 часа) или заменена новой. Попросите владельца выдать новую ссылку.</p>
      </Frame>
    );
  }
  if (state === 'done') {
    return (
      <Frame foot="Приглашение">
        <h1>Готово</h1>
        <p className="lead">Учётная запись активна. Войдите с e-mail, паролем и кодом из приложения.</p>
        <button type="button" className="btn primary block" onClick={() => window.location.assign('/')}>Перейти ко входу</button>
      </Frame>
    );
  }
  return (
    <Frame foot="Обязательная 2FA для каждой учётной записи">
      <form onSubmit={submit} noValidate>
        <h1>Активация доступа</h1>
        <p className="lead">{info!.email} · {INVITE_ROLE[info!.role] ?? info!.role}</p>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="field">
          <label htmlFor="new-password">Придумайте пароль (от 12 символов)</label>
          <input id="new-password" className="control" type="password" autoComplete="new-password" value={password}
            onChange={(e) => setPassword(e.target.value)} autoFocus />
        </div>
        <div className="field">
          <label htmlFor="repeat-password">Повторите пароль</label>
          <input id="repeat-password" className="control" type="password" autoComplete="new-password" value={repeat}
            onChange={(e) => setRepeat(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="totp-key">Ключ для приложения 2FA</label>
          <p className="field-hint" style={{ margin: '0 0 6px' }}>
            В Google Authenticator или аналоге: «Добавить» → «Ввести ключ настройки», тип — по времени. На телефоне можно
            {' '}<a href={info!.otpauth}>открыть сразу в приложении</a>.
          </p>
          <input id="totp-key" className="control mono" readOnly value={info!.secret.replace(/(.{4})/g, '$1 ').trim()}
            onFocus={(e) => e.target.select()} />
        </div>
        <div className="field">
          <label htmlFor="first-code">Код из приложения</label>
          <input id="first-code" className="control mono" inputMode="numeric" autoComplete="one-time-code" maxLength={6}
            value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))} />
        </div>
        <button type="submit" className="btn primary block" disabled={busy || password.length < 12 || !repeat || code.length !== 6}>
          {busy ? 'Проверяем…' : 'Активировать'}
        </button>
      </form>
    </Frame>
  );
}
