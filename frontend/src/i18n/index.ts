import { az } from './az';
import { en } from './en';
import { es } from './es';
import { ptBR } from './ptBR';
import { ru, type Dictionary, type MessageKey } from './ru';
import { tr } from './tr';
import { uz } from './uz';

export type Lang = 'az' | 'ru' | 'en' | 'tr' | 'uz' | 'pt-BR' | 'es';
export const LANGS: readonly Lang[] = ['ru', 'az', 'en', 'tr', 'uz', 'pt-BR', 'es'];

/** BCP 47 locale of each interface language (plural rules, Intl formatting). */
export const LOCALES: Record<Lang, string> = { az: 'az', ru: 'ru', en: 'en', tr: 'tr', uz: 'uz', 'pt-BR': 'pt-BR', es: 'es' };
export const DEFAULT_LANG: Lang = 'ru';

export const dictionaries: Record<Lang, Dictionary> = { az, ru, en, tr, uz, 'pt-BR': ptBR, es };
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
  return typeof value === 'string' && (LANGS as readonly string[]).includes(value);
}

/**
 * Maps a Telegram `language_code` ("az", "ru-RU", "pt-br", "uz-Cyrl"…) to a supported
 * language; any Portuguese goes to Brazilian Portuguese, unknown codes to null (the
 * caller's safe default).
 */
export function langFromCode(code: string | null | undefined): Lang | null {
  const base = code?.toLowerCase().split(/[-_]/)[0];
  if (base === 'pt') return 'pt-BR';
  return base && isLang(base) ? (base as Lang) : null;
}

/** Splits a "a|b|c" list stored in dictionaries. */
export const listOf = (t: Translate, key: MessageKey): string[] => t(key).split('|');

const PLURAL_INDEX: Record<string, number> = { one: 0, few: 1, many: 2, other: 3 };

/**
 * Count-dependent text. The message holds the forms "one|few|many|other" (a language
 * lists only the ones it uses, in that order; missing later forms reuse the last one),
 * chosen by the language's own plural rules: "1 победа / 2 победы / 5 побед".
 */
export function plural(t: Translate, lang: Lang, key: MessageKey, n: number, vars: Record<string, string | number> = {}): string {
  const forms = t(key, { n, ...vars }).split('|');
  let category = new Intl.PluralRules(LOCALES[lang]).select(n);
  // Portuguese CLDR puts 0 with "one"; counts read naturally in the plural ("0 vitórias").
  if (n === 0 && category === 'one') category = 'other';
  const index = PLURAL_INDEX[category] ?? 3;
  return forms[Math.min(index, forms.length - 1)]!;
}
