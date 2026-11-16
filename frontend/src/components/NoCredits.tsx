import { Link } from 'react-router-dom';
import ui from './ui.module.css';
import { useSettings } from '../state/contexts';

/** Not enough credits for one forecast (HTTP 402): nothing was generated or charged. */
export function NoCredits({ cost = 10 }: { cost?: number }) {
  const { t } = useSettings();
  return (
    <section className={ui.section} data-testid="no-credits">
      <h2 className={ui.pageHeading}>{t('credits.noneTitle')}</h2>
      <p className={ui.body}>{t('credits.noneText', { cost })}</p>
      <Link to="/profile/credits" className={ui.primary}>
        {t('credits.open')}
      </Link>
    </section>
  );
}
