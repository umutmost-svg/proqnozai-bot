import { createSessionStorageMarker } from './signedOut';

const LAUNCH = 'query_id=AA&user=%7B%22id%22%3A501%7D&auth_date=1700000000&hash=' + 'a'.repeat(64);

describe('sessionStorage signed-out marker', () => {
  beforeEach(() => window.sessionStorage.clear());

  it('matches the same launch only and stores a fingerprint, not initData', async () => {
    const marker = createSessionStorageMarker();
    expect(await marker.matches(LAUNCH)).toBe(false);
    await marker.remember(LAUNCH);
    expect(await marker.matches(LAUNCH)).toBe(true);
    expect(await marker.matches(LAUNCH.replace('1700000000', '1700000001'))).toBe(false);

    const stored = JSON.stringify({ ...window.sessionStorage });
    expect(stored).not.toContain('auth_date');
    expect(stored).not.toContain('a'.repeat(64));
    expect(Object.values({ ...window.sessionStorage })).toEqual([expect.stringMatching(/^[0-9a-f]{64}$/)]);
  });

  it('a new page with the same storage still sees the marker (reload)', async () => {
    await createSessionStorageMarker().remember(LAUNCH);
    expect(await createSessionStorageMarker().matches(LAUNCH)).toBe(true);
  });

  it('broken storage never throws and never blocks', async () => {
    const broken = () => {
      throw new Error('storage disabled');
    };
    const marker = createSessionStorageMarker(broken as unknown as () => Storage);
    await expect(marker.remember(LAUNCH)).resolves.toBeUndefined();
    expect(await marker.matches(LAUNCH)).toBe(false);
  });
});
