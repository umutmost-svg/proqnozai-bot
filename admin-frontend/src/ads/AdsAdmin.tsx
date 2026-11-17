import { useState } from 'react';
import { useLoad } from '../state';
import { Empty, ErrorBox, Loading } from '../ui';
import type { DisplaySettings, Group, Partner, PartnerInput } from './adsApi';
import { adsApi, adsError, parseIds, parseList } from './adsApi';

const BLANK_PARTNER: PartnerInput = {
  name: '', hosts: [], countries: [], allowUnknownCountry: false, ageMin: 18, disclosure: {}, active: true,
};

/** Partners (owner): link hosts, allowed countries, age minimum, disclosure template, postback token. */
export function PartnersView({ owner }: { owner: boolean }) {
  const list = useLoad<{ items: Partner[] }>('/api/admin/ads/partners');
  const [editing, setEditing] = useState<PartnerInput | null>(null);
  const [token, setToken] = useState<{ partner: string; value: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rotate = async (p: Partner, revoke: boolean) => {
    setError(null);
    try {
      const r = await adsApi.postbackToken(p.id, revoke);
      setToken(r.token ? { partner: p.name, value: r.token } : null);
      list.reload();
    } catch (err) {
      setError(adsError(err));
    }
  };
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  return (
    <>
      {token && (
        <div className="banner warn" role="alert" data-testid="postback-token">
          <strong>Токен постбэка для «{token.partner}» показывается один раз.</strong> Передайте его партнёру по защищённому каналу:
          заголовок <code>X-Postback-Token</code> к <code>GET /api/ads/postback?partner=…&amp;click_id=…&amp;txn=…&amp;event=…</code>
          или поле <code>token</code> в теле <code>POST</code> (форма или JSON). В адресе токен не принимается. Действует после публикации.
          <div className="mono" style={{ marginTop: 6, userSelect: 'all' }}>{token.value}</div>
          <button type="button" className="btn link" onClick={() => setToken(null)}>Я сохранил токен</button>
        </div>
      )}
      {error && <div className="form-error" role="alert">{error}</div>}
      {list.data.items.length === 0 ? <Empty title="Партнёров пока нет" /> : (
        <div className="card table-card">
          <table className="table" data-testid="ads-partners">
            <thead><tr><th>Партнёр</th><th>Домены</th><th>Страны</th><th>Возраст</th><th>Постбэк</th><th /></tr></thead>
            <tbody>
              {list.data.items.map((p) => (
                <tr key={p.id}>
                  <td>{p.name}{!p.active && <span className="chip sm">выключен</span>}</td>
                  <td className="small">{p.hosts.join(', ')}</td>
                  <td className="small">{p.countries.join(', ') || '—'}{p.allowUnknownCountry ? ' + неизвестная' : ''}</td>
                  <td>{p.ageMin ? `${p.ageMin}+` : '—'}</td>
                  <td className="small">{p.postback ? 'есть' : 'нет'}</td>
                  <td>
                    {owner && (
                      <div className="row small">
                        <button type="button" className="btn link" onClick={() => setEditing({ ...p })}>Изменить</button>
                        <button type="button" className="btn link" onClick={() => rotate(p, false)}>{p.postback ? 'Новый токен' : 'Выдать токен'}</button>
                        {p.postback && <button type="button" className="btn link" onClick={() => rotate(p, true)}>Отозвать</button>}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {owner && !editing && <button type="button" className="btn" onClick={() => setEditing({ ...BLANK_PARTNER })}>Добавить партнёра</button>}
      {owner && editing && <PartnerForm key={editing.id ?? "new"} initial={editing} onDone={() => { setEditing(null); list.reload(); }} onCancel={() => setEditing(null)} />}
    </>
  );
}

export function PartnerForm({ initial, onDone, onCancel }: { initial: PartnerInput; onDone: () => void; onCancel: () => void }) {
  const [p, setP] = useState(initial);
  const [hosts, setHosts] = useState(initial.hosts.join(', '));
  const [countries, setCountries] = useState(initial.countries.join(', '));
  const [error, setError] = useState<string | null>(null);
  const set = (x: Partial<PartnerInput>) => setP((prev) => ({ ...prev, ...x }));
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await adsApi.savePartner({ ...p, hosts: parseList(hosts, false).map((h) => h.toLowerCase()), countries: parseList(countries) });
      onDone();
    } catch (err) {
      setError(adsError(err));
    }
  };
  return (
    <form className="card section" onSubmit={save} aria-label="Партнёр">
      <div className="field"><label htmlFor="pt-name">Название</label>
        <input id="pt-name" className="control" value={p.name} onChange={(e) => set({ name: e.target.value })} /></div>
      <div className="field"><label htmlFor="pt-hosts">Домены ссылок (точно, через запятую)</label>
        <input id="pt-hosts" className="control" value={hosts} placeholder="partner.example.com" onChange={(e) => setHosts(e.target.value)} /></div>
      <div className="field-row">
        <div className="field"><label htmlFor="pt-countries">Разрешённые страны (пусто — нигде)</label>
          <input id="pt-countries" className="control" value={countries} placeholder="AZ, TR, UZ" onChange={(e) => setCountries(e.target.value)} /></div>
        <div className="field"><label htmlFor="pt-age">Возраст, с которого показывать (0 — без подтверждения)</label>
          <input id="pt-age" className="control" type="number" min={0} max={25} value={p.ageMin} onChange={(e) => set({ ageMin: Number(e.target.value) })} /></div>
      </div>
      <div className="field-row">
        <div className="field"><label htmlFor="pt-adv">Рекламодатель (маркировка)</label>
          <input id="pt-adv" className="control" maxLength={120} value={p.disclosure.advertiser ?? ''} onChange={(e) => set({ disclosure: { ...p.disclosure, advertiser: e.target.value } })} /></div>
        <div className="field"><label htmlFor="pt-agel">Возрастная метка</label>
          <input id="pt-agel" className="control" maxLength={8} value={p.disclosure.ageLabel ?? ''} placeholder="18+" onChange={(e) => set({ disclosure: { ...p.disclosure, ageLabel: e.target.value } })} /></div>
      </div>
      <div className="row small" style={{ gap: 16 }}>
        <label><input type="checkbox" checked={p.allowUnknownCountry} onChange={(e) => set({ allowUnknownCountry: e.target.checked })} /> Неизвестная страна разрешена</label>
        <label><input type="checkbox" checked={p.active} onChange={(e) => set({ active: e.target.checked })} /> Активен</label>
      </div>
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button type="button" className="btn" onClick={onCancel}>Отмена</button>
        <button type="submit" className="btn primary">Сохранить</button>
      </div>
    </form>
  );
}

/** Groups: named lists of Telegram ids for targeting (members are personal data: owner/admin only). */
export function GroupsView({ manage }: { manage: boolean }) {
  const list = useLoad<{ items: Group[] }>('/api/admin/ads/groups');
  const [name, setName] = useState('');
  const [members, setMembers] = useState<{ id: number; text: string; remove: boolean } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const run = async (fn: () => Promise<string>) => {
    try {
      setMessage(await fn());
      list.reload();
    } catch (err) {
      setMessage(adsError(err));
    }
  };
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  return (
    <>
      {message && <div className="banner" role="status">{message}</div>}
      <div className="card table-card">
        <table className="table" data-testid="ads-groups">
          <thead><tr><th>#</th><th>Группа</th><th>Участников</th><th /></tr></thead>
          <tbody>
            {list.data.items.map((g) => (
              <tr key={g.id}>
                <td>{g.id}</td><td>{g.name}</td><td>{g.members}</td>
                <td>{manage && <button type="button" className="btn link" onClick={() => setMembers({ id: g.id, text: '', remove: false })}>Участники</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {manage && (
        <form className="row" style={{ marginTop: 12 }} onSubmit={(e) => { e.preventDefault(); void run(async () => { await adsApi.saveGroup(name.trim()); setName(''); return 'Группа создана'; }); }}>
          <input className="control" aria-label="Название группы" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} placeholder="Название новой группы" />
          <button type="submit" className="btn" disabled={!name.trim()}>Создать</button>
        </form>
      )}
      {members && (
        <form className="card section" aria-label="Участники группы" onSubmit={(e) => {
          e.preventDefault();
          const ids = parseIds(members.text);
          if (!ids || !ids.length) { setMessage('Telegram ID — положительные целые числа.'); return; }
          void run(async () => {
            const r = await adsApi.members(members.id, members.remove ? [] : ids, members.remove ? ids : []);
            setMembers(null);
            return `Добавлено ${r.added}, удалено ${r.removed}, всего ${r.total}. Изменения попадут к пользователям после публикации.`;
          });
        }}>
          <div className="field"><label htmlFor="gm-ids">Telegram ID (через запятую или с новой строки)</label>
            <textarea id="gm-ids" className="control" rows={4} value={members.text} onChange={(e) => setMembers({ ...members, text: e.target.value })} /></div>
          <label className="small"><input type="checkbox" checked={members.remove} onChange={(e) => setMembers({ ...members, remove: e.target.checked })} /> Удалить из группы</label>
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn" onClick={() => setMembers(null)}>Отмена</button>
            <button type="submit" className="btn primary">Применить</button>
          </div>
        </form>
      )}
    </>
  );
}

/** Global display settings (owner): blocked countries and the age gate — plain configuration. */
export function SettingsView({ owner }: { owner: boolean }) {
  const loaded = useLoad<DisplaySettings>('/api/admin/ads/settings');
  if (loaded.error) return <ErrorBox error={loaded.error} onRetry={loaded.reload} />;
  if (!loaded.data) return <Loading />;
  return <SettingsForm key={loaded.data.updatedAt ?? 0} initial={loaded.data} owner={owner} onSaved={loaded.reload} />;
}

export function SettingsForm({ initial, owner, onSaved }: { initial: DisplaySettings; owner: boolean; onSaved: () => void }) {
  const [blocked, setBlocked] = useState(initial.blockedCountries.join(', '));
  const [ageGate, setAgeGate] = useState(initial.ageGate);
  const [ageMin, setAgeMin] = useState(initial.ageMin);
  const [message, setMessage] = useState<string | null>(null);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await adsApi.saveSettings({ blockedCountries: parseList(blocked), ageGate, ageMin });
      setMessage('Сохранено. Вступит в силу после публикации.');
      onSaved();
    } catch (err) {
      setMessage(adsError(err));
    }
  };
  return (
    <form className="card section" onSubmit={save} aria-label="Настройки показа">
      <fieldset disabled={!owner} style={{ border: 0, padding: 0, margin: 0 }}>
        <div className="field"><label htmlFor="st-blocked">Запрещённые страны — реклама там не показывается ни у одного партнёра</label>
          <input id="st-blocked" className="control" value={blocked} placeholder="RU, US" onChange={(e) => setBlocked(e.target.value)} /></div>
        <div className="row small" style={{ gap: 16 }}>
          <label><input type="checkbox" checked={ageGate} onChange={(e) => setAgeGate(e.target.checked)} /> Возрастное подтверждение перед любой рекламой</label>
          <label>Минимальный возраст{' '}
            <input className="control" type="number" min={1} max={25} style={{ width: 80 }} aria-label="Минимальный возраст" value={ageMin} onChange={(e) => setAgeMin(Number(e.target.value))} />
          </label>
        </div>
        <p className="small muted">Страны партнёра и кампании сужают показ дальше; неизвестная страна — только если её разрешили и партнёр, и кампания.</p>
      </fieldset>
      {message && <div className="banner" role="status">{message}</div>}
      {owner && <div className="row" style={{ justifyContent: 'flex-end' }}><button type="submit" className="btn primary">Сохранить</button></div>}
    </form>
  );
}
