import { act, fireEvent, screen, within } from '@testing-library/react';
import { createHttpAuthApi, REQUEST_TIMEOUT_MS, type ApiResult, type AuthApi, type AuthSession } from '../auth/api';
import { offlineAuthApi, renderApp } from '../test/renderApp';
import { EXIT_MS, FAIL_AFTER_MS, SHOW_AFTER_MS, SLOW_AFTER_MS } from './AppLoadingGate';

/** A session check that answers only when the test says so. */
function slowSession() {
  let answer: (r: ApiResult<AuthSession>) => void = () => {};
  const me = vi.fn<AuthApi['me']>(() => new Promise((r) => (answer = r)));
  return { api: { ...offlineAuthApi, me }, answer: (r: ApiResult<AuthSession>) => act(() => answer(r)), me };
}

const advance = (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));
const gate = () => screen.queryByTestId('app-loading-gate');
const splash = () => screen.queryByTestId('app-loading');

let visibility: DocumentVisibilityState = 'visible';
let online = true;

beforeEach(() => {
  vi.useFakeTimers();
  visibility = 'visible';
  online = true;
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
  vi.spyOn(navigator, 'onLine', 'get').mockImplementation(() => online);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('AppLoadingGate', () => {
  it('a fast boot never shows the splash (no flash)', async () => {
    renderApp('#/');
    await advance(0);
    expect(gate()).toBeNull();
    await advance(5 * SHOW_AFTER_MS);
    expect(splash()).toBeNull();
    expect(screen.getByTestId('hero')).toBeInTheDocument();
  });

  it('shows the splash only after the delay, then leaves as soon as the session check ends', async () => {
    const s = slowSession();
    renderApp('#/', { authApi: s.api });
    await advance(SHOW_AFTER_MS - 1);
    expect(gate()).not.toBeNull();
    expect(splash()).toBeNull(); // plain cover only
    await advance(1);
    expect(splash()).toHaveAttribute('data-phase', 'connecting');
    expect(screen.getByText('Подключаем ProqnozAI')).toBeInTheDocument();
    expect(splash()).toHaveTextContent('PROQNOZAI');
    expect(splash()!.querySelector('img[src*="velocity-icon"]')).not.toBeNull();
    expect(document.querySelector('[inert]')).not.toBeNull(); // the app underneath is not reachable

    s.answer({ ok: false, error: 'unavailable' });
    await advance(0);
    expect(gate()).toHaveAttribute('data-mode', 'exit'); // fading out right away, no minimum time
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
    expect(document.querySelector('[inert]')).toBeNull();
  });

  it('says which real phase it waits for: data after the session', async () => {
    renderApp('#/', { latencyMs: 2000 });
    await advance(SHOW_AFTER_MS);
    expect(splash()).toHaveAttribute('data-phase', 'data');
    expect(screen.getByText('Загружаем матчи')).toBeInTheDocument();
    await advance(2000);
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
    expect(screen.getByTestId('hero')).toBeInTheDocument();
  });

  it('long wait: hint, then a failed view with retry and continue — never endless', async () => {
    const s = slowSession();
    renderApp('#/', { authApi: s.api });
    await advance(SLOW_AFTER_MS - 1);
    expect(screen.queryByTestId('app-loading-hint')).toBeNull();
    await advance(1);
    expect(screen.getByTestId('app-loading-hint')).toHaveTextContent('Это занимает больше времени, чем обычно');

    await advance(FAIL_AFTER_MS - SLOW_AFTER_MS);
    expect(splash()).toHaveAttribute('data-view', 'failed');
    expect(screen.getByRole('alert')).toHaveTextContent('Не удалось загрузить');
    expect(document.querySelectorAll('[data-step]')).toHaveLength(0); // motion stops

    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    await advance(0);
    expect(splash()).toHaveAttribute('data-view', 'loading'); // the wait is measured anew
    await advance(FAIL_AFTER_MS);
    expect(splash()).toHaveAttribute('data-view', 'failed');

    fireEvent.click(screen.getByRole('button', { name: 'Открыть без ожидания' }));
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
    expect(screen.getByTestId('hero')).toBeInTheDocument();
  });

  it('offline is said at once; back online clears it without restarting the wait', async () => {
    const s = slowSession();
    renderApp('#/', { authApi: s.api });
    await advance(SHOW_AFTER_MS);
    online = false;
    act(() => void window.dispatchEvent(new Event('offline')));
    expect(screen.getByTestId('app-loading-hint')).toHaveTextContent('Нет соединения');
    online = true;
    act(() => void window.dispatchEvent(new Event('online')));
    expect(screen.queryByTestId('app-loading-hint')).toBeNull();
    s.answer({ ok: false, error: 'unavailable' });
    await advance(0);
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
  });

  it('a failed first request hands over to the screen error state with retry', async () => {
    renderApp('#/', { latencyMs: 1000, failOnce: ['events'] });
    await advance(SHOW_AFTER_MS);
    expect(splash()).not.toBeNull();
    await advance(1000);
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
    expect(screen.getByTestId('error-state')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    await advance(SHOW_AFTER_MS * 4);
    expect(gate()).toBeNull(); // the screen's retry never brings the splash back
    await advance(1000);
    expect(screen.getByTestId('hero')).toBeInTheDocument();
  });

  it('time in the background does not count toward the limit', async () => {
    const s = slowSession();
    renderApp('#/', { authApi: s.api });
    await advance(FAIL_AFTER_MS - 1000);
    visibility = 'hidden';
    act(() => void document.dispatchEvent(new Event('visibilitychange')));
    await advance(60_000); // the WebView sat in the background
    visibility = 'visible';
    act(() => void document.dispatchEvent(new Event('visibilitychange')));
    expect(splash()).toHaveAttribute('data-view', 'loading');
    // Visible time accumulates: the background visit did not grant a new 12 s.
    await advance(1000);
    expect(splash()).toHaveAttribute('data-view', 'failed');
    s.answer({ ok: false, error: 'unavailable' });
    await advance(0);
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
  });

  it('returning from the background after boot never shows the splash again', async () => {
    renderApp('#/');
    await advance(0);
    visibility = 'hidden';
    act(() => void document.dispatchEvent(new Event('visibilitychange')));
    visibility = 'visible';
    act(() => void document.dispatchEvent(new Event('visibilitychange')));
    await advance(FAIL_AFTER_MS);
    expect(gate()).toBeNull();
  });

  it('reduced motion (in-app setting): static mark and text, no pulse, no signal, no dots', async () => {
    const s = slowSession();
    renderApp('#/', { authApi: s.api, settings: { reduceMotion: true } });
    await advance(SHOW_AFTER_MS);
    expect(splash()).toHaveAttribute('data-motion', 'static');
    expect(document.querySelectorAll('[data-step]')).toHaveLength(0);
    expect(screen.getByText('Подключаем ProqnozAI')).toBeInTheDocument();
  });

  it('full motion renders the sweeping signal and three dot states', async () => {
    const s = slowSession();
    renderApp('#/', { authApi: s.api });
    await advance(SHOW_AFTER_MS);
    expect(splash()).toHaveAttribute('data-motion', 'full');
    expect(document.querySelectorAll('[data-step]')).toHaveLength(3);
  });

  it('texts are localized (AZ, EN)', async () => {
    for (const [lang, text] of [['az', 'ProqnozAI-a qoşuluruq'], ['en', 'Connecting to ProqnozAI']] as const) {
      const s = slowSession();
      const { unmount } = renderApp('#/', { authApi: s.api, settings: { lang } });
      await advance(SHOW_AFTER_MS);
      expect(screen.getByText(text)).toBeInTheDocument();
      unmount();
    }
  });

  it('works under StrictMode: the first screen data is still awaited', async () => {
    renderApp('#/', { latencyMs: 2000, strict: true });
    await advance(SHOW_AFTER_MS);
    expect(splash()).toHaveAttribute('data-phase', 'data');
    await advance(2000);
    await advance(0);
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
  });

  it('a session check that never answers ends by the HTTP timeout, before the failed view', async () => {
    const hanging = vi.fn<typeof fetch>(() => new Promise<Response>(() => {}));
    renderApp('#/', { authApi: createHttpAuthApi(hanging) });
    await advance(SHOW_AFTER_MS);
    expect(splash()).toHaveAttribute('data-phase', 'connecting');
    await advance(REQUEST_TIMEOUT_MS - SHOW_AFTER_MS);
    await advance(0);
    await advance(EXIT_MS);
    expect(REQUEST_TIMEOUT_MS).toBeLessThan(FAIL_AFTER_MS);
    expect(gate()).toBeNull(); // the app opens signed out; the profile offers its own retry
    expect(hanging).toHaveBeenCalledTimes(1);
  });

  it('navigating while covered waits for the screen that is actually shown', async () => {
    renderApp('#/event/fb-rm-fcb', { latencyMs: 2000 });
    await advance(SHOW_AFTER_MS);
    expect(splash()).toHaveAttribute('data-phase', 'data');
    act(() => {
      window.location.hash = '#/matches'; // e.g. Telegram's BackButton during the splash
    });
    await advance(0);
    await advance(1500); // the first screen's request would have finished by now
    expect(splash()).not.toBeNull();
    await advance(500 + SHOW_AFTER_MS);
    await advance(0);
    await advance(EXIT_MS);
    expect(gate()).toBeNull();
    expect(screen.queryByTestId('loading-state')).toBeNull();
  });

  it('a flapping network neither restarts requests nor postpones the failed view', async () => {
    const s = slowSession();
    renderApp('#/', { authApi: s.api });
    for (let i = 0; i < 5; i += 1) {
      await advance(2000);
      online = false;
      act(() => void window.dispatchEvent(new Event('offline')));
      online = true;
      act(() => void window.dispatchEvent(new Event('online')));
    }
    expect(s.me).toHaveBeenCalledTimes(1); // nothing was restarted behind the user's back
    await advance(FAIL_AFTER_MS - 10_000);
    expect(splash()).toHaveAttribute('data-view', 'failed'); // the limit kept counting
  });

  it('the data message matches the screen that opens', async () => {
    for (const [hash, text] of [
      ['#/', 'Загружаем матчи'],
      ['#/event/fb-rm-fcb', 'Загружаем матч'],
      ['#/event/fb-rm-fcb/ai', 'Готовим AI-разбор'],
    ] as const) {
      const { unmount } = renderApp(hash, { latencyMs: 5000, aiLatencyMs: 5000 });
      await advance(SHOW_AFTER_MS);
      expect(splash()).toHaveAttribute('data-phase', 'data');
      expect(within(splash()!).getByText(text)).toBeInTheDocument(); // exact text match
      unmount();
    }
  });
});
