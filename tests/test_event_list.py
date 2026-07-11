"""Offline tests for the clean event list: normalization, identity, filtering,
status precedence, timezone bucketing, dedup, sorting, pagination. No network."""
from datetime import datetime, timedelta, timezone


import event_list as el
from event_list import (
    EventItem, FINISHED_GRACE, MAX_LEAGUES, MAX_MATCHES_PER_LEAGUE,
    group_by_league, league_rank, normalize_fixture, parse_kickoff_utc,
    select_visible, visible_bucket,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


def _raw(fid=1, t1="Arsenal", t2="Chelsea", league="Premier League",
         country="England", when="12.07.2026 18:00:00", live=False, **extra):
    m = {"id": fid, "team1Title": t1, "team2Title": t2, "lineCategory": "Football",
         "lineSubCategory": league, "lineSuperCategory": country,
         "matchBeginAt": when, "isLive": live}
    m.update(extra)
    return m


# ─── Kickoff parsing → UTC ────────────────────────────────────────────────────

def test_mostbet_time_parsed_to_utc():
    # 18:00 in UTC+3 (MOSTBET_SRC_TZ) → 15:00 UTC.
    dt = parse_kickoff_utc("12.07.2026 18:00:00")
    assert dt == datetime(2026, 7, 12, 15, 0, tzinfo=UTC)
    assert dt.tzinfo is not None


def test_iso_time_parsed_to_utc():
    assert parse_kickoff_utc("2026-07-12T15:00:00Z") == datetime(2026, 7, 12, 15, 0, tzinfo=UTC)


def test_bad_time_returns_none():
    assert parse_kickoff_utc("nonsense") is None
    assert parse_kickoff_utc("") is None


# ─── Identity ─────────────────────────────────────────────────────────────────

def test_fixture_id_is_authoritative_derived_keys_not_ids():
    it = normalize_fixture(_raw(fid=555))
    assert it.fixture_id == "555"
    assert it.fixture_id_source == "provider"
    # No native team/league ids in the Mostbet feed → nullable, NOT fabricated.
    assert it.league_id is None
    assert it.home_team_id is None
    assert it.away_team_id is None
    assert it.team_identity_source == "derived_name_key"
    assert it.league_identity_source == "derived_name_key"
    # Derived keys exist but are clearly separate from provider ids.
    assert it.home_team_key and it.league_key
    assert it.home_team_key != it.home_team_id


def test_native_ids_used_when_present():
    it = normalize_fixture(_raw(team1Id=10, team2Id=20, tournamentId=39))
    assert it.home_team_id == "10"
    assert it.away_team_id == "20"
    assert it.league_id == "39"
    assert it.team_identity_source == "provider"
    assert it.league_identity_source == "provider"


def test_reject_missing_fixture_id_or_teams_or_league():
    assert normalize_fixture(_raw(fid=None)) is None
    assert normalize_fixture({**_raw(), "team2Title": ""}) is None
    assert normalize_fixture({**_raw(), "lineSubCategory": ""}) is None


def test_reject_nonlive_without_valid_kickoff():
    assert normalize_fixture(_raw(when="")) is None


def test_live_without_kickoff_allowed():
    it = normalize_fixture(_raw(when="", live=True))
    assert it is not None and it.is_live and it.kickoff_utc is None


def test_virtual_and_outright_rejected():
    assert normalize_fixture(_raw(t1="Arsenal (FC 25)", t2="Chelsea (FC 25)")) is None
    assert normalize_fixture(_raw(t2="?")) is None


# ─── League priority ──────────────────────────────────────────────────────────

def test_league_priority_order():
    assert league_rank("Champions League", "Europe") == 0
    assert league_rank("Europa League", "Europe") == 1
    assert league_rank("Premier League", "England") < league_rank("Premier League", "Azerbaijan")
    assert league_rank("Some Random Cup", "Nowhere") == len(el._LEAGUE_PRIORITY)


def test_english_and_azerbaijan_premier_disambiguated():
    # Both are "Premier League" — country decides which ranks higher.
    assert league_rank("Premier League", "England") == 5
    assert league_rank("Premier League", "Azerbaijan") == 11


# ─── Status precedence & filtering ────────────────────────────────────────────

def _item(**kw):
    base = dict(fixture_id="1", provider="mostbet", home="A", away="B",
                league_name="L", country="C", kickoff_utc=NOW, is_live=False,
                status=None, sport="Football", league_key="c-l",
                home_team_key="a", away_team_key="b")
    base.update(kw)
    return EventItem(**base)


def test_explicit_finished_beats_kickoff():
    # Kickoff is now (would otherwise be TODAY) but status says finished → hidden.
    assert visible_bucket(_item(status="finished", kickoff_utc=NOW), NOW, UTC) is None


def test_live_flag_keeps_live_bucket():
    assert visible_bucket(_item(is_live=True, kickoff_utc=None), NOW, UTC) == el.LIVE


def test_nonlive_past_no_status_removed_after_grace():
    stale = NOW - FINISHED_GRACE - timedelta(minutes=1)
    assert visible_bucket(_item(kickoff_utc=stale), NOW, UTC) is None
    # Just inside the grace window it is still shown.
    fresh = NOW - FINISHED_GRACE + timedelta(minutes=1)
    assert visible_bucket(_item(kickoff_utc=fresh), NOW, UTC) == el.TODAY


def test_postponed_kept_and_flagged():
    it = _item(status="postponed", kickoff_utc=NOW + timedelta(hours=2))
    assert visible_bucket(it, NOW, UTC) == el.TODAY
    assert it.postponed is True


def test_later_excluded_by_default_included_on_request():
    it = _item(kickoff_utc=NOW + timedelta(days=3))
    assert visible_bucket(it, NOW, UTC) is None
    assert visible_bucket(it, NOW, UTC, include_later=True) == el.LATER


# ─── Timezone bucketing ───────────────────────────────────────────────────────

def test_today_tomorrow_depends_on_user_tz():
    # Kickoff 23:30 UTC. For UTC-5 it's 18:30 same day (TODAY); for UTC+3 it's
    # 02:30 next day (TOMORROW).
    ko = datetime(2026, 7, 12, 23, 30, tzinfo=UTC)
    west = timezone(timedelta(hours=-5))
    east = timezone(timedelta(hours=3))
    assert visible_bucket(_item(kickoff_utc=ko), NOW, west) == el.TODAY
    assert visible_bucket(_item(kickoff_utc=ko), NOW, east) == el.TOMORROW


def test_live_bucketing_across_timezones():
    # A live fixture is LIVE regardless of the user's timezone.
    it_w = _item(is_live=True, kickoff_utc=None)
    it_e = _item(is_live=True, kickoff_utc=None)
    assert visible_bucket(it_w, NOW, timezone(timedelta(hours=-8))) == el.LIVE
    assert visible_bucket(it_e, NOW, timezone(timedelta(hours=9))) == el.LIVE


# ─── Dedup ────────────────────────────────────────────────────────────────────

def test_duplicate_fixture_ids_collapsed():
    items = [normalize_fixture(_raw(fid=1)), normalize_fixture(_raw(fid=1))]
    out = select_visible(items, NOW, UTC)
    assert len(out) == 1


def test_duplicate_composite_different_fixture_ids_collapsed():
    # Same teams + kickoff, different line ids (e.g. two market lines).
    items = [normalize_fixture(_raw(fid=1)), normalize_fixture(_raw(fid=2))]
    out = select_visible(items, NOW, UTC)
    assert len(out) == 1
    assert out[0].fixture_id == "1"  # first wins


def test_distinct_matches_not_collapsed():
    a = normalize_fixture(_raw(fid=1, when="12.07.2026 18:00:00"))
    b = normalize_fixture(_raw(fid=2, t1="Liverpool", t2="Everton",
                               when="12.07.2026 20:00:00"))
    assert len(select_visible([a, b], NOW, UTC)) == 2


# ─── Sorting & pagination ─────────────────────────────────────────────────────

def test_matches_sorted_by_kickoff_live_first():
    raws = [
        _raw(fid=1, t1="C", t2="D", when="12.07.2026 20:00:00"),
        _raw(fid=2, t1="E", t2="F", when="12.07.2026 16:00:00"),
        _raw(fid=3, t1="G", t2="H", when="", live=True),
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC)
    groups, _ = group_by_league(items)
    ordered = groups[0].items
    assert ordered[0].is_live                       # live first
    assert ordered[1].kickoff_utc < ordered[2].kickoff_utc  # then ascending


def test_leagues_sorted_by_priority():
    raws = [
        _raw(fid=1, league="Some Local League", country="Nowhere"),
        _raw(fid=2, t1="X", t2="Y", league="Champions League", country="Europe"),
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC)
    groups, _ = group_by_league(items)
    assert groups[0].league_name == "Champions League"


def test_pagination_caps_and_flags_truncation():
    # 16 leagues, and one league with 12 matches → both truncated + flagged.
    raws = []
    for i in range(16):
        raws.append(_raw(fid=1000 + i, t1=f"T{i}a", t2=f"T{i}b",
                         league=f"League {i:02d}", country=f"Country{i}"))
    for j in range(12):
        # All later today (20:00–20:11 local = future vs NOW), 12 distinct kickoffs.
        raws.append(_raw(fid=2000 + j, t1=f"H{j}", t2=f"A{j}",
                         league="Busy League", country="Busyland",
                         when=f"12.07.2026 20:{j:02d}:00"))
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC)
    groups, leagues_truncated = group_by_league(items)
    assert leagues_truncated is True
    assert len(groups) == MAX_LEAGUES
    busy = next(g for g in groups if g.league_name == "Busy League")
    assert len(busy.items) == MAX_MATCHES_PER_LEAGUE
    assert busy.truncated is True


def test_every_visible_item_carries_identity_fields():
    items = select_visible([normalize_fixture(_raw(fid=7))], NOW, UTC)
    it = items[0]
    for attr in ("fixture_id", "league_key", "home_team_key", "away_team_key",
                 "fixture_id_source", "team_identity_source", "league_identity_source"):
        assert getattr(it, attr), attr
