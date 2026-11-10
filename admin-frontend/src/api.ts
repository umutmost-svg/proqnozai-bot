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

export function setCsrf(token: string): void {
  csrfToken = token;
}

export function setUnauthenticatedHandler(fn: () => void): void {
  onUnauthenticated = fn;
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? 'GET';
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
    if (response.status === 401 && !path.startsWith('/api/admin/auth/')) onUnauthenticated();
    throw error;
  }
  return data as T;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

const MESSAGES: Record<string, string> = {
  network: 'Сервер недоступен. Проверьте, что пилот запущен.',
  invalid_credentials: 'Неверная электронная почта или пароль.',
  locked: 'Учётная запись временно заблокирована после нескольких ошибок. Попробуйте через 15 минут.',
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
  internal: 'Внутренняя ошибка сервера.',
};

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return MESSAGES[error.code] ?? `Ошибка: ${error.code}`;
  return 'Неизвестная ошибка.';
}

// ---- response types ----

export type Role = 'owner' | 'admin' | 'analyst';
export interface Me {
  admin: { id: number; email: string; role: Role };
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
export interface SettingValue { value: boolean | number; version: number | null; updatedAt: number | null; type: string; min?: number; max?: number }
