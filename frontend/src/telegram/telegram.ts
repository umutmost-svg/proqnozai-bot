/**
 * Thin adapter over Telegram's WebApp SDK. Everything else in the app talks to
 * this interface, so the UI works the same outside Telegram (local demo mode).
 * initDataUnsafe is display-only. The raw initData string is handed to the
 * server once (POST body) and verified there; the client never trusts it.
 */

export interface Insets {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export type HapticKind = 'selection' | 'light';

export interface TelegramAdapter {
  /** True only when running inside a Telegram client with launch data. */
  readonly available: boolean;
  ready(): void;
  expand(): void;
  showBackButton(): void;
  hideBackButton(): void;
  onBackButton(handler: () => void): () => void;
  /** Subscribes to viewport / safe-area changes; returns an unsubscribe fn. */
  onViewportChange(handler: () => void): () => void;
  viewportHeight(): number | null;
  safeArea(): Insets | null;
  haptic(kind: HapticKind): void;
  /** Raw signed launch data for the server-side check; null outside Telegram. */
  initData(): string | null;
  /** Unverified display hints from initDataUnsafe. */
  userFirstName(): string | null;
  languageCode(): string | null;
}

interface WebAppInsets {
  top?: number;
  bottom?: number;
  left?: number;
  right?: number;
}

/** Subset of the SDK surface we rely on. */
export interface TelegramWebApp {
  initData?: string;
  initDataUnsafe?: { user?: { first_name?: string; language_code?: string } };
  viewportStableHeight?: number;
  safeAreaInset?: WebAppInsets;
  contentSafeAreaInset?: WebAppInsets;
  isVersionAtLeast?: (version: string) => boolean;
  ready?: () => void;
  expand?: () => void;
  onEvent?: (event: string, handler: () => void) => void;
  offEvent?: (event: string, handler: () => void) => void;
  BackButton?: {
    show(): void;
    hide(): void;
    onClick(handler: () => void): void;
    offClick(handler: () => void): void;
  };
  HapticFeedback?: {
    selectionChanged(): void;
    impactOccurred(style: 'light'): void;
  };
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

const VIEWPORT_EVENTS = ['viewportChanged', 'safeAreaChanged', 'contentSafeAreaChanged'];

const sumInsets = (a: WebAppInsets = {}, b: WebAppInsets = {}): Insets => ({
  top: (a.top ?? 0) + (b.top ?? 0),
  bottom: (a.bottom ?? 0) + (b.bottom ?? 0),
  left: (a.left ?? 0) + (b.left ?? 0),
  right: (a.right ?? 0) + (b.right ?? 0),
});

export function createTelegramAdapter(webApp: TelegramWebApp | undefined): TelegramAdapter {
  // The SDK script defines WebApp even in a plain browser; launch data tells them apart.
  const available = Boolean(webApp?.initData);
  const app = available ? webApp : undefined;
  const supports = (v: string) => Boolean(app?.isVersionAtLeast?.(v));

  return {
    available,
    ready: () => app?.ready?.(),
    expand: () => app?.expand?.(),
    showBackButton: () => {
      if (supports('6.1')) app?.BackButton?.show();
    },
    hideBackButton: () => {
      if (supports('6.1')) app?.BackButton?.hide();
    },
    onBackButton: (handler) => {
      if (!supports('6.1') || !app?.BackButton) return () => {};
      app.BackButton.onClick(handler);
      return () => app.BackButton?.offClick(handler);
    },
    onViewportChange: (handler) => {
      if (!app?.onEvent) return () => {};
      VIEWPORT_EVENTS.forEach((e) => app.onEvent?.(e, handler));
      return () => VIEWPORT_EVENTS.forEach((e) => app.offEvent?.(e, handler));
    },
    viewportHeight: () => (app?.viewportStableHeight ? app.viewportStableHeight : null),
    safeArea: () => (app && supports('8.0') ? sumInsets(app.safeAreaInset, app.contentSafeAreaInset) : null),
    haptic: (kind) => {
      if (!supports('6.1')) return;
      if (kind === 'selection') app?.HapticFeedback?.selectionChanged();
      else app?.HapticFeedback?.impactOccurred('light');
    },
    initData: () => app?.initData || null,
    userFirstName: () => app?.initDataUnsafe?.user?.first_name?.trim() || null,
    languageCode: () => app?.initDataUnsafe?.user?.language_code ?? null,
  };
}

/** Applies Telegram viewport + safe areas to CSS variables; no-op outside Telegram. */
export function bindViewport(tg: TelegramAdapter, root: HTMLElement = document.documentElement): () => void {
  if (!tg.available) return () => {};
  const apply = () => {
    const h = tg.viewportHeight();
    if (h) root.style.setProperty('--app-height', `${h}px`);
    const inset = tg.safeArea();
    if (inset) {
      root.style.setProperty('--safe-top', `${inset.top}px`);
      root.style.setProperty('--safe-bottom', `${inset.bottom}px`);
      root.style.setProperty('--safe-left', `${inset.left}px`);
      root.style.setProperty('--safe-right', `${inset.right}px`);
    }
  };
  apply();
  return tg.onViewportChange(apply);
}
