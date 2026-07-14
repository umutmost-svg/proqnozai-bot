import os
import sqlite3
import json
import logging
from contextlib import contextmanager
from datetime import datetime

from config import live_subs

logger = logging.getLogger(__name__)

# ─── DB ───────────────────────────────────────────────────────────────────────
_db_dir = os.environ.get("BOT_DB_DIR", ".")
os.makedirs(_db_dir, exist_ok=True)
DB = os.path.join(_db_dir, "bot.db")


@contextmanager
def con():
    """Connection context manager: commit on success, rollback on error,
    always close (sqlite3's own __exit__ commits but never closes)."""
    c = sqlite3.connect(DB, timeout=10)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA synchronous=NORMAL")
        with c:
            yield c
    finally:
        c.close()


def _one(sql, params=()):
    with con() as c:
        row = c.execute(sql, params).fetchone()
    return row[0] if row else None


def _all(sql, params=()):
    with con() as c:
        return c.execute(sql, params).fetchall()


def _run(sql, params=()):
    with con() as c:
        c.execute(sql, params)


def db_init():
    with con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            username        TEXT,
            display_name    TEXT,
            lang            TEXT DEFAULT 'az',
            is_registered   INTEGER DEFAULT 0,
            is_blocked      INTEGER DEFAULT 0,
            sports          TEXT DEFAULT '',
            experience      TEXT DEFAULT '',
            onboarding_done INTEGER DEFAULT 0,
            total_requests  INTEGER DEFAULT 0,
            last_active     TEXT DEFAULT '',
            joined_at       TEXT DEFAULT (datetime('now')),
            tz_offset       INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, msg_type TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS live_subscriptions (
            user_id INTEGER, match_id TEXT, match_name TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, match_id)
        );
        CREATE TABLE IF NOT EXISTS forecast_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, query TEXT, forecast TEXT,
            match_name TEXT DEFAULT '',
            feedback INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS conversation (
            user_id INTEGER PRIMARY KEY,
            messages TEXT DEFAULT '[]',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds_alerts (
            user_id INTEGER, match_id TEXT, market TEXT,
            last_odd REAL, created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, match_id, market)
        );
        CREATE TABLE IF NOT EXISTS request_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        for stmt in (
            "ALTER TABLE users ADD COLUMN tz_offset INTEGER DEFAULT 0",
            # Link odds alerts back to the live-subscription fixture so they can
            # be cleaned up on unwatch / full time, and keep a human-readable name.
            "ALTER TABLE odds_alerts ADD COLUMN fixture_id TEXT DEFAULT ''",
            "ALTER TABLE odds_alerts ADD COLUMN match_name TEXT DEFAULT ''",
        ):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


def db_flag_done(key: str) -> bool:
    return bool(_one("SELECT 1 FROM _migrations WHERE key=?", (key,)))


def db_flag_mark(key: str):
    _run("INSERT OR IGNORE INTO _migrations (key) VALUES (?)", (key,))


_LANG_TZ = {"az": 4, "ru": 3, "tr": 3, "kz": 5, "uz": 5, "ar": 3, "en": 0}

# Canonical language set (matches translations.T). ``ru`` is the safe default the
# whole app falls back to; ``en`` is the secondary fallback inside tr(). These are
# the ONLY values that may be stored in users.lang.
DEFAULT_LANG = "ru"
SUPPORTED_LANGS = frozenset({"az", "ru", "en", "tr", "kz", "uz", "ar"})


def normalize_lang(lang) -> str:
    """Coerce any language value to a supported code. Unknown / legacy / invalid
    values (including None and junk stored by old clients) normalize to
    DEFAULT_LANG instead of raising or leaking through the UI."""
    if isinstance(lang, str):
        low = lang.strip().lower()
        if low in SUPPORTED_LANGS:
            return low
    return DEFAULT_LANG


def detect_lang(tg_lang: str | None) -> str:
    if not tg_lang:
        return DEFAULT_LANG
    mapping = {
        "az": "az", "ru": "ru", "uk": "ru", "be": "ru",
        "tr": "tr", "kk": "kz", "uz": "uz",
        "ar": "ar", "fa": "ar", "en": "en",
    }
    return mapping.get(tg_lang.lower()[:2], DEFAULT_LANG)


def db_ensure(uid, uname, tg_lang=None):
    lang = detect_lang(tg_lang)
    tz = _LANG_TZ.get(lang, 0)
    _run("INSERT OR IGNORE INTO users (user_id,username,lang,tz_offset) VALUES (?,?,?,?)",
         (uid, uname, lang, tz))


def db_get_tz(uid) -> int:
    return _one("SELECT tz_offset FROM users WHERE user_id=?", (uid,)) or 0


_ALLOWED_FIELDS = {
    "lang", "display_name", "is_registered", "is_blocked",
    "sports", "experience", "onboarding_done", "tz_offset",
}


def db_set(uid, field, val):
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"db_set: disallowed field '{field}'")
    _run(f"UPDATE users SET {field}=? WHERE user_id=?", (val, uid))


def db_get(uid) -> dict | None:
    try:
        with con() as c:
            cur = c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
        return dict(zip(cols, row)) if row else None
    except Exception as e:
        logger.error(f"db_get uid={uid}: {e}")
        return None


def db_lang(uid) -> str:
    """The user's UI language, always normalized to a supported code so a legacy
    or corrupted stored value can never break rendering."""
    return normalize_lang(_one("SELECT lang FROM users WHERE user_id=?", (uid,)))


def db_is_reg(uid) -> bool:
    return bool(_one("SELECT is_registered FROM users WHERE user_id=?", (uid,)))


def db_is_blocked(uid) -> bool:
    return bool(_one("SELECT is_blocked FROM users WHERE user_id=?", (uid,)))


def db_all_uids() -> list[int]:
    try:
        return [r[0] for r in _all(
            "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0")]
    except Exception as e:
        logger.error(f"db_all_uids: {e}")
        return []


def db_log_req(uid, mtype):
    try:
        with con() as c:
            c.execute("INSERT INTO requests (user_id,msg_type) VALUES (?,?)", (uid, mtype))
            c.execute("UPDATE users SET total_requests=total_requests+1, last_active=? WHERE user_id=?",
                      (datetime.now().isoformat(), uid))
    except Exception as e:
        logger.error(f"db_log_req uid={uid}: {e}")


def db_stats() -> dict:
    with con() as c:
        def one(sql): return c.execute(sql).fetchone()[0]
        total   = one("SELECT COUNT(*) FROM users WHERE is_registered=1")
        today   = one("SELECT COUNT(*) FROM users WHERE date(joined_at)=date('now') AND is_registered=1")
        blocked = one("SELECT COUNT(*) FROM users WHERE is_blocked=1")
        rqtotal = one("SELECT COUNT(*) FROM requests")
        rqtoday = one("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now')")
        langs   = c.execute("SELECT lang,COUNT(*) FROM users WHERE is_registered=1 GROUP BY lang").fetchall()
        ob_done = one("SELECT COUNT(*) FROM users WHERE onboarding_done=1")
        live_ct = one("SELECT COUNT(*) FROM live_subscriptions")
        top_req = c.execute(
            "SELECT user_id,display_name,total_requests FROM users "
            "WHERE is_registered=1 ORDER BY total_requests DESC LIMIT 5").fetchall()
    return dict(total=total, today=today, blocked=blocked, rqtotal=rqtotal, rqtoday=rqtoday,
                langs=langs, ob_done=ob_done, live_ct=live_ct, top_req=top_req)


def like_escape(q: str) -> str:
    """Escape LIKE wildcards so user input matches literally (use ESCAPE '\\')."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def db_search(q) -> list[dict]:
    with con() as c:
        cur = c.execute(
            "SELECT * FROM users WHERE username LIKE ? ESCAPE '\\' "
            "OR display_name LIKE ? ESCAPE '\\' OR CAST(user_id AS TEXT)=? LIMIT 5",
            (f"%{like_escape(q)}%", f"%{like_escape(q)}%", q))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


def db_add_lsub(uid, mid, mname):
    _run("INSERT OR IGNORE INTO live_subscriptions (user_id,match_id,match_name) VALUES (?,?,?)",
         (uid, mid, mname))


def db_del_lsub(uid, mid):
    _run("DELETE FROM live_subscriptions WHERE user_id=? AND match_id=?", (uid, mid))


def db_user_lsubs(uid) -> list[dict]:
    rows = _all("SELECT match_id,match_name FROM live_subscriptions WHERE user_id=?", (uid,))
    return [dict(match_id=r[0], match_name=r[1]) for r in rows]


def db_restore_live_subs():
    rows = _all("SELECT user_id, match_id FROM live_subscriptions")
    for uid, mid in rows:
        live_subs[mid].add(uid)
    if rows:
        logger.info(f"Restored {len(rows)} live subscriptions from DB")


# ─── History ──────────────────────────────────────────────────────────────────
def db_save_history(uid, query, forecast, match_name=""):
    with con() as c:
        c.execute("INSERT INTO forecast_history (user_id,query,forecast,match_name) VALUES (?,?,?,?)",
                  (uid, query[:200], forecast[:2000], match_name))
        c.execute(
            "DELETE FROM forecast_history WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM forecast_history WHERE user_id=? ORDER BY id DESC LIMIT 10)",
            (uid, uid))


def db_get_history(uid) -> list[dict]:
    rows = _all(
        "SELECT id,query,forecast,match_name,feedback,created_at FROM forecast_history "
        "WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,))
    return [dict(id=r[0], query=r[1], forecast=r[2], match_name=r[3],
                 feedback=r[4], created_at=r[5]) for r in rows]


def db_set_feedback(uid, history_id, feedback):
    # Ownership check: callback data is client-forgeable, so never update
    # another user's history row.
    _run("UPDATE forecast_history SET feedback=? WHERE id=? AND user_id=?",
         (feedback, history_id, uid))


def db_feedback_stats(uid) -> dict:
    total = _one("SELECT COUNT(*) FROM forecast_history WHERE user_id=? AND feedback IS NOT NULL", (uid,)) or 0
    wins  = _one("SELECT COUNT(*) FROM forecast_history WHERE user_id=? AND feedback=1", (uid,)) or 0
    return dict(total=total, wins=wins, pct=round(wins / total * 100) if total > 0 else 0)


# ─── Conversation memory ──────────────────────────────────────────────────────
def db_get_conv(uid) -> list:
    row = _one("SELECT messages FROM conversation WHERE user_id=?", (uid,))
    if not row:
        return []
    try:
        return json.loads(row)[-6:]
    except Exception as e:
        logger.warning(f"db_get_conv parse error uid={uid}: {e}")
        return []


def db_save_conv(uid, messages: list):
    trimmed = messages[-6:]
    _run("INSERT OR REPLACE INTO conversation (user_id, messages, updated_at) VALUES (?,?,datetime('now'))",
         (uid, json.dumps(trimmed, ensure_ascii=False)))


def db_clear_conv(uid):
    _run("DELETE FROM conversation WHERE user_id=?", (uid,))
