"""Offline tests for event-menu callback safety: callbacks resolve only against
the frozen snapshot, missing/stale indexes yield the expired-menu state, an
already-rendered index cannot be re-pointed by refreshed data, and truncation is
surfaced to the user. No network."""
import types
from datetime import datetime, timedelta, timezone

import handlers.forecast as fc
from config import MOSTBET_SRC_TZ
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

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.edited = text


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


# ─── Truncation visible to the user ───────────────────────────────────────────

async def test_sport_cb_flags_more_leagues(temp_db):
    uid = 811006
    temp_db.db_ensure(uid, "u", "en")
    items = [normalize_fixture(_raw(1000 + i, f"T{i}a", f"T{i}b",
                                    league=f"League {i:02d}", country=f"C{i}"))
             for i in range(16)]
    q = _FakeQuery("fm_sp_0", uid)
    await fc.fm_sport_cb(_update(q), _ctx(fm_sports=[("Football", items)]))
    assert T["en"]["ev_more_leagues"] in q.edited


async def test_league_cb_flags_more_matches(temp_db):
    uid = 811007
    temp_db.db_ensure(uid, "u", "en")
    from event_list import group_by_league
    items = [normalize_fixture(_raw(2000 + j, f"H{j}", f"A{j}", league="Busy",
                                    country="Land", when=_when(24 + j)))
             for j in range(12)]
    groups, _ = group_by_league(items)
    q = _FakeQuery("fm_lg_0", uid)
    await fc.fm_league_cb(_update(q), _ctx(fm_leagues=groups))
    assert T["en"]["ev_more_matches"] in q.edited


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
