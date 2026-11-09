import { NavLink, useLocation } from 'react-router-dom';
import homeIcon from '../assets/figma/icon-home.svg';
import calendarIcon from '../assets/figma/icon-calendar.svg';
import personIcon from '../assets/figma/icon-person.svg';
import sparkIcon from '../assets/figma/icon-spark.svg';
import { useT } from '../state/contexts';
import s from './BottomNavigation.module.css';

const ITEMS = [
  { to: '/', icon: homeIcon, label: 'nav.home', match: (p: string) => p === '/' },
  { to: '/matches', icon: calendarIcon, label: 'nav.matches', match: (p: string) => p.startsWith('/matches') || p.startsWith('/event') || p.startsWith('/tournament') || p.startsWith('/team') },
  { to: '/quick', icon: sparkIcon, label: 'nav.quick', match: (p: string) => p.startsWith('/quick') },
  { to: '/profile', icon: personIcon, label: 'nav.profile', match: (p: string) => p.startsWith('/profile') },
] as const;

export function BottomNavigation() {
  const t = useT();
  const { pathname } = useLocation();
  return (
    <nav className={s.dock} aria-label={t('nav.label')}>
      <div className={s.bar}>
        {ITEMS.map((item) => {
          const active = item.match(pathname);
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={`${s.item} ${active ? s.active : ''}`}
              aria-current={active ? 'page' : undefined}
            >
              <img src={item.icon} alt="" width={20} height={20} />
              <span className={s.label}>{t(item.label)}</span>
            </NavLink>
          );
        })}
      </div>
    </nav>
  );
}
