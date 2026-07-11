"""Offline tests for deterministic form metrics (no network)."""
from metrics import compute_team_metrics, format_metrics_block


def _m(scored, conceded):
    return {"scored": scored, "conceded": conceded}


def test_basic_counts_and_averages():
    # W (2-1), D (1-1), L (0-2), W (3-0), clean sheet twice, one FTS.
    matches = [_m(2, 1), _m(1, 1), _m(0, 2), _m(3, 0)]
    r = compute_team_metrics(matches)
    assert r["matches"] == 4
    assert (r["wins"], r["draws"], r["losses"]) == (2, 1, 1)
    assert r["avg_scored"] == 1.5      # (2+1+0+3)/4
    assert r["avg_conceded"] == 1.0    # (1+1+2+0)/4
    assert r["avg_total"] == 2.5
    assert r["clean_sheet_pct"] == 0.25   # only 3-0
    assert r["failed_to_score_pct"] == 0.25  # only 0-2


def test_btts_and_over25_percentages():
    matches = [_m(2, 1), _m(0, 0), _m(3, 3)]  # btts: 1st & 3rd; over2.5: 1st (3) & 3rd (6)
    r = compute_team_metrics(matches)
    assert r["btts_pct"] == round(2 / 3, 2)
    assert r["over25_pct"] == round(2 / 3, 2)


def test_empty_sample_returns_null_not_zero():
    r = compute_team_metrics([])
    assert r["matches"] == 0
    for key in ("wins", "avg_scored", "avg_total", "btts_pct",
                "clean_sheet_pct", "failed_to_score_pct"):
        assert r[key] is None, key


def test_missing_goal_values_are_excluded_not_zeroed():
    # A match with a None goal must not be treated as 0-0.
    matches = [_m(2, 1), _m(None, 1), _m(3, None)]
    r = compute_team_metrics(matches)
    assert r["matches"] == 1          # only the fully-known match counts
    assert r["avg_scored"] == 2.0
    assert r["avg_conceded"] == 1.0


def test_all_missing_returns_null():
    r = compute_team_metrics([_m(None, None), _m(None, 1)])
    assert r["matches"] == 0
    assert r["avg_total"] is None


def test_bool_goals_rejected():
    # True/False must never be counted as goals.
    r = compute_team_metrics([{"scored": True, "conceded": False}])
    assert r["matches"] == 0


def test_format_block_unavailable_when_empty():
    block = format_metrics_block("Arsenal", compute_team_metrics([]))
    assert "unavailable" in block.lower()
    # Must not fabricate a zero record.
    assert "0/0/0" not in block


def test_format_block_includes_computed_values():
    block = format_metrics_block("Arsenal", compute_team_metrics([_m(2, 1), _m(1, 1)]))
    assert "Arsenal" in block
    assert "do not recompute" in block.lower()
