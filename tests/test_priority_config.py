"""Offline tests for priority_config's pure helpers, in particular
is_pure_stage_label — the one heuristic used to decide whether a Mostbet
subcategory field is standing in for the round vs is itself the competition
name (see event_list._resolve_competition)."""
from priority_config import is_pure_stage_label


def test_whole_field_stage_labels_recognized():
    assert is_pure_stage_label("Play-off") == "playoff"
    assert is_pure_stage_label("Final") == "final"
    assert is_pure_stage_label("Semi-final") == "semifinal"
    assert is_pure_stage_label("Quarter-final") == "quarterfinal"
    assert is_pure_stage_label("Group Stage") == "group"


def test_stage_embedded_in_longer_name_is_not_pure():
    """This is the guard against over-triggering the sub/sup swap: a
    competition name that happens to CONTAIN a stage word must not be treated
    as a pure stage label."""
    assert is_pure_stage_label("Champions League - Semi-final") is None
    assert is_pure_stage_label("Final Regional Cup") is None
    assert is_pure_stage_label("Grupo A Sudamericano") is None


def test_empty_and_none_are_not_stage_labels():
    assert is_pure_stage_label("") is None
    assert is_pure_stage_label(None) is None


def test_ordinary_competition_names_are_not_stage_labels():
    assert is_pure_stage_label("Premier League") is None
    assert is_pure_stage_label("World Cup 2026") is None
