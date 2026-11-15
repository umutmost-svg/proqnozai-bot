# Коэффициенты на весь каталог Mini App (план, утверждён владельцем 14.11.2026)

Цель: коэффициенты Mostbet по всем матчам каталога Mini App (117 турниров football-data), а не по 10 турнирам
`leagues.json`. Требования владельца: нормализация названий, допуск по времени/часовому поясу, неоднозначное
совпадение не матчится, без уверенного соответствия — «данные по коэффициентам недоступны» (никогда чужой матч),
логирование unmatched/ambiguous, без дублей, E2E smoke на нескольких реальных матчах (ближайший и более поздний).

## Решение владельца 14.11 (позже): источник — сама Mini App, бот не трогаем

Бот будет выведен из эксплуатации после полного запуска Mini App, поэтому коэффициенты не могут зависеть от него.
**Mini App сама ходит в Odds Checker API Mostbet** (`mostbet2.com`, белый список по IP — тот же сервер, тот же
исходящий IP): фоновая задача раз в 15 мин читает список линий, сопоставляет его со своим каталогом и запрашивает
цены по сопоставленным матчам. Время в линии Mostbet — UTC+3, переводится в UTC. Нынешняя передача от бота
(10 турниров) остаётся **запасным каналом** без изменений; при совпадении линии побеждает более свежая цена.
**Бюджет на переходный период (бот ещё работает, один IP):** список раз в 15 мин, не больше 20 запросов цен за
5 мин, 2 с между запросами, при любом 429 — пауза 2 мин. После отключения бота бюджет поднимается переменной.
Пункты 1 и 3–4 ниже (каталог от бота, `wanted`, оценка цен в боте) заменены этой задачей и в коде не реализованы (маршрут каталога от бота удалён); сопоставление (п. 2) и
свежесть (п. 5) — без изменений.

**Известный P2 (как у бота):** короткая страница списка считается концом курсора; если Mostbet когда-нибудь
отдаст неполную страницу посреди списка, матчи из непрочитанного хвоста на один цикл останутся без коэффициентов
(потеря покрытия, не чужая цена). Логи: `odds.source_list`, `odds.source_cycle` (счётчики причин) и раз в 15 мин
`odds.unmatched_examples` (названия unmatched/ambiguous/reversed для дополнения алиасов).

## Архитектура (первоначальная, с ботом — заменена разделом выше)

1. **Каталог линий (бот → Mini App, без запросов к API Mostbet).** Раз в цикл бот отправляет подписанным
   push'ем метаданные всех футбольных линий из своего кэша списка: не live, не виртуальные, не outright, начало в
   окне 7 дней (с 15.11; было 48 ч): `id, country, league, home, away, kickoff`. Это бесплатно (список бот и так обновляет каждые 15 мин).
2. **Сопоставление в Mini App (одно место правды).** Для каждой линии каталога — матч каталога Mini App:
   - кандидаты — события любого турнира с началом в пределах ±15 мин (всё в UTC; бот уже переводит время в UTC);
   - обе команды совпадают после нормализации (`team_key`: регистр, диакритика, пунктуация, `FC/CF/SK…`) по имени,
     короткому имени, алиасам поиска и алиасам коэффициентов того же id команды; порядок дома/в гостях тот же;
   - перевёрнутая пара, два кандидата, две линии на одно событие (дубли Mostbet) — **нет коэффициентов**;
   - если пара (страна, лига) линии есть в `leagues.json` и указывает на другой турнир — отказ;
   - итог по каждому событию: `matched` / `no_line` / `unknown_names` / `ambiguous` / `reversed` / `duplicate`;
     в лог — агрегированные счётчики и до N примеров названий unmatched/ambiguous (без пользовательских данных).
3. **Что оценивать (ответ Mini App на каталог).** Mini App отвечает списком `wanted` — id линий сопоставленных
   событий в порядке приоритета: ближние (≤ 6 ч до начала) — обновлять каждые 10 мин, дальние — раз в 45 мин.
4. **Оценка цен в боте.** Бот берёт цены только по `wanted` (вместо таблицы лиг), ближайшие первыми, в пределах
   прежнего бюджета: `MINIAPP_ODDS_MAX_LINES` за цикл (≤ 40), 2 с между запросами, пауза при любом 429 (бот
   пользователям важнее). Пока Mini App не прислала `wanted` (старая версия, сбой) — прежнее поведение по таблице.
5. **Свежесть в Mini App.** Срок годности цены зависит от времени до начала: ≤ 6 ч — 15 мин (как сейчас), дальше —
   60 мин. Отсечка за 15 мин до начала и live-маркеры — без изменений.
6. **Включение без второго перезапуска бота.** `wanted` присылается только при `MINIAPP_ODDS_CATALOG=true` в Mini App;
   иначе пустой список → бот работает как раньше. Бот перезапускается один раз (новая версия), режим включается
   перевыкаткой Mini App.

## Порядок выкладки
1. Mini App с приёмом каталога и сопоставлением, флаг выключен → лог покрытия (сколько событий matched / почему нет).
2. Бот (одна перевыкатка, stop-first, сценарий окна `deploy/...` как 12.11) — шлёт каталог, цены по-старому.
3. Несколько часов наблюдения покрытия и логов unmatched/ambiguous; дополнение алиасов.
4. Флаг в Mini App → бот оценивает `wanted`. E2E smoke: ближайший матч и матч на более позднюю дату — цены
   совпадают с линией Mostbet, у несопоставленного — «данные по коэффициентам недоступны».

## 15.11: names and the snapshot

- **Team names stay exact.** A loose pass was tried and dropped after three Codex rounds. Any rule that sets words aside ("AF", "RJ", "SE", "Regatas") or accepts one name inside another also accepted different clubs: "Portuguesa RJ" for "Portuguesa", "Regatas Brasil" for "Brasil", "Inter Milan" for "Milan", "Barcelona Femeni" for "Barcelona". Missing names go to the alias table (`leagues.json` aliases, keyed by the football-data team id). National teams also take the area aliases ("Czechia" / "Czech Republic"). The `odds.unmatched_examples` log now carries up to 30 samples per reason with both teams' provider ids, so an alias can be added directly. From the 14.11 log, "Vasco da Gama RJ" became alias 1780.
- **Snapshot.** Own prices are kept in `odds-snapshot.json` next to the database and written after every priced cycle (atomically). They are read once, right after the first complete list, and only lines that list still shows upcoming, with the same teams, competition and kickoff, come back. A deploy therefore no longer empties the odds for the roughly 30 minutes a refill takes, and a line withdrawn or gone live meanwhile is not served. They are re-checked by `read_book` and the usual age limits apply.
- **No odds before an analysis.** The owner's rule (15.11): prices are shown only inside an AI analysis or a quick forecast, never on the match page. `/api/sports/event/{id}` no longer carries `odds`, and the match page's odds card is removed.

## 15.11: the whole week and start at boot

- The catalogue window is 7 days (`CATALOG_WINDOW_S`), so matches further than 48 h ahead get prices too. There are three tiers:
  - up to 6 h ahead: refreshed every 10 min, served at most 15 min old;
  - 6–48 h: every 45 min, at most 60 min;
  - beyond 48 h: every 3 h, at most 4 h.

  A client keeps any price at most 1 h, then asks again. Within the budget, the soonest-to-go-stale line goes first. While any match beyond 6 h is due, a quarter of the requests (pages included) is reserved for those matches. The `stale=` count in `odds.source_cycle` measures coverage in production.
- The source starts with the app (a startup hook), no longer at the first signed-in request. After a deploy, or while nobody uses the app, prices no longer go stale.
