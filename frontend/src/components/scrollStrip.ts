/**
 * Brings a chip into view by scrolling ONLY its own horizontal strip.
 *
 * `Element.scrollIntoView` also scrolls every scrollable ancestor — including containers with
 * `overflow: hidden`, which the user then cannot scroll back. In the Telegram WebView that moved the
 * whole app sideways (iPhone report, 2026-11-12: a tournament whose chosen day sits at the end of the
 * strip). Here only the strip moves; the page never does.
 */
export function scrollChipIntoStrip(chip: HTMLElement | null, mode: 'center' | 'nearest'): void {
  const strip = chip?.parentElement;
  if (!chip || !strip) return;
  const left = chip.offsetLeft - strip.offsetLeft;
  const right = left + chip.offsetWidth;
  let target = strip.scrollLeft;
  if (mode === 'center') {
    target = left - (strip.clientWidth - chip.offsetWidth) / 2;
  } else if (left < strip.scrollLeft) {
    target = left;
  } else if (right > strip.scrollLeft + strip.clientWidth) {
    target = right - strip.clientWidth;
  }
  const max = Math.max(0, strip.scrollWidth - strip.clientWidth);
  strip.scrollLeft = Math.min(max, Math.max(0, target));
}
