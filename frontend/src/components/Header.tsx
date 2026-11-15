import { useNavigate } from 'react-router-dom';
import backIcon from '../assets/figma/icon-back.svg';
import bellIcon from '../assets/figma/icon-bell.svg';
import brandSymbol from '../assets/brand/velocity-symbol.svg';
import { brand } from '../config/brand';
import { useT } from '../state/contexts';
import { useGoBack } from '../hooks/useGoBack';
import s from './Header.module.css';

type Props =
  | { variant: 'home' }
  | { variant: 'brand'; backTo?: string }
  | { variant: 'screen'; title: string; backTo?: string };

/** Three header layouts used in the Figma frames: home, catalog (brand) and inner screens. */
export function Header(props: Props) {
  const t = useT();
  const navigate = useNavigate();
  const goBack = useGoBack(props.variant === 'home' ? '/' : (props.backTo ?? '/'));
  const openLanguage = () => navigate('/profile/language');
  const langButton = (className?: string) => (
    <button type="button" className={className} onClick={openLanguage} aria-label={t('common.changeLanguage')}>
      {t('lang.code')}
    </button>
  );

  if (props.variant === 'home') {
    return (
      <header className={s.bar}>
        <span className={s.mark} aria-hidden="true">
          <img className={s.symbol} src={brandSymbol} alt="" width={32} height={32} draggable={false} />
        </span>
        <h1 className={s.brand}>{brand.name}</h1>
        {langButton(s.langHome)}
        {/* Notifications arrive in M4: the bell is shown but explicitly unavailable. */}
        <button type="button" className={s.bell} disabled aria-label={t('home.notifications')} title={t('home.notifications')}>
          <img src={bellIcon} alt="" width={16} height={20} />
        </button>
      </header>
    );
  }

  if (props.variant === 'brand') {
    return (
      <header className={s.bar} style={{ gap: 12 }}>
        <button type="button" className={s.iconBack} onClick={goBack} aria-label={t('common.back')}>
          <img src={backIcon} alt="" width={22} height={22} />
        </button>
        <h1 className={s.centerBrand}>{brand.name}</h1>
        {langButton(s.lang)}
      </header>
    );
  }

  return (
    <header className={s.screenBar}>
      <button type="button" className={s.roundBack} onClick={goBack} aria-label={t('common.back')}>
        <span aria-hidden="true">‹</span>
      </button>
      <h1 className={s.screenTitle}>{props.title}</h1>
      {langButton(s.lang)}
    </header>
  );
}
