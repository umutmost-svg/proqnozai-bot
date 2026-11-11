# Доступ к Dokploy через admin.proqnozai.com (Cloudflare Tunnel + Access) и закрытие :3000

Подготовлено 2026-11-12. **Ничего не выполнено.** Каждый шаг с пометкой **[владелец]** или **[сервер]** — по
отдельному разрешению; firewall — только после успешной проверки нового доступа и отдельного разрешения.

## 1. Аудит (2026-11-12, только чтение)

| Что | Факт |
|---|---|
| `admin.proqnozai.com` | proxied через Cloudflare (ответ с IP Cloudflare); **Access уже работает**: без входа — 302 на `patient-mode-acdb.cloudflareaccess.com` (команда Zero Trust). Куда запись указывает за Cloudflare — снаружи не видно, туннеля нет |
| Туннель | `cloudflared` на сервере **нет** (ни пакета, ни контейнера, ни systemd-юнита) |
| Dokploy | сервис Swarm `dokploy`, порт **3000 опубликован в режиме host на 0.0.0.0 и [::]**; внутренний маршрут Traefik для панели — только `dokploy.docker.localhost` (наружу через Traefik панели нет) |
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
- Токен туннеля — в `/root/cloudflared/tunnel.env` (0600, `TUNNEL_TOKEN=…`), в контейнер через `--env-file`;
  в репозиторий, чат и логи не попадает. Удаленно управляемый туннель (настройки маршрутов — в Zero Trust).
- **Маршрут туннеля:** `admin.proqnozai.com` → `http://127.0.0.1:3000`. HTTP внутри сервера допустим: снаружи
  соединение шифрует туннель. WebSocket (логи, терминал Dokploy) туннель пропускает.
- **После закрытия :3000** в DigitalOcean Cloud Firewall порт остаётся доступен на самом сервере (127.0.0.1) —
  туннель и SSH-туннель работают; Dokploy и его данные не меняются.
- **Access:** существующее приложение для `admin.proqnozai.com` — одна политика **Allow**, Include → *Emails* =
  e-mail администратора(ов); никаких *Everyone*, *Bypass*, *Service Auth*; вход — One-time PIN или Google;
  сессия 24 ч. Вторым слоем остаётся логин Dokploy — включить в нём **2FA** (настройки профиля Dokploy).

## 3. План (кто, что, ожидаемый результат)

0. **[владелец] Проверки до начала (UI, только чтение):**
   - Dokploy → приложение бота → Deployments: были ли выкладки по webhook; Settings → Git / GitHub App:
     адрес webhook. Если webhook указывает на `http://164.90.180.208:3000/…` — после закрытия :3000 автодеплой
     бота из GitHub **перестанет работать**. Рекомендация: Autodeploy бота **выключить** (бот заморожен до
     вывода из эксплуатации; Mini App тоже без автодеплоя) — отдельное решение.
   - Cloudflare → DNS → `admin.proqnozai.com`: **записать** тип и значение (для отката).
   - Zero Trust → Access → Applications → `admin.proqnozai.com`: политики — только Allow для нужных e-mail.
1. **[владелец] Zero Trust → Networks → Tunnels → Create tunnel** (Cloudflared), имя `proqnozai-admin`.
   Скопировать токен (показывается в команде установки) и передать его на сервер **не через чат** — например,
   сам владелец создаёт файл по SSH: `install -d -m 700 /root/cloudflared && (umask 077; printf 'TUNNEL_TOKEN=%s\n' '<токен>' > /root/cloudflared/tunnel.env)`.
2. **[сервер, по разрешению] Запуск коннектора** (не трогает бота, Dokploy, Traefik):
   ```sh
   docker run -d --name cloudflared --network host --restart unless-stopped \
     --env-file /root/cloudflared/tunnel.env cloudflare/cloudflared:<версия> tunnel --no-autoupdate run
   docker logs --tail 20 cloudflared   # "Registered tunnel connection" ×4, без ошибок (токен в логи не пишется)
   ```
   Zero Trust → Tunnels: статус **Healthy**.
3. **[владелец] Public hostname** туннеля: `admin.proqnozai.com` → Service `HTTP` `127.0.0.1:3000`.
   Cloudflare создаёт CNAME на `<id>.cfargotunnel.com`; существующая запись `admin` должна быть **заменена**
   (Cloudflare не даст создать дубль) — удалить старую запись и сразу сохранить hostname. Перерыв доступа к
   панели по этому адресу — секунды; доступ по `IP:3000` пока работает.
4. **Проверка авторизации (до firewall):**
   - `curl -sI https://admin.proqnozai.com` без входа → 302 на `…cloudflareaccess.com` (не страница Dokploy);
   - приватное окно браузера, разрешённый e-mail → код → страница входа Dokploy → вход (с 2FA) → открыть логи
     приложения (WebSocket идёт) → выйти;
   - другой e-mail → Access отказывает (код не приходит / «not authorized»);
   - Zero Trust → Logs → Access: видны оба события;
   - бот: логи без ошибок, MB без ошибок (ничего не менялось).
   *Стоп:* любой пункт не прошёл → firewall не трогаем, разбор.
5. **[владелец, отдельное разрешение] DigitalOcean Cloud Firewall** `proqnozai-prod-fw`, применить к дроплету
   `proqnozai-prod`:

   | Направление | Протокол | Порты | Источник / назначение |
   |---|---|---|---|
   | Inbound | TCP | 22 | All IPv4, All IPv6 (вход только по ключу; сузить до IP владельца — позже, если IP постоянный) |
   | Inbound | TCP | 80, 443 | All IPv4, All IPv6 (Traefik: дашборд бота и партнёрские ссылки, затем Mini App) |
   | Inbound | UDP | 443 | All IPv4, All IPv6 (HTTP/3 Traefik — как сейчас) |
   | Inbound | ICMP | — | All IPv4, All IPv6 |
   | Outbound | TCP, UDP | All ports | All IPv4, All IPv6 (Telegram, MB, GitHub, Docker Hub, NTP, Cloudflare Tunnel 7844) |
   | Outbound | ICMP | — | All IPv4, All IPv6 |

   Всё остальное входящее (в т. ч. **3000**, 2377, 7946, 4789, 8080) — закрыто. UFW не меняется.
6. **Проверка после firewall (сразу):**
   - с внешней машины `sh deploy/miniapp/origin-check.sh --ports 164.90.180.208 none 3000` → `RESULT: isolated`;
   - SSH работает (новая сессия, старую не закрывать до проверки);
   - `https://admin.proqnozai.com` → Access → Dokploy работает;
   - `https://…httpworker…sslip.io/` → 401 (как до), партнёрская ссылка `/r/<партнёр>` → редирект;
   - бот: `/start` отвечает, логи без ошибок и конфликтов; MB — без ошибок; `docker service ls` — всё 1/1.
   *Стоп/откат:* что-то из этого не работает → убрать дроплет из firewall (Droplet → Networking → Firewalls).

## 4. Восстановление доступа

| Ситуация | Что делать |
|---|---|
| Access не пускает (ошибка политики, потерян e-mail) | владелец входит в Cloudflare (свой логин и 2FA аккаунта) → Access → политика; панель Dokploy тем временем — по SSH-туннелю |
| Туннель упал / контейнер `cloudflared` остановлен | SSH-туннель: `ssh -L 3000:127.0.0.1:3000 root@164.90.180.208` → `http://localhost:3000`; затем `docker logs cloudflared`, `docker restart cloudflared` |
| Токен туннеля скомпрометирован | Zero Trust → Tunnels → Refresh/rotate token → новый `tunnel.env` → `docker restart cloudflared` |
| SSH недоступен (ключ, sshd) | DigitalOcean → Droplet → **Recovery Console** (вход через панель DO); firewall на консоль не влияет |
| Ошибка firewall | DigitalOcean → Networking → Firewalls → убрать дроплет / поправить правило (действует сразу) |
| Нужно убрать всё | `docker rm -f cloudflared`; удалить public hostname и туннель в Zero Trust; вернуть записанную DNS-запись `admin`; удалить firewall |

## 5. Что дальше для Mini App на этом сервере

- Mini App получит свой поддомен (например, `app.proqnozai.com`) через Traefik на 443 — firewall из п. 5 менять
  не нужно. Ограничить 443 только диапазонами Cloudflare на этом сервере **нельзя**: дашборд бота и партнёрские
  ссылки идут на `sslip.io` напрямую, не через Cloudflare. Защита origin Mini App — секрет `X-Edge-Auth`
  (приложение отвечает 403 без него) и rate limit Cloudflare.
- Сборка образа на этом сервере теперь допустима по памяти (свободно ~2,6 ГБ), но под наблюдением `free -m` и
  в тихое время; альтернатива — сборка вне сервера и registry.
