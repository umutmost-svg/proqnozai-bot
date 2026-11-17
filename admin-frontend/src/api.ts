// Same-origin JSON client of the admin backend. Session = HttpOnly cookie; mutating
// requests carry the session's CSRF token (the browser adds Origin itself).

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public requestId: string | null,
  ) {
    super(code);
  }
}

let csrfToken = '';
let onUnauthenticated: () => void = () => {};
const SIGN_IN_PATHS = new Set(['/api/admin/auth/login', '/api/admin/auth/totp', '/api/admin/auth/invite',
  '/api/admin/auth/invite/accept', '/api/admin/auth/me']);

// Which session a request belongs to: a 401 of a request sent before a new sign-in is stale, not a lost session.
let generation = 0;

export function setCsrf(token: string): void {
  if (token && token !== csrfToken) generation += 1;
  csrfToken = token;
}

export function setUnauthenticatedHandler(fn: () => void): void {
  onUnauthenticated = fn;
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? 'GET';
  const sentIn = generation;
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (init.body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      credentials: 'same-origin',
      signal: AbortSignal.timeout(15000),
    });
  } catch {
    throw new ApiError(0, 'network', null);
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const payload = (data ?? {}) as { error?: string; requestId?: string };
    const error = new ApiError(response.status, payload.error ?? 'http_' + response.status,
      payload.requestId ?? response.headers.get('X-Request-ID'));
    // The sign-in steps answer 401 for a wrong password or an expired code: that is not a lost session.
    if (response.status === 401 && !SIGN_IN_PATHS.has(path) && sentIn === generation) onUnauthenticated();
    throw error;
  }
  return data as T;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

const MESSAGES: Record<string, string> = {
  network: 'Сервер админки недоступен. Попробуйте обновить страницу через минуту.',
  invalid_credentials: 'Неверная электронная почта или пароль, либо вход временно заблокирован после нескольких ошибок.',
  locked: 'Учётная запись временно заблокирована после нескольких ошибок кода 2FA (от 15 минут, каждая следующая блокировка за сутки дольше).',
  invalid_password: 'Неверный текущий пароль.',
  weak_password: 'Новый пароль: от 12 до 200 символов.',
  same_password: 'Новый пароль совпадает с текущим.',
  conflict: 'Пароль только что изменили в другой вкладке. Обновите страницу.',
  use_password_change: 'Свой вход сбросить нельзя: смените пароль в разделе «Безопасность».',
  not_active: 'Сбросить вход можно только у активной учётной записи.',
  broadcast_changed: 'Рассылку изменили после проверки. Проверьте текст и аудиторию ещё раз и запланируйте снова.',
  audience_changed: 'Получатели изменились после проверки (или ещё не загрузились). Откройте подтверждение заново.',
  message_rate_limited: 'Лимит личных сообщений: 60 в час и 300 в сутки на администратора. Попробуйте позже.',
  invalid_code: 'Неверный или уже использованный код. Дождитесь следующего кода.',
  expired: 'Время на ввод кода истекло. Войдите заново.',
  reason_required: 'Укажите причину (от 3 символов).',
  forbidden: 'Недостаточно прав для этого действия.',
  last_owner: 'Последнего владельца нельзя понизить или отключить.',
  email_taken: 'Такая учётная запись уже есть.',
  invalid_email: 'Проверьте адрес электронной почты.',
  invitation_pending: 'Приглашение ещё не принято: учётную запись нельзя включить.',
  invalid_value: 'Недопустимое значение.',
  csrf: 'Сессия устарела. Обновите страницу.',
  unauthenticated: 'Сессия истекла. Войдите заново.',
  not_found: 'Не найдено.',
  insufficient_balance: 'Недостаточно кредитов для списания.',
  invalid_rules: 'Проверьте условия сегмента.',
  name_taken: 'Сегмент с таким названием уже есть.',
  invalid_buttons: 'Кнопка: текст до 40 символов и ссылка https:// с адресом сайта или Mini App.',
  invalid_html: 'Разметка: только теги Telegram (b, i, u, s, code, pre, a href, blockquote, tg-spoiler), каждый закрыт.',
  body_too_long: 'Текст слишком длинный с учётом разметки и имени.',
  invalid_body: 'В тексте должно быть что-то кроме {имя}: у части пользователей имени нет.',
  not_cancellable: 'Рассылку уже нельзя отменить: она отправлена или отменена.',
  access_not_published: 'Блокировка записана, но Mini App её ещё не получил. Повторите действие.',
  not_a_draft: 'Изменять можно только черновик.',
  invalid_time: 'Время отправки: от текущего момента до 90 дней вперёд.',
  idempotency_key_reused: 'Повтор с другими данными. Откройте диалог заново.',
  invalid_mb_id: 'ID MB: только латиница, цифры, «-» и «_», до 32 символов.',
  mb_id_taken: 'Этот ID MB уже указан у другого пользователя.',
  invalid_amount: 'Сумма от 1 до 100 000.',
  invalid_message: 'Введите текст сообщения.',
  invalid_note: 'Введите текст заметки.',
  invalid_tag: 'Тег — от 1 до 32 символов.',
  invalid_name: 'Введите название (до 80 символов).',
  invalid_channel: 'Выберите канал.',
  invalid_code_format: 'Код: латиница в нижнем регистре и цифры, 3–24 символа.',
  invalid_platform: 'Выберите платформу из списка.', invalid_geo: 'Гео — двухбуквенный код страны (AZ, TR…).',
  invalid_pid: 'PID: латиница, цифры, _ . -, до 40 символов.',
  code_taken: 'Такой код уже используется.',
  retention_not_applied: 'Срок сохранён, но ещё не применён к файлу бота. Повторите — изменение применится.',
  inquiries_unavailable: 'Файл обращений сейчас не читается. Повторите позже.',
  dialogs_not_allowed: 'Нет права «Диалоги»: его выдаёт владелец.',
  credits_unavailable: 'Кредиты Mini App сейчас недоступны. Повторите позже.',
  invalid_days: 'Дней безлимита: от 0 до 366.',
  invalid_credit: 'Проверьте операцию и сумму.',
  invalid_users: 'Выберите от 1 до 5000 пользователей.',
  group_too_large: 'Слишком большая группа для одной операции.',
  too_large: 'Запрос слишком большой.',
  bad_filter: 'Недопустимый фильтр.',
  invalid_delete_after: 'Выберите срок автоудаления из списка.',
  invalid_broadcast: 'Рассылка не найдена или повреждена. Обновите страницу.',
  idempotency_key_required: 'Обновите страницу и повторите действие.',
  rate_limited: 'Слишком много запросов. Повторите через минуту.',
  internal: 'Внутренняя ошибка сервера.',
};

/** Why the bot could not deliver a job (codes from bot_outbox.py). "Unknown outcome" must not invite a manual resend. */
export const JOB_ERROR: Record<string, string> = {
  blocked_by_user: 'пользователь заблокировал бота или не запускал его',
  bad_request: 'Telegram отклонил сообщение',
  unknown_outcome: 'исход неизвестен (сбой сети во время отправки) — возможно, доставлено; не отправляйте повторно вручную',
  interrupted: 'исход неизвестен (бот перезапустился во время отправки) — возможно, доставлено',
  not_sent: 'нет связи с Telegram — бот повторит сам',
  rate_limited: 'лимит Telegram — бот повторит сам',
  bot_ban: 'пользователь заблокирован в боте',
  error: 'ошибка',
};

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return MESSAGES[error.code] ?? `Ошибка: ${error.code}`;
  return 'Неизвестная ошибка.';
}

// ---- response types ----

export type Role = 'owner' | 'admin' | 'analyst' | 'promoter';
export interface Me {
  admin: { id: number; email: string; role: Role; grants?: string[] };
  csrf: string;
  pilot: { dataset: string; demoTelemetry: boolean };
}

export interface DaySeries {
  day: string; dau: number; new: number; aiCreated: number; aiOpened: number; quick: number; coupons: number;
  aiCostUsd: number; apiAttempts: number; api429: number; apiErrors: number;
}
export type Telemetry = { source: 'not_collected' } | { source: 'demo'; series: DaySeries[]; wau: number; mau: number };

export interface ForecastRow {
  key: string; eventId: string; tournamentId: string; tournament: string; match: string | null;
  homeTeamId: string; awayTeamId: string; createdAt: string; kickoffAt: string; modelVersion: string;
  pick: string; pickProbability: number | null; noBetReason: string | null; score: [number, number] | null;
  status: 'pending' | 'settled' | 'void'; settledAt: string | null; revision: number;
}
export interface ForecastDetail extends ForecastRow {
  display: Record<string, number>; expectedGoals: [number, number]; sample: number; dataUntil: string;
  llmModel: string | null; source: string;
  settlements: { revision: number; score: [number, number] | null; status: string; recordedAt: string }[];
}
export interface QualityVersion {
  forecasts: number; logLoss1x2: number; brier1x2: number;
  markets: Record<string, { logLoss: number; ece: number }>;
  picks: { count: number; hitRate: number | null };
  byTournament: Record<string, { forecasts: number; logLoss1x2: number }>;
  prospectiveGate: { forecasts: number; minimum: number; passed: boolean };
}
export interface Quality {
  settled: number; minimum: number; currentVersion: string; currentGateForecasts: number;
  versions: Record<string, QualityVersion>;
}
export interface Overview {
  period: number;
  users: { total: number; new: number; loggedIn: number; withActiveSession: number };
  forecasts: { pending: number; settled: number; void: number; total: number; createdInPeriod: number };
  quality: Quality;
  upcoming: ForecastRow[];
  telemetry: Telemetry;
}
export interface UserRow {
  id: number; username: string | null; firstName: string | null; lastName: string | null; lang: string;
  createdAt: number; lastLoginAt: number; activeSessions: number;
}
export interface AdminEvent {
  id: number; at: number; adminId: number | null; adminEmail: string | null; action: string;
  targetType: string | null; targetId: string | null; before: Record<string, unknown> | null;
  after: Record<string, unknown> | null; reason: string | null; result: string;
}
export interface AiCall {
  id: string; at: number; operation: string; model: string; inputTokens: number | null; outputTokens: number | null;
  costUsd: number | null; latencyMs: number; result: string; userId: number | null;
}
export interface UserProfile extends UserRow {
  sessions: { createdAt: number; expiresAt: number; revokedAt: number | null; state: string }[];
  events: AdminEvent[];
  aiCalls: AiCall[] | null;
  credits: { enabled: boolean; balance: number | null; unlimited: boolean };
}
export interface Page<T> { total: number; page: number; size: number; items: T[] }
export interface AdminAccount {
  id: number; email: string; role: Role; state: 'active' | 'invited' | 'disabled'; twoFactor: boolean;
  lastLoginAt: number | null; createdAt: number;
}
export interface SettingValue { value: boolean | number; version: number | null; updatedAt: number | null; type: string; min?: number; max?: number; permission?: string }

// ---- CRM v2 ----

export type Rules = Partial<{
  registered_within_days: number; active_within_days: number; inactive_for_days: number; lang_in: string[];
  tag_has: string; tag_not: string; has_active_session: boolean; blocked: boolean;
  country_in: string[]; platform_in: string[]; forecasts_min: number; active_days_min: number; used_ai: boolean;
  opened_miniapp: boolean; clicked_ad: boolean; source_in: string[];
}>;
export interface Source { channel: 'bot' | 'miniapp'; code: string; at: number; linkId: number | null; name: string | null }
export interface ReferralStats { starts: number; botStarts: number; miniappStarts: number; inMiniApp: number; active7d: number; converted: number | null }
export interface ReferralLink {
  id: number; code: string; name: string; channel: 'bot' | 'miniapp'; archivedAt: number | null; createdAt: number;
  by: string | null; param: string; url: string | null; stats: ReferralStats;
  platform: string | null; geo: string | null; pid: string | null;
}
export interface ReferralGroup extends ReferralStats { value: string; links: number }
export interface ReferralsPage {
  items: ReferralLink[]; funnel: { starts: number; inMiniApp: number; active7d: number; converted: number | null };
  unknownCodes: { code: string; stats: ReferralStats }[]; telemetry: boolean; names: { bot: string | null; miniapp: string | null };
  breakdown?: { platform: ReferralGroup[]; geo: ReferralGroup[]; pid: ReferralGroup[] };
}
export interface InquiriesPage {
  collected: boolean; retentionDays: number;
  items: { userId: number; count: number; lastAt: number; name: string | null; username: string | null; status: 'open' | 'closed' }[];
}
export interface InquiryThread {
  userId: number; noticeAt: number | null; status: 'open' | 'closed'; truncated?: { inbound: boolean; outbound: boolean };
  user: { firstName: string | null; lastName: string | null; username: string | null; lang: string } | null;
  history: { id: string; from: 'user' | 'admin'; at: number; text: string; by?: string | null; status?: string; error?: string | null }[];
}
export interface OutboxPage {
  summary: { connected: boolean; byStatus: Record<string, number>; last24h: Record<string, number>; nextDueAt: number | null; lastSentAt: number | null;
    /** The bot paused sending (flood limit or network outage) until then; null when not paused. */
    pausedUntil?: number | null };
  limits: { batchPerRun: number; maxAttempts: number; stuckS: number; textRetentionDays: number; rowRetentionDays: number; jobTtlS: number };
  statuses: string[];
  items: { key: string; kind: 'message' | 'broadcast'; userId: number; batch: string | null; status: string; attempts: number;
    error: string | null; notBefore: number; createdAt: number; sentAt: number | null }[];
  log: AdminEvent[];
}
export interface RolesPage {
  roles: Role[]; permissions: { permission: string; roles: Role[] }[]; grants: { permission: string; roles: Role[] }[];
}
export interface ProductDay { day: string; dau: number; quick: number; coupons: number; aiCreated: number; aiOpened: number; quickFailed: number }
export interface ProductPage {
  period: number;
  product: { source: 'not_collected' } | { source: 'unavailable' } | {
    source: 'telemetry'; series: ProductDay[]; dau: number; wau: number; mau: number;
    totals: { quick: number; coupons: number; aiCreated: number; aiOpened: number; quickFailed: number; forecasts: number };
    cohorts: { week: string; size: number; retention: (number | null)[] }[];
  };
  referrals: { starts: number; inMiniApp: number; active7d: number; converted: number | null };
  oddsCoverage: { source: string };
}
export interface Geo { country: string | null; source: 'edge' | 'lang' | null }
export interface CrmUserRow extends UserRow {
  tags: string[]; credits: { balance: number; unlimitedUntil: number | null }; blocked: boolean; mbId: string | null;
  lastSeenAt?: number | null; forecasts?: number | null; activeDays30?: number | null; geo?: Geo; source?: Source | null;
}
export interface Segment { id: number; name: string; rules: Rules; updatedAt: number; size?: number }
export interface TagCount { tag: string; users: number }
export interface CrmUsersPage extends Page<CrmUserRow> {
  counts: { total: number; new: number; withActiveSession: number; chips: Record<string, number> };
  tags: TagCount[]; segments: Segment[]; telemetry?: boolean;
}
export interface TimelineItem {
  at: number; kind: string; title: string; detail: string | null; by?: string | null; status?: string; demo?: boolean;
}
/** A row of the Mini App's credit journal (docs/miniapp/credits.md). */
export interface LedgerRow {
  id: number; userId: number; kind: 'welcome' | 'monthly' | 'spend' | 'grant' | 'debit' | 'unlimited'; delta: number;
  balanceAfter: number; item: string | null; at: number;
}
export interface CreditRate { country: string; currency: string; perForecast: number }
export interface MbLink {
  userId: number; mbId: string; state: 'pending' | 'approved' | 'rejected'; submittedAt: number; decidedAt: number | null;
  reason: string | null;
  /** The submission's own id: a decision applies to exactly this submission. */
  submission: string;
}
export interface CrmProfile extends Omit<UserProfile, 'credits'> {
  tags: string[];
  notes: { id: number; body: string; at: number; by: string | null }[];
  messages: { id: number; body: string; status: string; at: number; by: string | null }[];
  ledger: LedgerRow[];
  credits: { balance: number | null; unlimitedUntil: number | null; enabled: boolean };
  mbLink: MbLink | null;
  blocked: { reason: string; at: number } | null;
  mbId: string | null;
  timeline: TimelineItem[];
  lastSeenAt?: number | null; forecasts?: number | null; geo?: Geo; source?: Source | null;
  activity?: {
    collected: boolean;
    summary: { lastSeenAt: number | null; forecasts: number; aiCreated: number; aiOpened: number; requests: number;
      activeDays: number; firstSeenAt: number | null } | null;
    daily: { day: string; requests: number; forecasts: number; aiOpened: number }[];
  };
  mbIdHistory?: { old: string | null; new: string | null; at: number; by: string | null }[];
  blockHistory?: { blocked: boolean; at: number; by: string | null; reason: string | null }[];
}
export interface Broadcast {
  id: number; name: string; rules: Rules; body: string; buttons: { text: string; url: string }[];
  status: 'draft' | 'scheduled' | 'cancelled' | 'sent'; scheduledAt: number | null; recipients: number | null;
  createdAt: number; updatedAt: number; by: string | null; audience?: number;
  /** Content the admin reviewed: sent back with the schedule request (refused if the draft changed since). */
  digest: string;
  /** Jobs by status from the bot's queue (scheduled, sent and cancelled broadcasts). */
  delivery?: Partial<Record<'queued' | 'sending' | 'sent' | 'failed' | 'expired' | 'cancelled', number>>;
  /** Hours after sending at which the bot deletes the message in each chat (null = never). */
  deleteAfterHours?: number | null;
}
export interface CreditsPage {
  enabled: boolean;
  totals: { inCirculation: number; accounts: number; unlimited: number; granted: number; spent: number; paidForecasts: number };
  ledger: LedgerRow[];
  rules: { cost: number; welcome: number; monthly: number };
  /** Partner deposits per forecast by country ("*" = every other country). */
  rates: CreditRate[];
}
export interface DemoAudience {
  source: 'demo';
  summary: { countries: number; peak: string; avgSessionS: number; mobileShare: number };
  geo: { code: string; name: string; count: number }[];
  platforms: { name: string; count: number }[];
  activityHeat: number[][];
  cohorts: { week: string; size: number; retention: number[] }[];
  sources: { name: string; count: number }[];
  features: { name: string; share: number }[];
  activeByDay: { day: string; dau: number }[];
  funnel: [string, number][];
}
export interface Analytics {
  period: number;
  dataset: string;
  real: {
    totalUsers: number; newUsers: number; newByDay: { day: string; new: number }[]; registrationHeat: number[][];
    languages: { lang: string; count: number }[]; leagues: { name: string; count: number }[]; tags: TagCount[];
  };
  demo: DemoAudience | null;
  /** D1 collection: users active in the period by sign-in country / Telegram platform, activity by hour (Baku). */
  edge?: EdgeOverview;
}
export type EdgeOverview =
  | { source: 'not_collected' | 'unavailable' }
  | { source: 'telemetry'; activeUsers: number; geo: { code: string; count: number }[];
      platforms: { name: string; count: number }[]; activityHeat: number[][] };
export interface DialogThread {
  userId: number; name: string; username: string; unread: boolean; tag: [string, string] | null; system: string | null;
  messages: { from: 'user' | 'admin'; text: string; at: number; by?: string }[];
}

export const RULE_LABELS: Record<string, string> = {
  registered_within_days: 'Регистрация не старше, дн', active_within_days: 'Входил за последние, дн',
  inactive_for_days: 'Не входил дольше, дн', lang_in: 'Язык', tag_has: 'Есть тег', tag_not: 'Нет тега',
  has_active_session: 'Есть активная сессия', blocked: 'Заблокирован',
  country_in: 'Страна', platform_in: 'Платформа', forecasts_min: 'Прогнозов за 30 дн, от',
  active_days_min: 'Активных дней из 30, от', used_ai: 'Брал AI-разбор (30 дн)', opened_miniapp: 'Открывал Mini App (год)',
  clicked_ad: 'Кликал рекламу (60 дн)', source_in: 'Пришёл по ссылке',
};
export const PLATFORM_LABELS: Record<string, string> = { ios: 'iOS', android: 'Android', desktop: 'Desktop', web: 'Web' };
export const SOURCE_LABELS: Record<string, string> = { bot: 'ссылка на бота', miniapp: 'ссылка на Mini App', none: 'без ссылки' };

export function describeRules(rules: Rules): string[] {
  return Object.entries(rules).map(([k, v]) => {
    const label = RULE_LABELS[k] ?? k;
    if (Array.isArray(v)) return `${label}: ${v.join(', ')}`;
    if (typeof v === 'boolean') return `${label}: ${v ? 'да' : 'нет'}`;
    return `${label}: ${String(v)}`;
  });
}

// ---- user value and top users (bot snapshot + Mini App telemetry) ----
export interface BotValue {
  users: number; forecastsPerUser: number; clicksPerUser: number; clickConversion: number; avgLifespanDays: number;
  promoConversion: number; cpa: number; revenuePerUser: number | null; revenueTotal: number | null;
}
export interface ValueData {
  personal: boolean;
  bot: { source: 'not_collected' | 'unavailable' } | {
    source: 'snapshot'; generatedAt: string; value: BotValue;
    topUsers: { userId: number | null; name: string; username: string; requests: number; forecasts: number; clicks: number; promo: boolean; lastActive: string }[];
  };
  miniapp: { source: 'not_collected' | 'unavailable' } | {
    source: 'telemetry'; windowDays: number; totalUsers: number; activeUsers: number;
    value: { forecastsPerUser: number; clicksPerUser: number; clickConversion: number; avgLifespanDays: number };
    topUsers: { userId: number | null; name: string; username: string; requests: number; forecasts: number; activeDays: number; lastDay: string; clicks: number }[];
  };
}
