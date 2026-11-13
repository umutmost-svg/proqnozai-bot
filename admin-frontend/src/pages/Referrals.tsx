import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api, errorText, type ReferralsPage, type ReferralStats } from '../api';
import { num, pct, utcDay } from '../format';
import { useLoad, useMe } from '../state';
import { Card, Empty, ErrorBox, Loading, Metric, Metrics, PageHead, Tag, Toast } from '../ui';

const CHANNEL: Record<string, string> = { bot: 'Бот (/start)', miniapp: 'Mini App (startapp)' };

function rate(part: number | null, whole: number): string {
  return part !== null && whole > 0 ? pct(part / whole) : '—';
}

function StatCells({ s }: { s: ReferralStats }) {
  return (
    <>
      <td className="num">{num(s.starts)}<span className="sub">бот {num(s.botStarts)} · app {num(s.miniappStarts)}</span></td>
      <td className="num">{num(s.inMiniApp)}</td>
      <td className="num">{num(s.active7d)}</td>
      <td className="num">{num(s.converted)}<span className="sub">{rate(s.converted, s.inMiniApp)}</span></td>
    </>
  );
}

export function Referrals() {
  const me = useMe();
  const manage = me.admin.role === 'owner' || me.admin.role === 'admin';
  const { data, error, loading, reload } = useLoad<ReferralsPage>('/api/admin/referrals');
  const [name, setName] = useState('');
  const [channel, setChannel] = useState<'bot' | 'miniapp'>('bot');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setFormError(null);
    try {
      const r = await api<{ code: string }>('/api/admin/referrals', { method: 'POST', body: { name, channel, code: code.trim() || undefined } });
      setName(''); setCode(''); setToast(`Ссылка ref_${r.code} создана`); reload();
    } catch (err) { setFormError(errorText(err)); } finally { setBusy(false); }
  };
  const toggleArchive = async (id: number, value: boolean) => {
    try { await api(`/api/admin/referrals/${id}/archive`, { method: 'POST', body: { archived: value } }); reload(); }
    catch (err) { setToast(errorText(err)); }
  };
  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text).then(() => setToast('Скопировано'), () => setToast('Не удалось скопировать'));
  };
  const items = data?.items.filter((i) => Boolean(i.archivedAt) === archived) ?? [];
  const f = data?.funnel;

  return (
    <>
      <PageHead title="Реферальные ссылки" subtitle="Индивидуальные ссылки бота и Mini App: атрибуция по первому запуску и воронка по каждой ссылке" />
      {f && (
        <Metrics>
          <Metric label="Запуски по ссылкам" value={num(f.starts)} caption="первые запуски с кодом" />
          <Metric label="Дошли до Mini App" value={num(f.inMiniApp)} caption={rate(f.inMiniApp, f.starts)} />
          <Metric label="Запросили прогноз" value={num(f.converted)} caption={data?.telemetry ? `${rate(f.converted, f.inMiniApp)} из дошедших` : 'нужна телеметрия Mini App'} />
          <Metric label="Активны 7 дней" value={num(f.active7d)} caption={`${rate(f.active7d, f.inMiniApp)} из дошедших`} />
        </Metrics>
      )}
      {manage && (
        <Card title="Новая ссылка" sub="Код — латиница в нижнем регистре и цифры, 3–24 символа; пусто — сгенерируется">
          <form className="filters" onSubmit={create} aria-label="Новая ссылка">
            <input className="control" aria-label="Название" placeholder="Название (видно только команде)" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
            <select className="control" aria-label="Канал" value={channel} onChange={(e) => setChannel(e.target.value as 'bot' | 'miniapp')}>
              <option value="bot">{CHANNEL.bot}</option><option value="miniapp">{CHANNEL.miniapp}</option>
            </select>
            <input className="control mono" aria-label="Код" placeholder="код (необязательно)" maxLength={24} value={code} onChange={(e) => setCode(e.target.value.toLowerCase())} />
            <button type="submit" className="btn primary" disabled={busy || !name.trim()}>Создать</button>
          </form>
          {formError && <div className="form-error" role="alert">{formError}</div>}
          {data && !data.names.bot && <p className="small muted" style={{ marginBottom: 0 }}>Имя бота не задано (ADMIN_BOT_USERNAME): показывается только параметр запуска.</p>}
        </Card>
      )}
      <div className="chips" role="group" aria-label="Активные или архив" style={{ margin: '16px 0' }}>
        <button type="button" className="chip-btn" aria-pressed={!archived} onClick={() => setArchived(false)}>Активные</button>
        <button type="button" className="chip-btn" aria-pressed={archived} onClick={() => setArchived(true)}>Архив</button>
      </div>
      {error ? <ErrorBox error={error} onRetry={reload} /> : loading && !data ? <Loading rows={5} /> : items.length === 0 ? (
        <Empty title={archived ? 'В архиве пусто' : 'Ссылок пока нет'}>Создайте ссылку и разместите её в канале привлечения.</Empty>
      ) : (
        <div className="table-card">
          <table className="table">
            <thead><tr><th>Ссылка</th><th>Канал</th><th className="num">Запуски</th><th className="num">В Mini App</th><th className="num">Активны 7д</th><th className="num">Прогноз запрошен</th><th /></tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id}>
                  <td><b>{i.name}</b><span className="sub mono">{i.url ?? i.param}</span><span className="sub">{utcDay(i.createdAt)} · {i.by ?? '—'}</span></td>
                  <td><Tag tone={i.channel === 'bot' ? 'accent' : 'violet'}>{CHANNEL[i.channel]}</Tag></td>
                  <StatCells s={i.stats} />
                  <td>
                    <div className="row">
                      <button type="button" className="btn" onClick={() => copy(i.url ?? i.param)}>Копировать</button>
                      {manage && <Link className="btn link" to={`/users?ref=${encodeURIComponent(i.code)}`}>Пользователи</Link>}
                      {manage && <button type="button" className="btn link" onClick={() => void toggleArchive(i.id, !i.archivedAt)}>{i.archivedAt ? 'Вернуть' : 'В архив'}</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && data.unknownCodes.length > 0 && (
        <Card title="Неизвестные коды" sub="Запуски с кодом, для которого нет ссылки (опечатка или удалённая ссылка)">
          <table className="table"><tbody>{data.unknownCodes.map((u) => (
            <tr key={u.code}><td className="mono">ref_{u.code}</td><StatCells s={u.stats} /></tr>
          ))}</tbody></table>
        </Card>
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </>
  );
}
