import type { ImageAsset } from '../data/models';

/** Scales an asset to fit a box without changing its proportions. */
export function fitBox(asset: ImageAsset, maxWidth: number, maxHeight: number) {
  const scale = Math.min(maxWidth / asset.width, maxHeight / asset.height);
  return { width: +(asset.width * scale).toFixed(2), height: +(asset.height * scale).toFixed(2) };
}

export function Logo({ asset, box, alt = '' }: { asset: ImageAsset; box: number; alt?: string }) {
  const size = fitBox(asset, box, box);
  return (
    <img
      src={asset.src}
      alt={alt}
      width={size.width}
      height={size.height}
      style={{ display: 'block', ...size, transform: asset.flipY ? 'scaleY(-1)' : undefined }}
      draggable={false}
    />
  );
}
