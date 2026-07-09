"""SQLite storage for the Mini App service.

Independent from the existing bot's bot.db — same Telegram user_id space,
but a separate database file and schema. See the migration plan for why.
"""
import os
import sqlite3
from contextlib import contextmanager

_DB_DIR = os.environ.get("MINIAPP_DB_DIR", ".")
os.makedirs(_DB_DIR, exist_ok=True)
DB_PATH = os.path.join(_DB_DIR, "miniapp.db")


@contextmanager
def con():
    """Connection context manager: commit on success, rollback on error, always close."""
    c = sqlite3.connect(DB_PATH, timeout=10)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA synchronous=NORMAL")
        with c:
            yield c
    finally:
        c.close()


def init_db():
    with con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            display_name TEXT,
            lang         TEXT DEFAULT 'ru',
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS forecast_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            match_summary  TEXT,
            forecast_text  TEXT,
            created_at     TEXT DEFAULT (datetime('now'))
        );
        """)


_SUPPORTED_LANGS = ("ru", "en")


def _detect_lang(tg_lang: str | None) -> str:
    if tg_lang and tg_lang.lower()[:2] == "en":
        return "en"
    return "ru"


def ensure_user(user_id: int, username: str | None, display_name: str | None,
                 tg_lang: str | None) -> bool:
    """Insert the user if they don't exist yet. Returns True iff this call created the row."""
    with con() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            return False
        c.execute(
            "INSERT INTO users (user_id, username, display_name, lang) VALUES (?,?,?,?)",
            (user_id, username, display_name, _detect_lang(tg_lang)),
        )
        return True


def get_user(user_id: int) -> dict | None:
    with con() as c:
        cur = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def set_lang(user_id: int, lang: str):
    if lang not in _SUPPORTED_LANGS:
        raise ValueError(f"unsupported lang: {lang!r}")
    with con() as c:
        c.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))


def save_forecast(user_id: int, match_summary: str, forecast_text: str) -> int:
    with con() as c:
        cur = c.execute(
            "INSERT INTO forecast_history (user_id, match_summary, forecast_text) VALUES (?,?,?)",
            (user_id, match_summary, forecast_text),
        )
        return cur.lastrowid


def get_history(user_id: int, limit: int = 20) -> list[dict]:
    with con() as c:
        cur = c.execute(
            "SELECT id, match_summary, forecast_text, created_at FROM forecast_history "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]
