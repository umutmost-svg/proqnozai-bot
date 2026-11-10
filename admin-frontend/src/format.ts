const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** "21 ноя · 12:30" in UTC — all panel times are UTC (labelled in the headers). */
export function utc(value: string | number | null | undefined, withYear = false): string {
  if (value === null || value === undefined || value === '') return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  const year = withYear ? ` ${d.getUTCFullYear()}` : '';
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}${year} · ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export function utcDay(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
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
