"""League prioritisation, stable sorting and match time-window selection.

Callback indices (fm_lg_{i}) are resolved by re-running the same sort, so
_sorted_leagues must be deterministic — these tests guard that contract.
"""
from datetime import datetime, timedelta, timezone

from config import MOSTBET_SRC_TZ
from handlers.forecast import (
    _is_priority_league,
    _league_rank,
    _match_in_window,
    _sorted_leagues,
)

SRC_TZ = timezone(timedelta(hours=MOSTBET_SRC_TZ))


def _dt(days_ahead: float) -> str:
    return (datetime.now(SRC_TZ) + timedelta(days=days_ahead)).strftime("%d.%m.%Y %H:%M:%S")


def test_priority_league_detection():
    assert _is_priority_league("FIFA World Cup 2026")
    assert _is_priority_league("UEFA Champions League")
    assert not _is_priority_league("Premier League")
    assert not _is_priority_league("")
    assert not _is_priority_league(None)


def test_league_rank_orders_by_priority_list():
    assert _league_rank("World Cup 2026") < _league_rank("Champions League")
    assert _league_rank("Champions League") < _league_rank("Serie A")


def test_sorted_leagues_pins_majors_before_busy_domestic_leagues():
    leagues = {
        "Premier League": [{}] * 30,
        "World Cup 2026": [{}] * 2,
        "Serie A": [{}] * 20,
    }
    order = _sorted_leagues(leagues)
    assert order[0] == "World Cup 2026"
    # Non-priority leagues sort by match count desc.
    assert order[1:] == ["Premier League", "Serie A"]


def test_sorted_leagues_is_deterministic():
    leagues = {
        "League A": [{}] * 5,
        "League B": [{}] * 5,
        "Champions League": [{}] * 1,
        "Euro 2028": [{}] * 1,
    }
    first = _sorted_leagues(leagues)
    for _ in range(5):
        assert _sorted_leagues(dict(leagues)) == first


def test_live_match_always_in_window():
    assert _match_in_window({"isLive": True, "matchBeginAt": _dt(30)})


def test_regular_league_uses_seven_day_window():
    assert _match_in_window({"lineSubCategory": "Premier League", "matchBeginAt": _dt(5)})
    assert not _match_in_window({"lineSubCategory": "Premier League", "matchBeginAt": _dt(10)})


def test_priority_league_uses_fourteen_day_window():
    assert _match_in_window({"lineSubCategory": "World Cup 2026", "matchBeginAt": _dt(10)})
    assert not _match_in_window({"lineSubCategory": "World Cup 2026", "matchBeginAt": _dt(20)})
