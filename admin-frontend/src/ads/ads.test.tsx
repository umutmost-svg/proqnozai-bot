import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PartnersView, SettingsForm } from './AdsAdmin';
import { ReportView } from './AdsAnalytics';
import type { AdsReport, Partner } from './adsApi';
import { fromLocalInput, parseIds, parseList, toLocalInput } from './adsApi';
import { CampaignForm, blankCampaign } from './CampaignForm';
import { fromCondition, newRule, toCondition } from './Targeting';

type Call = { url: string; method: string; body: unknown };

function mockFetch(handler: (url: string, method: string, body: unknown) => [number, unknown]) {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });
    const [status, data] = handler(url, method, body);
    return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

const PARTNER: Partner = {
  id: 4, name: 'Partner MB', hosts: ['p.example.com'], countries: ['AZ', 'TR'], allowUnknownCountry: false, ageMin: 18,
  disclosure: { advertiser: 'Partner LLC', ageLabel: '18+' }, active: true, postback: false, updatedAt: 1,
};

describe('audience builder', () => {
  it('turns a flat rule list into a condition and back', () => {
    const geo = { ...newRule('geo_in'), text: 'az, TR' };
    const users = { ...newRule('user_in'), text: '101, 202', not: true };
    const seg = newRule('segment');
    const tree = toCondition([geo, users, seg], 'any');
    expect(tree).toEqual({ any: [{ geo_in: ['AZ', 'TR'] }, { not: { user_in: [101, 202] } },
      { segment: { name: 'active', params: { days: 7, min_visits: 3 } } }] });
    const back = fromCondition(tree);
    expect(back?.join).toBe('any');
    expect(toCondition(back!.rules, back!.join)).toEqual(tree);
    expect(toCondition([], 'all')).toBeNull();
    expect(toCondition([{ ...newRule('mb_verified') }], 'all')).toEqual({ mb_verified: true });
  });

  it('nested or redacted trees go to the JSON editor; incomplete rules are reported', () => {
    expect(fromCondition({ all: [{ any: [{ geo_unknown: true }] }] })).toBeNull();
    expect(fromCondition({ user_in: { count: 3 } })).toBeNull(); // the analyst's redacted view
    expect(fromCondition({ mb_id_in: ['MB1'] })).toBeNull();
    expect(() => toCondition([newRule('geo_in')], 'all')).toThrow('Укажите страны');
    expect(() => toCondition([{ ...newRule('user_in'), text: '12, x' }], 'all')).toThrow();
  });

  it('parses lists, ids and UTC datetimes', () => {
    expect(parseList(' az,tr; az ')).toEqual(['AZ', 'TR']);
    expect(parseIds('1 2,2')).toEqual([1, 2]);
    expect(parseIds('1 -2')).toBeNull();
    expect(fromLocalInput(toLocalInput(1_800_000_000))).toBe(1_800_000_000 - (1_800_000_000 % 60));
  });
});

describe('campaign editor', () => {
  it('has no odds field and sends texts, links, A/B weights, schedule, caps and the price rule', async () => {
    const calls = mockFetch(() => [200, { id: 9 }]);
    const onSaved = vi.fn();
    render(<CampaignForm initial={blankCampaign(4, 1_800_000_000)} partners={[PARTNER]} groups={[]} readOnly={false} onSaved={onSaved} />);
    expect(screen.queryByLabelText(/^Коэффициент/)).toBeNull();
    await userEvent.type(screen.getByLabelText('Внутреннее название'), 'Осень');
    await userEvent.selectOptions(screen.getByLabelText('Варианты A/B'), '2');
    const a = screen.getByTestId('variant-A');
    const b = screen.getByTestId('variant-B');
    for (const [card, key] of [[a, 'A'], [b, 'B']] as const) {
      await userEvent.type(within(card).getByLabelText('Заголовок'), `Бонус ${key}`);
      await userEvent.type(within(card).getByLabelText('Текст кнопки'), 'Забрать');
      await userEvent.type(within(card).getByLabelText('Ссылка партнёра'), `https://p.example.com/${key}?sub={{click_id}`);
    }
    await userEvent.clear(screen.getByLabelText('Вес A'));
    await userEvent.type(screen.getByLabelText('Вес A'), '70');
    expect(screen.getByRole('alert')).toHaveTextContent('120 %');
    expect(screen.getByRole('button', { name: 'Сохранить' })).toBeDisabled();
    await userEvent.clear(screen.getByLabelText('Вес B'));
    await userEvent.type(screen.getByLabelText('Вес B'), '30');
    await userEvent.type(screen.getByLabelText('Страны (пусто — все страны партнёра)'), 'az');
    await userEvent.clear(screen.getByLabelText(/Показов в день/));
    await userEvent.type(screen.getByLabelText(/Показов в день/), '2');
    await userEvent.selectOptions(screen.getByLabelText('Без проверенного коэффициента'), 'required');
    await userEvent.selectOptions(screen.getByLabelText('Добавить условие'), 'segment');
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(9));
    const sent = calls[0]!.body as Record<string, unknown>;
    expect(calls[0]!.url).toBe('/api/admin/ads/campaigns');
    expect(sent).not.toHaveProperty('odds');
    expect(sent).toMatchObject({
      name: 'Осень', countries: ['AZ'], oddsMode: 'required', caps: { perDay: 2, total: 0 },
      condition: { segment: { name: 'active', params: { days: 7, min_visits: 3 } } },
      variants: [
        { key: 'A', weight: 70, headline: 'Бонус A', cta: 'Забрать', url: 'https://p.example.com/A?sub={click_id}' },
        { key: 'B', weight: 30, headline: 'Бонус B', cta: 'Забрать', url: 'https://p.example.com/B?sub={click_id}' },
      ],
    });
  });

  it('shows the server’s reason when a text states odds', async () => {
    mockFetch(() => [400, { error: 'odds_in_text' }]);
    render(<CampaignForm initial={{ ...blankCampaign(4, 1_800_000_000), name: 'x' }} partners={[PARTNER]} groups={[]} readOnly={false} onSaved={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('коэффициентов');
  });

  it('is read-only for an analyst or a running campaign', () => {
    render(<CampaignForm initial={blankCampaign(4, 1_800_000_000)} partners={[PARTNER]} groups={[]} readOnly onSaved={() => {}} />);
    expect(screen.queryByRole('button', { name: 'Сохранить' })).toBeNull();
    expect(screen.getByLabelText('Внутреннее название')).toBeDisabled();
  });
});

describe('partners', () => {
  it('shows a new postback token once and never keeps it in the list', async () => {
    let postback = false;
    const calls = mockFetch((_url, method) => {
      if (method === 'POST') {
        postback = true;
        return [200, { token: 'secret-token-value-123456789012345678901234' }];
      }
      return [200, { items: [{ ...PARTNER, postback }] }];
    });
    render(<PartnersView owner />);
    await userEvent.click(await screen.findByRole('button', { name: 'Выдать токен' }));
    const banner = await screen.findByTestId('postback-token');
    expect(banner).toHaveTextContent('secret-token-value');
    expect(banner).toHaveTextContent('X-Postback-Token');
    expect(calls.find((c) => c.method === 'POST')!.url).toBe('/api/admin/ads/partners/4/postback-token');
    await userEvent.click(screen.getByRole('button', { name: 'Я сохранил токен' }));
    expect(screen.queryByText(/secret-token-value/)).toBeNull();
    expect(await screen.findByRole('button', { name: 'Новый токен' })).toBeInTheDocument();
  });

  it('a non-owner sees partners without actions', async () => {
    mockFetch(() => [200, { items: [PARTNER] }]);
    render(<PartnersView owner={false} />);
    expect(await screen.findByTestId('ads-partners')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Выдать токен' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Добавить партнёра' })).toBeNull();
  });
});

describe('display settings', () => {
  it('saves blocked countries and the age gate (owner only)', async () => {
    const calls = mockFetch(() => [200, {}]);
    const { unmount } = render(<SettingsForm initial={{ blockedCountries: [], ageGate: false, ageMin: 18 }} owner onSaved={() => {}} />);
    await userEvent.type(screen.getByLabelText(/Запрещённые страны/), 'ru, us');
    await userEvent.click(screen.getByLabelText(/Возрастное подтверждение/));
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(calls[0]?.body).toEqual({ blockedCountries: ['RU', 'US'], ageGate: true, ageMin: 18 }));
    unmount();
    render(<SettingsForm initial={{ blockedCountries: ['RU'], ageGate: true, ageMin: 18 }} owner={false} onSaved={() => {}} />);
    expect(screen.getByLabelText(/Запрещённые страны/)).toBeDisabled();
  });
});

describe('ads analytics', () => {
  const report: AdsReport = {
    period: 365, source: 'telemetry', aggregated: true,
    variants: [{ campaignId: 1, campaign: 'Осень', variant: 'A', impressions: 200, reach: null, clicks: 10, clickers: null, ctr: 0.05,
      uniqueCtr: null, uniqueCtrCi: null, control: 0, conversions: 2, partnerConversions: 3 }],
    days: [], countries: [{ country: 'AZ', impressions: 200, clicks: 10 }],
    segments: [{ segment: 'new', impressions: 50, clicks: 5, control: 0 }],
    postbacks: [{ campaignId: 1, variant: 'A', event: 'first_deposit', count: 3, users: 3 }],
  };

  it('a 13-month period says that reach is not kept and shows segments and postbacks', () => {
    render(<ReportView report={report} />);
    expect(screen.getByTestId('ads-aggregated')).toBeInTheDocument();
    const row = within(screen.getByTestId('ads-variants')).getAllByRole('row')[1]!;
    expect(row).toHaveTextContent('Осень');
    expect(row).toHaveTextContent('200');
    expect(row).toHaveTextContent('5%');
    expect(within(row).getAllByText('—').length).toBeGreaterThanOrEqual(2); // reach and unique CTR unknown
    expect(screen.getByTestId('ads-segments')).toHaveTextContent('Новые (до 7 дней)');
    expect(screen.getByTestId('ads-segments')).toHaveTextContent('10%');
    expect(screen.getByTestId('ads-postbacks')).toHaveTextContent('first_deposit');
  });

  it('unreadable telemetry is not zero', () => {
    render(<ReportView report={{ ...report, aggregated: false, source: 'unavailable' }} />);
    expect(screen.getByRole('status')).toHaveTextContent('не «ноль»');
  });
});

describe('partner editing', () => {
  it('switching to another partner’s row edits that partner', async () => {
    const other = { ...PARTNER, id: 5, name: 'Second' };
    const calls = mockFetch((_url, method) => (method === 'POST' ? [200, { id: 5 }] : [200, { items: [PARTNER, other] }]));
    render(<PartnersView owner />);
    const edits = await screen.findAllByRole('button', { name: 'Изменить' });
    await userEvent.click(edits[0]!);
    expect(screen.getByLabelText('Название')).toHaveValue('Partner MB');
    await userEvent.click(edits[1]!);
    expect(screen.getByLabelText('Название')).toHaveValue('Second');
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ id: 5, name: 'Second' }));
  });
});
