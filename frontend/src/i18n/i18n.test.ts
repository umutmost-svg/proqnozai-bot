import { createTranslator, dictionaries, langFromCode, LANGS } from './index';

describe('dictionaries', () => {
  it('have the same keys in AZ, RU and EN', () => {
    const keys = Object.keys(dictionaries.ru).sort();
    for (const lang of LANGS) expect(Object.keys(dictionaries[lang]).sort()).toEqual(keys);
  });

  it('have no empty strings', () => {
    for (const lang of LANGS) for (const v of Object.values(dictionaries[lang])) expect(v.trim()).not.toBe('');
  });

  it('keep list keys aligned', () => {
    for (const lang of LANGS) {
      expect(dictionaries[lang]['date.weekdays'].split('|')).toHaveLength(7);
      expect(dictionaries[lang]['date.monthsShort'].split('|')).toHaveLength(12);
      expect(dictionaries[lang]['date.monthsGenitive'].split('|')).toHaveLength(12);
    }
  });

  it('interpolates variables', () => {
    expect(createTranslator('en')('catalog.results', { count: 3 })).toBe('Matches found: 3');
  });

  it('maps Telegram language codes', () => {
    expect(langFromCode('az')).toBe('az');
    expect(langFromCode('ru-RU')).toBe('ru');
    expect(langFromCode('de')).toBeNull();
    expect(langFromCode(undefined)).toBeNull();
  });
});
