import { loadSettings, saveSettings, STORAGE_KEY, defaultSettings } from './settings';

const memory = () => {
  const map = new Map<string, string>();
  return { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => void map.set(k, v) };
};

describe('settings persistence', () => {
  it('round-trips language, zone and motion settings', () => {
    const storage = memory();
    saveSettings(storage, { lang: 'az', timeZone: 'Europe/Moscow', reduceMotion: true, haptics: false });
    expect(loadSettings(storage, 'ru')).toEqual({ lang: 'az', timeZone: 'Europe/Moscow', reduceMotion: true, haptics: false });
  });

  it('falls back on missing or malformed data', () => {
    const storage = memory();
    expect(loadSettings(storage, 'en')).toEqual(defaultSettings('en'));
    storage.setItem(STORAGE_KEY, '{not json');
    expect(loadSettings(storage, 'ru')).toEqual(defaultSettings('ru'));
    storage.setItem(STORAGE_KEY, JSON.stringify({ lang: 'de', timeZone: 'Mars/Base', reduceMotion: 'yes' }));
    expect(loadSettings(storage, 'ru')).toEqual(defaultSettings('ru'));
  });

  it('survives unavailable storage', () => {
    expect(loadSettings(null, 'ru')).toEqual(defaultSettings('ru'));
    expect(() => saveSettings(null, defaultSettings())).not.toThrow();
  });
});
