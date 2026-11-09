import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ALL_TOURNAMENTS } from '../data/filter';
import type { Sport } from '../data/models';
import { createTranslator, DEFAULT_LANG, langFromCode } from '../i18n';
import { loadSettings, resolveTimeZone, saveSettings, type Settings } from '../settings/settings';
import { addDays, dayKeyOf, zonedTimeToInstant, type DayKey } from '../time/zoned';
import {
  FiltersContext,
  ServicesContext,
  SettingsContext,
  type Filters,
  type Services,
} from './contexts';

const INITIAL_FILTERS: Filters = { sport: 'football', day: null, tournamentId: ALL_TOURNAMENTS, query: '' };

function useSystemReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)';
  const [reduced, setReduced] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mql = window.matchMedia?.(query);
    if (!mql) return;
    const onChange = () => setReduced(mql.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

const safeStorage = (): Storage | null => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

export function AppProviders({ services, children }: { services: Services; children: ReactNode }) {
  const [settings, setSettings] = useState<Settings>(() =>
    loadSettings(safeStorage(), langFromCode(services.telegram.languageCode()) ?? DEFAULT_LANG),
  );
  const systemReducedMotion = useSystemReducedMotion();

  const update = useCallback((patch: Partial<Settings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      saveSettings(safeStorage(), next);
      return next;
    });
  }, []);

  const timeZone = resolveTimeZone(settings.timeZone);
  const motionReduced = settings.reduceMotion || systemReducedMotion;

  useEffect(() => {
    document.documentElement.lang = settings.lang;
    document.documentElement.dataset.reduceMotion = String(motionReduced);
  }, [settings.lang, motionReduced]);

  const settingsState = useMemo(
    () => ({ settings, update, t: createTranslator(settings.lang), timeZone, systemReducedMotion, motionReduced }),
    [settings, update, timeZone, systemReducedMotion, motionReduced],
  );

  const [filters, setFilters] = useState<Filters>(INITIAL_FILTERS);
  const [, setTick] = useState(0);
  const today = dayKeyOf(services.clock.now(), timeZone);

  // "Today" moves on at local midnight while the app stays open (and on return to the
  // foreground, where timers may have slept): the day shown follows it unless one was chosen.
  useEffect(() => {
    const midnight = zonedTimeToInstant(addDays(today, 1), '00:00', timeZone).getTime();
    const advance = () => dayKeyOf(services.clock.now(), timeZone) !== today && setTick((n) => n + 1);
    const timer = setTimeout(advance, Math.min(Math.max(midnight - services.clock.now().getTime(), 0) + 1000, 2_147_000_000));
    const onVisible = () => document.visibilityState === 'visible' && advance();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [today, timeZone, services.clock]);

  const filtersState = useMemo(
    () => ({
      filters,
      today,
      day: filters.day ?? today,
      setSport: (sport: Sport) =>
        // Tournaments belong to a sport, so switching sport clears the tournament.
        setFilters((f) => (f.sport === sport ? f : { ...f, sport, tournamentId: ALL_TOURNAMENTS })),
      setDay: (day: DayKey) => setFilters((f) => ({ ...f, day: day === today ? null : day })),
      setTournament: (tournamentId: string) => setFilters((f) => ({ ...f, tournamentId })),
      setQuery: (query: string) => setFilters((f) => ({ ...f, query })),
      reset: () => setFilters((f) => ({ ...INITIAL_FILTERS, sport: f.sport })),
    }),
    [filters, today],
  );

  return (
    <ServicesContext.Provider value={services}>
      <SettingsContext.Provider value={settingsState}>
        <FiltersContext.Provider value={filtersState}>{children}</FiltersContext.Provider>
      </SettingsContext.Provider>
    </ServicesContext.Provider>
  );
}
