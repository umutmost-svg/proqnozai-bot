# healthchecks.io — инструкция владельцу (1 страница)

Бесплатный тариф (до 20 проверок). Что пингует и почему — `infra-hardening.md` §4, `offsite-backup.md`.
**URL пинга — секрет** (кто его знает, может «успокоить» тревогу): не в чат, не в репозиторий, не в аргументы команд.

## 1. Аккаунт (≈ 5 мин)
1. healthchecks.io → **Sign Up** → e-mail → ссылка из письма.
2. Account Settings → **Two-factor authentication** → включить (TOTP или ключ).
3. Проект переименовать в `ProqnozAI` (Settings проекта).
4. **Integrations**: e-mail уже есть; добавить **Telegram** → «Add Integration» → бот `@HealthchecksBot` →
   `/start` в личном чате (или в чате команды) → подтвердить. Оставить оба канала.

## 2. Шесть проверок
Add Check → Name, Schedule **Simple** → Period / Grace → Save. Всем — тег `prod`, оба канала уведомлений.

| Name | Period | Grace | Кто пингует | Файл с URL (0600) |
|---|---|---|---|---|
| `dokploy-bypass` | 1 ч | 1 ч | сервер, cron :17 | `/root/.hc-dokploy-bypass` |
| `host` | 5 мин | 15 мин | сервер, cron */5 | `/root/.hc-host` |
| `miniapp-health` | 5 мин | 15 мин | сервер, cron */5 | `/root/.hc-miniapp-health` |
| `bot-backup` | 12 ч | 12 ч | Mac, 05:40/17:40 | `~/.config/proqnozai-backup/hc-bot-backup` |
| `miniapp-backup` | 12 ч | 12 ч | Mac, 05:40/17:40 | `~/.config/proqnozai-backup/hc-miniapp-backup` |
| `offsite-backup` | 12 ч | 12 ч | Mac, 06:20/18:20 | `~/.config/proqnozai-backup/hc-offsite-backup` |

(`offsite-backup` — шестая, для третьей копии; без бакета её можно создать позже.)

## 3. Передача URL — без показа где-либо
У каждой проверки — кнопка копирования ping URL (`https://hc-ping.com/<uuid>`). Копировать **по одному** и сразу
вводить в Terminal.app на Mac (ввод не отображается и не попадает в историю):

**Mac-проверки** (три команды, имя файла меняется):
```sh
mkdir -p -m 700 ~/.config/proqnozai-backup
read -rs U; (umask 077; printf '%s' "$U" > ~/.config/proqnozai-backup/hc-bot-backup); unset U
read -rs U; (umask 077; printf '%s' "$U" > ~/.config/proqnozai-backup/hc-miniapp-backup); unset U
read -rs U; (umask 077; printf '%s' "$U" > ~/.config/proqnozai-backup/hc-offsite-backup); unset U
```
**Серверные проверки** — сразу на сервер по SSH (URL идёт через stdin, не в аргументах; сервер его не печатает).
Это запись на сервер: выполняет владелец сам или я — по разрешению:
```sh
read -rs U; printf '%s' "$U" | ssh root@164.90.180.208 'umask 077; cat > /root/.hc-dokploy-bypass'; unset U
read -rs U; printf '%s' "$U" | ssh root@164.90.180.208 'umask 077; cat > /root/.hc-host'; unset U
read -rs U; printf '%s' "$U" | ssh root@164.90.180.208 'umask 077; cat > /root/.hc-miniapp-health'; unset U
```
Если SSH сейчас не нужен — временно положить на Mac в `~/.config/proqnozai-backup/handoff/hc-host` и т. п. (той же
командой, что для Mac-проверок, каталог `handoff` создать с `-m 700`); при установке cron я перенесу их
`scp -p` (0600) и удалю с Mac.

Проверка без показа значения:
```sh
for f in ~/.config/proqnozai-backup/hc-*; do printf '%s %s %s\n' "$(stat -f %Lp "$f")" \
  "$(grep -c '^https://hc-ping.com/[0-9a-f-]\{36\}$' "$f")" "$f"; done        # ожидается: 600 1 <файл>
ssh root@164.90.180.208 'for f in /root/.hc-*; do printf "%s %s %s\n" $(stat -c %a "$f") \
  $(grep -c "^https://hc-ping.com/[0-9a-f-]\{36\}$" "$f") "$f"; done'
```

## 4. Что прислать в чат
Только: «healthchecks готов: 6 проверок, Telegram+e-mail, файлы на месте (600 1 ×N)». **Не** присылать URL,
UUID, скриншоты страниц проверок, API-ключи.

## 5. После установки (вместе)
- Все проверки станут **up** после первого пинга (серверные — в течение часа после cron, Mac — после первого
  `pull`/`sync`). До первого пинга статус `new` — это нормально.
- **Тест тревоги** для каждой проверки (URL через stdin): `printf 'url = "%s/fail"\n' "$(cat ФАЙЛ)" | curl -fsS -K -`
  → уведомление пришло в Telegram и на почту → `printf 'url = "%s"\n' "$(cat ФАЙЛ)" | curl -fsS -K -` → up.
- Утечка URL: удалить проверку и создать заново с теми же параметрами (новый UUID) → заново шаг 3 для этого файла.
