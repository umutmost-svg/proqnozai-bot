"""Segment filters from Mini App telemetry and referrals (owner 15.11): country, platform, source, forecasts, active days,
AI analyses, opened the Mini App, ad clicks. Users without the data never match a rule that needs it."""
from __future__ import annotations

import sqlite3

import pytest

from miniapp.ads import telemetry
from miniapp.admin_web.crm import ActionError, matches, needs_extra, validate_rules
from miniapp.admin_web.insights import UserInsights

NOW = 1_800_000_000
USER = {"createdAt": NOW - 86400, "lastLoginAt": NOW, "lang": "ru", "activeSessions": 1}


@pytest.mark.parametrize("rules", [
    {"country_in": ["az"]}, {"country_in": ["AZE"]}, {"platform_in": ["symbian"]}, {"source_in": ["ads"]},
    {"active_days_min": 31}, {"forecasts_min": 0}, {"used_ai": 1}])
def test_bad_rules_are_rejected(rules):
    with pytest.raises(ActionError):
        validate_rules(rules)


def test_rules_match_on_the_extras():
    rules = validate_rules({"country_in": ["AZ"], "platform_in": ["android"], "forecasts_min": 3, "used_ai": True,
                            "source_in": ["bot"], "opened_miniapp": True, "clicked_ad": False, "active_days_min": 2})
    good = {"country": "AZ", "platform": "android_x", "forecasts": 5, "aiCreated": 1, "source": "bot", "opened": True,
            "activeDays": 2}
    assert needs_extra(rules) and matches(USER, rules, set(), False, NOW, good)
    for change in ({"country": "TR"}, {"country": None}, {"platform": "ios"}, {"forecasts": 2}, {"aiCreated": 0},
                   {"source": None}, {"opened": False}, {"adClicks": 1}, {"activeDays": 1}):
        assert not matches(USER, rules, set(), False, NOW, {**good, **change}), change
    assert not matches(USER, rules, set(), False, NOW, None)              # no data: never matches
    assert matches(USER, {"source_in": ["none"]}, set(), False, NOW, {"source": None})
    assert matches(USER, {"opened_miniapp": False}, set(), False, NOW, {"opened": False})
    for rule in ({"source_in": ["none"]}, {"opened_miniapp": False}, {"used_ai": False}):  # unknown is not "no"
        assert not matches(USER, rule, set(), False, NOW, {})


def test_extras_are_read_from_telemetry(tmp_path):
    path = tmp_path / "telemetry.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(telemetry._SCHEMA)
        for day, n in ((telemetry.day_of(NOW), 2), (telemetry.day_of(NOW - 86400), 1), (telemetry.day_of(NOW - 60 * 86400), 9)):
            conn.execute("INSERT INTO user_activity (day, user_id, first_at, last_at, ai_created, quick_single_ok) "
                         "VALUES (?, 7, ?, ?, 1, ?)", (day, NOW, NOW, n))
        conn.execute("INSERT INTO user_activity (day, user_id, first_at, last_at) VALUES (?, 8, ?, ?)",
                     (telemetry.day_of(NOW - 60 * 86400), NOW, NOW))
        conn.execute("INSERT INTO user_edge (user_id, country, platform, updated_at) VALUES (7, 'AZ', 'ios', ?)", (NOW,))
        conn.execute("INSERT INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, kind) "
                     "VALUES (?, ?, 7, 1, 'A', 'ai_pick', 'k', 'click')", (NOW, telemetry.day_of(NOW)))
    extras = UserInsights(path).segment_extras([7, 8, 9], NOW, frozenset({"activity", "edge", "ads"}))
    assert extras[7] == {"forecasts": 5, "aiCreated": 2, "activeDays": 2, "opened": True, "country": "AZ",
                         "platform": "ios", "adClicks": 1}            # the 60-day-old row counts only as "opened"
    assert extras[8] == {"opened": True} and 9 not in extras


def test_unreadable_or_missing_telemetry_is_none(tmp_path):
    assert UserInsights(tmp_path / "absent.db").segment_extras([1], NOW, frozenset({"activity"})) is None
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    assert UserInsights(bad).segment_extras([1], NOW, frozenset({"activity"})) is None


def test_only_the_parts_asked_for_are_read(tmp_path):
    path = tmp_path / "telemetry.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(telemetry._SCHEMA)
        conn.execute("INSERT INTO user_edge (user_id, country, platform, updated_at) VALUES (7, 'AZ', 'ios', ?)", (NOW,))
        conn.execute("INSERT INTO user_activity (day, user_id, first_at, last_at) VALUES (?, 7, ?, ?)",
                     (telemetry.day_of(NOW + 5 * 86400), NOW, NOW))         # future-dated: not "last 30 days"
    assert UserInsights(path).segment_extras([7], NOW, frozenset({"edge"})) == {7: {"country": "AZ", "platform": "ios"}}
    assert UserInsights(path).segment_extras([7], NOW, frozenset({"activity"})) == {}


def test_opened_and_clicks_use_windows_inside_retention(tmp_path):
    path = tmp_path / "telemetry.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(telemetry._SCHEMA)
        conn.execute("INSERT INTO user_activity (day, user_id, first_at, last_at) VALUES (?, 7, ?, ?)",
                     (telemetry.day_of(NOW - 380 * 86400), NOW, NOW))
        conn.execute("INSERT INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, kind) "
                     "VALUES (?, ?, 7, 1, 'A', 'ai_pick', 'k', 'click')", (NOW, telemetry.day_of(NOW - 70 * 86400)))
    assert UserInsights(path).segment_extras([7], NOW, frozenset({"activity", "ads"})) == {}


def test_missing_referral_records_are_unknown(tmp_path):
    from miniapp.admin_web.bot_crm import BotCrmFile
    bot = BotCrmFile(tmp_path / "absent.db")
    assert bot.has_table("referral_starts") is False


def test_unrecorded_miniapp_attribution_is_unknown(tmp_path):
    from miniapp.store import Store
    store = Store(tmp_path / "app.db")
    with sqlite3.connect(tmp_path / "app.db") as conn:
        conn.execute("DROP TABLE IF EXISTS user_attribution")
    assert store.attributions() == {}
    with pytest.raises(sqlite3.OperationalError):
        store.attributions(strict=True)
