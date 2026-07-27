"""DB helpers against a temporary SQLite database (see conftest: BOT_DB_DIR
points at a session temp dir, the production bot.db is never touched)."""
import pytest

from db import detect_lang, like_escape


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_detect_lang_direct_mappings():
    assert detect_lang("az") == "az"
    assert detect_lang("tr") == "tr"
    assert detect_lang("en") == "en"


def test_detect_lang_aliases():
    assert detect_lang("uk") == "ru"   # Ukrainian → ru UI
    assert detect_lang("be") == "ru"
    assert detect_lang("kk") == "kz"
    assert detect_lang("fa") == "ar"


def test_detect_lang_region_suffix_and_case():
    assert detect_lang("ru-RU") == "ru"
    assert detect_lang("AZ") == "az"


def test_detect_lang_fallbacks():
    assert detect_lang(None) == "ru"
    assert detect_lang("") == "ru"
    assert detect_lang("xx") == "ru"


def test_like_escape_wildcards():
    assert like_escape("100%") == "100\\%"
    assert like_escape("a_b") == "a\\_b"
    assert like_escape("back\\slash") == "back\\\\slash"
    assert like_escape("plain") == "plain"


# ── users CRUD & allowlist ────────────────────────────────────────────────────

def test_ensure_and_get(temp_db):
    temp_db.db_ensure(830001, "alice", "ru")
    u = temp_db.db_get(830001)
    assert u["username"] == "alice"
    assert u["lang"] == "ru"
    assert u["tz_offset"] == 3  # ru default tz


def test_ensure_is_idempotent(temp_db):
    temp_db.db_ensure(830002, "bob", "en")
    temp_db.db_set(830002, "lang", "tr")
    temp_db.db_ensure(830002, "bob", "en")  # must not reset lang
    assert temp_db.db_lang(830002) == "tr"


def test_db_set_allowlisted_field(temp_db):
    temp_db.db_ensure(830003, "carol", "en")
    temp_db.db_set(830003, "display_name", "Carol")
    assert temp_db.db_get(830003)["display_name"] == "Carol"


def test_db_set_rejects_non_allowlisted_field(temp_db):
    temp_db.db_ensure(830004, "dave", "en")
    with pytest.raises(ValueError):
        temp_db.db_set(830004, "total_requests", 9999)
    with pytest.raises(ValueError):
        temp_db.db_set(830004, "user_id; DROP TABLE users", 1)


def test_lang_fallback_for_unknown_user(temp_db):
    # Unknown user → the safe default language (DEFAULT_LANG), never a crash.
    assert temp_db.db_lang(839999) == temp_db.DEFAULT_LANG == "ru"


# ── Forecast history & feedback ownership ─────────────────────────────────────

def test_history_trimmed_to_ten(temp_db):
    uid = 830010
    temp_db.db_ensure(uid, "hist", "ru")
    for i in range(15):
        temp_db.db_save_history(uid, f"query {i}", f"forecast {i}")
    with temp_db.con() as c:
        count = c.execute("SELECT COUNT(*) FROM forecast_history WHERE user_id=?",
                          (uid,)).fetchone()[0]
    assert count == 10
    # db_get_history returns the 5 most recent, newest first.
    hist = temp_db.db_get_history(uid)
    assert len(hist) == 5
    assert hist[0]["query"] == "query 14"


def test_feedback_ownership_enforced(temp_db):
    owner, attacker = 830011, 830012
    temp_db.db_ensure(owner, "owner", "ru")
    temp_db.db_ensure(attacker, "attacker", "ru")
    temp_db.db_save_history(owner, "q", "f")
    hist_id = temp_db.db_get_history(owner)[0]["id"]

    # Forged callback from another user must not update the row.
    temp_db.db_set_feedback(attacker, hist_id, 1)
    assert temp_db.db_get_history(owner)[0]["feedback"] is None

    temp_db.db_set_feedback(owner, hist_id, 1)
    assert temp_db.db_get_history(owner)[0]["feedback"] == 1


def test_feedback_stats(temp_db):
    uid = 830013
    temp_db.db_ensure(uid, "stats", "ru")
    for verdict in (1, 1, 0):
        temp_db.db_save_history(uid, "q", "f")
        hist_id = temp_db.db_get_history(uid)[0]["id"]
        temp_db.db_set_feedback(uid, hist_id, verdict)
    s = temp_db.db_feedback_stats(uid)
    assert s["total"] == 3
    assert s["wins"] == 2
    assert s["pct"] == 67


# ── Conversation memory ───────────────────────────────────────────────────────

def test_conversation_roundtrip_and_trim(temp_db):
    uid = 830020
    msgs = [{"role": "user", "content": str(i)} for i in range(10)]
    temp_db.db_save_conv(uid, msgs)
    loaded = temp_db.db_get_conv(uid)
    assert len(loaded) == 6
    assert loaded[-1]["content"] == "9"


def test_conversation_corrupt_json_returns_empty(temp_db):
    uid = 830021
    with temp_db.con() as c:
        c.execute("INSERT OR REPLACE INTO conversation (user_id, messages) VALUES (?, ?)",
                  (uid, "{not json"))
    assert temp_db.db_get_conv(uid) == []


def test_clear_conversation(temp_db):
    uid = 830022
    temp_db.db_save_conv(uid, [{"role": "user", "content": "hi"}])
    temp_db.db_clear_conv(uid)
    assert temp_db.db_get_conv(uid) == []


# ── Migration flags ───────────────────────────────────────────────────────────

def test_flag_mark_and_done(temp_db):
    key = "test_flag_830030"
    assert not temp_db.db_flag_done(key)
    temp_db.db_flag_mark(key)
    assert temp_db.db_flag_done(key)
    temp_db.db_flag_mark(key)  # idempotent
    assert temp_db.db_flag_done(key)


# ── User search escaping ──────────────────────────────────────────────────────

def test_search_wildcards_are_literal(temp_db):
    temp_db.db_ensure(830040, "percent%user", "en")
    temp_db.db_ensure(830041, "plainuser", "en")
    results = temp_db.db_search("percent%")
    ids = {u["user_id"] for u in results}
    assert 830040 in ids
    assert 830041 not in ids  # '%' must not act as a wildcard


# ── Match Priority Engine: demand aggregation ─────────────────────────────────
# NOTE: `temp_db` is a SHARED session-wide sqlite file (see conftest), never
# reset between tests. Each test below therefore uses match names unique to
# ITSELF (embedding the test's own uid range) so its assertions can never be
# polluted by rows another test happened to insert — the same convention this
# file already uses for uids.

def test_match_demand_counts_unique_users_not_requests(temp_db):
    from priority_config import normalize_participant_tokens
    key = normalize_participant_tokens("Testklub830050 Rivalklub830050")

    temp_db.db_ensure(830050, "u1", "en")
    temp_db.db_ensure(830051, "u2", "en")
    # User 830050 requests the same match twice — must count as ONE user.
    temp_db.db_save_history(830050, "Testklub830050 Rivalklub830050", "forecast text")
    temp_db.db_save_history(830050, "Testklub830050 Rivalklub830050", "forecast text again")
    temp_db.db_save_history(830051, "Testklub830050 Rivalklub830050", "forecast text")

    demand = temp_db.db_match_demand()
    assert demand.get(key) == 2


def test_match_demand_merges_live_subscriptions(temp_db):
    from priority_config import normalize_participant_tokens
    key = normalize_participant_tokens("Testklub830052 Rivalklub830052")

    temp_db.db_ensure(830052, "u3", "en")
    temp_db.db_add_lsub(830052, "mid-1", "Testklub830052 vs Rivalklub830052")

    demand = temp_db.db_match_demand()
    assert demand.get(key) == 1


def test_match_demand_key_is_order_independent(temp_db):
    from priority_config import normalize_participant_tokens
    temp_db.db_ensure(830053, "u4", "en")
    temp_db.db_ensure(830054, "u5", "en")
    temp_db.db_save_history(830053, "Testklub830053 Rivalklub830053", "x")
    temp_db.db_save_history(830054, "Rivalklub830053 Testklub830053", "x")  # reversed order

    demand = temp_db.db_match_demand()
    key = normalize_participant_tokens("Testklub830053 Rivalklub830053")
    assert demand.get(key) == 2  # both requests merge into the same key


def test_match_demand_excludes_rows_outside_window(temp_db):
    from priority_config import normalize_participant_tokens
    temp_db.db_ensure(830055, "u6", "en")
    with temp_db.con() as c:
        c.execute(
            "INSERT INTO forecast_history (user_id, query, forecast, created_at) "
            "VALUES (?, ?, ?, datetime('now', '-30 days'))",
            (830055, "Stale830055 OldMatch830055", "text"))

    demand = temp_db.db_match_demand(days=14)
    key = normalize_participant_tokens("Stale830055 OldMatch830055")
    assert demand.get(key, 0) == 0


def test_match_demand_ignores_empty_query(temp_db):
    temp_db.db_ensure(830056, "u7", "en")
    temp_db.db_save_history(830056, "", "text")  # photo-flow forecasts have no query
    demand = temp_db.db_match_demand()
    assert frozenset() not in demand  # empty/no-token text never becomes a key


# ─── db_log_req: last_active stays in the SAME clock as date('now') ──────────

def test_log_req_increments_total_requests_and_inserts_row(temp_db):
    uid = 830060
    temp_db.db_ensure(uid, "u8", "en")
    temp_db.db_log_req(uid, "TEXT")
    temp_db.db_log_req(uid, "PHOTO")

    assert temp_db.db_get(uid)["total_requests"] == 2
    with temp_db.con() as c:
        count = c.execute("SELECT COUNT(*) FROM requests WHERE user_id=?", (uid,)).fetchone()[0]
    assert count == 2


def test_log_req_last_active_matches_sqlite_utc_now(temp_db):
    """last_active must be written via SQLite's datetime('now') (UTC), not
    Python's datetime.now() (process-local time) — otherwise the dashboard's
    date(last_active) >= date('now', ...) activity-segment queries in
    stats_server.py can drift by up to a day depending on server timezone."""
    uid = 830061
    temp_db.db_ensure(uid, "u9", "en")
    temp_db.db_log_req(uid, "TEXT")

    with temp_db.con() as c:
        same_day = c.execute(
            "SELECT date(last_active) = date('now') FROM users WHERE user_id=?",
            (uid,)).fetchone()[0]
    assert same_day == 1
