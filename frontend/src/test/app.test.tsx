import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from './renderApp';

const cards = () => screen.queryAllByTestId('event-card').map((c) => c.getAttribute('data-event-id'));

async function openCatalog(hash = '#/matches', opts?: Parameters<typeof renderApp>[1]) {
  const utils = renderApp(hash, opts);
  await screen.findByTestId('event-list');
  return utils;
}

describe('home', () => {
  it('shows the featured event, demo marks and filters', async () => {
    renderApp('#/');
    const hero = await screen.findByTestId('hero');
    expect(within(hero).getByText('Real Madrid')).toBeInTheDocument();
    expect(within(hero).getByText('ДЕМО')).toBeInTheDocument();
    expect(within(hero).getByText('8 ноября · 22:00')).toBeInTheDocument();
    expect(screen.getByTestId('demo-footer')).toHaveTextContent('Демо · 8 ноября 2026 · время Баку');
  });

  it('shows the ProqnozAI brand with the vector P symbol instead of a text mark', async () => {
    renderApp('#/');
    await screen.findByTestId('hero');
    const heading = screen.getByRole('heading', { level: 1, name: 'ProqnozAI' });
    const header = heading.closest('header')!;
    const symbol = header.querySelector('img[src*="brand-symbol-32"]');
    expect(symbol).not.toBeNull();
    expect(symbol).toHaveAttribute('alt', '');
    expect(header).not.toHaveTextContent(/Spor/i);
  });

  it('updates the hero when the sport changes and opens the catalog', async () => {
    const user = userEvent.setup();
    renderApp('#/');
    await screen.findByTestId('hero');
    await user.click(screen.getByRole('button', { name: 'Теннис' }));
    // Without a featured flag the hero takes the live event first.
    expect(await screen.findByText('Mateo Alvarez')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Все матчи/ }));
    await screen.findByTestId('event-list');
    expect(screen.getByRole('button', { name: 'Теннис' })).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('catalog filters and search', () => {
  it('filters by sport, tournament and date', async () => {
    const user = userEvent.setup();
    await openCatalog();
    expect(cards()).toEqual(['fb-costa-sierra', 'fb-kingsford-harbour', 'fb-rm-fcb']);

    await user.click(screen.getByRole('button', { name: /Premier League/ }));
    await waitFor(() => expect(cards()).toEqual(['fb-kingsford-harbour']));

    await user.click(screen.getByRole('button', { name: 'Все турниры' }));
    await user.click(screen.getByRole('button', { name: '7 ноября' }));
    await waitFor(() => expect(cards()).toEqual(['fb-finished']));

    await user.click(screen.getByRole('button', { name: 'MMA' }));
    await waitFor(() => expect(cards()).toEqual(['mma-finished']));
  });

  it('searches participants and shows an empty state with reset', async () => {
    const user = userEvent.setup();
    await openCatalog();
    await user.type(screen.getByPlaceholderText('Поиск матча или команды'), 'barcelona');
    await waitFor(() => expect(cards()).toEqual(['fb-rm-fcb']));

    await user.type(screen.getByPlaceholderText('Поиск матча или команды'), 'xyz');
    expect(await screen.findByText('Ничего не найдено')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Сбросить фильтры' }));
    await waitFor(() => expect(cards()).toHaveLength(3));
    expect(screen.getByPlaceholderText('Поиск матча или команды')).toHaveValue('');
  });

  it('shows an empty day', async () => {
    const user = userEvent.setup();
    await openCatalog();
    await user.click(screen.getByRole('button', { name: '11 ноября' }));
    expect(await screen.findByText('На эту дату матчей нет')).toBeInTheDocument();
  });

  it('shows loading, then error with a working retry', async () => {
    const user = userEvent.setup();
    renderApp('#/matches', { failOnce: ['events'] });
    expect(screen.getByTestId('loading-state')).toBeInTheDocument();
    expect(await screen.findByText('Расписание не загрузилось')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    await screen.findByTestId('event-list');
    expect(cards()).toHaveLength(3);
  });

  it('keeps the query on search error and retries it', async () => {
    const user = userEvent.setup();
    await openCatalog('#/matches', { failOnce: ['search'] });
    await user.click(screen.getByPlaceholderText('Поиск матча или команды'));
    await user.paste('real');
    expect(await screen.findByText('Поиск не загрузился')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    await waitFor(() => expect(cards()).toEqual(['fb-rm-fcb']));
    expect(screen.getByPlaceholderText('Поиск матча или команды')).toHaveValue('real');
  });

  it('filters by the calendar day of the selected time zone', async () => {
    const user = userEvent.setup();
    await openCatalog('#/matches', { settings: { timeZone: 'UTC' } });
    // Today in UTC is the 8th; the 00:30 Baku kickoff on the 9th is 20:30 UTC on the 8th.
    expect(cards()).toContain('fb-midnight');
    expect(screen.getByText(/08\.11\.2026 · 20:30/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '9 ноября' }));
    await waitFor(() => expect(cards()).not.toContain('fb-midnight'));
  });
});

describe('event navigation', () => {
  it('returns from an event with filters and list position preserved', async () => {
    const user = userEvent.setup();
    const { services } = await openCatalog();
    await user.click(screen.getByRole('button', { name: 'Теннис' }));
    await user.click(screen.getByRole('button', { name: /ATP Tour/ }));
    await waitFor(() => expect(cards()).toEqual(['tn-live']));

    const viewport = screen.getByTestId('scroll-viewport');
    viewport.scrollTop = 240;
    act(() => {
      viewport.dispatchEvent(new Event('scroll'));
    });
    expect(services.scrollMemory.get('/matches')).toBe(240);

    await user.click(screen.getAllByTestId('event-card')[0]!);
    expect(await screen.findByTestId('event-hero')).toHaveAttribute('data-sport', 'tennis');

    await user.click(screen.getByRole('button', { name: 'Назад' }));
    await waitFor(() => expect(cards()).toEqual(['tn-live']));
    expect(screen.getByRole('button', { name: 'Теннис' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /ATP Tour/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('scroll-viewport').scrollTop).toBe(240);
  });

  it('shows sport-specific stats', async () => {
    renderApp('#/event/bb-live?tab=stats');
    const stats = await screen.findByTestId('stats');
    expect(within(stats).getByText('Подборы')).toBeInTheDocument();
    expect(screen.getByTestId('score')).toHaveTextContent('74 : 68');
  });
});

describe('AI analysis', () => {
  it.each([
    ['fb-rm-fcb', 'football', ['Real Madrid', 'Ничья', 'Barcelona']],
    ['tn-live', 'tennis', ['Mateo Alvarez', 'Luca Bernardi']],
    ['bb-live', 'basketball', ['Bay City Comets', 'Lakeshore Pilots']],
    ['mma-live', 'mma', ['Kai Moreno', 'Elias Brandt']],
  ])('shows the %s demo result for the right sport', async (id, sport, outcomes) => {
    renderApp(`#/event/${id}/ai`);
    const result = await screen.findByTestId('ai-result');
    expect(result).toHaveAttribute('data-sport', sport);
    for (const o of outcomes) expect(within(result).getByText(o)).toBeInTheDocument();
    expect(screen.getByText('ПРИМЕР · ДЕМО')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Сохранить разбор/ })).toBeDisabled();
  });

  it('shows the waiting state first', async () => {
    renderApp('#/event/fb-rm-fcb/ai', { aiLatencyMs: 50 });
    expect(await screen.findByTestId('ai-loading')).toHaveTextContent('Готовим разбор');
    await screen.findByTestId('ai-result');
  });

  it('shows insufficient data without inventing probabilities', async () => {
    const user = userEvent.setup();
    renderApp('#/event/tn-wta-upcoming/ai');
    const block = await screen.findByTestId('ai-insufficient');
    expect(within(block).getByText('Нет свежей статистики')).toBeInTheDocument();
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Посмотреть доступную статистику' }));
    expect(await screen.findByRole('tab', { name: 'Статистика' })).toHaveAttribute('aria-selected', 'true');
  });

  it('recovers from an error with retry', async () => {
    const user = userEvent.setup();
    renderApp('#/event/mma-live/ai', { failOnce: ['ai'] });
    expect(await screen.findByTestId('ai-error')).toHaveTextContent('Разбор не создан');
    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByTestId('ai-result')).toHaveAttribute('data-sport', 'mma');
  });
});

describe('profile settings', () => {
  it('switches the interface language and persists it', async () => {
    const user = userEvent.setup();
    renderApp('#/profile');
    await user.click(screen.getByRole('link', { name: /Язык интерфейса/ }));
    await user.click(await screen.findByRole('radio', { name: 'English' }));
    await user.click(screen.getByRole('button', { name: 'Сохранить' }));
    expect(await screen.findByRole('link', { name: /Interface language/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Home' })).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem('sporai.settings.v1')!).lang).toBe('en');

    await user.click(screen.getByRole('link', { name: /Interface language/ }));
    // Route changes render in a transition, so wait for the screen instead of assuming it is there.
    await user.click(await screen.findByRole('radio', { name: 'Azərbaycan' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    // Wait for the profile screen itself: the language screen re-renders in AZ before it unmounts.
    expect(await screen.findByRole('link', { name: /İnterfeys dili/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ana səhifə' })).toBeInTheDocument();
  });

  it('changes the time zone used for dates', async () => {
    const user = userEvent.setup();
    renderApp('#/profile/timezone');
    await user.click(await screen.findByRole('radio', { name: /Москва · UTC\+3/ }));
    await user.click(screen.getByRole('button', { name: 'Сохранить' }));
    await user.click(await screen.findByRole('link', { name: 'Главная' }));
    expect(await screen.findByText('8 ноября · 21:00')).toBeInTheDocument();
    expect(screen.getByTestId('demo-footer')).toHaveTextContent('время Москвы');
  });

  it('turns decorative motion off', async () => {
    const user = userEvent.setup();
    renderApp('#/profile/appearance');
    expect(document.documentElement.dataset.reduceMotion).toBe('false');
    await user.click(screen.getByRole('switch', { name: /Меньше анимации/ }));
    await user.click(screen.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(document.documentElement.dataset.reduceMotion).toBe('true'));
  });

  it('marks deferred sections as unavailable instead of faking them', async () => {
    renderApp('#/profile');
    const row = (await screen.findByText('Избранные матчи')).parentElement!;
    expect(row).toHaveAttribute('aria-disabled', 'true');
    expect(within(row).getByText('Появится позже')).toBeInTheDocument();
  });
});
