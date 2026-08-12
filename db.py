import os
import sqlite3
import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timezone
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
        # journal_mode is a property of the DATABASE FILE and persists once set,
        # so it is applied in db_init() rather than re-issued on every
        # connection. busy_timeout and synchronous are per-connection and do
        # have to be set here — busy_timeout in particular is what lets a writer
        # wait instead of failing while another holds the lock.
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
        # Set once: WAL is stored in the database header and survives restarts,
        # so every later connection inherits it. Concurrency is unchanged —
        # readers still don't block the writer.
        c.execute("PRAGMA journal_mode=WAL")
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
        CREATE TABLE IF NOT EXISTS partner_clicks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            partner    TEXT,
            created_at TEXT DEFAULT (datetime('now'))
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
            # Outcome and duration of a forecast, so the dashboard can show a
            # real success rate and latency instead of guessing from the logs.
            # NULL on rows written before this existed, and on non-forecast
            # events — every query below filters for NOT NULL.
            "ALTER TABLE requests ADD COLUMN ok INTEGER DEFAULT NULL",
            "ALTER TABLE requests ADD COLUMN ms INTEGER DEFAULT NULL",
            # Promo codes are per partner now, each with its own cap. A row
            # written before this carries partner='' and keeps working.
            "ALTER TABLE promo_campaign ADD COLUMN partner TEXT DEFAULT ''",
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


# msg_type values written to `requests`. TEXT/PHOTO are inbound messages;
# FORECAST is a generated forecast, which is what the product actually does.
# FORECAST was missing entirely until now: the menu flow never logged, so every
# menu-only user looked permanently inactive to last_active, to the dashboard's
# activity counts, to the broadcast segments and to daily_push.
REQ_TEXT = "TEXT"
REQ_PHOTO = "PHOTO"
REQ_FORECAST = "FORECAST"
# The user opened the partner list. Paired with partner_clicks, this gives the
# only funnel we can measure on partner links.
REQ_PARTNERS_OPEN = "PARTNERS_OPEN"


def db_log_req(uid, mtype, ok: bool | None = None, ms: int | None = None):
    """Record one user event and refresh last_active.

    `ok`/`ms` are only meaningful for REQ_FORECAST — they carry whether the
    forecast was produced and how long it took."""
    try:
        with con() as c:
            c.execute("INSERT INTO requests (user_id,msg_type,ok,ms) VALUES (?,?,?,?)",
                      (uid, mtype, None if ok is None else int(ok), ms))
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


# ─── Product metrics ──────────────────────────────────────────────────────────
# Everything below is derived from the existing tables. Two measurement traps
# they deliberately avoid:
#   * forecast_history is capped at 10 rows per user, so it can never be used
#     as a volume metric — `requests` (uncapped) is the event log.
#   * "active" must mean "did something", which is a FORECAST/TEXT/PHOTO row,
#     not merely "is registered".

def db_activation_funnel() -> dict:
    """How far users get: arrived → registered → finished onboarding → got a
    forecast. The drop between two adjacent steps is where the product loses
    people.

    Each step is a strict subset of the one before it, which is what makes the
    percentages mean anything. That matters because the underlying columns are
    independent flags: a user carried over from before onboarding existed can
    have forecasts without onboarding_done, and counting the steps separately
    would produce a "funnel" that widens. `forecasted_any` keeps that raw
    number, since it is the honest count of people who ever got a forecast."""
    with con() as c:
        def one(sql):
            return c.execute(sql).fetchone()[0]
        started = one("SELECT COUNT(*) FROM users")
        registered = one("SELECT COUNT(*) FROM users WHERE is_registered=1")
        onboarded = one("SELECT COUNT(*) FROM users "
                        "WHERE is_registered=1 AND onboarding_done=1")
        forecasted = one(
            "SELECT COUNT(DISTINCT u.user_id) FROM users u "
            "JOIN requests r ON r.user_id = u.user_id AND r.msg_type='FORECAST' "
            "WHERE u.is_registered=1 AND u.onboarding_done=1")
        forecasted_any = one(
            "SELECT COUNT(DISTINCT user_id) FROM requests WHERE msg_type='FORECAST'")
    return dict(started=started, registered=registered, onboarded=onboarded,
                forecasted=forecasted, forecasted_any=forecasted_any)


def db_engagement() -> dict:
    """DAU / WAU / MAU plus stickiness (DAU/MAU) — the single number that says
    whether the product is a habit or a one-off."""
    with con() as c:
        def act(days):
            return c.execute(
                "SELECT COUNT(DISTINCT user_id) FROM requests "
                "WHERE created_at >= datetime('now', ?)", (f"-{days} days",)).fetchone()[0]
        dau, wau, mau = act(1), act(7), act(30)
        forecasts_7d = c.execute(
            "SELECT COUNT(*) FROM requests WHERE msg_type='FORECAST' "
            "AND created_at >= datetime('now','-7 days')").fetchone()[0]
    return dict(dau=dau, wau=wau, mau=mau,
                stickiness=round(dau / mau * 100) if mau else 0,
                forecasts_per_wau=round(forecasts_7d / wau, 1) if wau else 0.0)


def db_retention(cohort_days: int = 30) -> list[dict]:
    """Classic D1/D7/D30: of the users who joined on a given day, how many came
    back on day 1, within a week, within a month. Anchored on joined_at, counted
    from the uncapped `requests` log."""
    rows = _all(
        """
        WITH cohort AS (
            SELECT user_id, date(joined_at) AS day FROM users
            WHERE date(joined_at) >= date('now', ?)
        )
        SELECT c.day, COUNT(DISTINCT c.user_id),
               COUNT(DISTINCT CASE WHEN julianday(date(r.created_at))
                                      - julianday(c.day) BETWEEN 1 AND 1
                              THEN c.user_id END),
               COUNT(DISTINCT CASE WHEN julianday(date(r.created_at))
                                      - julianday(c.day) BETWEEN 1 AND 7
                              THEN c.user_id END),
               COUNT(DISTINCT CASE WHEN julianday(date(r.created_at))
                                      - julianday(c.day) BETWEEN 1 AND 30
                              THEN c.user_id END)
        FROM cohort c LEFT JOIN requests r ON r.user_id = c.user_id
        GROUP BY c.day ORDER BY c.day DESC
        """, (f"-{int(cohort_days)} days",))
    out = []
    today = datetime.now(timezone.utc).date()
    for day, size, d1, d7, d30 in rows:
        pct = lambda n: round(n / size * 100) if size else 0   # noqa: E731
        # A cohort registered today cannot have a D7 number yet. Reporting 0%
        # for it drags the eye down and makes healthy retention look broken, so
        # immature windows are returned as None and rendered as "—".
        age = (today - date.fromisoformat(day)).days if day else 0
        out.append(dict(day=day, size=size, age=age,
                        d1=pct(d1) if age >= 1 else None,
                        d7=pct(d7) if age >= 7 else None,
                        d30=pct(d30) if age >= 30 else None))
    return out


def db_feedback_coverage() -> dict:
    """What share of forecasts anyone actually rates. The bot-wide winrate is
    computed over rated forecasts only, so a low coverage means the headline
    accuracy figure rests on a thin, self-selected sample.

    The denominator is the FORECAST event count, not forecast_history: history
    is trimmed to ten rows per user, which would quietly shrink the denominator
    and overstate coverage. A forecast trimmed away can no longer be rated, so
    counting it as unrated is the honest reading."""
    total = _one("SELECT COUNT(*) FROM requests WHERE msg_type='FORECAST'") or 0
    rated = _one("SELECT COUNT(*) FROM forecast_history WHERE feedback IS NOT NULL") or 0
    # Ratings predate the FORECAST event, so early on `rated` can exceed the
    # events counted; clamp rather than show an impossible percentage.
    return dict(total=total, rated=rated,
                pct=min(100, round(rated / total * 100)) if total else 0)


def db_forecast_health(days: int = 7) -> dict:
    """Success rate and latency of forecast generation, from the outcome
    recorded alongside each FORECAST event."""
    with con() as c:
        row = c.execute(
            "SELECT COUNT(*), SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END), AVG(ms) "
            "FROM requests WHERE msg_type='FORECAST' AND ok IS NOT NULL "
            "AND created_at >= datetime('now', ?)", (f"-{int(days)} days",)).fetchone()
        # Median latency: SQLite has no percentile function, so take the middle
        # row of the ordered set.
        p50 = c.execute(
            "SELECT ms FROM requests WHERE msg_type='FORECAST' AND ms IS NOT NULL "
            "AND created_at >= datetime('now', ?) ORDER BY ms "
            "LIMIT 1 OFFSET (SELECT COUNT(*)/2 FROM requests "
            "                WHERE msg_type='FORECAST' AND ms IS NOT NULL "
            "                AND created_at >= datetime('now', ?))",
            (f"-{int(days)} days", f"-{int(days)} days")).fetchone()
    total, ok, avg_ms = row[0] or 0, row[1] or 0, row[2]
    return dict(total=total, ok=ok, failed=total - ok,
                ok_pct=round(ok / total * 100) if total else 0,
                avg_ms=round(avg_ms) if avg_ms else 0,
                p50_ms=p50[0] if p50 else 0)


def db_churn() -> dict:
    """Registered users by how long they have been silent. `never` is the
    activation leak: registered and then never did anything."""
    with con() as c:
        def one(sql):
            return c.execute(sql).fetchone()[0]
        base = "SELECT COUNT(*) FROM users WHERE is_registered=1"
        return dict(
            active_7d=one(base + " AND last_active != '' AND date(last_active) >= date('now','-7 days')"),
            silent_7_30=one(base + " AND last_active != '' AND date(last_active) < date('now','-7 days')"
                                   " AND date(last_active) >= date('now','-30 days')"),
            silent_30=one(base + " AND last_active != '' AND date(last_active) < date('now','-30 days')"),
            never=one(base + " AND (last_active IS NULL OR last_active='')"),
        )


def db_promo_funnel() -> dict:
    """Promo campaigns as a funnel: of the users who could claim, how many did.
    Aggregated across partners, with the per-partner breakdown alongside — that
    is the monetization step, so it gets its own conversion figure."""
    codes = db_list_promo_codes()
    eligible = _one("SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0") or 0
    if not codes:
        return dict(partners=[], max_uses=0, claimed=0, remaining=0,
                    eligible=eligible, conversion=0, claimed_7d=0, users=0)
    claimed = sum(c["claimed"] for c in codes)
    max_uses = sum(c["max_uses"] for c in codes)
    users = _one("SELECT COUNT(DISTINCT user_id) FROM promo_claims") or 0
    claimed_7d = _one("SELECT COUNT(*) FROM promo_claims "
                      "WHERE claimed_at >= datetime('now','-7 days')") or 0
    return dict(partners=codes, max_uses=max_uses, claimed=claimed,
                remaining=max(0, max_uses - claimed), eligible=eligible,
                conversion=round(users / eligible * 100) if eligible else 0,
                claimed_7d=claimed_7d, users=users)


# ─── Partner click tracking ───────────────────────────────────────────────────
def db_log_partner_click(uid, partner: str) -> None:
    """One click on a partner link. Best-effort: a failure here must never
    interfere with sending the user to the partner."""
    try:
        _run("INSERT INTO partner_clicks (user_id, partner) VALUES (?,?)",
             (uid, (partner or "")[:100]))
    except Exception as e:
        logger.error(f"db_log_partner_click: {e}")


def db_partner_clicks(days: int = 30) -> dict:
    """Clicks per partner plus unique clickers — the closest thing the bot has
    to a revenue signal."""
    cutoff = f"-{int(days)} days"
    rows = _all("SELECT partner, COUNT(*), COUNT(DISTINCT user_id) FROM partner_clicks "
                "WHERE created_at >= datetime('now', ?) GROUP BY 1 ORDER BY 2 DESC", (cutoff,))
    total = sum(r[1] for r in rows)
    uniq = _one("SELECT COUNT(DISTINCT user_id) FROM partner_clicks "
                "WHERE created_at >= datetime('now', ?)", (cutoff,)) or 0
    opened = _one("SELECT COUNT(DISTINCT user_id) FROM requests "
                  "WHERE msg_type='PARTNERS_OPEN' AND created_at >= datetime('now', ?)",
                  (cutoff,)) or 0
    # Clicks can arrive from a partner list opened before this window, so the
    # raw ratio can exceed 100%. Clamp it: the number is meant to read as "what
    # share of people who open the list go on to a partner", and a figure above
    # 100 only means the two counters cover slightly different populations.
    return dict(total=total, unique_users=uniq, opened_list=opened,
                click_through=min(100, round(uniq / opened * 100)) if opened else 0,
                by_partner=[[r[0], r[1], r[2]] for r in rows])


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
    sent this?" decision and the record of having sent it can't diverge.

    An event is marked seen BEFORE the notifications go out, which makes this
    at-most-once: if delivery then fails, that event is never retried. That is
    deliberate. Marking after delivery would make a crash mid-fanout re-notify
    everyone who already received it, and for a goal alert a duplicate is worse
    than a miss."""
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
def db_set_promo_code(partner: str, code: str, max_uses: int) -> None:
    """Set (or replace) ONE partner's promo code and its own use cap.

    Each partner has an independent cap, so Mostbet running out doesn't hide
    Topaz's code. Claims are tracked per code, so replacing a partner's code
    starts that partner's count fresh while the others keep theirs."""
    partner = (partner or "").strip()
    code = code.strip()
    with con() as c:
        # Claims are keyed by the code string (promo_claims PK), so two partners
        # sharing one code would share a claim count and a cap. Refuse rather
        # than silently merge them.
        clash = c.execute("SELECT partner FROM promo_campaign WHERE code=? AND partner!=?",
                          (code, partner)).fetchone()
        if clash:
            raise ValueError(f"code '{code}' is already used by partner '{clash[0]}'")
        c.execute("DELETE FROM promo_campaign WHERE partner=?", (partner,))
        c.execute("INSERT INTO promo_campaign (partner, code, max_uses) VALUES (?,?,?)",
                  (partner, code, int(max_uses)))


def db_delete_promo_code(partner: str) -> bool:
    """Remove one partner's code. Returns whether anything was removed."""
    with con() as c:
        cur = c.execute("DELETE FROM promo_campaign WHERE partner=?",
                        ((partner or "").strip(),))
        return bool(cur.rowcount)


def db_list_promo_codes() -> list[dict]:
    """Every configured code with how much of its cap is used."""
    rows = _all("SELECT partner, code, max_uses FROM promo_campaign ORDER BY partner")
    out = []
    for partner, code, max_uses in rows:
        claimed = _one("SELECT COUNT(*) FROM promo_claims WHERE code=?", (code,)) or 0
        out.append(dict(partner=partner, code=code, max_uses=max_uses,
                        claimed=claimed, available=max(0, max_uses - claimed)))
    return out


def db_claim_promos(uid) -> list[dict]:
    """Hand this user every code still available, one per partner.

    Idempotent per code: a user who already has a partner's code gets the same
    string back without consuming a second use. A partner whose cap is spent is
    simply absent from the result — the others are unaffected.

    BEGIN IMMEDIATE takes the write lock up front. A deferred transaction starts
    read-only and only upgrades on the INSERT, so two callers could both read
    used == max_uses - 1 and both insert, issuing one code over the cap."""
    granted = []
    with con() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT partner, code, max_uses FROM promo_campaign ORDER BY partner").fetchall()
        for partner, code, max_uses in rows:
            already = c.execute("SELECT 1 FROM promo_claims WHERE user_id=? AND code=?",
                                (uid, code)).fetchone()
            if already:
                granted.append(dict(partner=partner, code=code))
                continue
            used = c.execute("SELECT COUNT(*) FROM promo_claims WHERE code=?",
                             (code,)).fetchone()[0]
            if used >= max_uses:
                continue                     # this partner is out; others aren't
            c.execute("INSERT OR IGNORE INTO promo_claims (user_id, code) VALUES (?,?)",
                      (uid, code))
            granted.append(dict(partner=partner, code=code))
    return granted


def db_promo_stats() -> list[dict]:
    """Per-partner campaign state, for the admin readout."""
    return db_list_promo_codes()


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
