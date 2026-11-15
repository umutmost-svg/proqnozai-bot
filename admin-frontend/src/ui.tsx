import { useEffect, useRef, useState, type ReactNode } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { ApiError, errorText, newIdempotencyKey } from './api';
import { PERIOD_LABELS, useAuth, useMe, usePrefs, useServerMode, type Period } from './state';

const NAV = [
  ['/', 'Аналитика'], ['/users', 'Пользователи'], ['/inquiries', 'Обращения'], ['/dialogs', 'Диалоги'], ['/broadcasts', 'Рассылки'],
  ['/segments', 'Сегменты'], ['/credits', 'Кредиты'], ['/referrals', 'Ссылки'], ['/outbox', 'Очередь'], ['/ads', 'Реклама'],
] as const;
const NAV_DATA = [['/product', 'Продукт'], ['/forecasts', 'Прогнозы'], ['/ai', 'AI и расходы'], ['/sports-api', 'Спортивные API']] as const;
const NAV_ADMIN = [['/system', 'Система'], ['/settings', 'Настройки']] as const;

const ROLE_LABEL: Record<string, string> = { owner: 'Владелец', admin: 'Администратор', analyst: 'Аналитик' };

export function initials(name: string | null | undefined, fallback = '?'): string {
  const parts = (name ?? '').trim().split(/\s+/).filter(Boolean);
  const letters = parts.slice(0, 2).map((p) => p[0]!.toUpperCase()).join('');
  return letters || fallback;
}

export function Avatar({ name, large }: { name: string | null | undefined; large?: boolean }) {
  return <span className={`avatar${large ? ' lg' : ''}`} aria-hidden="true">{initials(name)}</span>;
}

export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'violet';
export function Tag({ children, tone = 'neutral', onRemove }: { children: ReactNode; tone?: Tone; onRemove?: () => void }) {
  return (
    <span className={`tag ${tone === 'neutral' ? '' : tone}`}>
      {children}
      {onRemove && <button type="button" aria-label="Снять тег" onClick={onRemove}>✕</button>}
    </span>
  );
}

const TAG_TONES: Record<string, Tone> = { VIP: 'violet', 'Жалоба': 'danger', 'Уходит': 'warning', 'Новый': 'accent', 'Тестер': 'neutral' };
export function tagTone(tag: string): Tone {
  if (TAG_TONES[tag]) return TAG_TONES[tag]!;
  if (tag.startsWith('Партнёр')) return 'accent';
  return 'neutral';
}

export function Card({ title, sub, action, children, className }: {
  title?: ReactNode; sub?: ReactNode; action?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className ?? ''}`}>
      {(title || action) && (
        <div className="card-head">
          <div>{title && <h2 className="card-title">{title}</h2>}{sub && <div className="card-sub">{sub}</div>}</div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

export function BarList({ rows, tone }: { rows: { label: ReactNode; value: number; extra?: ReactNode }[]; tone?: string }) {
  const total = rows.reduce((s, r) => s + r.value, 0) || 1;
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <div className="bar-list">
      {rows.map((r, i) => (
        <div className="bar-row" key={i}>
          <div className="bar-head"><span>{r.label} {r.extra}</span><span className="v">{r.value.toLocaleString('ru-RU')} · {Math.round((r.value / total) * 100)}%</span></div>
          <div className="bar-track"><div className={`bar-fill ${tone ?? ''}`} style={{ width: `${(r.value / max) * 100}%` }} /></div>
        </div>
      ))}
    </div>
  );
}

export function Layout() {
  const me = useMe();
  const { logout } = useAuth();
  const link = ([to, label]: readonly [string, string]) => <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-logo">S</span>
          <div><div className="brand-name">SporBot</div><div className="brand-sub">CRM · админ</div></div>
        </div>
        {/* The queue lists recipients (personal data): hidden for roles without read_personal (the API refuses too). */}
        <nav className="nav" aria-label="Разделы" style={{ marginTop: 24 }}>{NAV.filter(([to]) => to !== '/outbox' || me.admin.role !== 'analyst')
          .map(([to, label]) => [to, to === '/dialogs' && me.pilot.demoTelemetry ? 'Диалоги (демо)' : label] as const).map(link)}</nav>
        <div className="nav-caption">ДАННЫЕ</div>
        <nav className="nav" aria-label="Данные">{NAV_DATA.map(link)}</nav>
        <div className="nav-grow" />
        <nav className="nav" aria-label="Администрирование">{NAV_ADMIN.map(link)}</nav>
        <div className="account">
          <Avatar name={me.admin.email} />
          <div>
            <div className="account-email" title={me.admin.email}>{me.admin.email}</div>
            <div className="account-role">{ROLE_LABEL[me.admin.role]}</div>
            <button type="button" className="btn link" onClick={() => void logout()}>Выйти</button>
          </div>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}

export function Topbar({ crumb }: { crumb?: string }) {
  const me = useMe();
  const { demo, setDemo } = usePrefs();
  const server = useServerMode();
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  return (
    <>
      <div className="topbar">
        <form className="topbar-search" role="search" onSubmit={(e) => { e.preventDefault(); navigate(`/users?q=${encodeURIComponent(q.trim())}`); }}>
          <span aria-hidden="true" className="muted">⌕</span>
          <input aria-label="Глобальный поиск" placeholder="Поиск: пользователь, @username, Telegram ID…" value={q} onChange={(e) => setQ(e.target.value)} />
        </form>
        <div className="topbar-right">
          {me.pilot.dataset === 'demo' && <span className="tag violet" title="Пользователи и прогнозы — демонстрационный набор, созданный локально">Демо-набор</span>}
          {server ? (
            <span className="tag" title="Реальные данные Mini App; демо-данных на сервере нет">Рабочая панель</span>
          ) : (
            <>
              <label className="tag" style={{ gap: 6, cursor: 'pointer', height: 30 }}>
                <input type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} />
                Демо-телеметрия
              </label>
              <span className="tag">Локальный пилот</span>
            </>
          )}
        </div>
      </div>
      {crumb && <div className="crumbs" style={{ marginTop: 18 }}>{crumb}</div>}
      {demo && (
        <div className="banner" role="status">
          <strong>Демо-телеметрия включена.</strong> Гео, платформы, время активности, AI-вызовы, диалоги и события —
          вымышленные данные для оценки интерфейса. Реальный сбор начнётся после D1.
        </div>
      )}
    </>
  );
}

export function PageHead({ crumb, title, subtitle, actions }: { crumb?: string; title: string; subtitle: string; actions?: ReactNode }) {
  return (
    <>
      <Topbar crumb={crumb && crumb !== title ? crumb : undefined} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', gap: 16 }}>
        <div>
          <h1 className="page-title">{title}</h1>
          <p className="page-sub">{subtitle}</p>
        </div>
        {actions && <div className="row" style={{ marginBottom: 22 }}>{actions}</div>}
      </div>
    </>
  );
}

/** The period a page shows: one without a one-day view shows 7 days for "24 h" (the selector says the same). */
export function pagePeriod(period: Period, allowToday = true): Period {
  return !allowToday && period === 1 ? 7 : period;
}

export function PeriodSelect({ allowToday = true }: { allowToday?: boolean }) {
  const { period, setPeriod } = usePrefs();
  const options = (Object.keys(PERIOD_LABELS).map(Number) as Period[]).filter((p) => allowToday || p !== 1);
  return (
    <select className="control" aria-label="Период" value={pagePeriod(period, allowToday)} onChange={(e) => setPeriod(Number(e.target.value) as Period)}>
      {options.map((p) => <option key={p} value={p}>{PERIOD_LABELS[p]}</option>)}
    </select>
  );
}

export function Metrics({ children }: { children: ReactNode }) {
  return <div className="metrics">{children}</div>;
}

export function Metric({ label, value, caption, demo, hint }: {
  label: string; value: ReactNode; caption: ReactNode; demo?: boolean;
  /** A plain-language explanation, shown on hover / long press of the "?" next to the label. */
  hint?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="metric">
      <div className="metric-label">
        {label}
        {hint && (
          <button type="button" className="metric-hint" title={hint} aria-label={`Что это: ${label}`} aria-expanded={open}
            onClick={() => setOpen((v) => !v)}>?</button>
        )}
        {demo && <span className="chip demo sm">ДЕМО</span>}
      </div>
      {hint && open && <div className="metric-hint-text" role="note">{hint}</div>}
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
  const server = useServerMode();
  return (
    <Empty
      title={`${what} пока не собирается`}
      action={server ? undefined : <button type="button" className="btn" onClick={() => setDemo(true)}>Показать демо-данные</button>}
    >
      {server
        ? 'Сбор ещё не подключён в Mini App. Показатели не подставляются: здесь появятся реальные данные, когда сбор включат.'
        : 'Сбор начнётся после этапа D1 (телеметрия Mini App). До этого здесь нет реальных данных — показатели не подставляются. Для оценки интерфейса можно включить помеченную демо-телеметрию.'}
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
