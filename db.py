import os
import sqlite3
import json
import logging
import time
from collections import defaultdict
from contextlib import contextmanager

from config import live_subs, demand_cache, winrate_cache
from priority_config import normalize_participant_tokens

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
            fixture_key TEXT DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS live_events_seen (
            match_id   TEXT,
            event_key  TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (match_id, event_key)
        );
        CREATE TABLE IF NOT EXISTS promo_campaign (
            code       TEXT,
            max_uses   INTEGER,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS promo_claims (
            user_id    INTEGER,
            code       TEXT,
            claimed_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, code)
        );
        """)
        for stmt in (
            "ALTER TABLE users ADD COLUMN tz_offset INTEGER DEFAULT 0",
            # Link odds alerts back to the live-subscription fixture so they can
            # be cleaned up on unwatch / full time, and keep a human-readable name.
            "ALTER TABLE odds_alerts ADD COLUMN fixture_id TEXT DEFAULT ''",
            "ALTER TABLE odds_alerts ADD COLUMN match_name TEXT DEFAULT ''",
            # Scopes conversation memory to one fixture, so an analysis of match
            # A can't leak into an independent forecast for match B.
            "ALTER TABLE conversation ADD COLUMN fixture_key TEXT DEFAULT ''",
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
    # uz/ar are temporarily disabled in the UI (see handlers.utils.lang_kb), so
    # those locales land on a language the picker can actually offer instead of
    # a language the user cannot switch away from by name.
    mapping = {
        "az": "az", "ru": "ru", "uk": "ru", "be": "ru",
        "tr": "tr", "kk": "kz", "uz": "ru",
        "ar": "en", "fa": "en", "en": "en",
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
            # datetime('now') (SQLite, UTC) — NOT Python's datetime.now() (local
            # process time) — so last_active stays in the same UTC clock as
            # every date('now')/datetime('now') comparison elsewhere (joined_at,
            # requests.created_at, the dashboard's activity-segment queries in
            # stats_server.py). Mixing local time here used to let activity
            # segments drift by up to a day depending on the server's timezone.
            c.execute("UPDATE users SET total_requests=total_requests+1, last_active=datetime('now') WHERE user_id=?",
                      (uid,))
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


def db_lsub_name(mid) -> str | None:
    """The human-readable name of a watched match, from any subscriber's row —
    it is the same for all of them, so the poller reads it once per match
    instead of once per subscriber."""
    return _one("SELECT match_name FROM live_subscriptions WHERE match_id=? "
                "AND match_name != '' LIMIT 1", (str(mid),))


def db_user_lsubs(uid) -> list[dict]:
    rows = _all("SELECT match_id,match_name FROM live_subscriptions WHERE user_id=?", (uid,))
    return [dict(match_id=r[0], match_name=r[1]) for r in rows]


# ─── Live event de-duplication ────────────────────────────────────────────────
# Which live events a match has already notified about. Persisted rather than
# kept in memory: the previous in-memory "how many events did we see last time"
# counter reset on every restart, so a restart mid-match re-sent every goal and
# card that had already gone out. Keys are content-derived (see
# handlers.live._event_key), so a reordered or shortened provider response can't
# create duplicates either.
LIVE_EVENTS_RETENTION_DAYS = 7


def db_filter_new_live_events(match_id: str, keys: list[str]) -> list[str]:
    """Record `keys` for `match_id` and return only the ones not seen before,
    in the order given. Insert-and-check in one transaction, so the "have we
    sent this?" decision and the record of having sent it can't diverge."""
    if not keys:
        return []
    fresh = []
    with con() as c:
        for key in keys:
            cur = c.execute(
                "INSERT OR IGNORE INTO live_events_seen (match_id, event_key) VALUES (?,?)",
                (str(match_id), key))
            if cur.rowcount:            # 0 ⇒ the key was already there
                fresh.append(key)
    return fresh


def db_clear_live_events(match_id: str) -> None:
    """Drop a finished match's event keys — nothing left to de-duplicate."""
    _run("DELETE FROM live_events_seen WHERE match_id=?", (str(match_id),))


def db_purge_stale_live_events() -> None:
    """Safety net for matches that never reached a final status (the normal
    cleanup path is db_clear_live_events on FT)."""
    _run("DELETE FROM live_events_seen WHERE created_at < datetime('now', ?)",
         (f"-{LIVE_EVENTS_RETENTION_DAYS} days",))


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


# ─── Track record: bot-wide winrate + per-user activity streak ─────────────────
WINRATE_CACHE_TTL = 3600     # 1h — winrate is shown often, changes slowly
WINRATE_MIN_SAMPLES = 30     # cold-start: never show a % off a handful of votes


def db_bot_winrate(days: int = 30) -> dict | None:
    """Community forecast accuracy over the trailing `days`, from user 👍/👎
    feedback. Returns {wins, total, pct} or None when there aren't enough rated
    forecasts yet (cold-start) — we never show a percentage off a tiny sample.
    Cached in-memory. NOTE: forecast_history keeps only each user's last ~10
    forecasts, so this is a RECENT community signal, not a lifetime record."""
    now = time.time()
    cached = winrate_cache.get(days)
    if cached and now - cached[0] < WINRATE_CACHE_TTL:
        return cached[1]
    cutoff = f"-{int(days)} days"
    total = _one("SELECT COUNT(*) FROM forecast_history WHERE feedback IS NOT NULL "
                 "AND created_at >= datetime('now', ?)", (cutoff,)) or 0
    wins = _one("SELECT COUNT(*) FROM forecast_history WHERE feedback=1 "
                "AND created_at >= datetime('now', ?)", (cutoff,)) or 0
    result = None if total < WINRATE_MIN_SAMPLES else {
        "wins": wins, "total": total, "pct": round(wins / total * 100)}
    winrate_cache[days] = (now, result)
    return result


# ─── Promo campaign (one shared code, capped total uses) ──────────────────────
def db_set_promo_campaign(code: str, max_uses: int) -> None:
    """Set THE active promo code and its total-use cap (e.g. one code for 500
    users). Replaces any previous campaign. Claims are tracked per code, so
    setting a NEW code starts a fresh count."""
    with con() as c:
        c.execute("DELETE FROM promo_campaign")
        c.execute("INSERT INTO promo_campaign (code, max_uses) VALUES (?,?)",
                  (code.strip(), int(max_uses)))


def db_get_promo_campaign() -> dict | None:
    with con() as c:
        row = c.execute("SELECT code, max_uses FROM promo_campaign LIMIT 1").fetchone()
    return dict(code=row[0], max_uses=row[1]) if row else None


def db_claim_promo(uid) -> str | None:
    """Give this user the active promo code. Idempotent: a user who already
    claimed it gets the SAME code back (not a second use). Returns None when no
    campaign is set OR the use cap is reached.

    BEGIN IMMEDIATE takes the write lock up front. SQLite's default deferred
    transaction starts read-only and only upgrades on the INSERT, so two callers
    could both read used == max_uses - 1 and both insert, issuing one code over
    the cap. Taking the lock before the count makes read and write one unit."""
    with con() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT code, max_uses FROM promo_campaign LIMIT 1").fetchone()
        if not row:
            return None
        code, max_uses = row
        if c.execute("SELECT 1 FROM promo_claims WHERE user_id=? AND code=?",
                     (uid, code)).fetchone():
            return code  # already claimed → same code
        used = c.execute("SELECT COUNT(*) FROM promo_claims WHERE code=?", (code,)).fetchone()[0]
        if used >= max_uses:
            return None  # cap reached
        c.execute("INSERT OR IGNORE INTO promo_claims (user_id, code) VALUES (?,?)", (uid, code))
        return code


def db_promo_stats() -> dict:
    camp = db_get_promo_campaign()
    if not camp:
        return dict(code=None, max_uses=0, claimed=0, available=0)
    claimed = _one("SELECT COUNT(*) FROM promo_claims WHERE code=?", (camp["code"],)) or 0
    return dict(code=camp["code"], max_uses=camp["max_uses"], claimed=claimed,
                available=max(0, camp["max_uses"] - claimed))


def db_user_streak(uid) -> int:
    """Consecutive days (ending today or yesterday, UTC) on which the user
    interacted, from the uncapped `requests` table. Yesterday still counts so a
    user who hasn't opened the bot *yet today* isn't shown a 0."""
    from datetime import datetime, timezone, timedelta
    rows = _all("SELECT DISTINCT date(created_at) FROM requests WHERE user_id=? "
                "ORDER BY 1 DESC LIMIT 400", (uid,))
    day_set = {r[0] for r in rows if r[0]}
    if not day_set:
        return 0
    today = datetime.now(timezone.utc).date()
    if today.isoformat() in day_set:
        cur = today
    elif (today - timedelta(days=1)).isoformat() in day_set:
        cur = today - timedelta(days=1)
    else:
        return 0  # last activity is older than yesterday → streak broken
    streak = 0
    while cur.isoformat() in day_set:
        streak += 1
        cur -= timedelta(days=1)
    return streak


# ─── Conversation memory ──────────────────────────────────────────────────────
# Memory is scoped to a fixture so an analysis of match A never becomes context
# for an independent forecast of match B. `fixture_key` identifies the match
# (see handlers.forecast._fixture_key); an EMPTY key means "this request names no
# fixture" — a plain follow-up question — and keeps whatever context is stored.
def db_get_conv(uid, fixture_key: str = "") -> list:
    with con() as c:
        row = c.execute("SELECT messages, fixture_key FROM conversation WHERE user_id=?",
                        (uid,)).fetchone()
    if not row:
        return []
    messages_json, stored_key = row
    # A named fixture that differs from the stored one starts a fresh context.
    # Rows written before fixture_key existed carry '' and are dropped once, on
    # the user's next fixture-bearing request.
    if fixture_key and (stored_key or "") != fixture_key:
        return []
    try:
        return json.loads(messages_json)[-6:]
    except Exception as e:
        logger.warning(f"db_get_conv parse error uid={uid}: {e}")
        return []


def db_save_conv(uid, messages: list, fixture_key: str = ""):
    trimmed = messages[-6:]
    with con() as c:
        if not fixture_key:
            # A follow-up doesn't re-label the context it is continuing.
            prev = c.execute("SELECT fixture_key FROM conversation WHERE user_id=?",
                             (uid,)).fetchone()
            fixture_key = (prev[0] if prev else "") or ""
        c.execute("INSERT OR REPLACE INTO conversation (user_id, messages, fixture_key, updated_at) "
                  "VALUES (?,?,?,datetime('now'))",
                  (uid, json.dumps(trimmed, ensure_ascii=False), fixture_key))


def db_clear_conv(uid):
    _run("DELETE FROM conversation WHERE user_id=?", (uid,))


# ─── Match Priority Engine: internal demand aggregation ───────────────────────
# A full two-table scan on every menu open doesn't scale with DAU, and the
# signal itself is coarse and log-scaled (priority_engine caps its
# contribution at 5 points) — a few minutes of staleness never changes the
# resulting demand_bonus in any way a user would notice. Cached in-memory like
# mostbet_cache, keyed by `days` so different windows don't collide.
DEMAND_CACHE_TTL = 300  # 5 min


def db_match_demand(days: int = 14) -> dict[frozenset, int]:
    """Unique-user demand per normalized (unordered) participant-pair, over the
    trailing `days` window. Counts DISTINCT users per pair — NOT raw request
    count — so repeated requests from a single user cannot alone inflate a
    match's demand signal (see priority_engine.PriorityInput.demand_count).

    Built from forecast_history.query (menu flow stores "{home} {away}" as the
    query text) and live_subscriptions.match_name ("{home} vs {away}"). Both
    are free text without a stored team/team delimiter, so events are keyed by
    an unordered NORMALIZED TOKEN SET rather than a split (home, away) pair —
    this also makes the key naturally order-independent.

    No schema change: reads existing columns only.
    """
    now = time.time()
    cached = demand_cache.get(days)
    if cached and now - cached[0] < DEMAND_CACHE_TTL:
        return cached[1]

    cutoff = f"-{int(days)} days"
    rows = _all(
        "SELECT DISTINCT user_id, query FROM forecast_history "
        "WHERE query != '' AND created_at >= datetime('now', ?)", (cutoff,))
    rows += _all(
        "SELECT DISTINCT user_id, match_name FROM live_subscriptions "
        "WHERE match_name != '' AND created_at >= datetime('now', ?)", (cutoff,))

    users_by_key: dict[frozenset, set] = defaultdict(set)
    for uid, text in rows:
        key = normalize_participant_tokens(text)
        if key:
            users_by_key[key].add(uid)
    result = {key: len(uids) for key, uids in users_by_key.items()}
    demand_cache[days] = (now, result)
    return result
