import sqlite3
import json
import logging
from datetime import datetime

from config import live_subs

logger = logging.getLogger(__name__)

# ─── DB ───────────────────────────────────────────────────────────────────────
import os as _os
_db_dir = _os.environ.get("BOT_DB_DIR", ".")
_os.makedirs(_db_dir, exist_ok=True)
DB = _os.path.join(_db_dir, "bot.db")

def con() -> sqlite3.Connection:
    """Open a DB connection with WAL mode, busy timeout, and foreign keys."""
    c = sqlite3.connect(DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")   # concurrent readers don't block writers
    c.execute("PRAGMA busy_timeout=5000")  # wait up to 5s instead of raising immediately
    c.execute("PRAGMA synchronous=NORMAL") # safe with WAL, faster than FULL
    return c

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
        -- migrate: add tz_offset if missing
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
        # Migration: add tz_offset column to existing DBs
        try:
            c.execute("ALTER TABLE users ADD COLUMN tz_offset INTEGER DEFAULT 0")
        except Exception:
            pass  # column already exists

_LANG_TZ = {"az": 4, "ru": 3, "tr": 3, "kz": 5, "uz": 5, "ar": 3, "en": 0}

def detect_lang(tg_lang: str | None) -> str:
    """Map Telegram language_code to bot language."""
    if not tg_lang: return "ru"
    code = tg_lang.lower()[:2]
    mapping = {
        "az": "az", "ru": "ru", "uk": "ru", "be": "ru",
        "tr": "tr", "kk": "kz", "uz": "uz",
        "ar": "ar", "fa": "ar",
        "en": "en",
    }
    return mapping.get(code, "ru")

def db_ensure(uid, uname, tg_lang=None):
    lang = detect_lang(tg_lang)
    tz = _LANG_TZ.get(lang, 0)
    with con() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id,username,lang,tz_offset) VALUES (?,?,?,?)",
            (uid, uname, lang, tz))

def db_get_tz(uid) -> int:
    with con() as c:
        row = c.execute("SELECT tz_offset FROM users WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else 0

_ALLOWED_FIELDS = {
    "lang", "display_name", "is_registered", "is_blocked",
    "sports", "experience", "onboarding_done", "tz_offset",
}

def db_set(uid, field, val):
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"db_set: disallowed field '{field}'")
    with con() as c: c.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (val, uid))

def db_get(uid) -> dict | None:
    try:
        with con() as c:
            cur = c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            cols = [d[0] for d in cur.description]; row = cur.fetchone()
        return dict(zip(cols, row)) if row else None
    except Exception as e:
        logger.error(f"db_get uid={uid}: {e}"); return None

def db_lang(uid) -> str:
    u = db_get(uid); return u["lang"] if u else "az"

def db_is_reg(uid) -> bool:
    u = db_get(uid); return bool(u and u["is_registered"])

def db_is_blocked(uid) -> bool:
    u = db_get(uid); return bool(u and u["is_blocked"])

def db_all_uids() -> list[int]:
    try:
        with con() as c:
            return [r[0] for r in c.execute("SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0").fetchall()]
    except Exception as e:
        logger.error(f"db_all_uids: {e}"); return []

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
        total   = c.execute("SELECT COUNT(*) FROM users WHERE is_registered=1").fetchone()[0]
        today   = c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=date('now') AND is_registered=1").fetchone()[0]
        blocked = c.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
        rqtotal = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        rqtoday = c.execute("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now')").fetchone()[0]
        langs   = c.execute("SELECT lang,COUNT(*) FROM users WHERE is_registered=1 GROUP BY lang").fetchall()
        ob_done = c.execute("SELECT COUNT(*) FROM users WHERE onboarding_done=1").fetchone()[0]
        live_ct = c.execute("SELECT COUNT(*) FROM live_subscriptions").fetchone()[0]
        top_req = c.execute("SELECT user_id,display_name,total_requests FROM users WHERE is_registered=1 ORDER BY total_requests DESC LIMIT 5").fetchall()
    return dict(total=total, today=today, blocked=blocked, rqtotal=rqtotal, rqtoday=rqtoday,
                langs=langs, ob_done=ob_done, live_ct=live_ct, top_req=top_req)

def db_search(q) -> list[dict]:
    with con() as c:
        cur = c.execute(
            "SELECT * FROM users WHERE username LIKE ? OR display_name LIKE ? OR CAST(user_id AS TEXT)=? LIMIT 5",
            (f"%{q}%", f"%{q}%", q))
        cols = [d[0] for d in cur.description]; rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]

def db_add_lsub(uid, mid, mname):
    with con() as c: c.execute("INSERT OR IGNORE INTO live_subscriptions (user_id,match_id,match_name) VALUES (?,?,?)", (uid, mid, mname))

def db_del_lsub(uid, mid):
    with con() as c: c.execute("DELETE FROM live_subscriptions WHERE user_id=? AND match_id=?", (uid, mid))

def db_user_lsubs(uid) -> list[dict]:
    with con() as c:
        rows = c.execute("SELECT match_id,match_name FROM live_subscriptions WHERE user_id=?", (uid,)).fetchall()
    return [dict(match_id=r[0], match_name=r[1]) for r in rows]

def db_restore_live_subs():
    with con() as c:
        rows = c.execute("SELECT user_id, match_id FROM live_subscriptions").fetchall()
    for uid, mid in rows:
        live_subs[mid].add(uid)
    if rows:
        logger.info(f"Restored {len(rows)} live subscriptions from DB")

# ─── History ──────────────────────────────────────────────────────────────────
def db_save_history(uid, query, forecast, match_name=""):
    with con() as c:
        c.execute("INSERT INTO forecast_history (user_id,query,forecast,match_name) VALUES (?,?,?,?)",
                  (uid, query[:200], forecast[:2000], match_name))
        # Keep only last 10 per user
        c.execute("DELETE FROM forecast_history WHERE user_id=? AND id NOT IN "
                  "(SELECT id FROM forecast_history WHERE user_id=? ORDER BY id DESC LIMIT 10)",
                  (uid, uid))

def db_get_history(uid) -> list[dict]:
    with con() as c:
        rows = c.execute(
            "SELECT id,query,forecast,match_name,feedback,created_at FROM forecast_history "
            "WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    return [dict(id=r[0], query=r[1], forecast=r[2], match_name=r[3],
                 feedback=r[4], created_at=r[5]) for r in rows]

def db_set_feedback(history_id, feedback):
    with con() as c:
        c.execute("UPDATE forecast_history SET feedback=? WHERE id=?", (feedback, history_id))

def db_feedback_stats(uid) -> dict:
    with con() as c:
        total = c.execute("SELECT COUNT(*) FROM forecast_history WHERE user_id=? AND feedback IS NOT NULL", (uid,)).fetchone()[0]
        wins  = c.execute("SELECT COUNT(*) FROM forecast_history WHERE user_id=? AND feedback=1", (uid,)).fetchone()[0]
    return dict(total=total, wins=wins, pct=round(wins/total*100) if total > 0 else 0)


# ─── Conversation memory ──────────────────────────────────────────────────────
def db_get_conv(uid) -> list:
    """Get last 3 conversation turns for context."""
    with con() as c:
        row = c.execute("SELECT messages FROM conversation WHERE user_id=?", (uid,)).fetchone()
    if not row: return []
    try: return json.loads(row[0])[-6:]  # last 3 turns (6 messages)
    except Exception as e:
        logger.warning(f"db_get_conv parse error uid={uid}: {e}"); return []

def db_save_conv(uid, messages: list):
    """Save conversation history (keep last 6 messages)."""
    trimmed = messages[-6:]
    with con() as c:
        c.execute("INSERT OR REPLACE INTO conversation (user_id, messages, updated_at) VALUES (?,?,datetime('now'))",
                  (uid, json.dumps(trimmed, ensure_ascii=False)))

def db_clear_conv(uid):
    with con() as c: c.execute("DELETE FROM conversation WHERE user_id=?", (uid,))
