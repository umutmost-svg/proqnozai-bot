import { render, screen } from '@testing-library/react';
import { App } from './App';

const stats = { starts: 4, botStarts: 3, miniappStarts: 1, inMiniApp: 2, active7d: 1, converted: 1 };

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/api/admin/auth/me')
      ? { admin: { id: 1, email: 'a@x', role: 'analyst' }, csrf: 'c', pilot: { dataset: 'demo', demoTelemetry: false } }
      : {
        items: [{ id: 1, code: 'insta01', name: 'Инстаграм', channel: 'bot', archivedAt: null, createdAt: 1, by: 'o@x',
          param: 'ref_insta01', url: 'https://t.me/sporbot?start=ref_insta01', stats }],
        funnel: { starts: 4, inMiniApp: 2, active7d: 1, converted: 1 },
        unknownCodes: [{ code: 'ghost99', stats }], telemetry: true, names: { bot: 'sporbot', miniapp: null },
      };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});

afterEach(() => vi.unstubAllGlobals());

it('shows links with their funnel; an analyst cannot create links', async () => {
  window.history.pushState({}, '', '/referrals');
  render(<App />);
  expect(await screen.findByText('Инстаграм')).toBeInTheDocument();
  expect(screen.getByText('https://t.me/sporbot?start=ref_insta01')).toBeInTheDocument();
  expect(screen.getByText('ref_ghost99')).toBeInTheDocument();
  expect(screen.queryByRole('form', { name: 'Новая ссылка' })).not.toBeInTheDocument();
  expect(screen.getAllByRole('link', { name: 'Пользователи' }).some((a) => a.getAttribute('href') === '/users?ref=insta01')).toBe(false);
});
