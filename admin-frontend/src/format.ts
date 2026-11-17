const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** "21 ноя · 12:30" in the viewer's own time zone (the computer's clock; owner 16.11). Daily aggregates stay on
 * UTC days on the server and are labelled so where shown. */
export function localTime(value: string | number | null | undefined, withYear = false): string {
  if (value === null || value === undefined || value === '') return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  // The year is shown when asked, and always for another year (card histories span years).
  const year = withYear || d.getFullYear() !== new Date().getFullYear() ? ` ${d.getFullYear()}` : '';
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${year} · ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function localDay(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

/** "UTC+4" for the viewer's zone (shown next to time inputs). */
export function zoneLabel(at: Date = new Date()): string {
  const m = -at.getTimezoneOffset();
  const sign = m >= 0 ? '+' : '-';
  const h = Math.floor(Math.abs(m) / 60), mm = Math.abs(m) % 60;
  return `UTC${sign}${h}${mm ? `:${pad(mm)}` : ''}`;
}

/** Epoch seconds for a datetime-local value in the viewer's zone, or null when it is empty, invalid or a wall time
 * that does not exist (skipped by a DST change: it would silently move by an hour). An ambiguous fall-back time
 * resolves to its first occurrence; the confirmation shows the resulting offset. */
export function parseLocalInput(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime()) || toLocalInput(d) !== value) return null;
  return Math.floor(d.getTime() / 1000);
}

/** A naive "YYYY-MM-DD HH:MM[:SS]" written by the bot (its server clock is UTC) read as UTC. */
export function fromUtcText(value: string | null | undefined): string | null {
  if (!value) return null;
  return /[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value.replace(' ', 'T')}Z`;
}

/** A datetime-local input value ("2026-11-21T16:30") for a moment, in the viewer's zone. */
export function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(value * 100).toLocaleString('ru-RU', { maximumFractionDigits: digits })}%`;
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function userName(u: { firstName: string | null; lastName: string | null }): string {
  return [u.firstName, u.lastName].filter(Boolean).join(' ') || '—';
}

export const PICKS: Record<string, string> = {
  '1': 'П1', X: 'Ничья', '2': 'П2', 'O2.5': 'ТБ 2.5', 'U2.5': 'ТМ 2.5', BTTS_Y: 'Обе забьют', BTTS_N: 'Обе не забьют',
  NO_BET: 'Без ставки',
};

const COUNTRY_NAMES: Record<string, string> = {
  AZ: 'Азербайджан', TR: 'Турция', UZ: 'Узбекистан', BR: 'Бразилия', RU: 'Россия', KZ: 'Казахстан', DE: 'Германия',
  GE: 'Грузия', UA: 'Украина', US: 'США', GB: 'Великобритания', ES: 'Испания',
};

/** "AZ · по языку": the country with where it came from (the edge's code, or a guess from the language). */
export function geoLabel(geo: { country: string | null; source: string | null } | undefined): string {
  if (!geo || !geo.country) return '—';
  const name = COUNTRY_NAMES[geo.country] ?? geo.country;
  return geo.source === 'lang' ? `${name} · по языку` : name;
}

/** "Инстаграм · бот": the referral link of the user's first start, or "—" (organic / unknown). */
export function sourceLabel(src: { channel: string; code: string; name: string | null } | null | undefined): string {
  if (!src) return '—';
  return `${src.name ?? `ref_${src.code}`} · ${src.channel === 'bot' ? 'бот' : 'Mini App'}`;
}
