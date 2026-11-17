// Ads module client (docs/miniapp/ads-architecture.md §8): types of /api/admin/ads/* and their calls.
// Prices never appear here: an offer's odds come from the Mini App's verified odds store, not from the editor.
import { api, ApiError, errorText } from '../api';
import { parseLocalInput, toLocalInput as localInput } from '../format';

export type Tier = 'individual' | 'segment' | 'general';
export type Placement = 'quick_pick' | 'ai_pick';
export type OddsMode = 'optional' | 'required';
export type CampaignStatus = 'draft' | 'active' | 'paused' | 'archived';
export type CampaignState = CampaignStatus | 'scheduled' | 'ended';
export type Condition = Record<string, unknown> | null;

export interface Variant {
  key: 'A' | 'B' | 'C';
  weight: number;
  headline: string;
  body: string;
  extra: string;
  cta: string;
  url: string;
}

export interface Campaign {
  id: number;
  name: string;
  status: CampaignStatus;
  state: CampaignState;
  tier: Tier;
  priority: number;
  langIn: string[];
  placements: Placement[];
  countries: string[];
  allowUnknownCountry: boolean;
  startsAt: number;
  endsAt: number;
  partnerId: number;
  partnerName: string | null;
  disclosure: { advertiser?: string; adId?: string; ageLabel?: string };
  condition: Condition;
  controlPct: number;
  oddsMode: OddsMode;
  caps: { perDay: number; total: number };
  variants: Variant[];
  publishedVersion: number | null;
  createdAt: number;
  updatedAt: number;
}

export type CampaignInput = Omit<Campaign, 'id' | 'status' | 'state' | 'partnerName' | 'publishedVersion' | 'createdAt' | 'updatedAt'> & {
  id?: number;
};

export interface Partner {
  id: number;
  name: string;
  hosts: string[];
  countries: string[];
  allowUnknownCountry: boolean;
  ageMin: number;
  disclosure: { advertiser?: string; adId?: string; ageLabel?: string };
  active: boolean;
  postback: boolean;
  updatedAt: number;
}
export type PartnerInput = Omit<Partner, 'id' | 'postback' | 'updatedAt'> & { id?: number };

export interface Group { id: number; name: string; createdAt: number; members: number }

export interface Publication {
  latest: { version: number; sha256: string; campaigns: number; publishedAt: number } | null;
  fileVersion: number | null;
  appliedVersion: number | null;
  unpublishedChanges: boolean;
  publicKey: string;
}

export interface DisplaySettings { blockedCountries: string[]; ageGate: boolean; ageMin: number; updatedAt?: number | null }

export interface VariantStats {
  campaignId: number;
  campaign: string | null;
  variant: string;
  impressions: number;
  reach: number | null;
  clicks: number;
  clickers: number | null;
  ctr: number | null;
  uniqueCtr: number | null;
  uniqueCtrCi: [number, number] | null;
  control: number;
  conversions: number;
  partnerConversions: number;
}
export interface AdsReport {
  period: number;
  source: 'telemetry' | 'not_collected' | 'unavailable';
  aggregated: boolean;
  variants: VariantStats[];
  days: { day: string; impressions: number; clicks: number; control: number }[];
  countries: { country: string; impressions: number; clicks: number }[];
  segments: { segment: string; impressions: number; clicks: number; control: number }[];
  postbacks: { campaignId: number; variant: string; event: string; count: number; users: number }[];
}

export const PERIODS = [1, 7, 30, 90, 180, 365, 395] as const;
export type AdsPeriod = (typeof PERIODS)[number];
export const LANGS = ['az', 'ru', 'en', 'tr', 'uz', 'pt-BR', 'es'] as const;
export const SEGMENTS: Record<string, { label: string; params: Record<string, [string, number, number, number]> }> = {
  new: { label: 'Новые', params: { days: ['Регистрация не старше, дн', 7, 1, 365] } },
  active: { label: 'Активные', params: { days: ['Окно, дн', 7, 1, 90], min_visits: ['Дней с визитами, не меньше', 3, 1, 90] } },
  inactive: { label: 'Неактивные', params: { days: ['Не заходили, дн', 14, 1, 365] } },
  returned: { label: 'Вернувшиеся', params: { gap_days: ['Перерыв, дн', 14, 1, 365], within_days: ['Вернулись за, дн', 7, 1, 90] } },
  forecasts: { label: 'Запросы прогнозов', params: { days: ['Окно, дн', 7, 1, 90], min: ['Не меньше', 3, 1, 10000] } },
  coupons: { label: 'Купоны', params: { days: ['Окно, дн', 7, 1, 90], min: ['Не меньше', 1, 1, 10000] } },
  offer_clicks: { label: 'Клики по предложениям', params: { days: ['Окно, дн', 30, 1, 90], min: ['Не меньше', 1, 1, 10000] } },
};

const ADS_MESSAGES: Record<string, string> = {
  odds_in_text: 'В текстах не должно быть коэффициентов (чисел вида 1.85): цена берётся только из проверенной ленты.',
  odds_not_editable: 'Коэффициенты не редактируются: их даёт проверенная лента для рекомендованного исхода.',
  invalid_url: 'Ссылка: только https на домене партнёра, без порта и логина; из подстановок — только {click_id}.',
  url_host_not_allowed: 'Домен ссылки не входит в список доменов партнёра.',
  invalid_weights: 'Веса вариантов — целые проценты, в сумме 100.',
  invalid_variants: 'Варианты A/B/C: от одного до трёх, без повторов.',
  invalid_window: 'Окончание показа должно быть позже начала.',
  invalid_countries: 'Страны — коды ISO из двух букв (AZ, TR…), без XX и T1.',
  invalid_caps: 'Ограничение частоты: в день 0–100, всего 0–1000 (0 — без ограничения).',
  invalid_odds_mode: 'Неизвестный режим коэффициента.',
  invalid_condition: 'Проверьте условия аудитории.',
  too_many_conditions: 'Слишком много условий (не больше 30).',
  invalid_partner: 'Выберите партнёра.',
  partner_inactive: 'Партнёр выключен: кампанию нельзя запустить.',
  window_over: 'Срок показа уже закончился.',
  not_editable: 'Редактировать можно черновик или кампанию на паузе.',
  invalid_transition: 'Это действие недоступно в текущем статусе.',
  invalid_settings: 'Проверьте настройки показа.',
  invalid_host: 'Домены: от одного до 20, в нижнем регистре.',
  invalid_headline: 'Заголовок: от 1 до 80 символов.',
  invalid_cta: 'Текст кнопки: от 1 до 30 символов.',
  invalid_body: 'Текст: до 300 символов.',
  invalid_extra: 'Дополнительная строка: до 120 символов.',
  invalid_users: 'Telegram ID — положительные целые числа.',
  invalid_mb_ids: 'Отчёт партнёра: от 1 до 400 ID MB за раз.',
  telemetry_unavailable: 'Телеметрия сейчас недоступна — повторите позже.',
  snapshot_too_large: 'Снимок слишком велик (аудитории или кампании).',
  bad_period: 'Недопустимый период.',
};

export function adsError(error: unknown): string {
  if (error instanceof ApiError && ADS_MESSAGES[error.code]) return ADS_MESSAGES[error.code]!;
  return errorText(error);
}

export const adsApi = {
  campaigns: () => api<{ items: Campaign[]; partners: Partner[]; publication: Publication }>('/api/admin/ads/campaigns'),
  campaign: (id: number) => api<Campaign>(`/api/admin/ads/campaigns/${id}`),
  saveCampaign: (body: CampaignInput) => api<{ id: number }>('/api/admin/ads/campaigns', { method: 'POST', body }),
  setStatus: (id: number, action: 'activate' | 'pause' | 'archive') =>
    api<{ status: CampaignStatus }>(`/api/admin/ads/campaigns/${id}/status`, { method: 'POST', body: { action } }),
  copy: (id: number) => api<{ id: number }>(`/api/admin/ads/campaigns/${id}/copy`, { method: 'POST', body: {} }),
  partners: () => api<{ items: Partner[] }>('/api/admin/ads/partners'),
  savePartner: (body: PartnerInput) => api<{ id: number }>('/api/admin/ads/partners', { method: 'POST', body }),
  postbackToken: (id: number, revoke = false) =>
    api<{ token: string | null }>(`/api/admin/ads/partners/${id}/postback-token`, { method: 'POST', body: { revoke } }),
  groups: () => api<{ items: Group[] }>('/api/admin/ads/groups'),
  saveGroup: (name: string, id?: number) => api<{ id: number }>('/api/admin/ads/groups', { method: 'POST', body: { name, id } }),
  members: (id: number, add: number[], remove: number[]) =>
    api<{ added: number; removed: number; total: number }>(`/api/admin/ads/groups/${id}/members`, { method: 'POST', body: { add, remove } }),
  settings: () => api<DisplaySettings>('/api/admin/ads/settings'),
  saveSettings: (body: DisplaySettings) => api<DisplaySettings>('/api/admin/ads/settings', { method: 'POST', body }),
  publication: () => api<Publication>('/api/admin/ads/publication'),
  publish: (body: { reason: string; code: string; idempotencyKey: string; pauseAll?: boolean }) =>
    api<{ after: { version: number; campaigns: number[]; skipped: { id: number; code: string }[] }; fileWritten: boolean }>(
      '/api/admin/ads/publish', { method: 'POST', body }),
  report: (days: AdsPeriod, campaign: number | null) =>
    api<AdsReport>(`/api/admin/ads/analytics?days=${days}${campaign ? `&campaign=${campaign}` : ''}`),
  csvUrl: (days: AdsPeriod, campaign: number | null) =>
    `/api/admin/ads/analytics.csv?days=${days}${campaign ? `&campaign=${campaign}` : ''}`,
  conversions: (id: number, mbIds: string[]) =>
    api<{ reported: number; matched: number; added: number; attributed: number }>(
      `/api/admin/ads/campaigns/${id}/conversions`, { method: 'POST', body: { mbIds } }),
};

/** "AZ, tr ,UZ" -> ["AZ","TR","UZ"]; the server validates the codes. */
export function parseList(text: string, upper = true): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((s) => (upper ? s.trim().toUpperCase() : s.trim())).filter(Boolean))];
}

/** Telegram ids from free text; null when anything is not a positive integer. */
export function parseIds(text: string): number[] | null {
  const parts = text.split(/[\s,;]+/).filter(Boolean);
  const ids = parts.map(Number);
  return ids.every((n) => Number.isSafeInteger(n) && n > 0) ? [...new Set(ids)] : null;
}

/** Unix seconds <-> the value of an <input type="datetime-local"> read as UTC. */
/** Campaign windows are edited in the viewer's zone, like every other date in the panel (stored as epoch seconds). */
export function toLocalInput(ts: number): string {
  return localInput(new Date(ts * 1000));
}
export function fromLocalInput(value: string): number | null {
  return parseLocalInput(value);
}
