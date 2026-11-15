import { formatUtcOffset, offsetMinutes, parseDayKey, timeOf, zonedParts, type DayKey } from '../time/zoned';
import { listOf, type MessageKey, type Translate } from './index';

const pad = (n: number) => String(n).padStart(2, '0');

/** "8 ноября" */
export function formatDayMonth(t: Translate, day: DayKey): string {
  const { month, day: d } = parseDayKey(day);
  return t('date.dayMonth', { day: d, month: listOf(t, 'date.monthsGenitive')[month - 1] ?? '' });
}

/** "8 ноября 2026" */
export function formatLongDate(t: Translate, day: DayKey): string {
  const { year, month, day: d } = parseDayKey(day);
  return t('date.dayMonthYear', { day: d, month: listOf(t, 'date.monthsGenitive')[month - 1] ?? '', year });
}

/** "08.11.2026" in the given zone. */
export function formatNumericDate(instant: Date, timeZone: string): string {
  const p = zonedParts(instant, timeZone);
  return `${pad(p.day)}.${pad(p.month)}.${p.year}`;
}

/** "08.11.2026 · 22:00" */
export function formatDateTime(instant: Date, timeZone: string): string {
  return `${formatNumericDate(instant, timeZone)} · ${timeOf(instant, timeZone)}`;
}

/** Human label of a zone: "время Баку" for known ones, otherwise the IANA id. */
export function zoneLabel(t: Translate, timeZone: string): string {
  const key = `zone.${timeZone}` as MessageKey;
  const label = t(key);
  return label === key ? timeZone : label;
}

/** "Баку · UTC+4" style suffix for kickoff lines. */
export function zoneWithOffset(t: Translate, timeZone: string, at: Date): string {
  const key = `city.${timeZone}` as MessageKey;
  const city = t(key) === key ? timeZone.split('/').pop()?.replace(/_/g, ' ') ?? timeZone : t(key);
  const offset = formatUtcOffset(offsetMinutes(at, timeZone));
  return city === offset ? offset : `${city} · ${offset}`;
}
