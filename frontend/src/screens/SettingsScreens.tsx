import { useState } from 'react';
import { AppShell } from '../components/AppShell';
import { Header } from '../components/Header';
import ui from '../components/ui.module.css';
import { dictionaries, LANGS, type Lang } from '../i18n';
import { useGoBack } from '../hooks/useGoBack';
import { TIME_ZONE_OPTIONS, type TimeZoneSetting } from '../settings/settings';
import { useServices, useSettings } from '../state/contexts';
import { deviceTimeZone, formatUtcOffset, offsetMinutes } from '../time/zoned';
import s from './ProfileScreen.module.css';

export function LanguageScreen() {
  const { t, settings, update } = useSettings();
  const { auth } = useServices();
  const goBack = useGoBack('/profile');
  const [draft, setDraft] = useState<Lang>(settings.lang);
  return (
    <AppShell scrollKey="/profile/language">
      <Header variant="screen" title={t('language.title')} backTo="/profile" />
      <h2 className={ui.pageHeading}>{t('language.heading')}</h2>
      <div className={s.options} role="radiogroup" aria-label={t('language.heading')}>
        {LANGS.map((lang) => (
          <button
            key={lang}
            type="button"
            role="radio"
            aria-checked={draft === lang}
            lang={lang}
            className={draft === lang ? s.optionActive : s.option}
            onClick={() => setDraft(lang)}
          >
            {dictionaries[lang]['lang.name']}
          </button>
        ))}
      </div>
      <p className={ui.notice}>{t('language.note')}</p>
      <button
        type="button"
        className={ui.primary}
        onClick={() => {
          update({ lang: draft });
          // Records the explicit choice server-side so Telegram's language_code does not override it.
          if (draft !== settings.lang) void auth.syncLanguage(draft);
          goBack();
        }}
      >
        {t('common.save')}
      </button>
    </AppShell>
  );
}

export function TimezoneScreen() {
  const { t, settings, update } = useSettings();
  const { clock } = useServices();
  const goBack = useGoBack('/profile');
  const [draft, setDraft] = useState<TimeZoneSetting>(settings.timeZone);
  const now = clock.now();
  const offset = (zone: string) => formatUtcOffset(offsetMinutes(now, zone));
  const label = (zone: TimeZoneSetting) => {
    switch (zone) {
      case 'auto':
        return { title: t('timezone.auto'), sub: t('timezone.autoSub', { zone: deviceTimeZone() }) };
      case 'Asia/Baku':
        return { title: t('timezone.baku', { offset: offset(zone) }), sub: zone };
      case 'Europe/Moscow':
        return { title: t('timezone.moscow', { offset: offset(zone) }), sub: zone };
      case 'Europe/Istanbul':
        return { title: t('timezone.istanbul', { offset: offset(zone) }), sub: zone };
      case 'UTC':
        return { title: t('timezone.utc'), sub: null };
    }
  };
  return (
    <AppShell scrollKey="/profile/timezone">
      <Header variant="screen" title={t('timezone.title')} backTo="/profile" />
      <h2 className={ui.pageHeading}>{t('timezone.heading')}</h2>
      <div className={s.options} role="radiogroup" aria-label={t('timezone.heading')}>
        {TIME_ZONE_OPTIONS.map((zone) => {
          const { title, sub } = label(zone);
          return (
            <button
              key={zone}
              type="button"
              role="radio"
              aria-checked={draft === zone}
              className={draft === zone ? s.optionActive : s.option}
              onClick={() => setDraft(zone)}
            >
              <span>{title}</span>
              {sub && <span className={s.optionSub}>{sub}</span>}
            </button>
          );
        })}
      </div>
      <p className={ui.notice}>{t('timezone.note')}</p>
      <button
        type="button"
        className={ui.primary}
        onClick={() => {
          update({ timeZone: draft });
          goBack();
        }}
      >
        {t('common.save')}
      </button>
    </AppShell>
  );
}

function Toggle({ title, sub, checked, onChange }: { title: string; sub: string; checked: boolean; onChange(v: boolean): void }) {
  return (
    <button type="button" role="switch" aria-checked={checked} className={s.toggleRow} onClick={() => onChange(!checked)}>
      <span className={s.toggleCopy}>
        {title}
        <span className={s.optionSub}>{sub}</span>
      </span>
      <span className={checked ? s.switchOn : s.switch} aria-hidden="true" />
    </button>
  );
}

export function AppearanceScreen() {
  const { t, settings, update, systemReducedMotion } = useSettings();
  const goBack = useGoBack('/profile');
  const [reduceMotion, setReduceMotion] = useState(settings.reduceMotion);
  const [haptics, setHaptics] = useState(settings.haptics);
  return (
    <AppShell scrollKey="/profile/appearance">
      <Header variant="screen" title={t('appearance.title')} backTo="/profile" />
      <Toggle
        title={t('appearance.reduceMotion')}
        sub={t('appearance.reduceMotionSub')}
        checked={reduceMotion}
        onChange={setReduceMotion}
      />
      {systemReducedMotion && <p className={ui.notice}>{t('appearance.systemReduced')}</p>}
      <Toggle title={t('appearance.haptics')} sub={t('appearance.hapticsSub')} checked={haptics} onChange={setHaptics} />
      <section className={ui.section}>
        <h2 className={ui.sectionTitle}>{t('appearance.themeTitle')}</h2>
        <p className={ui.body}>{t('appearance.themeText')}</p>
      </section>
      <button
        type="button"
        className={ui.primary}
        onClick={() => {
          update({ reduceMotion, haptics });
          goBack();
        }}
      >
        {t('common.save')}
      </button>
    </AppShell>
  );
}
