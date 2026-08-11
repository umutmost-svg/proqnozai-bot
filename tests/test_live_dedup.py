"""Live-event de-duplication survives restarts, reorders and truncation.

The old logic compared list LENGTH against an in-memory counter
(`evs[len(prev):]`), so a restart mid-match re-sent every goal and card that had
already gone out, and any reordering or shortening of the provider response
produced duplicates. Identity is now content-derived and persisted. Offline.
"""
from handlers.live import _event_key


def _goal(minute, player, team="Barca", extra=None):
    return {"type": "Goal", "detail": "Normal Goal",
            "team": {"name": team}, "player": {"name": player},
            "time": {"elapsed": minute, "extra": extra}}


def _card(minute, player, detail="Yellow Card", team="Real"):
    return {"type": "Card", "detail": detail,
            "team": {"name": team}, "player": {"name": player},
            "time": {"elapsed": minute}}


def _new(temp_db, mid, events):
    """The poller's filter step: events not notified about before."""
    keys = {}
    for ev in events:
        keys.setdefault(_event_key(ev), ev)
    fresh = temp_db.db_filter_new_live_events(mid, list(keys))
    return [keys[k] for k in fresh]


# ─── event identity ───────────────────────────────────────────────────────────

def test_provider_id_is_preferred_when_present():
    assert _event_key({"id": "evt-77", "type": "Goal"}) == "id:evt-77"


def test_key_is_stable_for_the_same_event():
    ev = _goal(23, "Messi")
    assert _event_key(ev) == _event_key(dict(ev))


def test_key_separates_events_that_differ_only_by_minute():
    assert _event_key(_goal(23, "Messi")) != _event_key(_goal(67, "Messi"))


def test_key_separates_stoppage_time_from_regular_time():
    assert _event_key(_goal(45, "Messi")) != _event_key(_goal(45, "Messi", extra=2))


def test_key_separates_yellow_from_red():
    assert _event_key(_card(30, "Ramos")) != _event_key(_card(30, "Ramos", detail="Red Card"))


# ─── de-duplication behaviour ─────────────────────────────────────────────────

def test_first_sighting_is_new(temp_db):
    evs = [_goal(12, "Lewandowski"), _card(30, "Ramos")]
    assert len(_new(temp_db, "m-100", evs)) == 2


def test_restart_does_not_resend_already_sent_events(temp_db):
    """The regression: in-memory state is gone, the DB record is not."""
    evs = [_goal(12, "Lewandowski"), _card(30, "Ramos")]
    _new(temp_db, "m-101", evs)
    # …process restarts; the poller polls the same match again…
    assert _new(temp_db, "m-101", evs) == []


def test_reordered_provider_response_creates_no_duplicates(temp_db):
    evs = [_goal(12, "Lewandowski"), _card(30, "Ramos")]
    _new(temp_db, "m-102", evs)
    assert _new(temp_db, "m-102", list(reversed(evs))) == []


def test_shortened_provider_response_creates_no_duplicates(temp_db):
    """A response that drops earlier events must not make the rest look new."""
    evs = [_goal(12, "Lewandowski"), _card(30, "Ramos"), _goal(55, "Pedri")]
    _new(temp_db, "m-103", evs)
    assert _new(temp_db, "m-103", evs[-1:]) == []


def test_duplicate_payload_within_one_poll_is_sent_once(temp_db):
    ev = _goal(12, "Lewandowski")
    assert len(_new(temp_db, "m-104", [ev, dict(ev)])) == 1


def test_genuinely_new_event_is_still_sent(temp_db):
    evs = [_goal(12, "Lewandowski")]
    _new(temp_db, "m-105", evs)
    fresh = _new(temp_db, "m-105", evs + [_goal(66, "Pedri")])
    assert [e["player"]["name"] for e in fresh] == ["Pedri"]


def test_matches_do_not_share_dedup_state(temp_db):
    ev = _goal(12, "Lewandowski")
    _new(temp_db, "m-106", [ev])
    assert len(_new(temp_db, "m-107", [ev])) == 1


def test_clearing_a_finished_match_frees_its_keys(temp_db):
    ev = _goal(12, "Lewandowski")
    _new(temp_db, "m-108", [ev])
    temp_db.db_clear_live_events("m-108")
    with temp_db.con() as c:
        left = c.execute("SELECT COUNT(*) FROM live_events_seen WHERE match_id=?",
                         ("m-108",)).fetchone()[0]
    assert left == 0


def test_purge_drops_only_stale_rows(temp_db):
    _new(temp_db, "m-109", [_goal(12, "Lewandowski")])
    with temp_db.con() as c:
        c.execute("INSERT OR REPLACE INTO live_events_seen (match_id, event_key, created_at) "
                  "VALUES (?,?,datetime('now','-30 days'))", ("m-110", "old-key"))
    temp_db.db_purge_stale_live_events()
    with temp_db.con() as c:
        assert c.execute("SELECT COUNT(*) FROM live_events_seen WHERE match_id=?",
                         ("m-110",)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM live_events_seen WHERE match_id=?",
                         ("m-109",)).fetchone()[0] == 1
