"""Offline tests for event-menu callback safety: callbacks resolve only against
the frozen snapshot, missing/stale indexes yield the expired-menu state, an
already-rendered index cannot be re-pointed by refreshed data, and truncation is
surfaced to the user. No network."""
import types
from datetime import datetime, timedelta, timezone

import handlers.forecast as fc
from config import MOSTBET_SRC_TZ, msg_times, nav_times
from event_list import normalize_fixture
from translations import T

_SRC_TZ = timezone(timedelta(hours=MOSTBET_SRC_TZ))


def _when(hours_from_now: float = 6.0) -> str:
    """Kickoff in Mostbet's source-tz string format, RELATIVE to the real clock.

    forecast_menu_start buckets matches against datetime.now(); a hardcoded
    calendar date here silently rots as real time advances (a past kickoff gets
    filtered as finished and the menu takes the empty-state early return). All
    fixtures that must be VISIBLE therefore derive from now, never a literal
    date."""
    return (datetime.now(_SRC_TZ) + timedelta(hours=hours_from_now)).strftime(
        "%d.%m.%Y %H:%M:%S")


def _raw(fid, t1, t2, league="Premier League", country="England",
         when=None, live=False):
    return {"id": fid, "team1Title": t1, "team2Title": t2, "lineCategory": "Football",
            "lineSubCategory": league, "lineSuperCategory": country,
            "matchBeginAt": when if when is not None else _when(), "isLive": live}


class _FakeQuery:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = types.SimpleNamespace(id=uid)
        self.message = types.SimpleNamespace(text="header")
        self.edited = None
        self.markup = None

    async def answer(self, text=None, show_alert=False):
        pass

    async def edit_message_text(self, text, **kw):
        self.edited = text
        self.markup = kw.get("reply_markup")

    def button_callbacks(self) -> list[str]:
        if self.markup is None:
            return []
        return [btn.callback_data for row in self.markup.inline_keyboard for btn in row]


class _FakeMsg:
    def __init__(self):
        self.text = None

    async def edit_text(self, text, **kw):
        self.text = text


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return _FakeMsg()

    async def send_chat_action(self, chat_id, action):
        pass


def _update(q):
    return types.SimpleNamespace(callback_query=q)


def _ctx(bot=None, **ud):
    return types.SimpleNamespace(user_data=dict(ud), bot=bot or _FakeBot())


# ─── Expired / stale snapshot ─────────────────────────────────────────────────

async def test_match_cb_missing_snapshot_is_expired(temp_db):
    uid = 811001
    temp_db.db_ensure(uid, "u", "en")
    q = _FakeQuery("fm_mt_0", uid)
    await fc.fm_match_cb(_update(q), _ctx())  # no fm_matches stored
    assert q.edited == T["en"]["ev_menu_expired"]


async def test_match_cb_stale_index_is_expired(temp_db):
    uid = 811002
    temp_db.db_ensure(uid, "u", "en")
    it = normalize_fixture(_raw(1, "Arsenal", "Chelsea"))
    q = _FakeQuery("fm_mt_5", uid)  # index beyond the stored 1-item snapshot
    await fc.fm_match_cb(_update(q), _ctx(fm_matches=[it]))
    assert q.edited == T["en"]["ev_menu_expired"]


async def test_league_cb_missing_snapshot_is_expired(temp_db):
    uid = 811003
    temp_db.db_ensure(uid, "u", "en")
    q = _FakeQuery("fm_lg_0", uid)
    await fc.fm_league_cb(_update(q), _ctx())
    assert q.edited == T["en"]["ev_menu_expired"]


# ─── Frozen resolution: an index maps to its snapshot item, not refreshed data ─

async def test_match_cb_resolves_against_frozen_snapshot(monkeypatch, temp_db):
    uid = 811004
    temp_db.db_ensure(uid, "u", "en")

    async def _noop_odds(mid):
        return {}

    async def _noop_real(t1, t2, hint):
        return ""

    async def _forecast(uid_, content, sys, tok):
        return "OK"

    monkeypatch.setattr(fc, "mostbet_get_odds", _noop_odds)
    monkeypatch.setattr(fc, "fetch_real_data", _noop_real)
    monkeypatch.setattr(fc, "format_mostbet_odds", lambda o, l: "")
    monkeypatch.setattr(fc, "claude_forecast", _forecast)

    frozen = [normalize_fixture(_raw(101, "Arsenal", "Chelsea")),
              normalize_fixture(_raw(202, "Liverpool", "Everton"))]
    q = _FakeQuery("fm_mt_1", uid)
    ctx = _ctx(fm_matches=frozen)

    await fc.fm_match_cb(_update(q), ctx)

    # Index 1 must resolve to the SECOND frozen item by authoritative fixture id,
    # regardless of any provider refresh elsewhere.
    assert ctx.user_data["pending_mostbet_line_id"] == "202"
    assert ctx.user_data["match_ref"]["home"] == "Liverpool"


async def test_new_session_invalidates_old_deep_keyboard(temp_db, monkeypatch):
    uid = 811005
    temp_db.db_ensure(uid, "u", "en")

    async def _load():
        return [_raw(1, "Arsenal", "Chelsea"), _raw(2, "Barca", "Madrid",
                     league="La Liga", country="Spain")]

    monkeypatch.setattr(fc, "_mostbet_load_matches", _load)

    # Simulate a live match list load: this must clear fm_leagues/fm_matches.
    ctx = _ctx(fm_matches=[normalize_fixture(_raw(9, "Old", "Stale"))])
    msg = _FakeMsg()
    update = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid),
        message=types.SimpleNamespace(reply_text=lambda *a, **k: _async_msg(msg)))
    await fc.forecast_menu_start(update, ctx)
    assert ctx.user_data["fm_matches"] is None  # old deep snapshot invalidated

    # An old fm_mt_ callback now hits a missing snapshot → expired.
    q = _FakeQuery("fm_mt_0", uid)
    await fc.fm_match_cb(_update(q), ctx)
    assert q.edited == T["en"]["ev_menu_expired"]


async def _async_msg(msg):
    return msg


async def test_new_session_preserves_unrelated_forecast_state(temp_db, monkeypatch):
    uid = 811008
    temp_db.db_ensure(uid, "u", "en")

    async def _load():
        return [_raw(1, "Arsenal", "Chelsea")]

    monkeypatch.setattr(fc, "_mostbet_load_matches", _load)

    # Seed forecast state unrelated to the event snapshot.
    ctx = _ctx(fm_matches=[normalize_fixture(_raw(9, "Old", "Stale"))],
               odds_attached=True, has_real_data=True,
               parsed_teams=("X", "Y"), pending_content=[{"type": "text", "text": "keep"}])
    msg = _FakeMsg()
    update = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid),
        message=types.SimpleNamespace(reply_text=lambda *a, **k: _async_msg(msg)))

    await fc.forecast_menu_start(update, ctx)

    # Only the event snapshot is invalidated…
    assert ctx.user_data["fm_matches"] is None
    assert ctx.user_data["fm_leagues"] is None
    # …unrelated forecast state is untouched.
    assert ctx.user_data["odds_attached"] is True
    assert ctx.user_data["has_real_data"] is True
    assert ctx.user_data["parsed_teams"] == ("X", "Y")
    assert ctx.user_data["pending_content"] == [{"type": "text", "text": "keep"}]


async def test_menu_start_filters_finished_and_stays_deterministic(temp_db, monkeypatch):
    """Regression for the calendar-date rot that broke CI: a kickoff far in the
    past must be filtered from a fresh menu on ANY run date, while a relative
    future kickoff stays visible — so this suite can never go stale again."""
    uid = 811010
    temp_db.db_ensure(uid, "u", "en")

    async def _load():
        return [_raw(1, "Past", "Gone", when=_when(-48)),      # long finished → hidden
                _raw(2, "Soon", "Visible")]                     # relative future → shown

    monkeypatch.setattr(fc, "_mostbet_load_matches", _load)
    ctx = _ctx()
    msg = _FakeMsg()
    update = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid),
        message=types.SimpleNamespace(reply_text=lambda *a, **k: _async_msg(msg)))

    await fc.forecast_menu_start(update, ctx)

    # The menu was built (not the empty-state early return) from the one
    # visible match; the finished one is gone.
    sports = ctx.user_data["fm_sports"]
    assert sports and len(sports[0][1]) == 1
    assert sports[0][1][0].home == "Soon"


def test_fmt_kickoff_uses_user_timezone(temp_db):
    from datetime import datetime, timezone
    uid = 811009
    temp_db.db_ensure(uid, "u", "en")
    temp_db.db_set(uid, "tz_offset", 5)
    out = fc._fmt_kickoff(datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc), uid)
    assert "17:00" in out          # 12:00 UTC → 17:00 at UTC+5
    assert "UTC+5" in out


# ─── Pagination visible to the user (replaces the old hard truncation) ───────

async def test_sport_cb_shows_day_filter_screen(temp_db):
    """fm_sport_cb leads to the day-filter screen when more than one day bucket
    is present — the league list itself is reached via fm_day_cb/fm_ctry_cb.
    (With a single day option the day step auto-skips; see
    test_sport_cb_auto_skips_day_when_single_option.)"""
    uid = 811006
    temp_db.db_ensure(uid, "u", "en")
    # Spread across today AND tomorrow so there are two day options.
    items = [normalize_fixture(_raw(1000 + i, f"T{i}a", f"T{i}b",
                                    league=f"League {i:02d}", country=f"C{i}",
                                    when=_when(6 if i % 2 else 30)))
             for i in range(16)]
    q = _FakeQuery("fm_sp_0", uid)
    await fc.fm_sport_cb(_update(q), _ctx(fm_sports=[("Football", items)]))
    assert q.edited == T["en"]["ev_day_title"]


async def test_sport_cb_auto_skips_day_when_single_option(temp_db):
    """With only one day bucket the day screen is a redundant tap, so fm_sport_cb
    skips straight to the country screen (and its back leads to the sport list,
    not a day screen that was never shown)."""
    uid = 811033
    temp_db.db_ensure(uid, "u", "en")
    # All today, but two countries → country screen (not league) is next.
    items = [normalize_fixture(_raw(1000 + i, f"T{i}a", f"T{i}b",
                                    league=f"League {i:02d}",
                                    country="England" if i % 2 else "Spain",
                                    when=_when(6)))
             for i in range(6)]
    q = _FakeQuery("fm_sp_0", uid)
    ctx = _ctx(fm_sports=[("Football", items)])
    await fc.fm_sport_cb(_update(q), ctx)
    assert q.edited == T["en"]["ev_country_title"]        # day skipped
    assert ctx.user_data["fm_country_back"] == "fm_back_sport"
    # The country screen's back button points at the sport list.
    cbs = [b.callback_data for row in q.markup.inline_keyboard for b in row]
    assert "fm_back_sport" in cbs


async def test_league_list_paginates_beyond_one_page(temp_db):
    uid = 811006
    temp_db.db_ensure(uid, "u", "en")
    # Same country on every item so the country-filter screen auto-skips
    # (nothing to narrow by country) — this test is about LEAGUE pagination.
    items = [normalize_fixture(_raw(1000 + i, f"T{i}a", f"T{i}b",
                                    league=f"League {i:02d}", country="Same"))
             for i in range(16)]
    q = _FakeQuery("fm_day_0", uid)
    await fc.fm_day_cb(_update(q), _ctx(fm_sport_items=items, fm_day_options=[],
                                        fm_sports=[("Football", items)]))
    # 16 leagues > PAGE_SIZE(10) → a "show more" pagination button, not a cap.
    assert any(cb and cb.startswith("fm_lgpg_") for cb in q.button_callbacks())


async def test_league_cb_shows_pagination_button_for_many_matches(temp_db):
    uid = 811007
    temp_db.db_ensure(uid, "u", "en")
    from event_list import group_by_league
    items = [normalize_fixture(_raw(2000 + j, f"H{j}", f"A{j}", league="Busy",
                                    country="Land", when=_when(24 + j)))
             for j in range(12)]
    groups = group_by_league(items)
    q = _FakeQuery("fm_lg_0", uid)
    await fc.fm_league_cb(_update(q), _ctx(fm_leagues=groups))
    # 12 matches > PAGE_SIZE(10) → a "show more" pagination button, not a cap.
    assert any(cb and cb.startswith("fm_mtpg_") for cb in q.button_callbacks())
    # And ALL 12 matches are reachable (via the frozen full list), never capped.
    assert len(groups[0].items) == 12


# ─── Full day → country → league → match flow, with back navigation ─────────

async def test_full_filter_flow_and_back_navigation(temp_db, monkeypatch):
    uid = 811012
    temp_db.db_ensure(uid, "u", "en")

    async def _noop_odds(mid):
        return {}

    async def _noop_real(t1, t2, hint):
        return ""

    async def _forecast(uid_, content, sys, tok):
        return "OK"

    monkeypatch.setattr(fc, "mostbet_get_odds", _noop_odds)
    monkeypatch.setattr(fc, "fetch_real_data", _noop_real)
    monkeypatch.setattr(fc, "format_mostbet_odds", lambda o, l: "")
    monkeypatch.setattr(fc, "claude_forecast", _forecast)

    # Two day buckets (today + tomorrow) so the day screen is actually shown
    # (a single bucket would auto-skip it); "All days" then keeps all three.
    items = [
        normalize_fixture(_raw(1, "Arsenal", "Chelsea", league="Premier League",
                               country="England", when=_when(6))),
        normalize_fixture(_raw(2, "Liverpool", "Everton", league="Premier League",
                               country="England", when=_when(6))),
        normalize_fixture(_raw(3, "Real Madrid", "Barcelona", league="La Liga",
                               country="Spain", when=_when(30))),
    ]
    ctx = _ctx(fm_sports=[("Football", items)])

    # Every step below is gated by nav_guard (its own nav budget) or cb_guard
    # (the strict text/expensive budget). This test exercises FLOW correctness,
    # not rate-limiting (covered in test_callback_guard.py) — reset both
    # counters before each step so rapid calls for one uid can't trip a limit.
    def _reset_rate():
        msg_times[uid].clear()
        nav_times[uid].clear()

    # 1. Sport → day filter screen.
    _reset_rate()
    q1 = _FakeQuery("fm_sp_0", uid)
    await fc.fm_sport_cb(_update(q1), ctx)
    assert q1.edited == T["en"]["ev_day_title"]
    assert ctx.user_data["fm_sport_items"] == items

    # 2. Day "All" → two countries present → country screen (not skipped).
    _reset_rate()
    q2 = _FakeQuery("fm_day_0", uid)
    await fc.fm_day_cb(_update(q2), ctx)
    assert q2.edited == T["en"]["ev_country_title"]
    assert dict(ctx.user_data["fm_country_options"]) == {"England": 2, "Spain": 1}

    # 3. Country "England" (index 1: first real option after the fixed "All"
    #    at index 0) → league list, scoped to England only.
    _reset_rate()
    q3 = _FakeQuery("fm_ctry_1", uid)
    await fc.fm_ctry_cb(_update(q3), ctx)
    groups = ctx.user_data["fm_leagues"]
    assert [g.league_name for g in groups] == ["Premier League"]
    assert ctx.user_data["fm_league_back"] == "fm_back_country"

    # 4. League → match list (both England matches, Chelsea/Everton fixtures).
    _reset_rate()
    q4 = _FakeQuery("fm_lg_0", uid)
    await fc.fm_league_cb(_update(q4), ctx)
    matches = ctx.user_data["fm_matches"]
    assert {m.fixture_id for m in matches} == {"1", "2"}

    # 5. Match → resolves by absolute index against the frozen snapshot.
    _reset_rate()
    q5 = _FakeQuery("fm_mt_1", uid)
    await fc.fm_match_cb(_update(q5), ctx)
    assert ctx.user_data["match_ref"]["home"] == "Liverpool"

    # 6. Back from the match screen → league list, with the RIGHT back target
    #    (country, since the country screen was actually shown for this sport).
    _reset_rate()
    q6 = _FakeQuery("fm_back_league", uid)
    await fc.fm_back_cb(_update(q6), ctx)
    assert "fm_back_country" in q6.button_callbacks()

    # 7. Back from the league list → country screen.
    _reset_rate()
    q7 = _FakeQuery("fm_back_country", uid)
    await fc.fm_back_cb(_update(q7), ctx)
    assert q7.edited == T["en"]["ev_country_title"]

    # 8. Back from the country screen → day screen.
    _reset_rate()
    q8 = _FakeQuery("fm_back_day", uid)
    await fc.fm_back_cb(_update(q8), ctx)
    assert q8.edited == T["en"]["ev_day_title"]

    # 9. Back from the day screen → sport screen.
    _reset_rate()
    q9 = _FakeQuery("fm_back_sport", uid)
    await fc.fm_back_cb(_update(q9), ctx)
    assert q9.edited == fc._SPORT_TITLE["en"]


async def test_single_country_skips_country_screen(temp_db):
    """When every match in the (sport, day) scope shares one country, the
    country screen must be skipped entirely — nothing to choose."""
    uid = 811013
    temp_db.db_ensure(uid, "u", "en")
    items = [
        normalize_fixture(_raw(1, "Arsenal", "Chelsea", league="Premier League", country="England")),
        normalize_fixture(_raw(2, "Liverpool", "Everton", league="Championship", country="England")),
    ]
    ctx = _ctx(fm_sport_items=items, fm_day_options=[], fm_sports=[("Football", items)])
    q = _FakeQuery("fm_day_0", uid)
    await fc.fm_day_cb(_update(q), ctx)
    # Straight to the league list — never the country title.
    assert q.edited != T["en"]["ev_country_title"]
    assert ctx.user_data["fm_league_back"] == "fm_back_day"


# ─── Block A fixes: dead-end screen, malformed callbacks, sport pagination,
# country back-navigation page ────────────────────────────────────────────────

async def test_empty_filter_result_still_has_a_back_button(temp_db):
    """A day+country combination that yields zero matches must never leave
    the user on a screen with no buttons at all."""
    uid = 811014
    temp_db.db_ensure(uid, "u", "en")
    ctx = _ctx(fm_sports=[("Football", [])], fm_sport_idx=0,
               fm_now_utc=None, fm_demand=None)
    q = _FakeQuery("x", uid)
    await fc._show_league_list(q, ctx, uid, [], back_cb="fm_back_day")
    assert T["en"]["ev_filter_empty"] in q.edited
    assert "fm_back_day" in q.button_callbacks()


async def test_malformed_callback_data_degrades_to_expired_menu(temp_db):
    """A hand-crafted/replayed callback_data with a non-numeric index must
    never crash the handler — it degrades like an out-of-range index."""
    uid = 811015
    temp_db.db_ensure(uid, "u", "en")

    q1 = _FakeQuery("fm_day_x", uid)
    await fc.fm_day_cb(_update(q1), _ctx(fm_sport_items=[1], fm_day_options=[]))
    assert q1.edited == T["en"]["ev_menu_expired"]

    q2 = _FakeQuery("fm_ctry_x", uid)
    await fc.fm_ctry_cb(_update(q2), _ctx(fm_day_filtered=[1], fm_country_options=[]))
    assert q2.edited == T["en"]["ev_menu_expired"]

    q3 = _FakeQuery("fm_ctrypg_x", uid)
    await fc.fm_ctrypg_cb(_update(q3), _ctx(fm_country_options=[("England", 1)]))
    assert q3.edited == T["en"]["ev_menu_expired"]

    q4 = _FakeQuery("fm_lgpg_x", uid)
    await fc.fm_lgpg_cb(_update(q4), _ctx(fm_leagues=["x"]))
    assert q4.edited == T["en"]["ev_menu_expired"]

    q5 = _FakeQuery("fm_mtpg_x", uid)
    await fc.fm_mtpg_cb(_update(q5), _ctx(fm_matches=["x"]))
    assert q5.edited == T["en"]["ev_menu_expired"]

    q6 = _FakeQuery("fm_sppg_x", uid)
    await fc.fm_sppg_cb(_update(q6), _ctx(fm_sports=[("Football", [])]))
    assert q6.edited == T["en"]["ev_menu_expired"]

    # The three ORIGINAL (pre-existing) handlers must degrade the same way —
    # found by the Codex audit: they parsed the index with a bare int() and
    # had no guard at all for malformed callback_data.
    q7 = _FakeQuery("fm_sp_x", uid)
    await fc.fm_sport_cb(_update(q7), _ctx(fm_sports=[("Football", [1])]))
    assert q7.edited == T["en"]["ev_menu_expired"]

    q8 = _FakeQuery("fm_lg_x", uid)
    await fc.fm_league_cb(_update(q8), _ctx(fm_leagues=["x"]))
    assert q8.edited == T["en"]["ev_menu_expired"]

    q9 = _FakeQuery("fm_mt_x", uid)
    await fc.fm_match_cb(_update(q9), _ctx(fm_matches=["x"]))
    assert q9.edited == T["en"]["ev_menu_expired"]


async def test_negative_index_never_resolves_via_python_wraparound(temp_db):
    """A negative index (e.g. from a hand-crafted callback) must degrade to
    the expired-menu path, never silently resolve to the LAST item via
    Python's negative-indexing semantics — found by the Codex audit."""
    uid = 811018
    temp_db.db_ensure(uid, "u", "en")
    items = [normalize_fixture(_raw(1, "Arsenal", "Chelsea")),
             normalize_fixture(_raw(2, "Liverpool", "Everton"))]

    q1 = _FakeQuery("fm_sp_-1", uid)
    await fc.fm_sport_cb(_update(q1), _ctx(fm_sports=[("Football", items)]))
    assert q1.edited == T["en"]["ev_menu_expired"]

    q2 = _FakeQuery("fm_mt_-1", uid)
    await fc.fm_match_cb(_update(q2), _ctx(fm_matches=items))
    assert q2.edited == T["en"]["ev_menu_expired"]

    day_options = [(fc.DAY_TODAY, 1)]
    q3 = _FakeQuery("fm_day_-1", uid)
    await fc.fm_day_cb(_update(q3), _ctx(fm_sport_items=items, fm_day_options=day_options))
    assert q3.edited == T["en"]["ev_menu_expired"]

    country_options = [("England", 2)]
    q4 = _FakeQuery("fm_ctry_-1", uid)
    await fc.fm_ctry_cb(_update(q4), _ctx(fm_day_filtered=items, fm_country_options=country_options))
    assert q4.edited == T["en"]["ev_menu_expired"]

    q5 = _FakeQuery("fm_lg_-1", uid)
    await fc.fm_league_cb(_update(q5), _ctx(fm_leagues=["x", "y"]))
    assert q5.edited == T["en"]["ev_menu_expired"]


async def test_stale_pagination_page_renders_consistent_absolute_indices(temp_db):
    """A stale/out-of-range page number (e.g. fm_sppg_99 sent after the list
    shrank) must clamp to the last real page AND use that clamped page for
    the rendered buttons' absolute indices — not the raw stale page number
    (the offset/nav bug found by the Codex audit)."""
    uid = 811019
    temp_db.db_ensure(uid, "u", "en")
    sport_groups = [(f"Sport{i}", [1]) for i in range(12)]  # 2 pages at size 10
    ctx = _ctx(fm_sports=sport_groups)

    q = _FakeQuery("fm_sppg_99", uid)
    await fc.fm_sppg_cb(_update(q), ctx)
    # Clamps to the real last page (page 1: absolute indices 10..11) and the
    # STORED page must match — never the raw stale 99.
    assert ctx.user_data["fm_sport_page"] == 1
    callbacks = [cb for cb in q.button_callbacks() if cb and cb.startswith("fm_sp_")]
    # …but the rendered buttons must reference REAL, in-range absolute
    # indices (10, 11), never fm_sp_990/fm_sp_991-style garbage.
    assert callbacks == ["fm_sp_10", "fm_sp_11"]
    for cb in callbacks:
        idx = int(cb.split("_")[2])
        assert 0 <= idx < len(sport_groups)


async def test_stale_pagination_page_renders_consistent_indices_leagues(temp_db):
    """Same offset/nav-clamping guarantee as the sport list, for the league
    keyboard — the Codex audit noted this was only exercised for sports."""
    from event_list import LeagueGroup

    uid = 811020
    temp_db.db_ensure(uid, "u", "en")
    groups = [LeagueGroup(f"key{i}", f"League{i}", "England") for i in range(12)]

    ctx = _ctx(fm_sports=[("Football", [])], fm_leagues=groups)
    q = _FakeQuery("fm_lgpg_99", uid)
    await fc.fm_lgpg_cb(_update(q), ctx)
    assert ctx.user_data["fm_league_page"] == 1
    callbacks = [cb for cb in q.button_callbacks() if cb and cb.startswith("fm_lg_")]
    assert callbacks == ["fm_lg_10", "fm_lg_11"]
    for cb in callbacks:
        idx = int(cb.split("_")[2])
        assert 0 <= idx < len(groups)


async def test_stale_pagination_page_renders_consistent_indices_matches(temp_db):
    """Same guarantee for the match keyboard."""
    uid = 811021
    temp_db.db_ensure(uid, "u", "en")
    matches = [normalize_fixture(_raw(i, f"H{i}", f"A{i}")) for i in range(12)]

    ctx = _ctx(fm_matches=matches, fm_leagues=[])
    q = _FakeQuery("fm_mtpg_99", uid)
    await fc.fm_mtpg_cb(_update(q), ctx)
    assert ctx.user_data["fm_match_page"] == 1
    callbacks = [cb for cb in q.button_callbacks() if cb and cb.startswith("fm_mt_")]
    assert callbacks == ["fm_mt_10", "fm_mt_11"]
    for cb in callbacks:
        idx = int(cb.split("_")[2])
        assert 0 <= idx < len(matches)


async def test_stale_pagination_page_country_all_button_and_indices(temp_db):
    """The country keyboard's "All" shortcut must only appear on the REAL
    (clamped) first page — a stale fm_ctrypg_99 must not resurrect it, and
    the option buttons must carry real, in-range absolute indices."""
    uid = 811022
    temp_db.db_ensure(uid, "u", "en")
    country_options = [(f"Country{i}", 1) for i in range(12)]

    ctx = _ctx(fm_country_options=country_options)
    q = _FakeQuery("fm_ctrypg_99", uid)
    await fc.fm_ctrypg_cb(_update(q), ctx)
    assert ctx.user_data["fm_country_page"] == 1
    callbacks = [cb for cb in q.button_callbacks() if cb]
    # Clamped to the real last page (index 1) — no "All" shortcut (fm_ctry_0)
    # since that only belongs on page 0.
    assert "fm_ctry_0" not in callbacks
    opt_callbacks = [cb for cb in callbacks if cb.startswith("fm_ctry_")]
    assert opt_callbacks == ["fm_ctry_11", "fm_ctry_12"]
    for cb in opt_callbacks:
        idx = int(cb.split("_")[2])
        assert 1 <= idx <= len(country_options)


async def test_sport_list_paginates_beyond_one_page(temp_db):
    uid = 811016
    temp_db.db_ensure(uid, "u", "en")
    sport_groups = [(f"Sport{i}", [1]) for i in range(12)]
    ctx = _ctx(fm_sports=sport_groups)
    # Rendering the sport keyboard directly (as forecast_menu_start would).
    kb = fc._build_sport_kb(sport_groups, 0, uid)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any(cb.startswith("fm_sppg_") for cb in callbacks)

    q2 = _FakeQuery("fm_sppg_1", uid)
    await fc.fm_sppg_cb(_update(q2), ctx)
    assert ctx.user_data["fm_sport_page"] == 1
    assert any(cb and cb.startswith("fm_sp_1") for cb in q2.button_callbacks())


async def test_back_to_country_preserves_pagination_page(temp_db):
    uid = 811017
    temp_db.db_ensure(uid, "u", "en")
    country_options = [(f"Country{i}", 1) for i in range(15)]
    ctx = _ctx(fm_country_options=country_options)

    # Paginate to page 1 first.
    q1 = _FakeQuery("fm_ctrypg_1", uid)
    await fc.fm_ctrypg_cb(_update(q1), ctx)
    assert ctx.user_data["fm_country_page"] == 1

    # Back from a deeper screen must return to the SAME page, not page 0.
    q2 = _FakeQuery("fm_back_country", uid)
    await fc.fm_back_cb(_update(q2), ctx)
    assert q2.edited == T["en"]["ev_country_title"]
    assert any(cb and cb.startswith("fm_ctrypg_") for cb in q2.button_callbacks())
    # Page-1 content: Country10.. shown, not Country0.
    assert any("Country10" in getattr(btn, "text", "") for row in q2.markup.inline_keyboard for btn in row)


async def test_menu_shows_match_five_days_ahead(temp_db, monkeypatch):
    """Regression for the World Cup report: a fixture days ahead (e.g. the
    final) must appear in the menu — the old today/tomorrow-only window hid it
    while the bot happily forecasts 7 days out."""
    uid = 811011
    temp_db.db_ensure(uid, "u", "en")

    async def _load():
        return [_raw(1, "France", "Spain", league="Play-off",
                     country="World Cup 2026", when=_when(5 * 24))]

    monkeypatch.setattr(fc, "_mostbet_load_matches", _load)
    ctx = _ctx()
    msg = _FakeMsg()
    update = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid),
        message=types.SimpleNamespace(reply_text=lambda *a, **k: _async_msg(msg)))

    await fc.forecast_menu_start(update, ctx)

    sports = ctx.user_data["fm_sports"]
    assert sports and sports[0][1][0].home == "France"


# ─── Home shortcut + page counter (UX) ────────────────────────────────────────

def test_match_kb_has_home_shortcut_and_page_counter(temp_db):
    """Deep screens carry a "🏠 to start" shortcut (callback fm_back_sport) and,
    when the list spans multiple pages, a read-only "page X / Y" counter
    (callback fm_noop)."""
    temp_db.db_ensure(830001, "u", "en")
    matches = [normalize_fixture(_raw(i, f"H{i}", f"A{i}")) for i in range(12)]  # 2 pages
    kb = fc._build_match_kb(matches, 0, 830001)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "fm_back_sport" in cbs       # home shortcut
    assert "fm_back_league" in cbs      # one-step back
    assert "fm_noop" in cbs             # page counter
    counter = [b.text for row in kb.inline_keyboard for b in row if b.callback_data == "fm_noop"]
    assert counter == ["1 / 2"]


def test_single_page_has_no_counter(temp_db):
    temp_db.db_ensure(830002, "u", "en")
    matches = [normalize_fixture(_raw(i, f"H{i}", f"A{i}")) for i in range(3)]  # 1 page
    kb = fc._build_match_kb(matches, 0, 830002)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "fm_noop" not in cbs


def test_home_shortcut_deduped_when_back_is_already_sport(temp_db):
    """If the league list's own back already targets the sport list, the home
    shortcut would be identical — show only one button."""
    from event_list import LeagueGroup
    temp_db.db_ensure(830003, "u", "en")
    groups = [LeagueGroup(f"k{i}", f"League{i}", "England") for i in range(2)]
    bottom = fc._build_league_kb(groups, 0, "fm_back_sport", 830003).inline_keyboard[-1]
    assert [b.callback_data for b in bottom] == ["fm_back_sport"]


async def test_more_matches_button_reopens_menu(temp_db, monkeypatch):
    """The "📋 More matches" button (fm_restart) posts a fresh loading message
    and rebuilds a new event-list session, so the user gets back into the menu
    without re-typing."""
    uid = 830040
    temp_db.db_ensure(uid, "u", "en")

    async def _load():
        return [_raw(1, "Arsenal", "Chelsea")]

    monkeypatch.setattr(fc, "_mostbet_load_matches", _load)
    bot = _FakeBot()
    ctx = _ctx(bot)
    q = _FakeQuery("fm_restart", uid)
    await fc.fm_restart_cb(_update(q), ctx)

    assert bot.sent                       # a new loading message was posted
    assert ctx.user_data.get("fm_sports") # a fresh menu session was built
