import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ReactNode } from 'react';
import { AuthProvider, PrefsProvider, useAuth } from '../state';
import { AdsRoutes } from '.';

/** The section as the shell mounts it (<Route path="ads/*" element={<AdsRoutes />} />): routes, publication, roles. */
type Call = { url: string; method: string; body: Record<string, unknown> | undefined };

const CAMPAIGN = {
  id: 7, name: 'Осень', status: 'active', state: 'active', tier: 'general', priority: 5, langIn: [], placements: ['ai_pick'],
  countries: [], allowUnknownCountry: false, startsAt: 1_800_000_000, endsAt: 1_900_000_000, partnerId: 4, partnerName: 'Partner MB',
  disclosure: {}, condition: null, controlPct: 10, oddsMode: 'required', caps: { perDay: 1, total: 0 },
  variants: [{ key: 'A', weight: 100, headline: 'Бонус', body: '', extra: '', cta: 'Go', url: 'https://p.example.com/' }],
  publishedVersion: 3, createdAt: 1, updatedAt: 2,
};
const REPORT = { period: 30, source: 'telemetry', aggregated: false, variants: [], days: [{ day: '2027-01-01', impressions: 4, clicks: 1, control: 0 }],
  countries: [], segments: [], postbacks: [] };

function SignedIn({ children }: { children: ReactNode }) {
  return useAuth().state.status === 'signed-in' ? <>{children}</> : null;
}

function mount(role: 'owner' | 'admin' | 'analyst', path: string, opts: { written?: boolean } = {}) {
  const calls: Call[] = [];
  const disk = { version: null as number | null, publishes: 0 };
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    calls.push({ url, method, body: init.body ? JSON.parse(String(init.body)) : undefined });
    let data: unknown = {};
    if (url === '/api/admin/auth/me') data = { admin: { id: 1, email: 'o@x.test', role }, csrf: 'c', pilot: { dataset: 'real', demoTelemetry: false } };
    else if (url === '/api/admin/ads/campaigns') {
      // The first status read after a failed write still finds the old file; the next one finds it repaired.
      const latest = disk.publishes ? { version: 9, sha256: 's', campaigns: 0, publishedAt: 1_800_000_000 } : null;
      data = { items: [CAMPAIGN], partners: [], publication: { latest, fileVersion: disk.version, appliedVersion: null, unpublishedChanges: !latest, publicKey: 'k' } };
      if (disk.publishes) disk.version = 9;
    }
    else if (url === '/api/admin/ads/publish') {
      disk.publishes += 1;
      if (opts.written ?? true) disk.version = 9;
    }
    if (url === '/api/admin/ads/publish') data = { after: { version: 9, campaigns: [7], skipped: [] }, fileWritten: opts.written ?? true };
    else if (url.startsWith('/api/admin/ads/analytics')) data = REPORT;
    return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter initialEntries={[path]}>
          <SignedIn><Routes><Route path="ads/*" element={<AdsRoutes />} /></Routes></SignedIn>
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe('ads section mounted at /ads/*', () => {
  it('lists campaigns, publishes and stops everything with a reason and a 2FA code', async () => {
    const calls = mount('owner', '/ads');
    const table = await screen.findByTestId('ads-campaigns');
    expect(within(table).getByText('Осень')).toBeInTheDocument();
    expect(screen.getByTestId('ads-publication')).toHaveTextContent('неопубликованные изменения');
    for (const [button, confirm, pauseAll] of [['Опубликовать', 'Опубликовать', false], ['Остановить всё', 'Остановить', true]] as const) {
      await userEvent.click(screen.getByRole('button', { name: button }));
      const dialog = screen.getByRole('dialog');
      await userEvent.type(within(dialog).getByLabelText('Причина'), 'запуск');
      await userEvent.type(within(dialog).getByLabelText('Код двухфакторной аутентификации'), '123456');
      await userEvent.click(within(dialog).getByRole('button', { name: confirm }));
      await waitFor(() => expect(calls.filter((c) => c.url === '/api/admin/ads/publish').at(-1)?.body).toMatchObject({ pauseAll, reason: 'запуск', code: '123456' }));
    }
  });

  it('an analyst reads campaigns and analytics but gets no actions and no conversion upload', async () => {
    mount('analyst', '/ads');
    await screen.findByTestId('ads-campaigns');
    expect(screen.queryByRole('button', { name: 'Опубликовать' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Новая кампания' })).toBeNull();
  });

  it('analytics shows the daily series; the conversion upload is for owners and admins only', async () => {
    mount('analyst', '/ads/analytics');
    expect(await screen.findByTestId('ads-days')).toHaveTextContent('2027-01-01');
    await screen.findByRole('option', { name: '#7 Осень' });
    await userEvent.selectOptions(await screen.findByLabelText('Кампания'), '7');
    await screen.findByTestId('ads-days');
    expect(screen.queryByLabelText(/ID MB из отчёта/)).toBeNull();
  });

  it('an admin choosing a campaign can upload the partner report', async () => {
    mount('admin', '/ads/analytics');
    await screen.findByRole('option', { name: '#7 Осень' });
    await userEvent.selectOptions(await screen.findByLabelText('Кампания'), '7');
    expect(await screen.findByLabelText(/ID MB из отчёта/)).toBeInTheDocument();
  });

  it('a stop committed without its snapshot file says the Mini App still shows the old ads', async () => {
    mount('owner', '/ads', { written: false });
    await screen.findByTestId('ads-campaigns');
    await userEvent.click(screen.getByRole('button', { name: 'Остановить всё' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Причина'), 'стоп');
    await userEvent.type(within(dialog).getByLabelText('Код двухфакторной аутентификации'), '123456');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Остановить' }));
    expect(await screen.findByTestId('ads-write-failed')).toHaveTextContent('файл снимка не записан');
    expect(screen.getByTestId('ads-publication')).toHaveTextContent('На диске версия —, не 9');
    await userEvent.click(screen.getByRole('button', { name: 'Проверить снова' }));
    await waitFor(() => expect(screen.queryByTestId('ads-write-failed')).toBeNull());
    expect(screen.getByTestId('ads-publication')).toHaveTextContent('совпадает');
  });
});
