# Доступ к Dokploy через admin.proqnozai.com (Cloudflare Tunnel + Access) и закрытие :3000

Подготовлено 2026-11-12. **Ничего не выполнено.** Найден второй публичный путь к панели (через Traefik :80, раздел 1) — закрывается шагом 2a. Каждый шаг с пометкой **[владелец]** или **[сервер]** — по
отдельному разрешению; firewall — только после успешной проверки нового доступа и отдельного разрешения.

## 1. Аудит (2026-11-12, только чтение)

| Что | Факт |
|---|---|
| `admin.proqnozai.com` | proxied через Cloudflare (ответ с IP Cloudflare); **Access уже работает**: без входа — 302 на `patient-mode-acdb.cloudflareaccess.com` (команда Zero Trust). Куда запись указывает за Cloudflare — снаружи не видно, туннеля нет |
| Туннель | `cloudflared` на сервере **нет** (ни пакета, ни контейнера, ни systemd-юнита) |
| Dokploy | сервис Swarm `dokploy`, порт **3000 опубликован в режиме host на 0.0.0.0 и [::]** |
| **Второй путь к панели** | `/etc/dokploy/traefik/dynamic/dokploy.yml`: роутер `dokploy-router-app`, `Host(\`dokploy.docker.localhost\`)` на точке входа `web` (:80) → `http://dokploy:3000`. Traefik сверяет Host, присланный клиентом: **`curl -H 'Host: dokploy.docker.localhost' http://164.90.180.208/` → 200 (панель Dokploy)** — проверено снаружи 2026-11-12. По 443 и с Host `admin.proqnozai.com` — 404. Firewall этот путь не закрывает (80 нужен) — **закрывается правкой маршрута (шаг 2a)** |
| :3000 снаружи | **открыт** (TCP-соединение с внешней машины, до и после ресайза) |
| UFW | активен: deny incoming, разрешены 22, 80, 443 (v4/v6). Порт 3000 публикует Docker (`docker-proxy`/iptables) — **UFW его не закрывает**; поэтому нужен DigitalOcean Cloud Firewall |
| Порты Swarm | dockerd слушает 2377/tcp, 7946/tcp+udp, 4789/udp на всех интерфейсах; снаружи **закрыты** (их режет UFW — это не порты docker-proxy) |
| Traefik | `dokploy-traefik`: 80/tcp, 443/tcp, 443/udp (HTTP/3). Обслуживает дашборд бота `…httpworker…sslip.io` (публичный, 401 без токена; через него работают партнёрские ссылки `/r/…`) |
| SSH | порт 22, только ключи (`PasswordAuthentication no`), root по ключу |
| IPv6 | глобального IPv6 у дроплета нет (сокеты [::] слушают, но снаружи недоступны) |
| Деплой по webhook | в логах Dokploy за 30 дней 0 обращений `deploy/github`/webhook — признаков, что GitHub ходит на :3000, нет; **подтвердить в UI** (шаг 0) |

## 2. Решение

```
Браузер админа ──HTTPS──> Cloudflare Access (вход только разрешённого e-mail)
                              │
                     Cloudflare Tunnel (исходящее соединение с сервера, вход не нужен)
                              │
            cloudflared (контейнер, сеть host) ──http──> 127.0.0.1:3000 (Dokploy)
```

- **Коннектор:** отдельный контейнер `cloudflare/cloudflared:<фиксированная версия>` с `--network host` и
  `--restart unless-stopped`, **вне Swarm и вне Dokploy**: панель не управляет собственным путём доступа
  (сломанный Dokploy не отрезает доступ к нему же), выкладки приложений его не трогают. Без установки пакетов.
- Токен туннеля — файл `/root/cloudflared/token` (владелец UID 65532 — пользователь образа cloudflared,
  права 0400), монтируется только на чтение и читается через `--token-file`: в `docker inspect`, аргументах и
  окружении процесса его нет. Удаленно управляемый туннель (маршруты — в Zero Trust). Версия образа
  зафиксирована: `cloudflare/cloudflared:2026.10.5` (amd64 есть; флаги `--token-file`, `--edge-ip-version`,
  `--no-autoupdate` проверены).
- **Сеть коннектора (проверено на сервере 2026-11-12, только чтение):** в сети host контейнер видит те же
  адреса, что сам сервер: `http://127.0.0.1:3000` → 200 (Dokploy опубликован в режиме host на 0.0.0.0 и [::],
  loopback не зависит от облачного firewall). Исходящие к Cloudflare: TCP 7844 до `region1`/`region2.v2.argotunnel.com`
  открыт, `api.cloudflare.com:443` открыт. DNS отдаёт для туннеля и IPv6-адреса, а глобального IPv6 у
  дроплета нет — отсюда `--edge-ip-version 4`. Отдельная Docker-сеть или подключение к `dokploy-network` не
  нужны: без них коннектор не видит контейнеры приложений, только Dokploy на loopback.
- **Маршрут туннеля:** `admin.proqnozai.com` → `http://127.0.0.1:3000`. HTTP внутри сервера допустим: снаружи
  соединение шифрует туннель. WebSocket (логи, терминал Dokploy) туннель пропускает.
- **После закрытия :3000** в DigitalOcean Cloud Firewall порт остаётся доступен на самом сервере (127.0.0.1) —
  туннель и SSH-туннель работают; Dokploy и его данные не меняются.
- **Access:** существующее приложение для `admin.proqnozai.com` — одна политика **Allow**, Include → *Emails* =
  e-mail администратора(ов); никаких *Everyone*, *Bypass*, *Service Auth*; вход — One-time PIN или Google;
  сессия 24 ч. Вторым слоем остаётся логин Dokploy — включить в нём **2FA** (настройки профиля Dokploy).

## 3. План (кто, что, ожидаемый результат)

0. **[владелец] Проверки до начала (UI, только чтение):**
   - DigitalOcean → Droplet → Networking → Firewalls: какие firewall уже применены (правила складываются; широкое
     Allow оставит :3000 открытым). Записать.
   - GitHub → репозиторий → Settings → Webhooks (адреса и Recent Deliveries) и Settings → GitHub Apps; Dokploy →
     бот → Deployments (источник выкладок), Autodeploy, API-токены Dokploy. Логи Dokploy за 30 дней не содержат
     webhook-запросов, но это не доказательство. **Гейт:** каждый найденный вызов :3000 — либо выводится
     (Autodeploy бота OFF — рекомендация), либо получает отдельный защищённый путь; только потом firewall.
   - Dokploy → приложение бота → Deployments: были ли выкладки по webhook; Settings → Git / GitHub App:
     адрес webhook. Если webhook указывает на `http://164.90.180.208:3000/…` — после закрытия :3000 автодеплой
     бота из GitHub **перестанет работать**. Рекомендация: Autodeploy бота **выключить** (бот заморожен до
     вывода из эксплуатации; Mini App тоже без автодеплоя) — отдельное решение.
   - Cloudflare → DNS → `admin.proqnozai.com`: **записать** тип и значение (для отката).
   - Zero Trust → Access → Applications: приложение типа Self-hosted на **весь** хост `admin.proqnozai.com`
     (без пути); нет других приложений на этот хост или его пути (более конкретный путь перекрывает общее
     приложение); политики — только Allow с явными e-mail; нет Bypass, Service Auth, Everyone.
   - Dokploy → профиль → **включить 2FA**, сохранить резервные коды (не в чат), выйти и войти заново с кодом.
1. **[владелец] Zero Trust → Networks → Tunnels → Create tunnel** (Cloudflared), имя `proqnozai-admin`.
   Токен (показывается в команде установки) передаётся на сервер **не через чат и не в истории оболочки**:
   владелец по SSH выполняет
   `install -d -m 700 /root/cloudflared; read -rs T; (umask 077; printf '%s' "$T" > /root/cloudflared/token); unset T; chown 65532:65532 /root/cloudflared/token; chmod 400 /root/cloudflared/token; stat -c '%a %u' /root/cloudflared/token`
   (ожидается `400 65532`; с владельцем root cloudflared файл не прочитает — проверено: `permission denied`).
2. **[сервер, по разрешению] Запуск коннектора** (не трогает бота, Dokploy, Traefik):
   ```sh
   docker run -d --name cloudflared --network host --restart unless-stopped \
     -v /root/cloudflared/token:/etc/cloudflared/token:ro cloudflare/cloudflared:2026.10.5 \
     tunnel --no-autoupdate --edge-ip-version 4 run --token-file /etc/cloudflared/token
   docker logs --tail 20 cloudflared   # "Registered tunnel connection", без ошибок
   ```
   Zero Trust → Tunnels: статус **Healthy**.
2a. **[сервер, по разрешению] Закрыть второй путь к панели через Traefik** (файл сгенерировал Dokploy;
   данные Dokploy и сам сервис не меняются; Traefik v3.6.7 перечитывает каталог `dynamic` сам — `watch: true`):
   ```sh
   F=/etc/dokploy/traefik/dynamic/dokploy.yml
   cp -p $F /root/dokploy.yml.bak-$(date -u +%Y%m%dT%H%M%SZ)
   # в rule роутера dokploy-router-app — только соединения с самого сервера (ClientIP: один CIDR на вызов):
   #   rule: Host(`dokploy.docker.localhost`) && PathPrefix(`/`) && ClientIP(`127.0.0.1/32`)
   sleep 5
   # правило ЗАГРУЖЕНО и работает: с loopback внутри Traefik — панель (200)
   docker exec dokploy-traefik wget -q -S -O /dev/null --header 'Host: dokploy.docker.localhost' http://127.0.0.1/ 2>&1 | grep -m1 -oE 'HTTP/1.1 [0-9]+'   # HTTP/1.1 200
   # и ЗАКРЫТО для всех остальных (с самого сервера источник — публичный IP, не loopback)
   curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: dokploy.docker.localhost' http://164.90.180.208/   # 404
   ```
   Обе проверки обязательны: ошибочное правило Traefik отбрасывает молча (в логах INFO ничего), и тогда тоже
   будет 404 снаружи — но и не 200 изнутри. Проверено локально на `traefik:v3.6.7`: исходное правило — 200;
   с `ClientIP(\`127.0.0.1/32\`)` — снаружи 404, с поддельными `X-Forwarded-For`/`X-Real-Ip: 127.0.0.1` — 404,
   с loopback внутри Traefik — 200; правило с двумя аргументами `ClientIP` — 404 и без записи в логе.
   Снаружи то же: `curl -s -o /dev/null -w '%{http_code}' -H 'Host: dokploy.docker.localhost' http://164.90.180.208/` → **404**
   (было 200); `…httpworker…sslip.io` → 401 как прежде. Маршрут больше никому не нужен: панель — через туннель
   на :3000. Откат: вернуть копию файла.
   **Устойчивость правки (исходный код Dokploy v0.29.12 + факт на сервере):** при каждом старте Dokploy вызывает
   `createDefaultServerTraefikConfig()`, который пишет `dokploy.yml` **только если файла нет** — в этой версии
   правка переживает перезапуски и перезагрузки; что будет при **обновлении** Dokploy, по коду текущей версии
   не гарантировать — после каждого обновления обязателен ручной прогон проверки; на сервере файл не менялся с 2026-07-17, хотя Dokploy
   перезапускался (последний раз — 2026-11-11 при ресайзе). Файл перезаписывают только: Settings → Web Server →
   **домен сервера** (`updateServerTraefik` ставит `Host(<домен>)` без `ClientIP` — и открыл бы панель на
   этом домене мимо Access) и редактор Traefik-конфига там же (`updateWebServerTraefikConfig`); удаление файла
   вернёт исходный маршрут при следующем старте. **Эти настройки Dokploy не трогать**; почасовая проверка ниже
   сигналит о любом изменении файла.
   **Повторяющаяся проверка с уведомлением — часть шага 2a, без неё шаг не закрыт** (Dokploy может переписать
   маршрут при смене «Web Server/Domain» или обновлении):
   - **[владелец]** бесплатный аккаунт healthchecks.io → check `dokploy-bypass`, период 1 ч, grace 1 ч,
     уведомления на e-mail и/или Telegram владельца; URL пинга (`https://hc-ping.com/<uuid>`) — не секрет уровня
     ключа, но в репозиторий и чат не публикуется; на сервер — в `/root/.hc-dokploy-bypass` (0600).
   - скрипт `deploy/ops/dokploy-bypass-check.sh` → `/root/dokploy-bypass-check.sh` (0700, root). Сразу после
     проверенной правки зафиксировать одобренную версию файла:
     `sha256sum /etc/dokploy/traefik/dynamic/dokploy.yml > /root/.dokploy-route.sha256` (0600); cron root:
     `17 * * * * sh /root/dokploy-bypass-check.sh`. Тревога, если **хоть одно**: (1) `dokploy.yml` отличается от
     одобренной версии (точный SHA-256 — любая перезапись Dokploy: домен в Web Server, редактор конфига,
     обновление; разбор человеком, что бы там ни было, в т. ч. правила с `||`); (2) другой файл в `dynamic`
     называет бэкенд Dokploy (`dokploy-service-app`, `dokploy:3000`, без учёта регистра); (3) у сервиса `dokploy` появились метки Traefik
     (маршрут через Swarm-провайдер); (4) старый Host с публичного адреса отвечает не 404. Всё в порядке — пинг
     healthchecks, иначе `/fail` и syslog (`dokploy-bypass`); нет пинга — уведомление по истечении grace.
     Проверено локально: одобренный файл → 0; переписанный (в т. ч. `Host(new) || ClientIP(…)`) → OPEN; чужой
     файл с `dokploy-service-app@file` → OPEN; метки Traefik → OPEN; нет одобренного хеша → OPEN. На сервере
     сейчас ложных срабатываний нет: остальные файлы `dynamic` на Dokploy не ссылаются, меток Traefik у
     `dokploy` нет, `sha256sum`/`logger`/`curl` есть.
     После **намеренного** изменения `dokploy.yml` (повтор шага 2a) — снова обе проверки шага 2a и новый хеш.
     **Граница проверки:** она ловит **случайное** повторное открытие — через настройки Web Server самого Dokploy
     (они пишут только `dokploy.yml`), обновление, ручную правку, скопированный файл маршрута или метки на
     сервисе `dokploy`. Маршрут, который администратор Dokploy или root **намеренно** строит иначе (домен
     приложения, метки другого контейнера, адрес сервиса по IP), она не ловит: у такого человека и так полный
     доступ к панели и серверу. Защита от этого — сам доступ: Access, 2FA Dokploy, SSH только по ключу.
   - **Тест уведомления (обязательно):** `curl -fsS "$(cat /root/.hc-dokploy-bypass)/fail"` → владелец получил
     уведомление; затем `curl -fsS "$(cat /root/.hc-dokploy-bypass)"` → статус снова up.
   - Плюс ручной прогон `sh /root/dokploy-bypass-check.sh` после любого изменения настроек или обновления Dokploy;
     при `OPEN` — повторить шаг 2a.
   Тот же сервис подходит как канал `NOTIFY` для бэкапов (`production.md`, шаг 8).
3. **[владелец] Public hostname** туннеля: `admin.proqnozai.com` → Service `HTTP` `127.0.0.1:3000`.
   Cloudflare создаёт CNAME на `<id>.cfargotunnel.com` и **отклонит** сохранение, пока есть другая запись
   `admin` (A/AAAA/CNAME): сначала удалить все записи `admin` (значения записаны в шаге 0), потом сохранить
   hostname. Затем в DNS убедиться: одна
   запись CNAME `admin` → `<id>.cfargotunnel.com`, Proxied. Пока не проверено — доступ по SSH-туннелю.
4. **Проверка авторизации (до firewall):**
   - без входа → 302 на `…cloudflareaccess.com` (не Dokploy) для `/`, `/api/`, `/api/trpc/settings.health`,
     `/dashboard/projects`, любого случайного пути: `for p in / /api/ /api/trpc/settings.health /dashboard/projects /x$RANDOM; do curl -s -o /dev/null -w "$p %{http_code}\n" https://admin.proqnozai.com$p; done` — все 302;
   - приватное окно браузера, разрешённый e-mail → код → страница входа Dokploy → вход (с 2FA) → открыть логи
     приложения (WebSocket идёт) → выйти;
   - другой e-mail → Access отказывает (код не приходит / «not authorized»);
   - Zero Trust → Logs → Access: видны оба события;
   - бот: логи без ошибок, MB без ошибок (ничего не менялось).
   *Стоп:* любой пункт не прошёл → firewall не трогаем, разбор.
5. **[владелец, отдельное разрешение] DigitalOcean Cloud Firewall** `proqnozai-prod-fw`, применить к дроплету
   `proqnozai-prod`. **Гейт:** ни один уже применённый к дроплету firewall (шаг 0) не разрешает 3000 и не
   содержит «все порты» — правила разных firewall складываются (Allow добавляется); если такой есть — сначала
   убрать его или его правило:

   | Направление | Протокол | Порты | Источник / назначение |
   |---|---|---|---|
   | Inbound | TCP | 22 | All IPv4, All IPv6 (вход только по ключу; сузить до IP владельца — позже, если IP постоянный) |
   | Inbound | TCP | 80 | All IPv4, All IPv6 (Traefik: дашборд бота и партнёрские ссылки, затем Mini App) |
   | Inbound | TCP | 443 | All IPv4, All IPv6 (то же) |
   | Inbound | UDP | 443 | All IPv4, All IPv6 (HTTP/3 Traefik — как сейчас) |
   | Inbound | ICMP | — | All IPv4, All IPv6 |
   | Outbound | TCP | All ports | All IPv4, All IPv6 (Telegram, MB, GitHub, Docker Hub, Cloudflare Tunnel 7844, healthchecks) |
   | Outbound | UDP | All ports | All IPv4, All IPv6 (DNS 53, NTP 123, Cloudflare Tunnel QUIC 7844) |
   | Outbound | ICMP | — | All IPv4, All IPv6 |

   Каждая строка — отдельное правило DigitalOcean (несмежные порты одним правилом не задаются). Всё остальное
   входящее (в т. ч. **3000**, 2377, 7946, 4789, 8080) — закрыто. UFW не меняется.
6. **Проверка после firewall (сразу):**
   - с внешней машины `sh deploy/miniapp/origin-check.sh --ports 164.90.180.208 none 3000` → `RESULT: isolated`;
   - SSH работает (новая сессия, старую не закрывать до проверки);
   - `https://admin.proqnozai.com` → Access → Dokploy работает;
   - `https://…httpworker…sslip.io/` → 401 (как до), партнёрская ссылка `/r/<партнёр>` → редирект;
   - бот: `/start` отвечает, логи без ошибок и конфликтов; MB — без ошибок; `docker service ls` — всё 1/1.
   - DigitalOcean → Droplet → Networking: итоговые (суммарные) правила — 3000 нигде не разрешён;
   - снаружи: `curl -s -o /dev/null -w '%{http_code}' -H 'Host: dokploy.docker.localhost' http://164.90.180.208/` → 404;
   - уже установленные до firewall соединения к :3000 DigitalOcean не обрывает (ни у владельца, ни у кого-либо
     ещё); новые — закрыты (`origin-check.sh`). Чтобы гарантированно оборвать все старые, после firewall можно
     перезапустить контейнер Dokploy — это отдельное решение (перерыв панели ~1 мин, бот не затрагивается).
   *Стоп/откат:* что-то из этого не работает → убрать дроплет из firewall (Droplet → Networking → Firewalls).

## 4. Восстановление доступа

| Ситуация | Что делать |
|---|---|
| Access не пускает (ошибка политики, потерян e-mail) | владелец входит в Cloudflare (свой логин и 2FA аккаунта) → Access → политика; панель Dokploy тем временем — по SSH-туннелю |
| Туннель упал / контейнер `cloudflared` остановлен | SSH-туннель: `ssh -L 3000:127.0.0.1:3000 root@164.90.180.208` → `http://localhost:3000`; затем `docker logs cloudflared`, `docker restart cloudflared` |
| Токен туннеля скомпрометирован | Zero Trust → Tunnels → туннель → Refresh token (старый токен больше не даёт **новых** подключений); новый токен — в `/root/cloudflared/token` тем же способом, что в шаге 1; `docker restart cloudflared` (файл читается при каждом старте процесса); затем **удалить существующие подключения туннеля** (Zero Trust → туннель → Connectors / API «delete tunnel connections»), чтобы отключить чужой коннектор; статус Healthy только с нашим |
| SSH недоступен (ключ, sshd) | DigitalOcean → Droplet → **Recovery Console** (через панель DO; firewall на неё не влияет). На сервере вход только по ключу: консоли может понадобиться сброс пароля root (в настройках дроплета: Reset root password — раздел Access/Settings в панели DO) — это тоже выключение/перезагрузка, планировать как окно |
| Ошибка firewall | DigitalOcean → Networking → Firewalls → убрать дроплет / поправить правило (действует сразу) |
| Нужно убрать всё | `docker rm -f cloudflared`; удалить public hostname и туннель в Zero Trust; вернуть записанную DNS-запись `admin`; удалить firewall |

## 5. Что дальше для Mini App на этом сервере

- Mini App получит свой поддомен (например, `app.proqnozai.com`) через Traefik на 443 — firewall из п. 5 менять
  не нужно. Ограничить 443 только диапазонами Cloudflare на этом сервере **нельзя**: дашборд бота и партнёрские
  ссылки идут на `sslip.io` напрямую, не через Cloudflare. Защита origin Mini App — секрет `X-Edge-Auth`
  (приложение отвечает 403 без него) и rate limit Cloudflare.
- Сборка образа на этом сервере теперь допустима по памяти (свободно ~2,6 ГБ), но под наблюдением `free -m` и
  в тихое время; альтернатива — сборка вне сервера и registry.
