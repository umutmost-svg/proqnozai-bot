import { download, setCsrf, setUnauthenticatedHandler } from './api';

afterEach(() => vi.unstubAllGlobals());

it('a CSV download with a lost session opens the sign-in flow instead of a 401 page', async () => {
  setCsrf('download-test');
  const lost = vi.fn();
  setUnauthenticatedHandler(lost);
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'unauthenticated' }),
    { status: 401, headers: { 'Content-Type': 'application/json' } })));
  await expect(download('/api/admin/ads/analytics.csv?days=30', 'ads.csv')).rejects.toMatchObject({ status: 401 });
  expect(lost).toHaveBeenCalledTimes(1);
});
