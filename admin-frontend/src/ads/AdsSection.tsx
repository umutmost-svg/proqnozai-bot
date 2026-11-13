import { useState } from 'react';
import { Link, NavLink, Route, Routes, useNavigate, useParams } from 'react-router-dom';
import { useLoad, useMe } from '../state';
import { DangerDialog, Empty, ErrorBox, Loading, PageHead, Status, Toast } from '../ui';
import { utc } from '../format';
import { AdsAnalyticsView } from './AdsAnalytics';
import type { Campaign, Group, Partner, Publication } from './adsApi';
import { adsApi, adsError } from './adsApi';
import { CampaignForm, blankCampaign, inputOf } from './CampaignForm';
import { GroupsView, PartnersView, SettingsView } from './AdsAdmin';

/**
 * The ads section of the admin (ads-architecture.md §8). Mounted by the shell with ONE route:
 *   <Route path="ads/*" element={<AdsRoutes />} />
 * and a sidebar link ['/ads', 'Реклама'] (docs/miniapp/ads-architecture.md §12).
 * Roles: owner — everything; admin — campaigns, groups, publication; analyst — read-only and analytics.
 */
export function AdsRoutes() {
  return (
    <Routes>
      <Route index element={<CampaignsPage />} />
      <Route path="campaigns/new" element={<CampaignPage />} />
      <Route path="campaigns/:id" element={<CampaignPage />} />
      <Route path="partners" element={<Page title="Партнёры" subtitle="Домены ссылок, страны, возраст, постбэк"><PartnersGate /></Page>} />
      <Route path="groups" element={<Page title="Группы" subtitle="Списки пользователей для таргетинга"><GroupsGate /></Page>} />
      <Route path="settings" element={<Page title="Настройки показа" subtitle="Запрещённые страны и возрастное подтверждение"><SettingsGate /></Page>} />
      <Route path="analytics" element={<Page title="Аналитика рекламы" subtitle="Показы, охват, клики, CTR, конверсии"><AdsAnalyticsView /></Page>} />
    </Routes>
  );
}

const TABS = [['/ads', 'Кампании'], ['/ads/analytics', 'Аналитика'], ['/ads/partners', 'Партнёры'], ['/ads/groups', 'Группы'],
  ['/ads/settings', 'Настройки']] as const;

function Tabs() {
  return (
    <nav className="tabs" aria-label="Реклама" style={{ marginBottom: 16 }}>
      {TABS.map(([to, label]) => <NavLink key={to} to={to} end={to === '/ads'}>{label}</NavLink>)}
    </nav>
  );
}

function Page({ title, subtitle, children, actions }: { title: string; subtitle: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <>
      <PageHead crumb="Реклама" title={title} subtitle={subtitle} actions={actions} />
      <Tabs />
      {children}
    </>
  );
}

export function useAdsRole() {
  const role = useMe().admin.role;
  return { manage: role === 'owner' || role === 'admin', owner: role === 'owner' };
}

const STATE_LABEL: Record<string, [string, string]> = {
  draft: ['warn', 'Черновик'], scheduled: ['info', 'Запланирована'], active: ['ok', 'Активна'], paused: ['warn', 'Пауза'],
  ended: ['', 'Завершена'], archived: ['', 'Архив'],
};

function CampaignsPage() {
  const { manage } = useAdsRole();
  const list = useLoad<{ items: Campaign[]; partners: Partner[]; publication: Publication }>('/api/admin/ads/campaigns');
  const [dialog, setDialog] = useState<null | 'publish' | 'stop'>(null);
  const [toast, setToast] = useState<string | null>(null);
  const publish = async (input: { reason: string; code: string; idempotencyKey: string }, pauseAll: boolean) => {
    const r = await adsApi.publish({ ...input, pauseAll });
    setDialog(null);
    const skipped = r.after.skipped.length ? `, пропущено: ${r.after.skipped.map((s) => `#${s.id} (${s.code})`).join(', ')}` : '';
    setToast(`Опубликована версия ${r.after.version}: кампаний ${r.after.campaigns.length}${skipped}`);
    list.reload();
  };
  const pub = list.data?.publication;
  return (
    <Page title="Реклама" subtitle="Кампании партнёров в «Рекомендуемой ставке» Mini App" actions={manage && (
      <>
        <Link className="btn" to="/ads/campaigns/new">Новая кампания</Link>
        <button type="button" className="btn primary" onClick={() => setDialog('publish')}>Опубликовать</button>
        <button type="button" className="btn danger" onClick={() => setDialog('stop')}>Остановить всё</button>
      </>
    )}>
      {pub && (
        <div className={`banner ${pub.unpublishedChanges ? 'warn' : ''}`} role="status" data-testid="ads-publication">
          {pub.latest ? `Опубликована версия ${pub.latest.version} (${utc(pub.latest.publishedAt)}), кампаний: ${pub.latest.campaigns}. ` : 'Ещё ничего не опубликовано. '}
          {pub.appliedVersion !== null ? `Mini App применил версию ${pub.appliedVersion}. ` : 'Mini App снимок ещё не применял. '}
          {pub.unpublishedChanges && <strong>Есть неопубликованные изменения — пользователи их не видят до публикации.</strong>}
        </div>
      )}
      {list.error ? <ErrorBox error={list.error} onRetry={list.reload} /> : !list.data ? <Loading /> : list.data.items.length === 0 ? (
        <Empty title="Кампаний пока нет" action={manage ? <Link to="/ads/campaigns/new">Создать кампанию →</Link> : undefined} />
      ) : (
        <div className="card table-card">
          <table className="table" data-testid="ads-campaigns">
            <thead><tr><th>Кампания</th><th>Статус</th><th>Партнёр</th><th>Уровень · приоритет</th><th>Показ</th><th>Варианты</th><th>Коэфф.</th></tr></thead>
            <tbody>
              {list.data.items.map((c) => {
                const [tone, label] = STATE_LABEL[c.state] ?? ['', c.state];
                return (
                  <tr key={c.id}>
                    <td><Link to={`/ads/campaigns/${c.id}`}>{c.name}</Link><div className="small muted">#{c.id}</div></td>
                    <td><span className={`chip ${tone}`}>{label}</span></td>
                    <td>{c.partnerName ?? '—'}</td>
                    <td>{c.tier} · {c.priority}</td>
                    <td className="small">{utc(c.startsAt)} — {utc(c.endsAt)}</td>
                    <td>{c.variants.map((v) => `${v.key} ${v.weight}%`).join(' / ')}{c.controlPct ? ` · контроль ${c.controlPct}%` : ''}</td>
                    <td className="small">{c.oddsMode === 'required' ? 'обязателен' : 'если есть'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {dialog && (
        <DangerDialog
          title={dialog === 'publish' ? 'Опубликовать рекламу?' : 'Остановить все кампании?'}
          lead={dialog === 'publish'
            ? 'Mini App получит активные кампании, настройки показа и токены постбэка в течение 30 секунд.'
            : 'Все активные кампании встанут на паузу, Mini App получит пустой снимок в течение 30 секунд.'}
          confirmLabel={dialog === 'publish' ? 'Опубликовать' : 'Остановить'}
          tone={dialog === 'publish' ? 'primary' : 'danger'}
          onConfirm={(input) => publish(input, dialog === 'stop')}
          onClose={() => setDialog(null)}
        />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </Page>
  );
}

function CampaignPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { manage } = useAdsRole();
  const isNew = id === undefined;
  const item = useLoad<Campaign>(isNew ? null : `/api/admin/ads/campaigns/${id}`);
  const partners = useLoad<{ items: Partner[] }>('/api/admin/ads/partners');
  const groups = useLoad<{ items: Group[] }>('/api/admin/ads/groups');
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [now] = useState(() => Math.floor(Date.now() / 1000));
  const loadError = item.error ?? partners.error ?? groups.error;
  const ready = partners.data && groups.data && (isNew || item.data);
  const act = async (fn: () => Promise<unknown>, done: string) => {
    setError(null);
    try {
      await fn();
      setToast(done);
      item.reload();
    } catch (err) {
      setError(adsError(err));
    }
  };
  const c = item.data;
  const editable = manage && (isNew || c?.status === 'draft' || c?.status === 'paused');
  return (
    <Page title={isNew ? 'Новая кампания' : c?.name ?? 'Кампания'} subtitle={c ? `#${c.id} · опубликована версия ${c.publishedVersion ?? '—'}` : 'Черновик'} actions={c && manage && (
      <>
        {c.status !== 'active' && c.status !== 'archived' && <button type="button" className="btn primary" onClick={() => act(() => adsApi.setStatus(c.id, 'activate'), 'Кампания активна — опубликуйте изменения')}>Запустить</button>}
        {c.status === 'active' && <button type="button" className="btn" onClick={() => act(() => adsApi.setStatus(c.id, 'pause'), 'Пауза — опубликуйте изменения')}>Пауза</button>}
        {(c.status === 'draft' || c.status === 'paused') && <button type="button" className="btn" onClick={() => act(() => adsApi.setStatus(c.id, 'archive'), 'В архиве')}>В архив</button>}
        <button type="button" className="btn" onClick={() => act(async () => navigate(`/ads/campaigns/${(await adsApi.copy(c.id)).id}`), 'Создана копия')}>Копировать</button>
      </>
    )}>
      {c && <p className="small">Статус: <Status value={c.state} label={STATE_LABEL[c.state]?.[1]} /></p>}
      {error && <div className="form-error" role="alert">{error}</div>}
      {loadError ? <ErrorBox error={loadError} /> : !ready ? <Loading rows={6} /> : partners.data!.items.length === 0 ? (
        <Empty title="Сначала добавьте партнёра" action={<Link to="/ads/partners">Партнёры →</Link>} />
      ) : (
        <CampaignForm
          key={c ? `${c.id}:${c.updatedAt}` : 'new'}
          initial={c ? inputOf(c) : blankCampaign(partners.data!.items[0]!.id, now)}
          partners={partners.data!.items}
          groups={groups.data!.items}
          readOnly={!editable}
          onSaved={(saved) => { setToast('Сохранено'); if (isNew) navigate(`/ads/campaigns/${saved}`); else item.reload(); }}
        />
      )}
      {c && !editable && manage && <p className="small muted">Активную кампанию нельзя менять на ходу: поставьте её на паузу.</p>}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </Page>
  );
}

function PartnersGate() {
  const { owner } = useAdsRole();
  return <PartnersView owner={owner} />;
}

function GroupsGate() {
  const { manage } = useAdsRole();
  return <GroupsView manage={manage} />;
}

function SettingsGate() {
  const { owner } = useAdsRole();
  return <SettingsView owner={owner} />;
}
