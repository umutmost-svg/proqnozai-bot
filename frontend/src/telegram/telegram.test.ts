import { bindViewport, createTelegramAdapter, type TelegramWebApp } from './telegram';

function mockWebApp(overrides: Partial<TelegramWebApp> = {}) {
  const handlers = new Map<string, () => void>();
  const backHandlers = new Set<() => void>();
  const app = {
    initData: 'query_id=1&user=%7B%7D&hash=x',
    initDataUnsafe: { user: { first_name: 'Murad', language_code: 'az' } },
    viewportStableHeight: 700,
    safeAreaInset: { top: 10, bottom: 20, left: 0, right: 0 },
    contentSafeAreaInset: { top: 30, bottom: 0, left: 0, right: 0 },
    isVersionAtLeast: () => true,
    ready: vi.fn(),
    close: vi.fn(),
    expand: vi.fn(),
    onEvent: (e: string, h: () => void) => void handlers.set(e, h),
    offEvent: (e: string) => void handlers.delete(e),
    BackButton: {
      show: vi.fn(),
      hide: vi.fn(),
      onClick: (h: () => void) => void backHandlers.add(h),
      offClick: (h: () => void) => void backHandlers.delete(h),
    },
    HapticFeedback: { selectionChanged: vi.fn(), impactOccurred: vi.fn() },
    ...overrides,
  };
  return { app, handlers, backHandlers };
}

describe('Telegram adapter', () => {
  it('is inert outside Telegram (SDK present but no launch data)', () => {
    const { app } = mockWebApp({ initData: '' });
    const tg = createTelegramAdapter(app);
    expect(tg.available).toBe(false);
    tg.ready();
    tg.close();
    tg.showBackButton();
    expect(app.ready).not.toHaveBeenCalled();
    expect(app.close).not.toHaveBeenCalled();
    expect(app.BackButton.show).not.toHaveBeenCalled();
    expect(tg.userFirstName()).toBeNull();
    expect(createTelegramAdapter(undefined).available).toBe(false);
  });

  it('drives ready/expand, BackButton and haptics inside Telegram', () => {
    const { app, backHandlers } = mockWebApp();
    const tg = createTelegramAdapter(app);
    tg.ready();
    tg.expand();
    tg.close();
    expect(app.close).toHaveBeenCalled();
    expect(app.ready).toHaveBeenCalled();
    expect(app.expand).toHaveBeenCalled();
    const off = tg.onBackButton(() => {});
    expect(backHandlers.size).toBe(1);
    off();
    expect(backHandlers.size).toBe(0);
    tg.haptic('selection');
    expect(app.HapticFeedback.selectionChanged).toHaveBeenCalled();
    expect(tg.userFirstName()).toBe('Murad');
    expect(tg.languageCode()).toBe('az');
  });

  it('applies viewport and combined safe areas to CSS variables', () => {
    const { app, handlers } = mockWebApp();
    const root = document.createElement('div');
    const unbind = bindViewport(createTelegramAdapter(app), root);
    expect(root.style.getPropertyValue('--app-height')).toBe('700px');
    expect(root.style.getPropertyValue('--safe-top')).toBe('40px');
    expect(root.style.getPropertyValue('--safe-bottom')).toBe('20px');
    expect(handlers.has('safeAreaChanged')).toBe(true);
    unbind();
    expect(handlers.size).toBe(0);
  });
});
