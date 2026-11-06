# ProqnozAI — Velocity V2 brand kit

Approved mark: Figma `0VSXaKhEwO1aC6mfmOKoui`, presentation node `162:111`
(symbol `162:83`, wordmark `162:102`, app icon `162:103`). The original
lemon-mint Light Glass palette is used, with no new colours.

These files are **not bundled** into the Mini App. The app uses the runtime vectors in
`src/assets/brand/`:

| File | Purpose |
|---|---|
| `src/assets/brand/velocity-symbol.svg` | Light symbol, 256 viewBox (header, 32 px tile) |
| `src/assets/brand/velocity-icon.svg` | App icon 512 (splash) |
| `src/assets/brand/favicon.svg` | Favicon, identical to the app icon (enforced by a unit test) |
| `brand-kit/velocity-symbol-dark.svg` | Dark symbol for dark backgrounds (not used in the app yet) |
| `brand-kit/velocity-icon-512.png` | App icon 512×512, for Telegram |
| `brand-kit/velocity-icon-2048.png` | App icon 2048×2048, for stores, marketing and presentations |

The PNGs were rendered from `velocity-icon.svg` in Chrome. The 512 render was
compared pixel by pixel with Figma's own export of `162:103`: 0.09 % of pixels
differ by more than 24/255, all of them on anti-aliased edges. The PNG corners are transparent
(rounded square, radius 116/512).

No wordmark SVG is exported: in the app the name "ProqnozAI" is live text
(Manrope) next to the symbol, as in `162:102`.

## Telegram bot avatar: manual step, not done automatically

Do this manually, and only when the release is approved:

1. Open @BotFather and send `/setuserpic`.
2. Choose the bot (the stand test bot first; production only with a separate decision).
3. Send `velocity-icon-512.png` **as a photo**.

Telegram crops avatars to a circle. The transparent corners of the rounded square
lie entirely outside that circle. The symbol stays inside the safe area
(at most about 176 px from the centre against a 256 px radius). The glass rim near the
edges is partly cut off by the circle; this is expected.

## Visual baselines

Pixel references for the mark (tolerance 0.05 % of pixels, anti-aliasing only):
`e2e/layout.spec.ts-snapshots/velocity-scale-{1x,3x}-darwin.png` (symbol at 32/40/64 px) and
`e2e/loading.spec.ts-snapshots/splash-velocity-icon-darwin.png` (static splash mark).
The baselines are macOS renders: Playwright runs locally, not in CI. Regenerate them only on an
intentional, approved logo change:
`npx playwright test -g "Velocity V2 symbol at|system reduced motion: static" --update-snapshots`.
