import type { EventStatus } from '../data/models';
import { useT } from '../state/contexts';
import s from './StatusBadge.module.css';

/** "СКОРО · ДЕМО" / "LIVE · ДЕМО": status with the mandatory demo mark. */
export function StatusBadge({ status, demo = true }: { status: EventStatus; demo?: boolean }) {
  const t = useT();
  const label = [t(`status.${status}`), demo ? t('common.demoUpper') : null].filter(Boolean).join(' · ');
  return <span className={`${s.badge} ${status === 'live' ? s.live : ''}`}>{label}</span>;
}

/** Small "ДЕМО" chip used on the home hero. */
export function DemoChip({ label }: { label?: string }) {
  const t = useT();
  return <span className={s.chip}>{label ?? t('common.demoUpper')}</span>;
}

/** Larger "ПРИМЕР · ДЕМО" chip used on AI results. */
export function DemoBanner({ label }: { label: string }) {
  return <span className={s.solidChip}>{label}</span>;
}
