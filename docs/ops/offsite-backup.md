# Третья копия бэкапов (вне сервера и вне Mac) + учения восстановления — runbook

Подготовлено 2026-11-13 (ветка `feat/infra-hardening`). **Ничего не создано и не оплачено, на сервере ничего не
выполнено.** **[владелец]** — делает владелец сам; **[разрешение]** — только по отдельному разрешению.
Опирается на `infra-hardening.md` §3 (Mac забирает зашифрованные архивы с сервера) — этот документ добавляет
третье место и учения из него.

## 0. Требование и схема

Требование владельца: проверенная копия **вне production-сервера и вне Mac** — до удаления незашифрованного
`/app/data/bot-backup-20261113T080743Z.db` из тома бота.

```
сервер ──(ssh, forced command)──> Mac: pull_backups.py ── шифрует (AES-256-CBC, PBKDF2 300000, пароль в Keychain)
                                        │  ~/.proqnozai-backups/auto/{bot,miniapp}/*.tar.enc + manifest
                                        │  ~/.proqnozai-backups/bot-backup-*.db.enc + .db.manifest (ручные)
                                        └─ offsite_backup.py sync (launchd 06:20 и 18:20)
                                             ├─ rclone copyto --immutable → бакет (только шифротекст + манифест)
                                             ├─ проверка КОПИИ ИЗ БАКЕТА: скачать во временный 0700-каталог →
                                             │  SHA-256 шифротекста = манифест; удалённый манифест = локальный байт в байт →
                                             │  расшифровать → SHA-256 открытого текста / файлов = манифест →
                                             │  PRAGMA integrity_check, обязательные таблицы → каталог удаляется
                                             ├─ свежесть: новейшая копия каждого вида в бакете моложе 36 ч
                                             └─ healthchecks `offsite-backup`: пинг / `/fail` с причиной
```

Свойства:
- **Где бывает открытый текст:** только на Mac — во временных 0700-каталогах под `~/.proqnozai-backups/auto/.work`
  (удаляются сразу; после SIGKILL — ближайшим `pull`/`verify`/`sync`, старше 6 ч) и, **только во время учений**
  (§5), в помеченном томе Docker Desktop, который учения удаляют сами, а после убитого запуска — каждая плановая
  выгрузка (`restore-drill-local.sh sweep`, с `/fail`, пока не удалён). В бакет и на сервер — никогда.
- **Каждый `sync` заново скачивает и проверяет новейшую копию каждого вида** в бакете (а не верит локальной
  записи): порча или подмена хранимой копии даёт `/fail` в ближайший запуск. Архив без манифеста дольше 1 ч
  (прерванный `pull`) — тоже `/fail`.
- **Шифруем мы, до отправки.** Провайдер видит только шифротекст; пароли — в Keychain Mac и (обязательно) в
  менеджере паролей владельца. Без копии паролей вне Mac третья копия бесполезна при потере Mac.
- **Скрипт ничего не удаляет и не перезаписывает в бакете** (`--immutable`; испорченная удалённая копия даёт
  `/fail`, а не тихую замену). Ключ хранилища **без права удаления**: украденный Mac не сотрёт третью копию.
  Срок хранения — правило жизненного цикла бакета.
- Сервер к бакету не ходит и ключей не держит (как и в §3).

Объём: бот ~5 МБ × 2/сутки, Mini App — сотни КБ; при 90 днях хранения ≈ 1 ГБ.

Файлы: `deploy/ops/offsite_backup.py` (stdlib + rclone; переиспользует проверки `pull_backups.py`),
`deploy/ops/restore-drill-offsite.sh`, `deploy/ops/com.proqnozai.backup-offsite.plist`; офлайн-тесты
`deploy/ops/tests/test_offsite_backup.py` (16 тестов: фальшивый rclone, настоящий openssl; порча копии при загрузке и
после проверки, подмена и потеря манифеста, неверный/битый манифест, недоступный бакет, устаревшие копии, архив
без манифеста, учения со скачиванием).

## 1. Варианты третьего места

| | **Backblaze B2** (рекомендую) | Cloudflare R2 | Hetzner Storage Box BX11 | Google Drive (личный) |
|---|---|---|---|---|
| Цена при ~1–2 ГБ | **$0** (первые 10 ГБ бесплатно; далее $6/ТБ·мес ≈ $0,006/ГБ) | $0 (10 ГБ·мес бесплатно; далее $0,015/ГБ); нужна карта для активации R2 | ≈ €3,2–3,9/мес за 1 ТБ (фикс.) | $0 (15 ГБ общих с почтой) |
| Скачивание (восстановление) | бесплатно до 3× объёма в месяц | бесплатно | бесплатно | бесплатно (лимиты API) |
| Независимость от уже используемых | отдельный вендор (не DO, не Cloudflare, не Apple) | **тот же аккаунт Cloudflare**, что туннель/DNS/Access — один взлом/блокировка = всё сразу | отдельный вендор | аккаунт Google владельца (почта и пр.) |
| Защита от удаления с Mac | ключ без `deleteFiles` + Object Lock (по желанию) | токены R2 «Object Read & Write» умеют удалять; есть bucket lock rules | субаккаунт + снимки; SFTP-доступ обычно с удалением | нет: OAuth-токен rclone может удалить всё |
| Настройка | ~15 мин: аккаунт, бакет, ключ (b2 CLI), `rclone config` | ~15 мин + карта | ~20 мин, иногда проверка личности; SFTP/ключ | ~10 мин, OAuth в браузере |
| Регион | EU (Амстердам) при создании аккаунта или US | авто/EU jurisdiction | DE/FI | — |

Шифрование at rest нашими средствами одинаково для всех (архивы уже зашифрованы до rclone); провайдер добавляет
своё (SSE-B2 / R2 / диск) поверх.

**Рекомендация — Backblaze B2, регион EU:** $0/мес на нашем объёме (при росте до 50 ГБ — около $0,24/мес),
отдельный от DigitalOcean и Cloudflare домен отказов, ключ можно выпустить без права удаления, rclone
поддерживает нативно, восстановление с любой машины. Цены — по прайсу на 2026 г.; сверить на странице
тарифов при регистрации. **[разрешение владельца]** нужно только при выходе за бесплатные 10 ГБ (не ожидается).

## 2. Настройка B2 [владелец]

Всё — в своём Terminal.app на Mac; ключи и пароли **не** присылать в чат.

1. Сгенерировать в менеджере паролей два пароля (≥ 40 случайных символов) и сохранить там:
   «ProqnozAI rclone config» и (если ещё нет) «ProqnozAI auto-backup» (`infra-hardening.md` §3 п. 1). Там же
   должен лежать пароль ручных копий (Keychain `proqnozai-bot-backup`) — проверить, что он есть вне Mac.
2. backblaze.com → Sign Up (B2 Cloud Storage), **регион EU Central**; включить 2FA (My Settings → Two-Factor).
3. Buckets → Create a Bucket: имя `proqnozai-backups-<6 случайных символов>`, **Private**, Default Encryption
   **Enable** (SSE-B2), Object Lock — **Enable** (по желанию: тогда Bucket Settings → Default retention
   **Governance, 30 дней** — даже мастер-ключ без особого права не удалит копию раньше).
4. Lifecycle Settings бакета → «Use custom lifecycle rules»: префикс `auto/` — скрывать через **90** дней после
   загрузки, удалять скрытые через **1** день. Для `manual/` правила нет (ручные копии хранятся, пока владелец
   не решит иначе).
5. Ключ приложения **без права удаления** (веб-интерфейс такого не предлагает — через b2 CLI):
   ```sh
   brew install b2-tools rclone
   b2 account authorize            # спросит Master Application Key ID и Key (Account → Application Keys)
   b2 key create --bucket proqnozai-backups-XXXXXX proqnozai-mac listBuckets,listFiles,readFiles,writeFiles
   # печатает keyID и applicationKey — один раз; сразу ввести в rclone (шаг 6), нигде не сохранять
   b2 account clear                # мастер-ключ больше не нужен на Mac
   ```
6. rclone (конфиг зашифрован паролем, пароль — в Keychain):
   ```sh
   security add-generic-password -s proqnozai-offsite-rclone -a proqnozai -T /usr/bin/security -w   # спросит дважды
   rclone config
   ```
   Интерактивно (ключ вводится в rclone, не в аргументы команд): `n` → имя `proqnozai-offsite` → тип `b2`
   → account = keyID, key = applicationKey → `hard_delete` = false → Enter до конца; затем там же
   `s) Set configuration password` (или `rclone config encryption set` в rclone ≥ 1.65) — вставить пароль
   «ProqnozAI rclone config». Проверка (значения не печатаются):
   ```sh
   export RCLONE_CONFIG_PASS="$(security find-generic-password -s proqnozai-offsite-rclone -a proqnozai -w)"
   rclone lsd proqnozai-offsite:proqnozai-backups-XXXXXX && echo bucket-ok; unset RCLONE_CONFIG_PASS
   grep -c 'RCLONE_ENCRYPT_V0' ~/.config/rclone/rclone.conf    # 1 = конфиг зашифрован
   ```
7. Конфиг скрипта и проверка прав ключа (удаление должно быть **запрещено**):
   ```sh
   printf 'REMOTE=proqnozai-offsite:proqnozai-backups-XXXXXX\n' > ~/.config/proqnozai-backup/offsite.env
   chmod 600 ~/.config/proqnozai-backup/offsite.env
   export RCLONE_CONFIG_PASS="$(security find-generic-password -s proqnozai-offsite-rclone -a proqnozai -w)"
   P=$(mktemp) && head -c 64 /dev/urandom > "$P"      # случайные байты: ни одного осмысленного байта в бакет
   rclone copyto "$P" proqnozai-offsite:proqnozai-backups-XXXXXX/probe.bin
   rclone deletefile proqnozai-offsite:proqnozai-backups-XXXXXX/probe.bin; echo "exit=$? (ожидается ошибка 401/unauthorized)"
   unset RCLONE_CONFIG_PASS; rm -f "$P"
   ```
   (`probe.bin` остаётся в бакете — безвредно, удалить в веб-интерфейсе мастер-доступом.)

Что прислать в чат: только «B2 готов, bucket-ok, удаление запрещено» и **имя** бакета (не ключи).

## 3. Установка на Mac (после `infra-hardening.md` §3)

```sh
install -m 600 deploy/ops/offsite_backup.py ~/proqnozai-ops/
install -m 700 deploy/ops/restore-drill-offsite.sh ~/proqnozai-ops/
# healthchecks: проверка offsite-backup (healthchecks-owner.md) → ~/.config/proqnozai-backup/hc-offsite-backup
/opt/homebrew/bin/python3 ~/proqnozai-ops/offsite_backup.py sync          # "offsite OK: N uploaded and verified; …"
/opt/homebrew/bin/python3 ~/proqnozai-ops/offsite_backup.py list
sed "s/OWNER/$(id -un)/g" deploy/ops/com.proqnozai.backup-offsite.plist > ~/Library/LaunchAgents/com.proqnozai.backup-offsite.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.proqnozai.backup-offsite.plist
```
Тест тревоги: `REMOTE=proqnozai-offsite:no-such-bucket-xyz python3 ~/proqnozai-ops/offsite_backup.py sync`
→ healthchecks прислал «offsite backup failed: rclone lsf failed…». Откат: `launchctl bootout
gui/$(id -u)/com.proqnozai.backup-offsite` (бакет остаётся; копии в нём не трогаются).

`sync` загружает всё, что есть локально (автоматические за срок хранения Mac + ручные `bot-backup-*.db.enc`) и
ещё не проверено в бакете; учёт — `~/.proqnozai-backups/auto/.offsite-verified.json`. С выгрузкой `pull` не
пересекается: перед поиском архивов `sync` дожидается идущих `pull` (их замков, до 20 мин; дольше — `/fail`),
а каждый архив загружает под тем же замком, что и выгрузка/ретенция этого вида.

## 4. Гейт удаления `/app/data/bot-backup-20261113T080743Z.db`

Порядок строгий; любой FAIL — стоп, файл на сервере не трогать.

1. **[владелец/я на Mac]** Третья копия и её проверка **из бакета**:
   ```sh
   python3 ~/proqnozai-ops/offsite_backup.py push ~/.proqnozai-backups/bot-backup-20261113T080743Z.db.enc
   # PASS: proqnozai-offsite:…/manual/bot-backup-20261113T080743Z.db.enc stored and verified from the remote copy (integrity=ok rows=…)
   python3 ~/proqnozai-ops/offsite_backup.py verify manual/bot-backup-20261113T080743Z.db.enc     # повторно: PASS
   ```
   Проверено этим: шифротекст в бакете = манифест, расшифровывается паролем из Keychain `proqnozai-bot-backup`,
   SHA-256 открытого текста = `plaintext_sha256` манифеста, `integrity_check = ok`, таблица `users` есть.
2. **[владелец]** Пароль `proqnozai-bot-backup` есть в менеджере паролей (вне Mac) — без него копия в бакете
   нерасшифруема при потере Mac.
3. **[разрешение — SSH, только чтение]** Файл на сервере — тот же, что зашифрован:
   ```sh
   sed -n 2p ~/.proqnozai-backups/bot-backup-20261113T080743Z.db.manifest      # plaintext_sha256 <hash>
   ssh root@164.90.180.208 'c=$(docker ps -q -f name=proqnozai-proqnozaibot-uejbpm); [ $(echo "$c" | wc -w) = 1 ] &&
     docker exec "$c" sha256sum /app/data/bot-backup-20261113T080743Z.db'
   ```
   Хеши обязаны совпасть.
4. **[разрешение — запись на сервер]** Удаление (только этого файла; список — сначала `ls`):
   ```sh
   ssh root@164.90.180.208 'c=$(docker ps -q -f name=proqnozai-proqnozaibot-uejbpm); docker exec "$c" ls -l /app/data;
     docker exec "$c" rm -f /app/data/bot-backup-20261113T080743Z.db; docker exec "$c" ls -l /app/data'
   ```
   `bot.db` и прочие файлы не трогаются. Остальные старые копии в томе — по `infra-hardening.md` §3 (у
   `bot-backup-20261112T150519Z.db` зашифрованного экземпляра на Mac нет — сначала `backup-export.sh` +
   `pull`, затем удалять; тот же гейт с шагом 3 для каждого файла, у которого есть Mac-копия).

## 5. Учения восстановления (scratch, никогда не прод)

Цель — доказать, что из каждого места можно восстановить рабочие базы. Ни один шаг не обращается к серверу.

**5a. Ежемесячно, на Mac с Docker — из бакета** (локальные архивы и сервер не используются):
```sh
sh ~/proqnozai-ops/restore-drill-offsite.sh bot
MINIAPP_IMAGE=proqnozai-miniapp:<коммит в проде> sh ~/proqnozai-ops/restore-drill-offsite.sh miniapp
# последняя строка: PASS: off-site auto/<kind>/<архив> restored and checked (local archives and server not used)
```
Скрипт скачивает новейший архив вида (шифротекст + манифест) во временный каталог под `.work`, запускает
`restore-drill-local.sh` на нём (расшифровка и проверки → **новый** том Docker с меткой → `integrity_check`,
таблицы и число строк **из тома** против манифеста → для Mini App контейнер приложения `--network none` без
секретов должен стать healthy) и удаляет всё (каталог, том, контейнеры), в том числе при сбое.
Результат записать в журнал учений (дата, архив, PASS/FAIL, время восстановления).

**5b. Раз в квартал — «Mac потерян»: чистая машина, только бакет + менеджер паролей.** Scratch-машина —
временная Linux-VM (например, Droplet $6 на час — **[разрешение]**, или локальная VM/контейнер), **не** prod:
```sh
docker run --rm -it --network bridge debian:12 bash      # или свежая VM
apt-get update && apt-get install -y rclone openssl sqlite3 ca-certificates
rclone config            # remote b2 с ключом ТОЛЬКО на чтение (отдельный: b2 key create … listBuckets,listFiles,readFiles)
rclone lsf -R offsite:proqnozai-backups-XXXXXX | sort | tail
rclone copy offsite:proqnozai-backups-XXXXXX/auto/bot/ ./r --include 'bot-<UTC>.*'
cd r && sha256sum bot-<UTC>.tar.enc && grep ciphertext_sha256 bot-<UTC>.manifest.json      # совпадают
openssl enc -d -aes-256-cbc -pbkdf2 -iter 300000 -md sha256 -in bot-<UTC>.tar.enc -out b.tar   # пароль вводится вручную
sha256sum b.tar && grep plaintext_tar_sha256 bot-<UTC>.manifest.json                          # совпадают
tar -xf b.tar && sqlite3 bot-backup-*.db 'PRAGMA integrity_check; SELECT count(*) FROM users;'  # ok; = манифест
```
Для Mini App то же с `auto/miniapp/`; базы — `<UTC>/miniapp.db`, `<UTC>/miniapp.forecasts.db`, затем
(по желанию) `docker run` образа Mini App на томе с ними, как в `deploy/miniapp/restore-drill.sh`. Для ручной
копии — `openssl enc -d … -in bot-backup-<UTC>.db.enc -out bot.db` и сверка с `plaintext_sha256`. После — VM/контейнер
уничтожить (данные пользователей), read-only ключ можно отозвать. OpenSSL 1.1.1+/3.x (у LibreSSL на macOS
те же параметры совместимы).

**5c. Настоящее восстановление в production** — решение владельца, по `production.md` §7 и
`droplet-resize.md` «Откат → Данные бота»; источник — ближайший по времени архив (Mac → бакет), файл переносится
на сервер 0600 и удаляется с промежуточных мест.

## 6. Риски и границы

- Пароли архивов — единственное, что делает третью копию полезной: при их потере вместе с Mac восстановление
  невозможно. Копия в менеджере паролей — обязательное условие, не опция.
- Третья копия зависит от Mac как отправителя: Mac выключен > 36 ч → `offsite-backup` уходит в `/fail` (свежесть).
- `aes-256-cbc` без MAC: целостность — SHA-256 из манифеста; манифест хранится рядом в бакете и сверяется с
  локальным при каждой загрузке. Подделка обоих в бакете требует ключа записи (он без удаления, но с записью —
  `--immutable` и Object Lock не дают заменить уже лежащее).
- B2 Object Lock + ключ без `deleteFiles` — защита от удаления с Mac, но не от владельца мастер-ключа B2
  (2FA обязательна).
