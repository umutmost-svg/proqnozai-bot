"""Retention loop: bot-wide winrate + per-user activity streak.
All offline — no network. db_bot_winrate is GLOBAL (all rows), so its tests wipe
forecast_history first; streak is per-uid, so unique uids keep it isolated."""


def _seed_history(temp_db, wins, losses):
    with temp_db.con() as c:
        c.execute("DELETE FROM forecast_history")
        for i in range(wins):
            c.execute("INSERT INTO forecast_history (user_id,query,forecast,feedback,created_at) "
                      "VALUES (?,?,?,1,datetime('now'))", (700000 + i, "q", "f"))
        for i in range(losses):
            c.execute("INSERT INTO forecast_history (user_id,query,forecast,feedback,created_at) "
                      "VALUES (?,?,?,0,datetime('now'))", (710000 + i, "q", "f"))


# ─── Bot winrate ──────────────────────────────────────────────────────────────

def test_bot_winrate_cold_start_returns_none(temp_db):
    from config import winrate_cache
    _seed_history(temp_db, wins=5, losses=3)   # 8 rated < WINRATE_MIN_SAMPLES
    winrate_cache.clear()
    assert temp_db.db_bot_winrate() is None


def test_bot_winrate_computes_pct_over_threshold(temp_db):
    from config import winrate_cache
    _seed_history(temp_db, wins=24, losses=16)  # 40 rated, 60%
    winrate_cache.clear()
    wr = temp_db.db_bot_winrate()
    assert wr == {"wins": 24, "total": 40, "pct": 60}


def test_bot_winrate_is_cached(temp_db):
    from config import winrate_cache
    _seed_history(temp_db, wins=30, losses=10)  # 75%
    winrate_cache.clear()
    first = temp_db.db_bot_winrate()
    _seed_history(temp_db, wins=0, losses=40)   # DB changed…
    assert temp_db.db_bot_winrate() == first    # …but cache still served


# ─── Activity streak ──────────────────────────────────────────────────────────

def _seed_requests(temp_db, uid, day_offsets):
    with temp_db.con() as c:
        for off in day_offsets:
            c.execute("INSERT INTO requests (user_id,msg_type,created_at) "
                      "VALUES (?,?,datetime('now', ?))", (uid, "TEXT", f"-{off} days"))


def test_streak_counts_consecutive_days_including_today(temp_db):
    _seed_requests(temp_db, 730001, [0, 1, 2])   # today, yesterday, 2d ago
    assert temp_db.db_user_streak(730001) == 3


def test_streak_breaks_on_a_gap(temp_db):
    _seed_requests(temp_db, 730002, [0, 2, 3])   # missed yesterday
    assert temp_db.db_user_streak(730002) == 1


def test_streak_from_yesterday_still_counts(temp_db):
    _seed_requests(temp_db, 730003, [1, 2])      # last active yesterday
    assert temp_db.db_user_streak(730003) == 2


def test_streak_zero_when_last_activity_too_old(temp_db):
    _seed_requests(temp_db, 730004, [3, 4])      # last active 3 days ago
    assert temp_db.db_user_streak(730004) == 0


def test_streak_zero_for_new_user(temp_db):
    assert temp_db.db_user_streak(739999) == 0
