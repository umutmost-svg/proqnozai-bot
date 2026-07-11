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
    assert temp_db.db_lang(839999) == "az"


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
