import { addDays, dayKeyOf, formatUtcOffset, offsetMinutes, weekdayOf, zonedTimeToInstant } from './zoned';

describe('zoned time', () => {
  it('maps wall-clock time in a zone to the right instant', () => {
    expect(zonedTimeToInstant('2026-11-08', '22:00', 'Asia/Baku').toISOString()).toBe('2026-11-08T18:00:00.000Z');
    expect(zonedTimeToInstant('2026-11-09', '00:30', 'Asia/Baku').toISOString()).toBe('2026-11-08T20:30:00.000Z');
  });

  it('resolves the calendar day in the selected zone, not UTC', () => {
    const instant = new Date('2026-11-08T20:30:00Z'); // 00:30 next day in Baku
    expect(dayKeyOf(instant, 'Asia/Baku')).toBe('2026-11-09');
    expect(dayKeyOf(instant, 'UTC')).toBe('2026-11-08');
    expect(dayKeyOf(instant, 'Europe/Moscow')).toBe('2026-11-08'); // 23:30
  });

  it('handles month and year boundaries', () => {
    expect(addDays('2026-11-30', 1)).toBe('2026-12-01');
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28');
  });

  it('computes weekday and offsets', () => {
    expect(weekdayOf('2026-11-08')).toBe(6); // Sunday
    expect(offsetMinutes(new Date('2026-11-08T12:00:00Z'), 'Asia/Baku')).toBe(240);
    expect(formatUtcOffset(240)).toBe('UTC+4');
    expect(formatUtcOffset(330)).toBe('UTC+5:30');
    expect(formatUtcOffset(0)).toBe('UTC');
  });

  it('respects DST in zones that have it', () => {
    expect(zonedTimeToInstant('2026-07-01', '12:00', 'Europe/Berlin').toISOString()).toBe('2026-07-01T10:00:00.000Z');
    expect(zonedTimeToInstant('2026-12-01', '12:00', 'Europe/Berlin').toISOString()).toBe('2026-12-01T11:00:00.000Z');
  });
});
