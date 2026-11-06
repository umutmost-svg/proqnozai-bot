import { createContext, useContext } from 'react';
import type { AuthController } from '../auth/controller';
import type { SportsDataProvider } from '../data/provider';
import type { Sport } from '../data/models';
import type { Translate } from '../i18n';
import type { Settings } from '../settings/settings';
import type { TelegramAdapter } from '../telegram/telegram';
import type { Clock } from '../time/clock';
import type { DayKey } from '../time/zoned';

export interface Services {
  clock: Clock;
  data: SportsDataProvider;
  telegram: TelegramAdapter;
  /** Server session state; independent of whether sports data is demo. */
  auth: AuthController;
  /** Scroll offsets of scroll containers, keyed by route, kept for back navigation. */
  scrollMemory: Map<string, number>;
}

export interface SettingsState {
  settings: Settings;
  update(patch: Partial<Settings>): void;
  t: Translate;
  /** Resolved IANA zone used for every date shown and filtered. */
  timeZone: string;
  systemReducedMotion: boolean;
  motionReduced: boolean;
}

export interface Filters {
  sport: Sport;
  /** null = "today" in the selected zone, so it follows the clock and zone changes. */
  day: DayKey | null;
  tournamentId: string;
  query: string;
}

export interface FiltersState {
  filters: Filters;
  /** Concrete calendar day after resolving "today". */
  day: DayKey;
  today: DayKey;
  setSport(sport: Sport): void;
  setDay(day: DayKey): void;
  setTournament(id: string): void;
  setQuery(query: string): void;
  reset(): void;
}

export const ServicesContext = createContext<Services | null>(null);
export const SettingsContext = createContext<SettingsState | null>(null);
export const FiltersContext = createContext<FiltersState | null>(null);

function use<T>(ctx: React.Context<T | null>, name: string): T {
  const value = useContext(ctx);
  if (!value) throw new Error(`${name} is used outside AppProviders`);
  return value;
}

export const useServices = () => use(ServicesContext, 'useServices');
export const useSettings = () => use(SettingsContext, 'useSettings');
export const useT = () => useSettings().t;
export const useFilters = () => use(FiltersContext, 'useFilters');
