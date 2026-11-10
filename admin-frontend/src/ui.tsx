import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, NavLink, Outlet } from 'react-router-dom';
import { ApiError, errorText, newIdempotencyKey } from './api';
import { PERIOD_LABELS, useAuth, useMe, usePrefs, type Period } from './state';

const NAV = [
  ['/', 'Обзор'], ['/users', 'Пользователи'], ['/forecasts', 'Прогнозы'], ['/ai', 'AI и расходы'],
  ['/sports-api', 'Спортивные API'], ['/system', 'Система и логи'], ['/settings', 'Настройки'], ['/credits', 'Кредиты'],
] as const;

const ROLE_LABEL: Record<string, string> = { owner: 'Владелец', admin: 'Администратор', analyst: 'Аналитик' };

export function Layout() {
  const me = useMe();
  const { logout } = useAuth();
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-name">SporBot</div>
          <div className="brand-sub">ADMIN</div>
        </div>
        <div className="nav-caption">РАЗДЕЛЫ</div>
        <nav className="nav" aria-label="Разделы">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>
          ))}
        </nav>
        <div className="account">
          <div className="account-email" title={me.admin.email}>{me.admin.email}</div>
          <div className="account-role">{ROLE_LABEL[me.admin.role]}</div>
          <button type="button" className="btn link small" onClick={() => void logout()}>Выйти</button>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}

export function Topbar({ crumb }: { crumb: string }) {
  const me = useMe();
  const { demo, setDemo } = usePrefs();
  return (
    <>
      <div className="topbar">
        <div className="crumbs"><Link to="/">SporBot</Link>&nbsp;&nbsp;/&nbsp;&nbsp;{crumb}</div>
        <div className="topbar-right">
          {me.pilot.dataset === 'demo' && <span className="chip demo" title="Пользователи и прогнозы — демонстрационный набор, созданный локально">ДЕМО-НАБОР</span>}
          <label className="chip" style={{ gap: 8, cursor: 'pointer' }}>
            <input type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} />
            Демо-телеметрия
          </label>
          <span className="chip">ЛОКАЛЬНЫЙ ПИЛОТ</span>
        </div>
      </div>
      {demo && (
        <div className="banner" role="status">
          <strong>Демо-телеметрия включена.</strong> Активность, AI-вызовы, расходы, API и события — вымышленные данные для
          оценки интерфейса. Реальный сбор начнётся после D1.
        </div>
      )}
    </>
  );
}

export function PageHead({ crumb, title, subtitle }: { crumb: string; title: string; subtitle: string }) {
  return (
    <>
      <Topbar crumb={crumb} />
      <h1 className="page-title">{title}</h1>
      <p className="page-sub">{subtitle}</p>
    </>
  );
}

export function PeriodSelect({ allowToday = true }: { allowToday?: boolean }) {
  const { period, setPeriod } = usePrefs();
  const options = (Object.keys(PERIOD_LABELS).map(Number) as Period[]).filter((p) => allowToday || p !== 1);
  return (
    <select className="control" aria-label="Период" value={period} onChange={(e) => setPeriod(Number(e.target.value) as Period)}>
      {options.map((p) => <option key={p} value={p}>{PERIOD_LABELS[p]}</option>)}
    </select>
  );
}

export function Metrics({ children }: { children: ReactNode }) {
  return <div className="metrics">{children}</div>;
}

export function Metric({ label, value, caption, demo }: { label: string; value: ReactNode; caption: ReactNode; demo?: boolean }) {
  return (
    <div className="metric">
      <div className="metric-label">{label}{demo && <span className="chip demo sm">ДЕМО</span>}</div>
      <div className="metric-value">{value}</div>
      <div className="metric-caption">{caption}</div>
    </div>
  );
}

export function SectionHead({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <div className="section-head">
      <h2 className="section-title">{title}</h2>
      {action}
    </div>
  );
}

const STATUS: Record<string, [string, string]> = {
  pending: ['warn', 'Ожидает'], settled: ['ok', 'Сверено'], void: ['', 'Аннулирован'],
  active: ['ok', 'Активна'], expired: ['', 'Истекла'], revoked: ['bad', 'Отозвана'],
  invited: ['warn', 'Приглашён'], disabled: ['', 'Отключён'],
  delivered: ['ok', 'Доставлен'], not_delivered: ['warn', 'Не доставлен'],
  done: ['ok', 'Выполнено'], requested: ['warn', 'Запрошено'], completed: ['ok', 'Завершено'],
  denied: ['bad', 'Отказано'], failed: ['bad', 'Ошибка'],
  ok: ['ok', 'В норме'], warning: ['warn', 'Внимание'], error: ['bad', 'Ошибка'], unavailable: ['bad', 'Недоступно'],
};

export function Status({ value, label }: { value: string; label?: string }) {
  const [tone, text] = STATUS[value] ?? ['bad', value];
  return <span className={`chip ${tone}`}>{label ?? text}</span>;
}

export function Note({ title, children, demo }: { title: string; children: ReactNode; demo?: boolean }) {
  return (
    <div className={`note${demo ? ' demo' : ''}`}>
      <div className="note-title">{title}</div>
      <div className="note-body">{children}</div>
    </div>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty" role="status">
      <div className="empty-title">{title}</div>
      {children && <div className="empty-body">{children}</div>}
      {action}
    </div>
  );
}

export function Loading({ rows = 4 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Загрузка">
      {Array.from({ length: rows }, (_, i) => <div key={i} className="skeleton sk-line" style={{ width: `${92 - i * 9}%` }} />)}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const rid = error instanceof ApiError ? error.requestId : null;
  return (
    <div className="error-box" role="alert">
      <div>
        <div>Не удалось загрузить данные. {errorText(error)}</div>
        {rid && <div className="mono">request_id: {rid}</div>}
      </div>
      {onRetry && <button type="button" className="btn" onClick={onRetry}>Повторить</button>}
    </div>
  );
}

export function SortHeader({ label, field, sort, onSort, className }: {
  label: string; field: string; sort: { field: string; desc: boolean }; onSort: (field: string) => void; className?: string;
}) {
  const active = sort.field === field;
  return (
    <th className={className} aria-sort={active ? (sort.desc ? 'descending' : 'ascending') : 'none'}>
      <button type="button" onClick={() => onSort(field)}>
        {label}
        <span className="arrow" aria-hidden="true">{active ? (sort.desc ? '▼' : '▲') : ''}</span>
      </button>
    </th>
  );
}

export function useSort(initial: string, desc = true) {
  const [sort, setSort] = useState({ field: initial, desc });
  const onSort = (field: string) => setSort((s) => (s.field === field ? { field, desc: !s.desc } : { field, desc: true }));
  return { sort, onSort };
}

export function Pager({ page, size, total, onPage }: { page: number; size: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / size));
  const from = total === 0 ? 0 : (page - 1) * size + 1;
  return (
    <div className="pager">
      <span>{from}–{Math.min(total, page * size)} из {total}</span>
      <div className="btns">
        <button type="button" className="btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Назад</button>
        <button type="button" className="btn" disabled={page >= pages} onClick={() => onPage(page + 1)}>Вперёд →</button>
      </div>
    </div>
  );
}

export function NotCollected({ what }: { what: string }) {
  const { setDemo } = usePrefs();
  return (
    <Empty
      title={`${what} пока не собирается`}
      action={<button type="button" className="btn" onClick={() => setDemo(true)}>Показать демо-данные</button>}
    >
      Сбор начнётся после этапа D1 (телеметрия Mini App). До этого здесь нет реальных данных — показатели не
      подставляются. Для оценки интерфейса можно включить помеченную демо-телеметрию.
    </Empty>
  );
}

export function Bars({ values, labels, format, title }: {
  values: number[]; labels: [string, string]; format: (v: number) => string; title: string;
}) {
  const max = Math.max(1, ...values);
  return (
    <figure style={{ margin: 0 }} aria-label={title}>
      <div className="bars">
        {values.map((v, i) => (
          <div key={i} className="bar" style={{ height: `${(v / max) * 100}%` }} title={format(v)} />
        ))}
      </div>
      <div className="bars-axis"><span>{labels[0]}</span><span>{labels[1]}</span></div>
    </figure>
  );
}

export function Toast({ text, onDone }: { text: string; onDone: () => void }) {
  useEffect(() => {
    const t = setTimeout(onDone, 4000);
    return () => clearTimeout(t);
  }, [onDone]);
  return <div className="toast" role="status">{text}</div>;
}

/** Dangerous-action confirmation: mandatory reason + a fresh 2FA code, one idempotency key per dialog. */
export function DangerDialog({ title, lead, confirmLabel, tone = 'danger', onConfirm, onClose, children }: {
  title: string; lead: ReactNode; confirmLabel: string; tone?: 'danger' | 'primary';
  onConfirm: (input: { reason: string; code: string; idempotencyKey: string }) => Promise<void>;
  onClose: () => void; children?: ReactNode;
}) {
  const [reason, setReason] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = useRef(newIdempotencyKey());
  const first = useRef<HTMLInputElement>(null);
  useEffect(() => {
    first.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);
  const valid = reason.trim().length >= 3 && /^\d{6}$/.test(code);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm({ reason: reason.trim(), code, idempotencyKey: key.current });
    } catch (err) {
      setError(errorText(err));
      if (err instanceof ApiError && err.code === 'invalid_code') setCode('');
      setBusy(false);
    }
  };
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="dlg-title" onSubmit={submit}>
        <h2 id="dlg-title">{title}</h2>
        <p className="lead">{lead}</p>
        {children}
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="field">
          <label htmlFor="dlg-reason">Причина</label>
          <input id="dlg-reason" ref={first} className="control" value={reason} maxLength={300}
            placeholder="Укажите причину" onChange={(e) => setReason(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="dlg-code">Код двухфакторной аутентификации</label>
          <input id="dlg-code" className="control" value={code} inputMode="numeric" autoComplete="one-time-code"
            placeholder="6 цифр" maxLength={6} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>Отмена</button>
          <button type="submit" className={`btn ${tone}`} disabled={!valid || busy}>{busy ? 'Выполняется…' : confirmLabel}</button>
        </div>
      </form>
    </div>
  );
}
