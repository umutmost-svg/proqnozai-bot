import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { api, type CrmUsersPage } from '../api';
import { geoLabel, num, sourceLabel, userName, localTime, localDay } from '../format';
import { can, useLoad, useMe } from '../state';
import { Avatar, Empty, ErrorBox, Loading, PageHead, Pager, SortHeader, Tag, tagTone, Toast, useSort } from '../ui';
import { CreditsDialog, TagDialog } from './dialogs';

const CHIPS: [string, string][] = [['', 'Все'], ['new', 'Новые 7д'], ['active', 'Активные 7д'], ['inactive', 'Уходящие 14д+'], ['blocked', 'Заблокированные']];
const LANGS = ['ru', 'az', 'en', 'tr', 'uz', 'pt-BR', 'es'];

export function Users() {
  const navigate = useNavigate();
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get('q') ?? '');
  // A search started from the top bar while this page is open changes the address: the box follows it — but not the
  // address this page wrote itself from the box (that would undo what is typed meanwhile).
  const urlQuery = params.get('q') ?? '';
  const written = useRef(urlQuery);
  useEffect(() => { if (urlQuery !== written.current) { written.current = urlQuery; setQuery(urlQuery); } }, [urlQuery]);
  const chip = params.get('chip') ?? '';
  const tag = params.get('tag') ?? '';
  const segment = params.get('segment') ?? '';
  const lang = params.get('lang') ?? '';
  const ref = params.get('ref') ?? '';
  const page = Number(params.get('page') ?? '1') || 1;
  const { sort, onSort } = useSort(params.get('sort') ?? 'created', params.get('order') !== 'asc');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [dialog, setDialog] = useState<'tag' | 'credits' | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const manage = can(me.admin.role, 'manage_users');

  // Patch the CURRENT address (a delayed search sync must not restore stale filters).
  const update = (patch: Record<string, string>) => {
    const next = new URLSearchParams(window.location.search);
    for (const [k, v] of Object.entries(patch)) { if (v) next.set(k, v); else next.delete(k); }
    setParams(next, { replace: true });
  };
  useEffect(() => {
    const t = setTimeout(() => {
      if ((new URLSearchParams(window.location.search).get('q') ?? '') !== query) { written.current = query; update({ q: query, page: '' }); }
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);
  useEffect(() => {
    const cur = new URLSearchParams(window.location.search);
    if (cur.get('sort') !== sort.field || (cur.get('order') ?? 'desc') !== (sort.desc ? 'desc' : 'asc')) {
      update({ sort: sort.field, order: sort.desc ? 'desc' : 'asc', page: '' });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort]);

  const qs = new URLSearchParams({ q: params.get('q') ?? '', chip, tag, segment, lang, ref, page: String(page), size: '15',
    sort: sort.field, order: sort.desc ? 'desc' : 'asc' });
  const { data, error, loading, reload } = useLoad<CrmUsersPage>(`/api/admin/users?${qs}`);
  const listKey = qs.toString();
  const [selectedIn, setSelectedIn] = useState(listKey);
  if (selectedIn !== listKey) { setSelectedIn(listKey); setSelected(new Set()); }
  const filtered = Boolean(params.get('q') || chip || tag || segment || lang || ref);
  // The search text is not an audience rule: with only a search, a segment or broadcast would mean "everyone".
  const onlySearch = Boolean(params.get('q')) && !(chip || tag || segment || lang || ref);
  const toggle = (id: number) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const allOnPage = data ? data.items.length > 0 && data.items.every((u) => selected.has(u.id)) : false;
  const audienceRules = () => {
    const rules: Record<string, unknown> = {};
    if (chip === 'new') rules.registered_within_days = 7;
    if (chip === 'active') rules.active_within_days = 7;
    if (chip === 'inactive') rules.inactive_for_days = 14;
    if (chip === 'blocked') rules.blocked = true;
    if (tag) rules.tag_has = tag;
    if (lang) rules.lang_in = [lang];
    const seg = data?.segments.find((s) => String(s.id) === segment);
    return seg ? { ...seg.rules, ...rules } : rules;
  };

  return (
    <>
      <PageHead title="Пользователи" subtitle={`${num(data?.counts.total)} человек · карточки, теги, сегменты и массовые действия`}
        actions={<>
          <button type="button" className="btn" onClick={() => navigate(`/segments?draft=${encodeURIComponent(JSON.stringify(audienceRules()))}`)} disabled={!filtered || !manage || Boolean(ref) || onlySearch} title={ref ? 'Фильтр по ссылке нельзя сохранить в сегмент' : onlySearch ? 'Поиск по тексту не сохраняется в сегмент' : undefined}>Сохранить как сегмент</button>
          <button type="button" className="btn primary" onClick={() => navigate(`/broadcasts/new?rules=${encodeURIComponent(JSON.stringify(audienceRules()))}`)} disabled={!manage || Boolean(ref) || Boolean(params.get('q'))} title={ref ? 'Фильтр по ссылке не переносится в рассылку' : params.get('q') ? 'Поиск по тексту не переносится в рассылку: уберите его или сохраните сегмент' : undefined}>Рассылка по фильтру</button>
        </>} />
      <div className="chips" role="group" aria-label="Быстрые сегменты">
        {CHIPS.map(([key, label]) => (
          <button key={key} type="button" className="chip-btn" aria-pressed={chip === key && !segment}
            onClick={() => update({ chip: key, segment: '', page: '' })}>
            {label}<span className="n">{key ? num(data?.counts.chips[key]) : num(data?.counts.total)}</span>
          </button>
        ))}
        {data?.segments.slice(0, 4).map((s) => (
          <button key={s.id} type="button" className="chip-btn" aria-pressed={segment === String(s.id)}
            onClick={() => update({ segment: segment === String(s.id) ? '' : String(s.id), chip: '', page: '' })}>{s.name}</button>
        ))}
        <Link to="/segments" className="small">+ сегмент</Link>
      </div>
      <div className="filters" style={{ marginBottom: 14 }}>
        <input className="control" type="search" placeholder="Поиск по TG ID, ID MB, username или имени" aria-label="Поиск"
          value={query} onChange={(e) => setQuery(e.target.value)} />
        <select className="control" aria-label="Язык" value={lang} onChange={(e) => update({ lang: e.target.value, page: '' })}>
          <option value="">Язык: все</option>
          {LANGS.map((l) => <option key={l} value={l}>Язык: {l}</option>)}
        </select>
        <select className="control" aria-label="Тег" value={tag} onChange={(e) => update({ tag: e.target.value, page: '' })}>
          <option value="">Теги: любые</option>
          {data?.tags.map((t) => <option key={t.tag} value={t.tag}>{t.tag} ({t.users})</option>)}
        </select>
        {ref && <span className="tag accent">Ссылка: ref_{ref}</span>}
        {filtered && <button type="button" className="btn link" onClick={() => { setQuery(''); written.current = ''; setParams({}, { replace: true }); }}>Сбросить фильтры</button>}
      </div>
      {data && data.telemetry === false && (
        <p className="small muted" style={{ margin: '0 0 12px' }}>Активность и прогнозы считаются по телеметрии Mini App — она пока не ведётся, поэтому в колонках «—». Страна — только по языку интерфейса.</p>
      )}
      {selected.size > 0 && (
        <div className="bulk" role="region" aria-label="Массовые действия">
          <span className="count">Выбрано: {selected.size}</span>
          <button type="button" className="btn" disabled={!manage} onClick={() => setDialog('tag')}>Добавить тег</button>
          <button type="button" className="btn" disabled={!manage} onClick={() => setDialog('credits')}>Кредиты</button>
          <button type="button" className="btn link" onClick={() => setSelected(new Set())}>Снять выбор</button>
        </div>
      )}
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={8} /> : data && data.items.length === 0 ? (
        filtered ? <Empty title="Никого не нашли">Измените запрос или сбросьте фильтры.</Empty>
          : <Empty title="Список пользователей появится здесь">Пользователи появятся после первого входа в Mini App.</Empty>
      ) : data && (
        <div className="table-card">
          <table className="table" aria-busy={loading} style={{ opacity: loading ? 0.45 : 1, pointerEvents: loading ? 'none' : undefined }} inert={loading}>
            <thead>
              <tr>
                <th style={{ width: 36 }}><input type="checkbox" aria-label="Выбрать всех на странице" checked={allOnPage}
                  onChange={() => setSelected((s) => { const n = new Set(s); for (const u of data.items) { if (allOnPage) n.delete(u.id); else n.add(u.id); } return n; })} /></th>
                <SortHeader label="Пользователь" field="username" sort={sort} onSort={onSort} />
                <SortHeader label="Telegram ID" field="id" sort={sort} onSort={onSort} />
                <th>Язык</th>
                <SortHeader label="Регистрация" field="created" sort={sort} onSort={onSort} />
                <SortHeader label="Последняя активность" field="seen" sort={sort} onSort={onSort} />
                <SortHeader label="Прогнозы" field="forecasts" sort={sort} onSort={onSort} className="num" />
                <th>Страна</th>
                <th>Источник</th>
                <SortHeader label="Сессии" field="sessions" sort={sort} onSort={onSort} className="num" />
                <th>Кредиты</th>
                <th>Теги</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((u) => (
                <tr key={u.id} className={`clickable${selected.has(u.id) ? ' selected' : ''}`} onClick={() => navigate(`/users/${u.id}`)}>
                  <td onClick={(e) => e.stopPropagation()}><input type="checkbox" aria-label={`Выбрать ${u.id}`} checked={selected.has(u.id)} onChange={() => toggle(u.id)} /></td>
                  <td>
                    <div className="who"><Avatar name={userName(u)} />
                      <div><Link to={`/users/${u.id}`} className="name" onClick={(e) => e.stopPropagation()}>{userName(u)}</Link>
                        <div className="handle">{u.username ? `@${u.username}` : '—'}{u.mbId && <> · MB <span className="mono">{u.mbId}</span></>}{u.blocked && <> · <span style={{ color: 'var(--bad)' }}>заблокирован</span></>}</div></div>
                    </div>
                  </td>
                  <td className="mono">{u.id}</td>
                  <td>{u.lang}</td>
                  <td>{localDay(u.createdAt)}</td>
                  <td>{localTime(u.lastSeenAt ?? u.lastLoginAt)}</td>
                  <td className="num">{u.forecasts ?? '—'}</td>
                  <td>{geoLabel(u.geo)}</td>
                  <td>{sourceLabel(u.source)}</td>
                  <td className="num">{u.activeSessions}</td>
                  <td style={{ color: 'var(--demo)', fontWeight: 600 }}>{u.credits.unlimitedUntil ? '∞' : num(u.credits.balance)}</td>
                  <td><div className="tags">{u.tags.map((t) => <Tag key={t} tone={tagTone(t)}>{t}</Tag>)}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && <Pager page={data.page} size={data.size} total={data.total} onPage={(p) => update({ page: String(p) })} />}
        </div>
      )}
      {dialog === 'tag' && (
        <TagDialog count={selected.size} onClose={() => setDialog(null)} onSave={async (t) => {
          const r = await api<{ tagged: number }>('/api/admin/users/bulk/tags', { method: 'POST', body: { userIds: [...selected], tag: t } });
          setDialog(null); setToast(`Тег «${t}» добавлен: ${r.tagged}`); reload();
        }} />
      )}
      {dialog === 'credits' && (
        <CreditsDialog userIds={[...selected]} onClose={() => setDialog(null)} onDone={(text) => { setDialog(null); setToast(text); reload(); }} />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}
