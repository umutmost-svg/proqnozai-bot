/**
 * Calendar maths in an explicit IANA time zone. Events are filtered by the
 * calendar day of the user's selected zone, never by the UTC date string.
 */

/** 'YYYY-MM-DD' calendar day. */
export type DayKey = string;

export interface ZonedParts {
  year: number;
  month: number; // 1-12
  day: number;
  hour: number;
  minute: number;
  weekday: number; // 0 = Monday … 6 = Sunday
}

const formatterCache = new Map<string, Intl.DateTimeFormat>();

function partsFormatter(timeZone: string): Intl.DateTimeFormat {
  let f = formatterCache.get(timeZone);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      weekday: 'short',
    });
    formatterCache.set(timeZone, f);
  }
  return f;
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export function zonedParts(instant: Date, timeZone: string): ZonedParts {
  const out: Record<string, string> = {};
  for (const p of partsFormatter(timeZone).formatToParts(instant)) out[p.type] = p.value;
  return {
    year: Number(out.year),
    month: Number(out.month),
    day: Number(out.day),
    hour: Number(out.hour),
    minute: Number(out.minute),
    weekday: WEEKDAYS.indexOf(out.weekday ?? ''),
  };
}

const pad = (n: number) => String(n).padStart(2, '0');

export function dayKeyOf(instant: Date, timeZone: string): DayKey {
  const p = zonedParts(instant, timeZone);
  return `${p.year}-${pad(p.month)}-${pad(p.day)}`;
}

export function parseDayKey(key: DayKey): { year: number; month: number; day: number } {
  const [y, m, d] = key.split('-').map(Number);
  return { year: y ?? 0, month: m ?? 1, day: d ?? 1 };
}

export function addDays(key: DayKey, days: number): DayKey {
  const { year, month, day } = parseDayKey(key);
  const d = new Date(Date.UTC(year, month - 1, day + days));
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

/** Weekday of a calendar day, 0 = Monday. */
export function weekdayOf(key: DayKey): number {
  const { year, month, day } = parseDayKey(key);
  return (new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % 7;
}

/** Offset of `timeZone` from UTC at `instant`, in minutes. */
export function offsetMinutes(instant: Date, timeZone: string): number {
  const p = zonedParts(instant, timeZone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
  const truncated = Math.floor(instant.getTime() / 60000) * 60000;
  return Math.round((asUtc - truncated) / 60000);
}

/** The instant at which the wall clock in `timeZone` shows `day` `hh:mm`. */
export function zonedTimeToInstant(day: DayKey, hhmm: string, timeZone: string): Date {
  const { year, month, day: d } = parseDayKey(day);
  const [h, m] = hhmm.split(':').map(Number);
  const wall = Date.UTC(year, month - 1, d, h ?? 0, m ?? 0);
  let guess = wall - offsetMinutes(new Date(wall), timeZone) * 60000;
  // Second pass settles the offset around DST transitions.
  guess = wall - offsetMinutes(new Date(guess), timeZone) * 60000;
  return new Date(guess);
}

export function formatUtcOffset(minutes: number): string {
  if (minutes === 0) return 'UTC';
  const sign = minutes > 0 ? '+' : '-';
  const abs = Math.abs(minutes);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return `UTC${sign}${h}${m ? `:${pad(m)}` : ''}`;
}

export function timeOf(instant: Date, timeZone: string): string {
  const p = zonedParts(instant, timeZone);
  return `${pad(p.hour)}:${pad(p.minute)}`;
}

export function deviceTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}
