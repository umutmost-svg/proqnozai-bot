import { BootTracker } from './boot';

const noop = () => {};
/** Snapshots are published once per batch, in a microtask. */
const flush = () => Promise.resolve();

describe('BootTracker', () => {
  it('waits for Telegram, the session check and the first screen', async () => {
    const boot = new BootTracker();
    await flush();
    expect(boot.getSnapshot()).toEqual({ pending: ['telegram', 'session', 'data'], failed: false, settled: false });
    boot.markTelegramReady();
    boot.markSessionChecked();
    await flush();
    expect(boot.getSnapshot().pending).toEqual(['data']); // the first screen has not mounted yet
    boot.closeRegistration(); // a screen without requests
    await flush();
    expect(boot.getSnapshot()).toEqual({ pending: [], failed: false, settled: true });
  });

  it('a boot request keeps data pending until it succeeds', async () => {
    const boot = new BootTracker();
    boot.markTelegramReady();
    boot.markSessionChecked();
    boot.reportTask('a', 'pending', noop);
    boot.closeRegistration();
    await flush();
    expect(boot.getSnapshot().pending).toEqual(['data']);
    boot.reportTask('a', 'done', noop);
    await flush();
    expect(boot.getSnapshot().settled).toBe(true);
  });

  it('a failed request ends the wait for data but not for the session', async () => {
    const boot = new BootTracker();
    boot.markTelegramReady();
    boot.reportTask('a', 'failed', noop);
    boot.closeRegistration();
    await flush();
    expect(boot.getSnapshot()).toEqual({ pending: ['session'], failed: true, settled: false });
    boot.markSessionChecked();
    await flush();
    expect(boot.getSnapshot()).toEqual({ pending: [], failed: true, settled: true });
  });

  it('requests started after the first screen are not boot data', async () => {
    const boot = new BootTracker();
    boot.closeRegistration();
    boot.reportTask('later', 'pending', noop);
    await flush();
    expect(boot.getSnapshot().pending).not.toContain('data');
  });

  it('survives StrictMode effect replay (forget, reopen, report, close)', async () => {
    const boot = new BootTracker();
    boot.markTelegramReady();
    boot.markSessionChecked();
    boot.reportTask('a', 'pending', noop);
    boot.closeRegistration();
    boot.reopenRegistration(); // parent cleanup
    boot.forgetTask('a'); // child cleanup
    boot.reportTask('a', 'pending', noop); // child effect again
    boot.closeRegistration(); // parent effect again
    await flush();
    expect(boot.getSnapshot().pending).toEqual(['data']);
  });

  it('retryData re-runs only requests that did not succeed', async () => {
    const boot = new BootTracker();
    const ok = vi.fn();
    const bad = vi.fn();
    boot.reportTask('ok', 'done', ok);
    boot.reportTask('bad', 'failed', bad);
    boot.retryData();
    expect(ok).not.toHaveBeenCalled();
    expect(bad).toHaveBeenCalledTimes(1);
  });

  it('notifies only on visible changes and keeps the snapshot stable', async () => {
    const boot = new BootTracker();
    const listener = vi.fn();
    boot.subscribe(listener);
    const before = boot.getSnapshot();
    boot.reportTask('a', 'pending', noop); // data was already pending
    await flush();
    expect(listener).not.toHaveBeenCalled();
    await flush();
    expect(boot.getSnapshot()).toBe(before);
    boot.markTelegramReady();
    await flush();
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('once settled, stays settled: later retries belong to the screen', async () => {
    const boot = new BootTracker();
    boot.markTelegramReady();
    boot.markSessionChecked();
    boot.reportTask('a', 'failed', noop);
    boot.closeRegistration();
    await flush();
    expect(boot.getSnapshot().settled).toBe(true);
    boot.reportTask('a', 'pending', noop); // the screen's own retry
    await flush();
    expect(boot.getSnapshot().settled).toBe(true);
  });

  it('an intermediate state inside one batch never settles the boot (navigation)', async () => {
    const boot = new BootTracker();
    boot.markTelegramReady();
    boot.markSessionChecked();
    boot.reportTask('old', 'pending', noop);
    boot.closeRegistration();
    await flush();
    // One commit: the old screen's cleanup, the gate's cleanup, the new screen, the gate.
    boot.forgetTask('old');
    boot.reopenRegistration();
    boot.reportTask('new', 'pending', noop);
    boot.closeRegistration();
    await flush();
    expect(boot.getSnapshot()).toEqual({ pending: ['data'], failed: false, settled: false });
  });
});
