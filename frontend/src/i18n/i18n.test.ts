import { createTranslator, dictionaries, langFromCode, LANGS, plural, type Lang } from './index';

const placeholders = (v: string) => [...new Set(v.match(/\{\w+\}/g) ?? [])].sort();

describe('dictionaries (all 7 languages, part of the regular CI test run)', () => {
  it('cover exactly 7 languages', () => {
    expect([...LANGS].sort()).toEqual(['az', 'en', 'es', 'pt-BR', 'ru', 'tr', 'uz']);
  });

  it('have the same keys in every language', () => {
    const keys = Object.keys(dictionaries.ru).sort();
    for (const lang of LANGS) expect(Object.keys(dictionaries[lang]).sort(), lang).toEqual(keys);
  });

  it('have no empty strings and no untranslated key names', () => {
    for (const lang of LANGS) {
      for (const [k, v] of Object.entries(dictionaries[lang])) {
        expect(v.trim(), `${lang}:${k}`).not.toBe('');
        expect(v, `${lang}:${k}`).not.toBe(k);
      }
    }
  });

  it('keep every placeholder of each message', () => {
    for (const lang of LANGS) {
      for (const [k, v] of Object.entries(dictionaries.ru)) {
        expect(placeholders(dictionaries[lang][k as keyof typeof dictionaries.ru]), `${lang}:${k}`).toEqual(placeholders(v));
      }
    }
  });

  it('keep list keys aligned', () => {
    const lengths: Record<string, number> = {
      'date.weekdays': 7, 'date.monthsShort': 12, 'date.monthsGenitive': 12, 'date.months': 12,
      'ai.formLetters': 3, 'ai.formNames': 3,
    };
    for (const lang of LANGS) {
      for (const [k, n] of Object.entries(lengths)) {
        expect(dictionaries[lang][k as keyof typeof dictionaries.ru].split('|'), `${lang}:${k}`).toHaveLength(n);
      }
    }
  });

  it('give every count a form with the number in it', () => {
    const keys = ['ai.n.wins', 'ai.n.draws', 'ai.n.losses', 'ai.n.points', 'ai.n.pointsWith', 'ai.n.goals', 'ai.n.lastMatches'] as const;
    for (const lang of LANGS) {
      const t = createTranslator(lang);
      for (const key of keys) for (const n of [0, 1, 2, 3, 5, 11, 21, 101]) expect(plural(t, lang, key, n), `${lang}:${key}:${n}`).toContain(String(n));
    }
  });

  it('keep brand and source names untranslated', () => {
    for (const lang of LANGS) {
      expect(dictionaries[lang]['support.faq.data.a'], lang).toContain('football-data.org');
      expect(dictionaries[lang]['support.faq.what.q'], lang).toContain('ProqnozAI');
    }
  });

  it('interpolates variables', () => {
    expect(createTranslator('en')('catalog.results', { count: 3 })).toBe('Matches found: 3');
  });

  it.each([
    ['az', 'az'], ['ru-RU', 'ru'], ['en-GB', 'en'], ['tr', 'tr'], ['uz', 'uz'], ['uz-Latn', 'uz'],
    ['pt-br', 'pt-BR'], ['pt', 'pt-BR'], ['pt-PT', 'pt-BR'], ['es-419', 'es'], ['es', 'es'],
    ['de', null], ['', null], [undefined, null], ['pt-BR-x', 'pt-BR'],
  ] as [string | undefined, Lang | null][])('maps Telegram language code %s', (code, lang) => {
    expect(langFromCode(code)).toBe(lang);
  });
});
