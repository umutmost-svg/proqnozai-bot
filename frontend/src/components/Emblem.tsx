import type { Competitor } from '../data/models';
import { initialOf } from '../data/present';
import { fitBox } from './Logo';
import s from './Emblem.module.css';

interface Props {
  competitor: Competitor;
  /** Outer size of the round platform. */
  size: number;
  /** Max box the crest is fitted into, keeping its aspect ratio. */
  crestBox: number;
  variant?: 'platform' | 'hero' | 'plain';
  useHeroAsset?: boolean;
}

/**
 * Real crest when we have it; otherwise the neutral initial badge used in the
 * Figma event frames for athletes and fictional demo teams.
 */
export function Emblem({ competitor, size, crestBox, variant = 'platform', useHeroAsset = false }: Props) {
  const asset = (useHeroAsset && competitor.heroEmblem) || competitor.emblem;
  // Without a crest the initial always sits on the round platform, as in the Figma event frames.
  const className = variant === 'hero' ? s.hero : variant === 'plain' && asset ? s.plain : s.platform;
  return (
    <span className={className} style={{ width: size, height: size }} data-testid="emblem">
      {asset ? (
        <img src={asset.src} alt={competitor.name} {...fitBox(asset, crestBox, crestBox)} style={fitBox(asset, crestBox, crestBox)} />
      ) : (
        <span className={s.initial} style={{ fontSize: Math.round(size * 0.47) }} aria-hidden="true">
          {initialOf(competitor)}
        </span>
      )}
    </span>
  );
}
