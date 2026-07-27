"""Offline tests for the clean event list: normalization, identity, filtering,
status precedence, timezone bucketing, dedup, sorting, pagination. No network."""
from datetime import datetime, timedelta, timezone


import event_list as el
from event_list import (
    EventItem, FINISHED_GRACE,
    group_by_league, league_rank, normalize_fixture, parse_kickoff_utc,
    select_visible, visible_bucket,
    paginate, available_day_options, filter_by_day, DAY_LIVE, DAY_TODAY, DAY_TOMORROW, DAY_ALL,
    available_countries, filter_by_country, COUNTRY_ALL, COUNTRY_INTERNATIONAL,
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


def test_iso_time_with_offset_parsed_to_utc():
    # +02:00 → normalized back to UTC.
    assert parse_kickoff_utc("2026-07-12T17:00:00+02:00") == datetime(2026, 7, 12, 15, 0, tzinfo=UTC)


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
    # An empty subcategory no longer drops the row — it falls back to the
    # super category (World Cup regression, see below); only a row with NO
    # league information at all is rejected.
    assert normalize_fixture({**_raw(), "lineSubCategory": "",
                              "lineSuperCategory": ""}) is None


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


def test_other_domestic_premier_leagues_not_given_english_priority():
    # A country with no explicit entry must NOT inherit England's PL rank.
    assert league_rank("Premier League", "Egypt") == len(el._LEAGUE_PRIORITY)
    assert league_rank("Premier League", "Egypt") != 5


def test_conference_league_not_matched_as_europa():
    # Old and new names both resolve to Conference (rank 2), never Europa (1).
    assert league_rank("UEFA Europa Conference League", "Europe") == 2
    assert league_rank("UEFA Conference League", "Europe") == 2
    assert league_rank("UEFA Europa League", "Europe") == 1


def test_super_lig_matched_with_diacritic():
    assert league_rank("Süper Lig", "Turkey") == 10


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


def test_live_stays_visible_even_when_kickoff_older_than_grace():
    # A live match that kicked off hours ago (past the grace window) is LIVE, not
    # dropped — the live flag/status wins over the kickoff-grace fallback.
    old = NOW - FINISHED_GRACE - timedelta(hours=2)
    assert visible_bucket(_item(is_live=True, kickoff_utc=old), NOW, UTC) == el.LIVE
    assert visible_bucket(_item(status="live", is_live=False, kickoff_utc=old), NOW, UTC) == el.LIVE


def test_cancelled_and_abandoned_excluded():
    assert visible_bucket(_item(status="cancelled", kickoff_utc=NOW), NOW, UTC) is None
    assert visible_bucket(_item(status="abandoned", kickoff_utc=NOW), NOW, UTC) is None


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


def test_same_teams_two_competitions_not_collapsed():
    # Same teams, same kickoff, DIFFERENT competition → distinct fixtures.
    a = normalize_fixture(_raw(fid=1, league="Premier League", country="England"))
    b = normalize_fixture(_raw(fid=2, league="FA Cup", country="England"))
    assert len(select_visible([a, b], NOW, UTC)) == 2


def test_two_legged_tie_different_dates_not_collapsed():
    a = normalize_fixture(_raw(fid=1, league="Champions League", country="Europe",
                               when="12.07.2026 20:00:00"))
    b = normalize_fixture(_raw(fid=2, league="Champions League", country="Europe",
                               when="13.07.2026 20:00:00"))
    assert len(select_visible([a, b], NOW, UTC)) == 2


def test_women_and_senior_not_collapsed():
    a = normalize_fixture(_raw(fid=1, t1="Arsenal", t2="Chelsea"))
    b = normalize_fixture(_raw(fid=2, t1="Arsenal W", t2="Chelsea W"))
    assert len(select_visible([a, b], NOW, UTC)) == 2


def test_reserve_and_senior_not_collapsed():
    a = normalize_fixture(_raw(fid=1, t1="Barcelona", t2="Sevilla"))
    b = normalize_fixture(_raw(fid=2, t1="Barcelona B", t2="Sevilla"))
    assert len(select_visible([a, b], NOW, UTC)) == 2


# ─── Sorting & pagination ─────────────────────────────────────────────────────

def test_matches_sorted_by_kickoff_live_first():
    raws = [
        _raw(fid=1, t1="C", t2="D", when="12.07.2026 20:00:00"),
        _raw(fid=2, t1="E", t2="F", when="12.07.2026 16:00:00"),
        _raw(fid=3, t1="G", t2="H", when="", live=True),
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC)
    groups = group_by_league(items)
    ordered = groups[0].items
    assert ordered[0].is_live                       # live first
    assert ordered[1].kickoff_utc < ordered[2].kickoff_utc  # then ascending


def test_leagues_sorted_by_priority():
    # Neutral (non-popular, non-derby) team names on both sides: this test is
    # about tournament prestige specifically, not team popularity/derby.
    raws = [
        _raw(fid=1, t1="P", t2="Q", league="Some Local League", country="Nowhere"),
        _raw(fid=2, t1="X", t2="Y", league="Champions League", country="Europe"),
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC)
    groups = group_by_league(items)
    assert groups[0].league_name == "Champions League"


def test_group_by_league_returns_all_leagues_and_matches_uncapped():
    """group_by_league no longer hard-truncates — pagination for the Telegram
    UI happens in the handler layer via `paginate`, not here."""
    raws = []
    for i in range(16):
        raws.append(_raw(fid=1000 + i, t1=f"T{i}a", t2=f"T{i}b",
                         league=f"League {i:02d}", country=f"Country{i}"))
    for j in range(12):
        raws.append(_raw(fid=2000 + j, t1=f"H{j}", t2=f"A{j}",
                         league="Busy League", country="Busyland",
                         when="12.07.2026 18:00:00"))
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC)
    groups = group_by_league(items)
    assert len(groups) == 17
    busy = next(g for g in groups if g.league_name == "Busy League")
    assert len(busy.items) == 12


# ─── Pagination ("show more") ───────────────────────────────────────────────

def test_paginate_slices_and_reports_prev_next():
    seq = list(range(25))
    page0, clamped0, has_prev0, has_next0 = paginate(seq, 0, page_size=10)
    assert page0 == list(range(10))
    assert clamped0 == 0
    assert has_prev0 is False and has_next0 is True

    page1, clamped1, has_prev1, has_next1 = paginate(seq, 1, page_size=10)
    assert page1 == list(range(10, 20))
    assert clamped1 == 1
    assert has_prev1 is True and has_next1 is True

    page2, clamped2, has_prev2, has_next2 = paginate(seq, 2, page_size=10)
    assert page2 == list(range(20, 25))
    assert clamped2 == 2
    assert has_prev2 is True and has_next2 is False


def test_paginate_clamps_out_of_range_page():
    """The returned clamped page must be usable by the caller for offset/nav
    math — the whole point of returning it is that an out-of-range `page`
    (e.g. a stale/replayed callback) never leaks into absolute-index math."""
    seq = list(range(5))
    page, clamped, has_prev, has_next = paginate(seq, 99, page_size=10)
    assert page == seq
    assert clamped == 0  # only one page exists — 99 clamps down to it
    assert has_prev is False and has_next is False


def test_paginate_negative_page_clamps_to_zero():
    seq = list(range(25))
    page, clamped, has_prev, has_next = paginate(seq, -5, page_size=10)
    assert page == list(range(10))
    assert clamped == 0
    assert has_prev is False and has_next is True


def test_paginate_empty_sequence():
    assert paginate([], 0) == ([], 0, False, False)


# ─── Day filter ─────────────────────────────────────────────────────────────

def test_available_day_options_reflects_present_buckets():
    raws = [
        _raw(fid=1, when="", live=True),
        _raw(fid=2, when="12.07.2026 18:00:00"),           # TODAY
        _raw(fid=3, when="13.07.2026 12:00:00"),            # TOMORROW
        _raw(fid=4, t1="X", t2="Y", when="16.07.2026 12:00:00"),  # LATER, specific date
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC, include_later=True)
    options = available_day_options(items, UTC)
    keys = [k for k, _ in options]
    assert keys[0] == DAY_LIVE
    assert DAY_TODAY in keys and DAY_TOMORROW in keys
    assert "2026-07-16" in keys


def test_filter_by_day_restricts_to_one_bucket():
    raws = [
        _raw(fid=1, when="", live=True),
        _raw(fid=2, when="12.07.2026 18:00:00"),
        _raw(fid=3, when="13.07.2026 12:00:00"),
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC, include_later=True)
    today_only = filter_by_day(items, DAY_TODAY, UTC)
    assert len(today_only) == 1 and today_only[0].fixture_id == "2"
    assert filter_by_day(items, DAY_ALL, UTC) == items


def test_filter_by_day_specific_date():
    raws = [
        _raw(fid=1, t1="X", t2="Y", when="16.07.2026 12:00:00"),
        _raw(fid=2, t1="P", t2="Q", when="17.07.2026 12:00:00"),
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC, include_later=True)
    day16 = filter_by_day(items, "2026-07-16", UTC)
    assert len(day16) == 1 and day16[0].fixture_id == "1"


# ─── Country filter ─────────────────────────────────────────────────────────

def test_available_countries_counts_and_international_fallback():
    raws = [
        _raw(fid=1, league="Premier League", country="England"),
        _raw(fid=2, t1="X", t2="Y", league="La Liga", country="Spain"),
        _raw(fid=3, t1="P", t2="Q", league="World Cup 2026", country=""),
    ]
    items = [normalize_fixture(r) for r in raws]
    countries = dict(available_countries(items))
    assert countries["England"] == 1
    assert countries["Spain"] == 1
    assert countries[COUNTRY_INTERNATIONAL] == 1


def test_filter_by_country_and_international():
    raws = [
        _raw(fid=1, league="Premier League", country="England"),
        _raw(fid=2, t1="X", t2="Y", league="La Liga", country="Spain"),
        _raw(fid=3, t1="P", t2="Q", league="World Cup 2026", country=""),
    ]
    items = [normalize_fixture(r) for r in raws]
    assert [it.fixture_id for it in filter_by_country(items, "England")] == ["1"]
    assert [it.fixture_id for it in filter_by_country(items, COUNTRY_INTERNATIONAL)] == ["3"]
    assert filter_by_country(items, COUNTRY_ALL) == items


def test_every_visible_item_carries_identity_fields():
    items = select_visible([normalize_fixture(_raw(fid=7))], NOW, UTC)
    it = items[0]
    for attr in ("fixture_id", "league_key", "home_team_key", "away_team_key",
                 "fixture_id_source", "team_identity_source", "league_identity_source"):
        assert getattr(it, attr), attr


# ─── World Cup visibility regressions ─────────────────────────────────────────

def test_empty_subcategory_falls_back_to_super_category():
    """International feeds may carry the tournament only in lineSuperCategory
    with an empty subcategory; such rows must become visible items, not be
    silently dropped (an entire World Cup vanished this way)."""
    it = normalize_fixture(_raw(league="", country="World Cup 2026"))
    assert it is not None
    assert it.league_name == "World Cup 2026"
    assert it.country is None            # no duplicated label
    # And the fallback name still ranks as a top tournament.
    assert league_rank(it.league_name, it.country) < league_rank("Random Cup", None)


def test_both_categories_empty_still_dropped():
    assert normalize_fixture(_raw(league="", country="")) is None


def test_later_window_capped_at_seven_days():
    """include_later shows up to MAX_DAYS_AHEAD (the forecast policy window);
    anything further stays hidden."""
    at_edge = _item(kickoff_utc=NOW + timedelta(days=7))
    beyond = _item(kickoff_utc=NOW + timedelta(days=8))
    assert visible_bucket(at_edge, NOW, UTC, include_later=True) == el.LATER
    assert visible_bucket(beyond, NOW, UTC, include_later=True) is None


def test_select_visible_with_later_includes_midweek_final():
    """A final five days ahead (the reported World Cup case) must be selectable
    when the menu asks for the full window."""
    it = _item(kickoff_utc=NOW + timedelta(days=5))
    kept = select_visible([it], NOW, UTC, include_later=True)
    assert kept and kept[0].bucket == el.LATER


# ─── Match Priority Engine integration ─────────────────────────────────────────

def test_group_by_league_ranks_by_priority_not_alphabet():
    """A big derby in an unlisted/obscure league must outrank an ordinary
    match of a recognized top-5 league — the point of the priority engine is
    that it is NOT just the old league whitelist + alphabet fallback."""
    raws = [
        _raw(fid=1, t1="Burnley", t2="Luton Town",
             league="Premier League", country="England"),
        _raw(fid=2, t1="Arsenal", t2="Tottenham",
             league="Regional Super Cup", country="Nowhere"),
    ]
    items = [normalize_fixture(r) for r in raws]
    groups = group_by_league(items, now_utc=NOW)
    assert groups[0].league_name == "Regional Super Cup"


def test_matches_within_league_ordered_by_priority_then_kickoff():
    raws = [
        _raw(fid=1, t1="Burnley", t2="Luton Town", when="12.07.2026 18:00:00"),
        _raw(fid=2, t1="Arsenal", t2="Tottenham", when="12.07.2026 20:00:00"),
    ]
    items = [normalize_fixture(r) for r in raws]
    groups = group_by_league(items, now_utc=NOW)
    # Same league (Premier League/England default); the derby ranks first
    # even though it kicks off later.
    assert groups[0].items[0].home == "Arsenal"


def test_live_shown_above_prematch_within_same_league():
    raws = [
        _raw(fid=1, t1="Burnley", t2="Luton Town", when="12.07.2026 13:00:00"),
        _raw(fid=2, t1="X", t2="Y", when="", live=True),
    ]
    items = select_visible([normalize_fixture(r) for r in raws], NOW, UTC)
    groups = group_by_league(items, now_utc=NOW)
    assert groups[0].items[0].is_live


def test_priority_sort_is_stable_for_equal_scores():
    """Two matches with identical priority components must still sort in a
    fixed, reproducible order (by normalized league/home/away), never by
    incidental input order."""
    raws = [
        _raw(fid=1, t1="Zeta", t2="Yankee", league="Regional Cup", country="Nowhere"),
        _raw(fid=2, t1="Alpha", t2="Bravo", league="Regional Cup", country="Nowhere"),
    ]
    items_a = [normalize_fixture(r) for r in raws]
    items_b = [normalize_fixture(r) for r in reversed(raws)]
    groups_a = group_by_league(items_a, now_utc=NOW)
    groups_b = group_by_league(items_b, now_utc=NOW)
    assert [it.home for it in groups_a[0].items] == [it.home for it in groups_b[0].items]
    # Deterministic: normalized home "alpha" sorts before "zeta".
    assert groups_a[0].items[0].home == "Alpha"


def test_group_order_deterministic_for_same_name_distinct_tournaments():
    """Two distinct provider tournaments can share an identical display name
    (league_name) while having different tournamentIds, hence different
    league_key. When their best priority score AND normalized league_name
    are equal, the final tie-break must be the absolute league_key — never
    incidental Mostbet feed/dict-insertion order."""
    raws_a = [
        _raw(fid=1, league="Regional Cup", country="Nowhere", tournamentId="200"),
        _raw(fid=2, league="Regional Cup", country="Nowhere", tournamentId="100"),
    ]
    raws_b = list(reversed(raws_a))
    items_a = [normalize_fixture(r) for r in raws_a]
    items_b = [normalize_fixture(r) for r in raws_b]
    groups_a = group_by_league(items_a, now_utc=NOW)
    groups_b = group_by_league(items_b, now_utc=NOW)
    assert [g.league_key for g in groups_a] == [g.league_key for g in groups_b]
    # Deterministic: the lexicographically smaller league_key sorts first.
    assert groups_a[0].league_key < groups_a[1].league_key


def test_priority_score_assigned_after_group_by_league():
    raws = [_raw(fid=1)]
    items = [normalize_fixture(r) for r in raws]
    assert items[0].priority_score is None
    groups = group_by_league(items, now_utc=NOW)
    assert groups[0].items[0].priority_score is not None
    assert 0 <= groups[0].items[0].priority_score <= 100


# ─── Competition / stage separation (post-validation-report fix) ──────────────

def test_world_cup_playoff_splits_into_competition_and_stage():
    """The confirmed real Mostbet shape: subcategory holds ONLY the round
    ("Play-off"), supercategory holds the real competition name."""
    it = normalize_fixture(_raw(league="Play-off", country="World Cup 2026"))
    assert it.league_name == "World Cup 2026"
    assert it.stage_raw == "Play-off"
    assert it.country is None


def test_stage_embedded_in_longer_name_is_not_split():
    """A stage word embedded in a longer subcategory string is NOT parsed
    apart — that heuristic is explicitly out of scope (unverified against a
    live feed). The whole string stays the competition name; stage_raw is
    empty (no false-positive stage points from an unverified pattern)."""
    it = normalize_fixture(_raw(league="Champions League - Semi-final", country="Europe"))
    assert it.league_name == "Champions League - Semi-final"
    assert it.stage_raw == ""


def test_one_tournament_id_different_rounds_group_as_one_competition():
    """The primary fix for round-fragmentation: a stable tournamentId groups
    all rounds of the same competition together regardless of subcategory
    text drift across rounds."""
    semi = normalize_fixture(_raw(fid=1, league="Semi-final", country="Champions League",
                                  tournamentId=777))
    group = normalize_fixture(_raw(fid=2, t1="X", t2="Y", league="Group Stage",
                                   country="Champions League", tournamentId=777))
    assert semi.league_key == group.league_key


def test_different_tournament_ids_same_name_text_do_not_collapse():
    """Two distinct tournaments that happen to share a display name (e.g. two
    unrelated "Regional Cup"s) must not merge just because tournamentId is
    trusted over the name when available."""
    a = normalize_fixture(_raw(fid=1, league="Regional Cup", country="Nowhere",
                               tournamentId=111))
    b = normalize_fixture(_raw(fid=2, t1="X", t2="Y", league="Regional Cup", country="Nowhere",
                               tournamentId=222))
    assert a.league_key != b.league_key


def test_stage_raw_never_participates_in_league_key():
    with_stage = normalize_fixture(_raw(fid=1, league="Play-off", country="World Cup 2026"))
    without_stage = normalize_fixture(_raw(fid=2, t1="X", t2="Y", league="World Cup 2026",
                                           country=""))
    assert with_stage.league_key == without_stage.league_key


def test_stage_raw_affects_tournament_stage_not_league_key():
    # Same neutral (non-popular, non-derby) teams on both sides, so the score
    # difference can only come from the stage component being isolated.
    from event_list import assign_priority_scores
    final = normalize_fixture(_raw(fid=1, t1="X", t2="Y", league="Final",
                                   country="Copa Libertadores"))
    no_stage = normalize_fixture(_raw(fid=2, t1="X", t2="Y", league="Copa Libertadores",
                                      country=""))
    assign_priority_scores([final, no_stage], NOW)
    assert final.league_key == no_stage.league_key
    # The final-stage match must score higher (stage component), everything
    # else being equal (same competition, same default kickoff/teams tier).
    assert final.priority_score > no_stage.priority_score


def test_competition_name_affects_prestige():
    from priority_engine import compute_priority, PriorityInput
    elite = compute_priority(PriorityInput(
        league_name="Champions League", country="Europe", home="A", away="B",
        is_live=False, kickoff_utc=NOW + timedelta(hours=3), now_utc=NOW, stage_hint=""))
    obscure = compute_priority(PriorityInput(
        league_name="Regional Cup", country="Nowhere", home="A", away="B",
        is_live=False, kickoff_utc=NOW + timedelta(hours=3), now_utc=NOW, stage_hint=""))
    assert elite.tournament_prestige > obscure.tournament_prestige


def test_fully_identical_matches_stabilized_by_fixture_id():
    """Two fixtures identical in every priority-relevant field (including
    league/home/away) must still sort deterministically — the last-resort
    fixture_id tie-break, independent of input order."""
    from event_list import sort_matches
    a = normalize_fixture(_raw(fid=5, t1="Zeta", t2="Yankee", league="Regional Cup", country="Nowhere"))
    b = normalize_fixture(_raw(fid=3, t1="Zeta", t2="Yankee", league="Regional Cup", country="Nowhere"))
    from event_list import assign_priority_scores
    assign_priority_scores([a, b], NOW)
    fwd = sort_matches([a, b])
    rev = sort_matches([b, a])
    assert [x.fixture_id for x in fwd] == [x.fixture_id for x in rev] == ["3", "5"]


def test_shuffled_input_gives_identical_output():
    from event_list import assign_priority_scores, sort_matches
    raws = [
        _raw(fid=1, t1="Real Madrid", t2="Barcelona", league="La Liga", country="Spain"),
        _raw(fid=2, t1="Arsenal", t2="Tottenham", league="Premier League", country="England"),
        _raw(fid=3, t1="P", t2="Q", league="Regional Cup", country="Nowhere"),
        _raw(fid=4, t1="R", t2="S", league="Regional Cup", country="Nowhere"),
    ]
    items = [normalize_fixture(r) for r in raws]
    assign_priority_scores(items, NOW)
    fwd = sort_matches(list(items))
    rev = sort_matches(list(reversed(items)))
    assert [x.fixture_id for x in fwd] == [x.fixture_id for x in rev]
