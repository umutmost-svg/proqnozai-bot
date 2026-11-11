"""Idempotent migration of bot users into the Mini App user table.

Runs INSIDE the Mini App container (it uses the app's own schema and language list), reading a COPY of
bot.db that was put on the Mini App volume:

    docker exec -i -u miniapp <ctr> python - /data/migration/bot-copy.db /data/miniapp.db          # dry run
    docker exec -i -u miniapp <ctr> python - /data/migration/bot-copy.db /data/miniapp.db --apply  # write

What moves (docs/miniapp/migration.md, mapping table):
  users.user_id      -> users.telegram_user_id   (identity; positive ids only)
  users.username     -> users.username
  users.display_name -> users.first_name         (Telegram refreshes names at the first login)
  users.lang         -> users.lang               (supported languages; lang_source='user' only when the
                                                  user chose it in the bot, i.e. is_registered=1)
  users.joined_at    -> users.created_at         (epoch seconds; unparsable -> migration time)
  users.last_active  -> users.last_login_at      (epoch; empty -> joined_at) — "last seen", kept for metrics
Never moves: chats, text forecasts, subscriptions, promo/partner data (archived with the bot backup).

Re-runnable without duplicates: INSERT ... ON CONFLICT DO NOTHING keyed by the Telegram id, in one
transaction. A user who already signed in to the Mini App keeps the Mini App row untouched (it is fresher).
Run it again on a later copy to bring over users who joined the bot since (the final delta at cutover).
Blocked bot users (is_blocked=1) are NOT migrated and are listed by COUNT only; their ids go to
--blocked-out (a file, 0600) for MINIAPP_BLOCKED_USER_IDS if the owner keeps them blocked.
Prints counts only, never names. Exit 0 = every eligible bot user is present in the Mini App afterwards.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from miniapp.store import DEFAULT_LANG, SUPPORTED_LANGS, Store


def epoch(text: str | None, fallback: int) -> int:
    if not text:
        return fallback
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text.strip().replace("Z", "").split("+")[0], fmt).replace(tzinfo=UTC).timestamp())
        except ValueError:
            continue
    return fallback


def plan(bot: sqlite3.Connection, now: int) -> tuple[list[tuple], list[int], dict[str, int]]:
    rows, blocked, stats = [], [], {"bot_users": 0, "bad_id": 0, "blocked": 0, "lang_user": 0, "lang_fallback": 0}
    for uid, username, display, lang, reg, is_blocked, joined, last in bot.execute(
            "SELECT user_id, username, display_name, lang, is_registered, is_blocked, joined_at, last_active FROM users"):
        stats["bot_users"] += 1
        if not isinstance(uid, int) or uid <= 0:
            stats["bad_id"] += 1
            continue
        if is_blocked:
            stats["blocked"] += 1
            blocked.append(uid)
            continue
        supported = lang in SUPPORTED_LANGS
        chosen = supported and bool(reg)
        stats["lang_user" if chosen else "lang_fallback"] += 1
        created = epoch(joined, now)
        rows.append((uid, (display or None) and str(display)[:64], (username or None) and str(username)[:64],
                     lang if supported else DEFAULT_LANG, "user" if chosen else "telegram",
                     created, epoch(last, created)))
    return rows, blocked, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bot_copy")
    ap.add_argument("miniapp_db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--blocked-out")
    a = ap.parse_args()
    bot_path, app_path = Path(a.bot_copy), Path(a.miniapp_db)
    if bot_path.resolve() == app_path.resolve():
        print("STOP: the bot copy and the Mini App database are the same file")
        return 2
    now = int(time.time())
    bot = sqlite3.connect(f"file:{bot_path}?mode=ro&immutable=1", uri=True)
    if bot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        print("STOP: the bot copy fails integrity_check")
        return 2
    rows, blocked, stats = plan(bot, now)
    bot.close()
    Store(app_path).init_schema()  # refuses a file that is not a Mini App database
    conn = sqlite3.connect(app_path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    before = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    present = {r[0] for r in conn.execute("SELECT telegram_user_id FROM users")}
    new = [r for r in rows if r[0] not in present]
    print(f"bot users {stats['bot_users']} | eligible {len(rows)} | blocked (not migrated) {stats['blocked']} | "
          f"bad id {stats['bad_id']} | language chosen in bot {stats['lang_user']} | language from Telegram {stats['lang_fallback']}")
    print(f"mini app users before {before} | already present {len(rows) - len(new)} | to insert {len(new)}")
    if a.blocked_out:
        old = os.umask(0o077)
        Path(a.blocked_out).write_text(",".join(str(u) for u in sorted(blocked)) + "\n")
        os.umask(old)
        print(f"blocked ids written to {a.blocked_out} ({len(blocked)})")
    if not a.apply:
        print("DRY RUN: nothing written (add --apply)")
        conn.close()
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT INTO users (telegram_user_id, first_name, last_name, username, language_code, lang, lang_source, "
            "created_at, last_login_at) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO NOTHING", rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    after = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    ids = {r[0] for r in conn.execute("SELECT telegram_user_id FROM users")}
    conn.close()
    missing = [r[0] for r in rows if r[0] not in ids]
    print(f"mini app users after {after} (+{after - before}) | eligible bot users missing {len(missing)}")
    ok = not missing and after - before == len(new)
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
