/**
 * What the first screen is waiting for, tracked from real signals only:
 *   telegram — WebApp.ready()/expand() have been called;
 *   session  — the auth bootstrap left `checking` (any outcome: the app also
 *              works signed out, so a failed check does not block it);
 *   data     — requests the first screen started while booting (useAsync).
 * Lives outside React like AuthController: one instance per page.
 */
export type BootPhase = 'telegram' | 'session' | 'data';

export interface BootSnapshot {
  /** Phases still running, in display order. */
  readonly pending: readonly BootPhase[];
  /** A boot request of the first screen failed: the screen shows its own error. */
  readonly failed: boolean;
  /** Nothing left to wait for (ready, or failed into the screen's error state). */
  readonly settled: boolean;
}

type TaskState = 'pending' | 'done' | 'failed';

export class BootTracker {
  private telegramReady = false;
  private sessionChecked = false;
  /** Boot requests may register until the first screen has mounted. */
  private registrationOpen = true;
  private readonly tasks = new Map<string, { state: TaskState; retry: () => void }>();
  private listeners = new Set<() => void>();
  private scheduled = false;
  private snapshot: BootSnapshot = this.compute();

  getSnapshot = (): BootSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  markTelegramReady(): void {
    if (this.telegramReady) return;
    this.telegramReady = true;
    this.emit();
  }

  markSessionChecked(): void {
    if (this.sessionChecked) return;
    this.sessionChecked = true;
    this.emit();
  }

  /** Reports a data request of the first screen. Later screens are not boot data. */
  reportTask(id: string, state: TaskState, retry: () => void): void {
    const known = this.tasks.get(id);
    if (!known && !this.registrationOpen) return;
    if (known && known.state === state && known.retry === retry) return;
    this.tasks.set(id, { state, retry });
    this.emit();
  }

  /** The request's component unmounted (e.g. StrictMode remount or navigation). */
  forgetTask(id: string): void {
    if (this.tasks.delete(id)) this.emit();
  }

  /**
   * Called once the first screen has mounted (its effects ran before this):
   * from now on only already known requests count. A screen without requests
   * has nothing to load.
   */
  closeRegistration(): void {
    if (!this.registrationOpen) return;
    this.registrationOpen = false;
    this.emit();
  }

  /** Undo of closeRegistration for StrictMode's effect replay. */
  reopenRegistration(): void {
    if (this.registrationOpen) return;
    this.registrationOpen = true;
    this.emit();
  }

  /** User asked to try again: re-run boot requests that have not succeeded. */
  retryData(): void {
    for (const task of this.tasks.values()) {
      if (task.state !== 'done') task.retry();
    }
  }

  private compute(): BootSnapshot {
    const states = [...this.tasks.values()].map((t) => t.state);
    const failed = states.includes('failed');
    const dataPending = this.registrationOpen || states.includes('pending');
    const pending: BootPhase[] = [];
    if (!this.telegramReady) pending.push('telegram');
    if (!this.sessionChecked) pending.push('session');
    if (dataPending && !failed) pending.push('data');
    // A failed request ends the wait for data; the session check still counts,
    // so the screen does not flash its error before identity is known.
    return { pending, failed, settled: pending.length === 0 };
  }

  /**
   * Re-evaluates once per batch, in a microtask. React runs one commit's effects
   * in an order this class must not depend on: on navigation the old screen's
   * forgetTask can run before the gate reopens registration, and evaluating in
   * between would see "no requests" and settle (and latch) too early.
   */
  private emit(): void {
    if (this.scheduled) return;
    this.scheduled = true;
    queueMicrotask(() => {
      this.scheduled = false;
      this.publish();
    });
  }

  private publish(): void {
    // Boot happens once: after it settled, later requests (e.g. a retry on the
    // screen's error state) belong to the screen, never to the loading screen.
    if (this.snapshot.settled) return;
    const next = this.compute();
    const prev = this.snapshot;
    if (prev.settled === next.settled && prev.failed === next.failed && prev.pending.join() === next.pending.join()) {
      return;
    }
    this.snapshot = next;
    for (const l of this.listeners) l();
  }
}
