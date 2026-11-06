/** Source of "now". The app uses the system clock; tests inject a fixed one. */
export interface Clock {
  now(): Date;
}

export const systemClock: Clock = { now: () => new Date() };

export function fixedClock(iso: string): Clock {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) throw new Error(`Invalid fixed clock: ${iso}`);
  return { now: () => new Date(at.getTime()) };
}
