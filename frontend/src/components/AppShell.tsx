import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react';
import washSrc from '../assets/figma/bg-lemon-wash.svg';
import atmosphereSrc from '../assets/figma/bg-atmosphere.svg';
import { useServices } from '../state/contexts';
import { BottomNavigation } from './BottomNavigation';
import s from './AppShell.module.css';

interface Props {
  children: ReactNode;
  /** Key under which the scroll offset is remembered (usually the route). */
  scrollKey: string;
  /** Restore the remembered offset once the content it belongs to is rendered. */
  restoreReady?: boolean;
  /** Home-only animated light layer. */
  atmosphere?: boolean;
  gap?: number;
}

export function AppShell({ children, scrollKey, restoreReady = true, atmosphere = false, gap }: Props) {
  const { scrollMemory } = useServices();
  const scrollRef = useRef<HTMLElement>(null);
  const restored = useRef(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const remember = () => scrollMemory.set(scrollKey, el.scrollTop);
    el.addEventListener('scroll', remember, { passive: true });
    return () => el.removeEventListener('scroll', remember);
  }, [scrollKey, scrollMemory]);

  useLayoutEffect(() => {
    if (!restoreReady || restored.current) return;
    restored.current = true;
    const el = scrollRef.current;
    const saved = scrollMemory.get(scrollKey);
    if (el && saved) el.scrollTop = saved;
  }, [restoreReady, scrollKey, scrollMemory]);

  return (
    <div className={s.app}>
      {/* Decoration lives in its own clipping box: it can never widen .app's scrollable area. */}
      <div className={s.decor} aria-hidden="true" data-testid="app-decor">
        <div className={s.wash}>
          <img src={washSrc} alt="" width={590} height={680} />
        </div>
        {atmosphere && (
          <div className={s.atmosphere}>
            <img src={atmosphereSrc} alt="" width={520} height={450} />
          </div>
        )}
      </div>
      <main ref={scrollRef} className={s.scroll} data-testid="scroll-viewport">
        <div className={s.content} style={gap ? { gap } : undefined}>
          {children}
        </div>
      </main>
      <div className={s.dock}>
        <BottomNavigation />
      </div>
    </div>
  );
}
