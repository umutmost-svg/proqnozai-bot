import { outcomeTones, type OutcomeTone } from '../../data/probabilities';
import { useSettings } from '../../state/contexts';
import s from './Ai.module.css';

const TONE_CLASS: Record<OutcomeTone, string> = {
  favourite: s.toneFavourite!,
  outsider: s.toneOutsider!,
  draw: s.toneDraw!,
  neutral: s.toneNeutral!,
};

/**
 * Probability bars: the bar width is exactly the percentage. Colour marks the
 * favourite (green) and the outsider (red) between the two teams, the draw is orange;
 * teams closer than CLOSE_MARGIN are both neutral — no false favourite.
 */
export function ProbabilityBlock({ title, labels, values, sport, badge, colourNote }: { title: string; labels: string[]; values: readonly number[]; sport: string; badge?: string; colourNote?: string }) {
  const { t } = useSettings();
  const three = values.length === 3;
  const tones = three
    ? outcomeTones(values[0]!, values[2]!)
    : (() => {
        const [h, , a] = outcomeTones(values[0]!, values[1]!);
        return [h, a];
      })();
  const close = tones[0] === 'neutral';
  return (
    <section className={s.card} data-testid="ai-result" data-sport={sport}>
      <h3 className={s.cardTitle} data-testid="ai-probabilities-title">
        {title}
      </h3>
      {badge && (
        <span className={s.limited} data-testid="ai-limited">
          {badge}
        </span>
      )}
      {values.map((p, i) => {
        const tone = tones[i]!;
        const badge = tone === 'favourite' ? t('ai.favourite') : tone === 'outsider' ? t('ai.outsider') : null;
        return (
          <div key={labels[i]} className={`${s.outcome} ${TONE_CLASS[tone]}`} data-testid="ai-outcome" data-tone={tone}>
            <div className={s.outcomeLabel}>
              <span className={s.outcomeName}>
                {labels[i]}
                {badge && <span className={s.badge}>{badge}</span>}
              </span>
              <span className={s.outcomeValue}>{p}%</span>
            </div>
            <div className={s.track} role="presentation">
              <div className={s.fill} style={{ width: `${p}%` }} data-testid="ai-bar" />
            </div>
          </div>
        );
      })}
      {close && <p className={s.hint}>{t('ai.closeOdds')}</p>}
      <p className={s.hint} data-testid="ai-colour-note">{colourNote ?? t('ai.colourNote')}</p>
    </section>
  );
}
