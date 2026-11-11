"""Bot -> Mini App user migration (deploy/migration/migrate_users.py): offline, temporary files only."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from miniapp.store import Store
from miniapp.telegram_auth import TelegramUser

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("migrate_users", ROOT / "deploy/migration/migrate_users.py")
mig = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mig)

# The bot's users table exactly as db.py creates it (plus the tz_offset migration).
BOT_USERS = """CREATE TABLE users (
    user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, lang TEXT DEFAULT 'az',
    is_registered INTEGER DEFAULT 0, is_blocked INTEGER DEFAULT 0, sports TEXT DEFAULT '',
    experience TEXT DEFAULT '', onboarding_done INTEGER DEFAULT 0, total_requests INTEGER DEFAULT 0,
    last_active TEXT DEFAULT '', joined_at TEXT DEFAULT (datetime('now')), tz_offset INTEGER DEFAULT 0)"""


def bot_db(path: Path, rows: list[tuple]) -> Path:
    c = sqlite3.connect(path)
    c.execute(BOT_USERS)
    c.executemany("INSERT INTO users (user_id, username, display_name, lang, is_registered, is_blocked, joined_at, last_active) "
                  "VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return path


ROWS = [
    (101, "anar", "Anar", "az", 1, 0, "2026-05-01 10:00:00", "2026-11-10 08:00:00"),
    (102, None, "Олег", "ru", 0, 0, "2026-06-01 10:00:00", ""),           # never chose a language
    (103, "kz_user", "Ali", "kz", 1, 0, "2026-07-01 10:00:00", "2026-11-01 08:00:00"),  # no Mini App locale
    (104, "spam", "Spam", "ru", 1, 1, "2026-07-02 10:00:00", ""),         # blocked by the admin
    (-5, "bad", "Bad", "ru", 1, 0, "2026-07-02 10:00:00", ""),            # not a Telegram user id
    (105, "ar_user", "Omar", "ar", 1, 0, "garbage", "2026-11-02 08:00:00"),
]


def run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["migrate_users.py", *argv])
    return mig.main()


def users(path: Path) -> dict[int, tuple]:
    c = sqlite3.connect(path)
    out = {r[0]: r[1:] for r in c.execute(
        "SELECT telegram_user_id, first_name, username, lang, lang_source, created_at, last_login_at FROM users")}
    c.close()
    return out


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    b, app = bot_db(tmp_path / "bot.db", ROWS), tmp_path / "miniapp.db"
    assert run(monkeypatch, str(b), str(app)) == 0
    assert users(app) == {}
    out = capsys.readouterr().out
    assert "eligible 4" in out and "blocked (not migrated) 1" in out and "bad id 1" in out and "DRY RUN" in out


def test_apply_maps_fields_and_skips_blocked_and_bad_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, app = bot_db(tmp_path / "bot.db", ROWS), tmp_path / "miniapp.db"
    assert run(monkeypatch, str(b), str(app), "--apply") == 0
    got = users(app)
    assert set(got) == {101, 102, 103, 105}
    assert got[101][:4] == ("Anar", "anar", "az", "user")
    assert got[101][4] == 1777629600 and got[101][5] == 1794297600  # joined_at, last_active as UTC epoch
    assert got[102][2:4] == ("ru", "telegram")      # not chosen: Telegram decides at the first login
    assert got[103][2:4] == ("ru", "telegram")      # kz has no Mini App locale
    assert got[105][2:4] == ("ru", "telegram")
    assert got[105][4] > 1794297600                 # unparsable joined_at -> migration time


def test_rerun_is_idempotent_and_keeps_existing_mini_app_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, app = bot_db(tmp_path / "bot.db", ROWS), tmp_path / "miniapp.db"
    store = Store(app)
    store.init_schema()
    store.upsert_user(TelegramUser(id=101, first_name="Fresh", last_name=None, username="fresh", language_code="en"), 1_800_000_000)
    assert run(monkeypatch, str(b), str(app), "--apply") == 0
    first = users(app)
    assert first[101][:4] == ("Fresh", "fresh", "en", "telegram")  # the Mini App row wins, untouched
    assert run(monkeypatch, str(b), str(app), "--apply") == 0
    assert users(app) == first
    assert len(first) == 4


def test_delta_brings_new_bot_users_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "miniapp.db"
    assert run(monkeypatch, str(bot_db(tmp_path / "b1.db", ROWS[:2])), str(app), "--apply") == 0
    assert run(monkeypatch, str(bot_db(tmp_path / "b2.db", ROWS)), str(app), "--apply") == 0
    assert set(users(app)) == {101, 102, 103, 105}


def test_first_login_after_migration_keeps_choice_and_join_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, app = bot_db(tmp_path / "bot.db", ROWS), tmp_path / "miniapp.db"
    assert run(monkeypatch, str(b), str(app), "--apply") == 0
    store = Store(app)
    u = store.upsert_user(TelegramUser(id=101, first_name="Anar", last_name="M", username="anar", language_code="ru"), 1_800_000_000)
    assert u.lang == "az" and u.lang_source == "user"   # chosen in the bot: Telegram does not override it
    u = store.upsert_user(TelegramUser(id=102, first_name="Oleg", last_name=None, username=None, language_code="en"), 1_800_000_000)
    assert u.lang == "en"
    assert users(app)[101][4] == 1777629600              # join date survives the login


def test_blocked_ids_file_and_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, app = bot_db(tmp_path / "bot.db", ROWS), tmp_path / "miniapp.db"
    out = tmp_path / "blocked.txt"
    assert run(monkeypatch, str(b), str(app), "--blocked-out", str(out)) == 0
    assert out.read_text() == "104\n" and (out.stat().st_mode & 0o077) == 0
    assert run(monkeypatch, str(b), str(b)) == 2                       # same file
    with pytest.raises(Exception):                                     # a bot.db is never a Mini App database
        run(monkeypatch, str(b), str(bot_db(tmp_path / "other-bot.db", ROWS)), "--apply")
