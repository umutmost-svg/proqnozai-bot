import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import type { Page, UserRow } from '../api';
import { num, userName, utc, utcDay } from '../format';
import { useLoad } from '../state';
import { Empty, ErrorBox, Loading, Metric, Metrics, Note, PageHead, Pager, SectionHead, SortHeader, useSort } from '../ui';

const SEGMENTS: Record<string, string> = {
  all: 'Все пользователи', new: 'Новые за 7 дней', active: 'Входили за 7 дней', inactive: 'Не входили 14+ дней',
};
const LANGS = ['ru', 'az', 'en', 'tr', 'uz', 'pt-BR', 'es'];

type UsersPage = Page<UserRow> & { counts: { total: number; new: number; loggedIn: number; withActiveSession: number } };

export function Users() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get('q') ?? '');
  const segment = params.get('segment') ?? 'all';
  const lang = params.get('lang') ?? '';
  const page = Number(params.get('page') ?? '1') || 1;
  const { sort, onSort } = useSort(params.get('sort') ?? 'created', params.get('order') !== 'asc');
  const [selected, setSelected] = useState<UserRow | null>(null);

  const update = (patch: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) { if (v) next.set(k, v); else next.delete(k); }
    setParams(next, { replace: true });
  };
  useEffect(() => {
    const t = setTimeout(() => { if ((params.get('q') ?? '') !== query) update({ q: query, page: '' }); }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);
  useEffect(() => {
    if (params.get('sort') !== sort.field || (params.get('order') ?? 'desc') !== (sort.desc ? 'desc' : 'asc')) {
      update({ sort: sort.field, order: sort.desc ? 'desc' : 'asc', page: '' });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort]);

  const qs = new URLSearchParams({ q: params.get('q') ?? '', segment, lang, page: String(page), size: '15',
    sort: sort.field, order: sort.desc ? 'desc' : 'asc' });
  const { data, error, loading, reload } = useLoad<UsersPage>(`/api/admin/users?${qs}`);
  const filtered = Boolean(params.get('q') || segment !== 'all' || lang);

  return (
    <>
      <PageHead crumb="Пользователи" title="Пользователи" subtitle="Поиск, карточки и управление сессиями" />
      <div className="filters">
        <input className="control" type="search" placeholder="Поиск по TG ID, username или имени" aria-label="Поиск"
          value={query} onChange={(e) => setQuery(e.target.value)} />
        <select className="control" aria-label="Сегмент" value={segment} onChange={(e) => update({ segment: e.target.value === 'all' ? '' : e.target.value, page: '' })}>
          {Object.entries(SEGMENTS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select className="control" aria-label="Язык" value={lang} onChange={(e) => update({ lang: e.target.value, page: '' })}>
          <option value="">Язык: все</option>
          {LANGS.map((l) => <option key={l} value={l}>Язык: {l}</option>)}
        </select>
        {filtered && <button type="button" className="btn link" onClick={() => { setQuery(''); setParams({}, { replace: true }); }}>Сбросить фильтры</button>}
      </div>
      {data && (
        <Metrics>
          <Metric label="Всего пользователей" value={num(data.counts.total)} caption="источник: users" />
          <Metric label="Новые за 7 дней" value={num(data.counts.new)} caption="по дате регистрации" />
          <Metric label="Активны за 7 дней" value="—" caption="после D1 (телеметрия)" />
          <Metric label="С активной сессией" value={num(data.counts.withActiveSession)} caption="источник: sessions" />
        </Metrics>
      )}
      <div className="cols">
        <section className="section">
          <SectionHead title="Пользователи" action={data && <span className="small muted">Найдено: {num(data.total)}</span>} />
          {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={8} /> : data && data.items.length === 0 ? (
            filtered ? <Empty title="Никого не нашли">Измените запрос или сбросьте фильтры.</Empty>
              : <Empty title="Список пользователей появится здесь">Пользователи появятся после первого входа в Mini App.</Empty>
          ) : data && (
            <>
              <table className="table">
                <thead>
                  <tr>
                    <SortHeader label="Telegram ID" field="id" sort={sort} onSort={onSort} />
                    <SortHeader label="Username" field="username" sort={sort} onSort={onSort} />
                    <SortHeader label="Регистрация" field="created" sort={sort} onSort={onSort} />
                    <SortHeader label="Последний вход" field="login" sort={sort} onSort={onSort} />
                    <SortHeader label="Сессии" field="sessions" sort={sort} onSort={onSort} className="num" />
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((u) => (
                    <tr key={u.id} className="clickable" aria-selected={selected?.id === u.id}
                      style={selected?.id === u.id ? { background: '#f6f8fb' } : undefined}
                      onClick={() => setSelected(u)} onDoubleClick={() => navigate(`/users/${u.id}`)}>
                      <td className="strong mono">
                        <Link to={`/users/${u.id}`} onClick={(e) => e.stopPropagation()} aria-label={`Открыть карточку ${u.id}`}>{u.id}</Link>
                      </td>
                      <td>{u.username ? `@${u.username}` : '—'}<span className="sub">{userName(u)} · {u.lang}</span></td>
                      <td>{utcDay(u.createdAt)}</td>
                      <td>{utc(u.lastLoginAt)}</td>
                      <td className="num">{u.activeSessions}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <Pager page={data.page} size={data.size} total={data.total} onPage={(p) => update({ page: String(p) })} />
            </>
          )}
        </section>
        <aside>
          <SectionHead title="Карточка пользователя" />
          {selected ? (
            <>
              <ul className="kv">
                <li><span className="k">Telegram ID</span><span className="v mono">{selected.id}</span></li>
                <li><span className="k">Имя</span><span className="v" style={{ fontSize: 14 }}>{userName(selected)}</span></li>
                <li><span className="k">Регистрация</span><span className="v" style={{ fontSize: 14 }}>{utcDay(selected.createdAt)}</span></li>
                <li><span className="k">Активные сессии</span><span className="v">{selected.activeSessions}</span></li>
              </ul>
              <button type="button" className="btn" style={{ marginTop: 24 }} onClick={() => navigate(`/users/${selected.id}`)}>Посмотреть карточку</button>
            </>
          ) : (
            <p className="muted" style={{ fontSize: 14, lineHeight: 1.4 }}>
              Выберите запись в таблице, чтобы открыть профиль, активные сессии и историю действий. Двойной щелчок открывает карточку сразу.
            </p>
          )}
          <Note title="Определение «последний вход»">Время создания последней сессии. Это не активность: просмотры и запросы
            начнут учитываться после D1.</Note>
        </aside>
      </div>
    </>
  );
}
