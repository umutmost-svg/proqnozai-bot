"""Retention loop: bot-wide winrate, per-user activity streak, match-of-day pick.
All offline — no network. db_bot_winrate is GLOBAL (all rows), so its tests wipe
forecast_history first; streak is per-uid, so unique uids keep it isolated."""
from datetime import datetime, timezone, timedelta

import handlers.forecast as fc
from event_list import normalize_fixture, select_visible, assign_priority_scores

UTC = timezone.utc
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


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


# ─── Match of the day pick ────────────────────────────────────────────────────

def _raw(fid, t1, t2, league, country, hours):
    ko = (NOW + timedelta(hours=hours)).strftime("%d.%m.%Y %H:%M:%S")
    # Mostbet times are MOSTBET_SRC_TZ(+3); NOW is UTC, so shift the string +3h
    # is unnecessary for a relative "future" check — kickoff just needs > now.
    return {"id": fid, "team1Title": t1, "team2Title": t2, "lineCategory": "Football",
            "lineSubCategory": league, "lineSuperCategory": country, "matchBeginAt": ko,
            "isLive": False}


def _items(raws, tz=UTC):
    items = select_visible([normalize_fixture(r) for r in raws], NOW, tz, include_later=True)
    assign_priority_scores(items, NOW, {})
    return items


def test_match_of_day_picks_highest_priority_today(temp_db):
    # A Champions League match (tier-1 prestige) today must outrank a regional
    # match today.
    raws = [
        _raw(1, "Team A", "Team B", "Regional Cup", "Nowhere", 4),
        _raw(2, "Team C", "Team D", "Champions League", "Europe", 5),
    ]
    pick, bucket = fc._pick_match_of_day(_items(raws), NOW)
    assert pick is not None and pick.fixture_id == "2"
    assert bucket == "TODAY"


def test_match_of_day_falls_back_to_tomorrow(temp_db):
    # Only a match ~30h out (tomorrow bucket in UTC) → picked as fallback.
    raws = [_raw(3, "Team E", "Team F", "Champions League", "Europe", 30)]
    pick, bucket = fc._pick_match_of_day(_items(raws), NOW)
    assert pick is not None and pick.fixture_id == "3"
    assert bucket in ("TOMORROW", "LATER")


def test_match_of_day_none_when_no_upcoming(temp_db):
    assert fc._pick_match_of_day([], NOW) == (None, None)
