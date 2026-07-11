"""Offline tests for football_api normalization helpers feeding the metrics
engine: correct home/away orientation, fixture de-duplication, preserved None
goals, and finished-only filtering. No network."""
from football_api import _finished_recent, _team_results


def _fx(fid, home_id, away_id, gh, ga, status="FT"):
    return {
        "fixture": {"id": fid, "status": {"short": status}, "date": f"2026-07-{fid:02d}T18:00:00+00:00"},
        "teams": {"home": {"id": home_id, "name": f"H{home_id}"},
                  "away": {"id": away_id, "name": f"A{away_id}"}},
        "goals": {"home": gh, "away": ga},
    }


def test_orientation_home_and_away():
    # Team 10 plays home (2-1) then away (0-3).
    fixtures = [_fx(1, 10, 20, 2, 1), _fx(2, 30, 10, 0, 3)]
    res = _team_results(fixtures, 10)
    assert res[0] == {"scored": 2, "conceded": 1}   # home
    assert res[1] == {"scored": 3, "conceded": 0}   # away view of 0-3


def test_duplicate_fixture_not_counted_twice():
    fixtures = [_fx(1, 10, 20, 2, 1), _fx(1, 10, 20, 2, 1), _fx(2, 10, 30, 1, 1)]
    res = _team_results(fixtures, 10)
    assert len(res) == 2  # the repeated fixture id 1 collapses to one


def test_missing_goals_preserved_as_none():
    fixtures = [_fx(1, 10, 20, None, None)]
    res = _team_results(fixtures, 10)
    assert res[0] == {"scored": None, "conceded": None}


def test_finished_recent_excludes_postponed_and_cancelled():
    fixtures = [
        _fx(1, 10, 20, 2, 1, status="FT"),
        _fx(2, 10, 30, 0, 0, status="PST"),   # postponed
        _fx(3, 10, 40, 1, 1, status="CANC"),  # cancelled
        _fx(4, 10, 50, 3, 0, status="NS"),    # not started
        _fx(5, 10, 60, 1, 2, status="AET"),   # finished (extra time)
    ]
    out = _finished_recent(fixtures, limit=10)
    statuses = {f["fixture"]["status"]["short"] for f in out}
    assert statuses == {"FT", "AET"}
