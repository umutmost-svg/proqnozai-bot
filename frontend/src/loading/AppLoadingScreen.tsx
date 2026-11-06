/**
 * ProqnozAI splash (Figma «ProqnozAI / Loading storyboard», 123:55): ENTER 0–450 ms,
 * LOADING with a 1.8 s soft pulse, sweeping signal and three dots, WAITING.
 * Pure presentation: when it shows and what it says is decided by AppLoadingGate.
 * All motion is CSS; with reduced motion only the static mark and text remain.
 */
import dots1 from '../assets/figma/loading-dots-1.svg';
import dots2 from '../assets/figma/loading-dots-2.svg';
import dots3 from '../assets/figma/loading-dots-3.svg';
import haloLime from '../assets/figma/loading-halo-lime.svg';
import haloMint from '../assets/figma/loading-halo-mint.svg';
import iconLight from '../assets/figma/loading-icon-light.svg';
import symbol from '../assets/figma/loading-symbol.svg';
import ui from '../components/ui.module.css';
import { brand } from '../config/brand';
import { useT } from '../state/contexts';
import s from './AppLoadingScreen.module.css';

export type LoadingPhase = 'connecting' | 'data';
export type LoadingHint = 'none' | 'slow' | 'offline';
/** What the first screen loads, so the message matches the screen that opens. */
export type LoadingSubject = 'matches' | 'event' | 'analysis';

const DATA_MESSAGE = {
  matches: 'loading.data',
  event: 'loading.dataEvent',
  analysis: 'loading.dataAnalysis',
} as const;

export interface AppLoadingScreenProps {
  /** What is being waited for right now (real boot phase, not a timer). */
  phase: LoadingPhase;
  subject: LoadingSubject;
  hint: LoadingHint;
  /** `failed`: the wait hit its limit; the user decides what happens next. */
  view: 'loading' | 'failed';
  reducedMotion: boolean;
  onRetry(): void;
  onContinue(): void;
}

export function AppLoadingScreen({ phase, subject, hint, view, reducedMotion, onRetry, onContinue }: AppLoadingScreenProps) {
  const t = useT();
  const animated = !reducedMotion && view === 'loading';
  return (
    <div
      className={s.screen}
      data-testid="app-loading"
      data-motion={animated ? 'full' : 'static'}
      data-view={view}
      data-phase={phase}
      data-hint={hint}
    >
      <div className={s.top} aria-hidden="true">
        <span className={s.wordmark}>{brand.name.toUpperCase()}</span>
        <span className={s.beta}>BETA</span>
      </div>

      <div className={s.center}>
        <div className={s.content}>
          <div className={s.stageWrap} aria-hidden="true">
            <img className={s.haloLime} src={haloLime} alt="" width={348} height={348} draggable={false} />
            <img className={s.haloMint} src={haloMint} alt="" width={210} height={210} draggable={false} />
            <div className={s.stage}>
              <div className={s.icon}>
                <img className={s.iconLight} src={iconLight} alt="" width={122.1} height={122.1} draggable={false} />
                <span className={s.iconEdge} />
                <img className={s.symbol} src={symbol} alt="" width={118.272} height={118.272} draggable={false} />
              </div>
            </div>
          </div>

          {view === 'loading' ? (
            <div className={s.status} role="status" aria-live="polite">
              <p className={s.brand}>{brand.name}</p>
              <p className={s.message}>{t(phase === 'connecting' ? 'loading.connecting' : DATA_MESSAGE[subject])}</p>
              {hint !== 'none' && (
                <p className={s.hint} data-testid="app-loading-hint">
                  {t(hint === 'offline' ? 'loading.offline' : 'loading.slow')}
                </p>
              )}
            </div>
          ) : (
            <div className={s.status}>
              <p className={s.brand}>{brand.name}</p>
              <p className={s.failedTitle} role="alert">
                {t('loading.failedTitle')}
              </p>
              <p className={s.message}>{t(hint === 'offline' ? 'loading.failedOffline' : 'loading.failedText')}</p>
            </div>
          )}

          {animated && (
            <>
              <div className={s.track} aria-hidden="true">
                <span className={s.signal} />
              </div>
              <div className={s.dots} aria-hidden="true">
                {[dots1, dots2, dots3].map((src, i) => (
                  <img key={src} className={s.dotState} data-step={i + 1} src={src} alt="" width={41} height={9} />
                ))}
              </div>
            </>
          )}

          {view === 'failed' && (
            <div className={s.actions}>
              <button type="button" className={ui.primary} onClick={onRetry}>
                {t('common.retry')}
              </button>
              <button type="button" className={ui.secondary} onClick={onContinue}>
                {t('loading.continue')}
              </button>
            </div>
          )}
        </div>
      </div>

      {view === 'loading' && <p className={s.tagline}>{t('loading.tagline')}</p>}
    </div>
  );
}
