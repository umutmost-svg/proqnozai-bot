import { az } from './az';
import { en } from './en';
import { ru, type Dictionary, type MessageKey } from './ru';

export type Lang = 'az' | 'ru' | 'en';
export const LANGS: readonly Lang[] = ['ru', 'az', 'en'];
export const DEFAULT_LANG: Lang = 'ru';

export const dictionaries: Record<Lang, Dictionary> = { az, ru, en };
export type { MessageKey };

export type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

export function createTranslator(lang: Lang): Translate {
  const dict = dictionaries[lang];
  return (key, vars) => {
    const template = dict[key] ?? ru[key] ?? key;
    if (!vars) return template;
    return template.replace(/\{(\w+)\}/g, (_, name: string) => String(vars[name] ?? `{${name}}`));
  };
}

export function isLang(value: unknown): value is Lang {
  return value === 'az' || value === 'ru' || value === 'en';
}

/** Maps a Telegram `language_code` (e.g. "az", "ru-RU") to a supported language. */
export function langFromCode(code: string | null | undefined): Lang | null {
  const base = code?.toLowerCase().split('-')[0];
  return isLang(base) ? base : null;
}

/** Splits a "a|b|c" list stored in dictionaries. */
export const listOf = (t: Translate, key: MessageKey): string[] => t(key).split('|');
