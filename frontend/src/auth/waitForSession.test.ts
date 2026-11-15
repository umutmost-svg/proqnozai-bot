import { AuthController } from './controller';
import { fail, ok, scriptedApi, SESSION } from './testing';
import { waitForSession } from './waitForSession';

const INIT = 'query_id=AA&user=%7B%22id%22%3A501%7D&auth_date=1&hash=' + '0'.repeat(64);

describe('waitForSession', () => {
  it('waits for the check and resolves true with a session', async () => {
    const auth = new AuthController(scriptedApi({ me: [ok(SESSION)] }), () => INIT);
    const waiting = waitForSession(auth);
    void auth.start();
    expect(await waiting).toBe(true);
  });

  it('resolves false when there is no session (outside Telegram, failed sign-in)', async () => {
    const outside = new AuthController(scriptedApi({}), () => null);
    void outside.start();
    expect(await waitForSession(outside)).toBe(false);
    const failed = new AuthController(scriptedApi({ me: [fail('unavailable')] }), () => INIT);
    void failed.start();
    expect(await waitForSession(failed)).toBe(false);
  });
});
