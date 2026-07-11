"""League prioritisation and deterministic ordering.

The event menu resolves callback indices against a frozen snapshot; the league
ordering that produces those indices must be deterministic and follow the
required priority. These tests guard that contract (logic now lives in
event_list; see also test_event_list.py and test_event_menu_snapshot.py).
"""
from event_list import group_by_league, league_rank, normalize_fixture


def _raw(fid, league, country, t1="A", t2="B"):
    return {"id": fid, "team1Title": t1, "team2Title": t2, "lineCategory": "Football",
            "lineSubCategory": league, "lineSuperCategory": country,
            "matchBeginAt": "12.07.2026 18:00:00", "isLive": False}


# The required priority order, top to bottom.
_ORDER = [
    ("Champions League", "Europe"),
    ("Europa League", "Europe"),
    ("Conference League", "Europe"),
    ("World Cup", "World"),
    ("Euro 2028", "Europe"),
    ("Premier League", "England"),
    ("La Liga", "Spain"),
    ("Serie A", "Italy"),
    ("Bundesliga", "Germany"),
    ("Ligue 1", "France"),
    ("Süper Lig", "Turkey"),
    ("Premier League", "Azerbaijan"),
]


def test_full_priority_order_is_strictly_increasing():
    ranks = [league_rank(name, country) for name, country in _ORDER]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)  # each named league gets a distinct rank


def test_unlisted_leagues_rank_after_all_named():
    named_max = max(league_rank(n, c) for n, c in _ORDER)
    assert league_rank("Some Regional Cup", "Nowhere") > named_max


def test_group_by_league_orders_by_priority():
    raws = [
        _raw(1, "Serie A", "Italy"),
        _raw(2, "Champions League", "Europe", t1="X", t2="Y"),
        _raw(3, "Random League", "Nowhere", t1="P", t2="Q"),
        _raw(4, "Premier League", "England", t1="M", t2="N"),
    ]
    items = [normalize_fixture(r) for r in raws]
    groups, _ = group_by_league(items)
    names = [g.league_name for g in groups]
    assert names[:3] == ["Champions League", "Premier League", "Serie A"]
    assert names[-1] == "Random League"


def test_group_by_league_is_deterministic():
    raws = [
        _raw(1, "League A", "X"), _raw(2, "League B", "Y", t1="C", t2="D"),
        _raw(3, "Champions League", "Europe", t1="E", t2="F"),
        _raw(4, "Euro 2028", "Europe", t1="G", t2="H"),
    ]
    items = [normalize_fixture(r) for r in raws]
    first = [g.league_key for g in group_by_league(items)[0]]
    for _ in range(5):
        again = [g.league_key for g in group_by_league(list(items))[0]]
        assert again == first
