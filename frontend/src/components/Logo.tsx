import { useState, type ReactNode } from 'react';
import type { ImageAsset } from '../data/models';

/** Scales an asset to fit a box without changing its proportions. */
export function fitBox(asset: ImageAsset, maxWidth: number, maxHeight: number) {
  const scale = Math.min(maxWidth / asset.width, maxHeight / asset.height);
  return {
    width: +(asset.width * scale).toFixed(2),
    height: +(asset.height * scale).toFixed(2),
  };
}

/**
 * Remote pictures (served by our backend) have unknown proportions, so they fill a
 * square box with object-fit; if one fails to load, `fallback` (or nothing) is shown
 * instead of a broken-image icon.
 */
export function Logo({
  asset,
  box,
  alt = '',
  fallback = null,
}: {
  asset: ImageAsset;
  box: number;
  alt?: string;
  /** Shown instead of a remote picture that failed to load. */
  fallback?: ReactNode;
}) {
  // The failure belongs to one picture: a new asset in the same component gets its own chance.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  if (failedSrc === asset.src) return <>{fallback}</>;
  const size = fitBox(asset, box, box);
  return (
    <img
      src={asset.src}
      alt={alt}
      width={size.width}
      height={size.height}
      style={{
        display: 'block',
        ...size,
        objectFit: asset.remote ? 'contain' : undefined,
        transform: asset.flipY ? 'scaleY(-1)' : undefined,
      }}
      draggable={false}
      onError={asset.remote ? () => setFailedSrc(asset.src) : undefined}
    />
  );
}
