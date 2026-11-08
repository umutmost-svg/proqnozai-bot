import { useState } from 'react';
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
 * Figma event frames for athletes and fictional demo teams. A crest that fails to
 * load (served by our backend) falls back to the same badge.
 */
export function Emblem({ competitor, size, crestBox, variant = 'platform', useHeroAsset = false }: Props) {
  const candidate = (useHeroAsset && competitor.heroEmblem) || competitor.emblem;
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const asset = candidate && candidate.src !== failedSrc ? candidate : undefined;
  // Without a crest the initial always sits on the round platform, as in the Figma event frames.
  const className = variant === 'hero' ? s.hero : variant === 'plain' && asset ? s.plain : s.platform;
  const box = asset ? fitBox(asset, crestBox, crestBox) : null;
  return (
    <span className={className} style={{ width: size, height: size }} data-testid="emblem">
      {asset && box ? (
        <img
          src={asset.src}
          alt={competitor.fullName ?? competitor.name}
          {...box}
          style={{ ...box, objectFit: asset.remote ? 'contain' : undefined }}
          onError={asset.remote ? () => setFailedSrc(asset.src) : undefined}
        />
      ) : (
        <span className={s.initial} style={{ fontSize: Math.round(size * 0.47) }} aria-hidden="true">
          {initialOf(competitor)}
        </span>
      )}
    </span>
  );
}
