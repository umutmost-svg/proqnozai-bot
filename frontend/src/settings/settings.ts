import { DEFAULT_LANG, isLang, type Lang } from '../i18n';
import { deviceTimeZone } from '../time/zoned';

export type TimeZoneSetting = 'auto' | 'Asia/Baku' | 'Europe/Moscow' | 'Europe/Istanbul' | 'UTC';
export const TIME_ZONE_OPTIONS: readonly TimeZoneSetting[] = [
  'auto',
  'Asia/Baku',
  'Europe/Moscow',
  'Europe/Istanbul',
  'UTC',
];

export interface Settings {
  lang: Lang;
  timeZone: TimeZoneSetting;
  reduceMotion: boolean;
  haptics: boolean;
}

export const STORAGE_KEY = 'sporai.settings.v1';

export function defaultSettings(lang: Lang = DEFAULT_LANG): Settings {
  return { lang, timeZone: 'auto', reduceMotion: false, haptics: true };
}

/** Reads persisted settings, ignoring anything malformed. */
export function loadSettings(storage: Pick<Storage, 'getItem'> | null, fallbackLang: Lang): Settings {
  const base = defaultSettings(fallbackLang);
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    if (!raw) return base;
    const parsed = JSON.parse(raw) as Partial<Settings>;
    return {
      lang: isLang(parsed.lang) ? parsed.lang : base.lang,
      timeZone: TIME_ZONE_OPTIONS.includes(parsed.timeZone as TimeZoneSetting)
        ? (parsed.timeZone as TimeZoneSetting)
        : base.timeZone,
      reduceMotion: typeof parsed.reduceMotion === 'boolean' ? parsed.reduceMotion : base.reduceMotion,
      haptics: typeof parsed.haptics === 'boolean' ? parsed.haptics : base.haptics,
    };
  } catch {
    return base;
  }
}

export function saveSettings(storage: Pick<Storage, 'setItem'> | null, settings: Settings): void {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // Storage can be unavailable (private mode); settings then live for the session only.
  }
}

export function resolveTimeZone(setting: TimeZoneSetting): string {
  return setting === 'auto' ? deviceTimeZone() : setting;
}
