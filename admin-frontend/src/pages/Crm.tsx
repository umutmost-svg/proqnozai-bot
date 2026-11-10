import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, describeRules, errorText, RULE_LABELS, type Broadcast, type CreditsPage, type DialogThread, type Rules, type Segment, type TagCount } from '../api';
import { num, utc } from '../format';
import { can, useLoad, useMe, usePrefs } from '../state';
import { Avatar, Card, DangerDialog, Empty, ErrorBox, Loading, Metric, Metrics, NotCollected, PageHead, Tag, tagTone, Toast } from '../ui';
import { CreditsDialog } from './dialogs';

const STATUS: Record<Broadcast['status'], [string, 'neutral' | 'accent' | 'success' | 'warning']> = {
  draft: ['Черновик', 'neutral'], scheduled: ['Запланирована', 'accent'], sent: ['Отправлена', 'success'], cancelled: ['Отменена', 'warning'],
};
const LANGS = ['ru', 'az', 'en', 'tr', 'uz', 'pt-BR', 'es'];

function parseRules(raw: string | null): Rules {
  if (!raw) return {};
  try { const v = JSON.parse(raw) as unknown; return v && typeof v === 'object' && !Array.isArray(v) ? v as Rules : {}; } catch { return {}; }
}

// ---------------- broadcasts ----------------

export function Broadcasts() {
  const navigate = useNavigate();
  const me = useMe();
  const [filter, setFilter] = useState<'all' | Broadcast['status']>('all');
  const { data, error, loading, reload } = useLoad<{ items: Broadcast[] }>('/api/admin/broadcasts');
  const [toast, setToast] = useState<string | null>(null);
  const items = (data?.items ?? []).filter((b) => filter === 'all' || b.status === filter);
  const count = (s: Broadcast['status']) => data?.items.filter((b) => b.status === s).length ?? 0;
  const manage = can(me.admin.role, 'manage_users');
  return (
    <>
      <PageHead title="Рассылки" subtitle="Кампании в Telegram: аудитория, расписание, доставка и клики"
        actions={<button type="button" className="btn primary" disabled={!manage} onClick={() => navigate('/broadcasts/new')}>Новая рассылка</button>} />
      <div className="banner warn" style={{ marginBottom: 16 }}>Пилот: рассылки сохраняются и планируются, но в Telegram не уходят — отправка появится через внутренний канал бота.</div>
      <Metrics>
        <Metric label="Всего кампаний" value={num(data?.items.length)} caption="в админке" />
        <Metric label="Запланированы" value={num(count('scheduled'))} caption="ждут отправки" />
        <Metric label="Черновики" value={num(count('draft'))} caption="можно редактировать" />
        <Metric label="Доставляемость и клики" value="—" caption="после подключения отправки" />
      </Metrics>
      <div className="chips">
        {(['all', 'draft', 'scheduled', 'sent', 'cancelled'] as const).map((s) => (
          <button key={s} type="button" className="chip-btn" aria-pressed={filter === s} onClick={() => setFilter(s)}>
            {s === 'all' ? 'Все' : STATUS[s][0]}<span className="n">{s === 'all' ? num(data?.items.length) : count(s)}</span>
          </button>
        ))}
      </div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={5} /> : items.length === 0 ? (
        <Empty title="Рассылок нет" action={manage ? <button type="button" className="btn" onClick={() => navigate('/broadcasts/new')}>Создать первую</button> : undefined}>
          Создайте кампанию: выберите аудиторию, напишите сообщение, добавьте кнопку и назначьте время.
        </Empty>
      ) : (
        <div className="table-card">
          <table className="table">
            <thead><tr><th>Кампания</th><th>Аудитория</th><th>Когда</th><th className="num">Получатели</th><th>Автор</th><th>Статус</th><th /></tr></thead>
            <tbody>
              {items.map((b) => (
                <tr key={b.id} className="clickable" onClick={() => navigate(`/broadcasts/${b.id}`)}>
                  <td className="strong"><Link to={`/broadcasts/${b.id}`} onClick={(e) => e.stopPropagation()} style={{ color: 'var(--ink)' }}>{b.name}</Link></td>
                  <td>{describeRules(b.rules).join(' · ') || 'Все пользователи'}</td>
                  <td>{b.scheduledAt ? utc(b.scheduledAt) : b.status === 'draft' ? 'черновик' : '—'}</td>
                  <td className="num">{num(b.recipients ?? b.audience)}</td>
                  <td>{b.by ?? '—'}</td>
                  <td><Tag tone={STATUS[b.status][1]}>{STATUS[b.status][0]}</Tag></td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {(b.status === 'draft' || b.status === 'scheduled') && manage && (
                      <button type="button" className="btn link" onClick={async () => {
                        try { await api(`/api/admin/broadcasts/${b.id}/cancel`, { method: 'POST', body: {} }); setToast('Рассылка отменена'); reload(); } catch (err) { setToast(errorText(err)); }
                      }}>Отменить</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

function RulesEditor({ rules, onChange, tags }: { rules: Rules; onChange: (r: Rules) => void; tags: TagCount[] }) {
  const set = (k: keyof Rules, v: unknown) => { const n = { ...rules } as Record<string, unknown>; if (v === undefined || v === '' || (Array.isArray(v) && !v.length)) delete n[k]; else n[k] = v; onChange(n as Rules); };
  const numField = (k: 'registered_within_days' | 'active_within_days' | 'inactive_for_days') => (
    <div className="field"><label className="sm" htmlFor={`r-${k}`}>{RULE_LABELS[k]}</label>
      <input id={`r-${k}`} className="control" inputMode="numeric" placeholder="любой" value={rules[k] ?? ''} onChange={(e) => set(k, e.target.value ? Number(e.target.value.replace(/\D/g, '')) || undefined : undefined)} /></div>
  );
  return (
    <>
      <div className="field-row">{numField('registered_within_days')}{numField('active_within_days')}{numField('inactive_for_days')}</div>
      <div className="field-row" style={{ marginTop: 12 }}>
        <div className="field"><label className="sm" htmlFor="r-lang">Язык</label>
          <select id="r-lang" className="control" value={rules.lang_in?.[0] ?? ''} onChange={(e) => set('lang_in', e.target.value ? [e.target.value] : undefined)}>
            <option value="">любой</option>{LANGS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select></div>
        <div className="field"><label className="sm" htmlFor="r-tag">Есть тег</label>
          <select id="r-tag" className="control" value={rules.tag_has ?? ''} onChange={(e) => set('tag_has', e.target.value || undefined)}>
            <option value="">не важно</option>{tags.map((t) => <option key={t.tag} value={t.tag}>{t.tag}</option>)}
          </select></div>
        <div className="field"><label className="sm" htmlFor="r-notag">Исключить тег</label>
          <select id="r-notag" className="control" value={rules.tag_not ?? ''} onChange={(e) => set('tag_not', e.target.value || undefined)}>
            <option value="">нет</option>{tags.map((t) => <option key={t.tag} value={t.tag}>{t.tag}</option>)}
          </select></div>
      </div>
    </>
  );
}

function useAudience(rules: Rules) {
  const [size, setSize] = useState<number | null>(null);
  const key = JSON.stringify(rules);
  useEffect(() => {
    let alive = true;
    const t = setTimeout(() => {
      api<{ size: number }>('/api/admin/audience/preview', { method: 'POST', body: { rules: JSON.parse(key) } })
        .then((r) => { if (alive) setSize(r.size); }).catch(() => { if (alive) setSize(null); });
    }, 250);
    return () => { alive = false; clearTimeout(t); };
  }, [key]);
  return size;
}

export function BroadcastEditor() {
  const { id } = useParams();
  const isNew = !id || id === 'new';
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const existing = useLoad<{ items: Broadcast[] }>('/api/admin/broadcasts');
  const tags = useLoad<{ items: TagCount[] }>('/api/admin/tags');
  const item = isNew ? null : existing.data?.items.find((b) => String(b.id) === id) ?? null;
  return existing.loading && !existing.data ? <><PageHead title="Рассылка" subtitle="" /><Loading rows={6} /></>
    : !isNew && !item ? <><PageHead title="Рассылка" subtitle="" /><Empty title="Рассылка не найдена" action={<Link to="/broadcasts">← К рассылкам</Link>} /></>
      : <BroadcastForm key={id ?? 'new'} item={item} initialRules={parseRules(params.get('rules'))} tags={tags.data?.items ?? []} autoConfirm={params.get('schedule') === '1'} onSaved={(bid, schedule) => navigate(`/broadcasts/${bid}${schedule ? '?schedule=1' : ''}`, { replace: true })} reload={existing.reload} />;
}

function BroadcastForm({ item, initialRules, tags, onSaved, reload, autoConfirm }: {
  item: Broadcast | null; initialRules: Rules; tags: TagCount[]; onSaved: (id: number, schedule: boolean) => void; reload: () => void;
  autoConfirm: boolean;
}) {
  const me = useMe();
  const editable = (!item || item.status === 'draft') && can(me.admin.role, 'manage_users');
  const [name, setName] = useState(item?.name ?? '');
  const [body, setBody] = useState(item?.body ?? '');
  const [rules, setRules] = useState<Rules>(item?.rules ?? initialRules);
  const [button, setButton] = useState(item?.buttons[0]?.text ?? 'Открыть Mini App');
  const [withButton, setWithButton] = useState(item ? item.buttons.length > 0 : true);
  const [when, setWhen] = useState(() => { const d = new Date(Date.now() + 3600_000); d.setUTCSeconds(0, 0); return d.toISOString().slice(0, 16); });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(autoConfirm && item?.status === 'draft');
  const [toast, setToast] = useState<string | null>(null);
  const size = useAudience(rules);
  const preview = useMemo(() => body.replaceAll('{имя}', 'Айгерим'), [body]);
  const save = async (): Promise<number | null> => {
    setBusy(true); setError(null);
    try {
      const r = await api<{ id: number }>('/api/admin/broadcasts', { method: 'POST', body: {
        id: item?.id, name, body, rules, buttons: withButton ? [{ text: button, url: 'miniapp' }] : [] } });
      setToast('Черновик сохранён'); reload(); return r.id;
    } catch (e) { setError(errorText(e)); return null; } finally { setBusy(false); }
  };
  return (
    <>
      <PageHead crumb="Рассылки" title={item ? item.name : 'Новая рассылка'}
        subtitle={item && item.status !== 'draft' ? `${STATUS[item.status][0]}${item.scheduledAt ? ` · ${utc(item.scheduledAt)} UTC` : ''} — изменения недоступны` : 'Черновик · отправка — с причиной и повторной 2FA'}
        actions={editable ? <>
          <button type="button" className="btn" disabled={busy || !name.trim() || !body.trim()} onClick={async () => { const nid = await save(); if (nid && !item) onSaved(nid, false); }}>Сохранить черновик</button>
          <button type="button" className="btn primary" disabled={busy || !name.trim() || !body.trim()} onClick={async () => { const nid = await save(); if (nid) { if (!item) onSaved(nid, true); else setConfirm(true); } }}>Запланировать</button>
        </> : <Link to="/broadcasts">← К рассылкам</Link>} />
      <div className="steps"><span className="s"><span className="d">1</span>Аудитория</span>—<span className="s"><span className="d">2</span>Сообщение</span>—<span className="s"><span className="d">3</span>Отправка</span></div>
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="grid card-3" style={{ gridTemplateColumns: 'minmax(0,1fr) 380px' }}>
        <div>
          <Card title="Аудитория" action={<Tag tone="accent">{size === null ? '…' : `≈ ${num(size)} получателей`}</Tag>}>
            <fieldset disabled={!editable} style={{ border: 0, padding: 0, margin: 0 }}>
              <RulesEditor rules={rules} onChange={setRules} tags={tags} />
            </fieldset>
            <p className="small muted" style={{ marginBottom: 0 }}>Пусто = все пользователи. Заблокированные исключаются автоматически при отправке.</p>
          </Card>
          <Card title="Сообщение" action={<Tag>HTML Telegram</Tag>}>
            <div className="field"><label htmlFor="b-name" className="sm">Название кампании (видно только в админке)</label>
              <input id="b-name" className="control" maxLength={120} value={name} disabled={!editable} onChange={(e) => setName(e.target.value)} placeholder="Например: Тур Лиги чемпионов" /></div>
            <div className="field"><label htmlFor="b-body" className="sm">Текст · подстановка {'{имя}'}</label>
              <textarea id="b-body" className="control" style={{ minHeight: 140 }} maxLength={4000} value={body} disabled={!editable} onChange={(e) => setBody(e.target.value)} placeholder="⚽ {имя}, тур уже завтра! …" /></div>
            <label className="row small" style={{ gap: 8 }}><input type="checkbox" checked={withButton} disabled={!editable} onChange={(e) => setWithButton(e.target.checked)} />Кнопка «открыть Mini App»</label>
            {withButton && <input className="control" style={{ marginTop: 8, maxWidth: 320 }} aria-label="Текст кнопки" maxLength={40} value={button} disabled={!editable} onChange={(e) => setButton(e.target.value)} />}
          </Card>
          <Card title="Отправка">
            <div className="field-row" style={{ gridTemplateColumns: '1fr 1fr' }}>
              <div className="field"><label htmlFor="b-when" className="sm">Когда (UTC)</label>
                <input id="b-when" type="datetime-local" className="control" value={when} disabled={!editable} onChange={(e) => setWhen(e.target.value)} /></div>
              <div className="field"><label className="sm">Скорость</label><input className="control" disabled value="Авто (лимит Telegram)" /></div>
            </div>
          </Card>
        </div>
        <div>
          <div className="small muted" style={{ fontWeight: 600, marginBottom: 8 }}>Превью в Telegram</div>
          <div className="phone">
            <div className="phone-chat">
              <div className="who"><span className="avatar">S</span><div><div className="name">SporBot</div><div className="handle">бот</div></div></div>
              <div className="tg-bubble">{preview || 'Текст появится здесь'}</div>
              {withButton && <div className="tg-btn">{button || 'Кнопка'}</div>}
            </div>
          </div>
          <Card className="" title="Перед отправкой">
            <div className="kv-row"><span>Получателей</span><b>{size === null ? '…' : `≈ ${num(size)}`}</b></div>
            <div className="kv-row"><span>Подтверждение</span><b>причина + 2FA</b></div>
            <div className="kv-row"><span>Журнал</span><b>запись при планировании</b></div>
            <div className="kv-row"><span>В пилоте</span><b>не отправляется</b></div>
          </Card>
        </div>
      </div>
      {confirm && item && (
        <DangerDialog title="Запланировать рассылку?" confirmLabel="Запланировать" tone="primary" onClose={() => setConfirm(false)}
          lead={<>«{name}» · ≈ {num(size)} получателей · {when.replace('T', ' ')} UTC. В пилоте сообщение не уйдёт в Telegram.</>}
          onConfirm={async (input) => {
            await api(`/api/admin/broadcasts/${item.id}/schedule`, { method: 'POST', body: { ...input, at: Math.floor(Date.parse(`${when}:00Z`) / 1000) } });
            setConfirm(false); setToast('Рассылка запланирована'); reload();
          }} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

// ---------------- segments ----------------

export function Segments() {
  const me = useMe();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { data, error, loading, reload } = useLoad<{ items: Segment[]; tags: TagCount[] }>('/api/admin/segments');
  const draft = parseRules(params.get('draft'));
  const [editing, setEditing] = useState<{ id?: number; name: string; rules: Rules } | null>(Object.keys(draft).length ? { name: '', rules: draft } : null);
  const [toast, setToast] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const size = useAudience(editing?.rules ?? {});
  const manage = can(me.admin.role, 'manage_users');
  return (
    <>
      <PageHead title="Сегменты" subtitle="Сохранённые аудитории для рассылок и аналитики — пересчитываются автоматически"
        actions={<button type="button" className="btn primary" disabled={!manage} onClick={() => setEditing({ name: '', rules: {} })}>Новый сегмент</button>} />
      <div className="grid card-3" style={{ gridTemplateColumns: 'minmax(0,1fr) 420px' }}>
        <div>
          {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={5} /> : !data?.items.length ? (
            <Empty title="Сегментов пока нет">Соберите аудиторию по активности, языку и тегам и сохраните её.</Empty>
          ) : (
            <div className="table-card">
              <table className="table">
                <thead><tr><th>Сегмент</th><th>Правила</th><th className="num">Размер</th><th>Обновлён</th><th /></tr></thead>
                <tbody>{data.items.map((s) => (
                  <tr key={s.id} className={`clickable${editing?.id === s.id ? ' selected' : ''}`} onClick={() => setEditing({ id: s.id, name: s.name, rules: s.rules })}>
                    <td><button type="button" className="btn link" onClick={(e) => { e.stopPropagation(); setEditing({ id: s.id, name: s.name, rules: s.rules }); }}><Tag tone={tagTone(s.name)}>{s.name}</Tag></button></td>
                    <td>{describeRules(s.rules).join(' · ') || 'все'}</td>
                    <td className="num strong">{num(s.size)}</td>
                    <td>{utc(s.updatedAt)}</td>
                    <td onClick={(e) => e.stopPropagation()}><Link to={`/users?segment=${s.id}`} className="small">Список →</Link></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </div>
        <div>
          {editing ? (
            <Card title={editing.id ? `Сегмент «${editing.name}»` : 'Новый сегмент'} action={<Tag tone="accent">{size === null ? '…' : `${num(size)} человек`}</Tag>}>
              <fieldset disabled={!manage} style={{ border: 0, padding: 0, margin: 0 }}>
                <div className="field"><label htmlFor="s-name" className="sm">Название</label>
                  <input id="s-name" className="control" maxLength={80} value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} /></div>
                <RulesEditor rules={editing.rules} onChange={(r) => setEditing({ ...editing, rules: r })} tags={data?.tags ?? []} />
              </fieldset>
              {err && <div className="form-error" role="alert" style={{ marginTop: 12 }}>{err}</div>}
              <div className="row" style={{ marginTop: 16, flexWrap: 'wrap' }}>
                <button type="button" className="btn primary" disabled={!manage || !editing.name.trim()} onClick={async () => {
                  setErr(null);
                  try { const r = await api<{ id: number }>('/api/admin/segments', { method: 'POST', body: { id: editing.id, name: editing.name, rules: editing.rules } }); setEditing({ ...editing, id: r.id }); setToast('Сегмент сохранён'); reload(); }
                  catch (e) { setErr(errorText(e)); }
                }}>Сохранить</button>
                <button type="button" className="btn" disabled={!manage} onClick={() => navigate(`/broadcasts/new?rules=${encodeURIComponent(JSON.stringify(editing.rules))}`)}>Рассылка по сегменту</button>
                <button type="button" className="btn link" onClick={() => setEditing(null)}>Закрыть</button>
              </div>
            </Card>
          ) : (
            <Card title="Конструктор сегмента"><p className="small muted">Выберите сегмент слева или создайте новый. Доступные условия: регистрация, активность, язык, теги.</p></Card>
          )}
        </div>
      </div>
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

// ---------------- credits ----------------

const KIND: Record<string, [string, 'success' | 'danger' | 'violet']> = {
  grant: ['Начисление', 'success'], debit: ['Списание', 'danger'], unlimited: ['Безлимит', 'violet'], unlimited_off: ['Безлимит выкл.', 'violet'],
};

export function Credits() {
  const me = useMe();
  const [kind, setKind] = useState('');
  const { data, error, loading, reload } = useLoad<CreditsPage>(`/api/admin/credits${kind ? `?kind=${kind}` : ''}`);
  const [group, setGroup] = useState<string>('');
  const [dialog, setDialog] = useState<number[] | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const segments = useLoad<{ items: Segment[] }>('/api/admin/segments');
  const users = useLoad<{ items: { id: number }[]; total: number }>(group ? `/api/admin/users?segment=${group}&size=100` : null);
  const manage = can(me.admin.role, 'manage_users');
  return (
    <>
      <PageHead title="Кредиты" subtitle="Балансы, ручные операции и правила — подготовлено к запуску" />
      <div className="banner warn" style={{ marginBottom: 16 }}>Кредиты выключены: прогнозы бесплатны и без ограничений. Операции записываются в журнал, но не списываются в Mini App и не ограничивают доступ.</div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : data && (
        <>
          <Metrics>
            <Metric label="В обороте" value={num(data.totals.inCirculation)} caption={`на ${num(data.totals.accounts)} счетах`} />
            <Metric label="Начислено за 30 дней" value={num(data.totals.granted)} caption="ручные и бонусы" />
            <Metric label="Списано за 30 дней" value={num(data.totals.debited)} caption="только вручную" />
            <Metric label="Безлимит" value={num(data.totals.unlimited)} caption="пользователей сейчас" />
          </Metrics>
          <div className="grid card-3" style={{ gridTemplateColumns: 'minmax(0,1fr) 380px' }}>
            <section className="table-card" aria-label="Журнал операций">
              <div className="card-head" style={{ padding: '14px 16px', margin: 0 }}>
                <h2 className="card-title">Журнал операций</h2>
                <div className="tags">{[['', 'Все'], ['grant', 'Начисления'], ['debit', 'Списания'], ['unlimited', 'Безлимит']].map(([k, l]) => (
                  <button key={k} type="button" className={`tag ${kind === k ? 'accent' : ''}`} style={{ cursor: 'pointer' }} aria-pressed={kind === k} onClick={() => setKind(k ?? '')}>{l}</button>
                ))}</div>
              </div>
              {data.ledger.length === 0 ? <Empty title="Операций пока нет" /> : (
                <table className="table">
                  <thead><tr><th>Время</th><th>Пользователь</th><th>Операция</th><th className="num">Сумма</th><th className="num">Баланс</th><th>Кто и почему</th></tr></thead>
                  <tbody>{data.ledger.map((l) => (
                    <tr key={l.id}>
                      <td>{utc(l.at)}</td>
                      <td><Link to={`/users/${l.userId}`} className="mono">{l.userId}</Link>{l.batch && <span className="sub">групповая</span>}</td>
                      <td><Tag tone={KIND[l.kind]![1]}>{KIND[l.kind]![0]}</Tag></td>
                      <td className="num strong">{l.kind.startsWith('unlimited') ? '∞' : l.delta > 0 ? `+${l.delta}` : l.delta}</td>
                      <td className="num">{l.balanceAfter}</td>
                      <td>{l.reason}<span className="sub">{l.by}</span></td>
                    </tr>
                  ))}</tbody>
                </table>
              )}
            </section>
            <div>
              <Card title="Групповая операция">
                <div className="field"><label htmlFor="c-seg" className="sm">Сегмент</label>
                  <select id="c-seg" className="control" value={group} onChange={(e) => setGroup(e.target.value)}>
                    <option value="">— выберите —</option>{segments.data?.items.map((s) => <option key={s.id} value={s.id}>{s.name} ({num(s.size)})</option>)}
                  </select></div>
                <button type="button" className="btn primary" disabled={!manage || !users.data?.items.length} onClick={() => setDialog(users.data!.items.map((u) => u.id))}>
                  Начислить группе{users.data ? ` (${users.data.items.length})` : ''}
                </button>
                {users.data && users.data.total > users.data.items.length && <p className="small muted">В пилоте групповая операция — до 100 человек за раз.</p>}
              </Card>
              <Card title="Правила" action={<Tag tone="warning">выкл</Tag>}>
                {[['Стоимость AI-разбора', `${data.rules.aiPrice} кредитов`], ['Быстрый прогноз', data.rules.quickPrice ? `${data.rules.quickPrice}` : 'бесплатно'],
                  ['Бесплатно в день', `${data.rules.freePerDay} разбора`], ['Бонус за регистрацию', `${data.rules.signupBonus} кредитов`], ['Бонус за приглашение', `${data.rules.inviteBonus} кредитов`]].map(([k, v]) => (
                  <div className="kv-row" key={k}><span>{k}</span><b className="muted">{v}</b></div>
                ))}
                <div className="kv-row" style={{ marginTop: 6 }}><span>Списывать кредиты в Mini App</span><button type="button" className="toggle" role="switch" aria-checked={false} disabled aria-label="Списывать кредиты" /></div>
                <p className="small muted" style={{ marginBottom: 0 }}>Включение — отдельный этап и решение владельца: цены, лимиты, уведомления пользователей.</p>
              </Card>
            </div>
          </div>
        </>
      )}
      {dialog && <CreditsDialog userIds={dialog} onClose={() => setDialog(null)} onDone={(t) => { setDialog(null); setToast(t); reload(); }} />}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}

// ---------------- dialogs ----------------

export function Dialogs() {
  const { demo } = usePrefs();
  const { data, error, loading, reload } = useLoad<{ source: 'not_collected' } | { source: 'demo'; threads: DialogThread[] }>(`/api/admin/dialogs${demo ? '?demo=1' : ''}`);
  const [current, setCurrent] = useState(0);
  const threads = data && data.source === 'demo' ? data.threads : null;
  const t = threads?.[current];
  return (
    <>
      <PageHead title="Диалоги" subtitle="Ответы пользователей боту и переписка поддержки в одном окне" />
      <div className="banner warn" style={{ marginBottom: 16 }}>Приём сообщений из бота ещё не подключён: нужно решение о хранении переписки (правило приватности проекта). Ниже — демонстрация.</div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : !threads ? <Card><NotCollected what="Переписка с пользователями" /></Card> : (
        <div className="inbox">
          <div className="threads" role="list" aria-label="Диалоги">
            <div style={{ padding: '12px 16px' }} className="tags"><Tag tone="accent">Новые · {threads.filter((x) => x.unread).length}</Tag><Tag tone="violet">ДЕМО</Tag></div>
            {threads.map((th, i) => (
              <button key={th.userId} type="button" className="thread" aria-current={i === current} onClick={() => setCurrent(i)}>
                <Avatar name={th.name} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="row" style={{ justifyContent: 'space-between' }}><b style={{ fontSize: 13 }}>{th.name}</b><span className="small muted">{utc(th.messages.at(-1)?.at)}</span></div>
                  <div className="preview">{th.messages.at(-1)?.text}</div>
                  {th.tag && <div style={{ marginTop: 4 }}><Tag tone={th.tag[1] as 'danger'}>{th.tag[0]}</Tag></div>}
                </div>
              </button>
            ))}
          </div>
          {t && (
            <div className="convo">
              <div className="card-head" style={{ padding: '14px 20px', margin: 0, borderBottom: '1px solid var(--line)' }}>
                <div className="who"><Avatar name={t.name} /><div><div className="name">{t.name}</div><div className="handle">@{t.username}</div></div></div>
                <Link to={`/users/${t.userId}`} className="btn">Открыть карточку</Link>
              </div>
              <div className="convo-msgs">
                {t.messages.map((m, i) => (
                  <div key={i} className={`msg${m.from === 'admin' ? ' mine' : ''}`}>{m.text}<div className="meta">{m.by ? `${m.by} · ` : ''}{utc(m.at)}</div></div>
                ))}
                {t.system && <div style={{ alignSelf: 'center' }}><Tag tone="warning">Система: {t.system}</Tag></div>}
              </div>
              <div style={{ padding: '14px 20px', borderTop: '1px solid var(--line)' }} className="row">
                <input className="control" style={{ flex: 1 }} disabled placeholder="Ответ из диалогов появится после подключения приёма сообщений — пока пишите из карточки пользователя" aria-label="Ответ" />
                <Link to={`/users/${t.userId}`} className="btn primary">Написать из карточки</Link>
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}
