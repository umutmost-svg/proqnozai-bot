"""The day's top matches (M3.6): deterministic ranking from source facts, no extra request."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.sports import top
from miniapp.sports.football_data import normalize_match
from miniapp.sports.service import build_service
from miniapp.tests.conftest import FakeClock, fresh, login
from miniapp.tests.test_sports import ALLOWED, DAY, FETCHED, KEY, FakeTransport, base_settings, match, no_sleep
from miniapp.tests.test_standings import block, row, table

W = top.WEIGHTS


def event(mid: int, status: str = "TIMED", *, code: str = "PL", stage: str | None = None, home=(57, "Arsenal FC"),
          away=(61, "Chelsea FC"), utc: str = f"{DAY}T15:00:00Z", **kw):
    raw = match(mid, status, code=code, home=home, away=away, utc=utc, **kw)
    if stage is not None:
        raw["stage"] = stage
    e = normalize_match(raw, FETCHED, ALLOWED | {"CL", "ELC"})
    assert e is not None
    return e


def code_of(t: str) -> str:
    return t.removeprefix("fd:")


def test_stage_is_kept_only_when_it_is_a_plain_provider_code() -> None:
    assert event(1, stage="LAST_16").stage == "LAST_16"
    assert event(2, stage="last 16 <b>").stage is None and event(3).stage is None


def test_competition_stage_and_table_order_the_day() -> None:
    pl, elc = event(1), event(2, code="ELC", home=(1, "A FC"), away=(2, "B FC"))
    final = event(3, code="ELC", stage="FINAL", home=(3, "C FC"), away=(4, "D FC"))
    ranked = top.rank([elc, pl, final], code_of, {})
    # ELC 10 + FINAL 30 beats PL 26; a plain ELC match comes last.
    assert [r.event.id for r in ranked] == [final.id, pl.id, elc.id]
    tab = top.Table({"fd:team:57": 1, "fd:team:61": 2, "fd:team:1": 19, "fd:team:2": 20}, 20)
    scored = top.score(pl, "PL", tab)
    assert scored.leaders and scored.score == pytest.approx(W.competition["PL"] + W.table * (1 + 18 / 19) / 2 + W.leaders + W.upcoming)
    assert not top.score(pl, "PL", top.Table(tab.positions, 5)).leaders  # too small a table: ignored


def test_only_real_states_rank_and_live_updates_never_reorder() -> None:
    a = event(1, home=(1, "A FC"), away=(2, "B FC"), utc=f"{DAY}T12:00:00Z")
    b = event(2, home=(3, "C FC"), away=(4, "D FC"), utc=f"{DAY}T18:00:00Z")
    postponed = event(3, "POSTPONED", home=(5, "E FC"), away=(6, "F FC"))
    before = top.rank([b, a, postponed], code_of, {})
    assert [r.event.id for r in before] == [a.id, b.id]  # equal score: earlier kickoff first
    live = replace(a, status="live", score=(3, 0), minute=70)
    assert [r.event.id for r in top.rank([b, live], code_of, {})] == [a.id, b.id]
    done = replace(a, status="finished", score=(3, 1))
    assert [r.event.id for r in top.rank([b, done], code_of, {})] == [b.id, a.id]


def test_a_match_starting_soon_ranks_higher_and_keeps_its_place_at_kickoff() -> None:
    soon = event(1, home=(1, "A FC"), away=(2, "B FC"), utc=f"{DAY}T10:00:00Z")
    later = event(2, home=(3, "C FC"), away=(4, "D FC"), utc=f"{DAY}T09:00:00Z")
    at_9 = datetime.fromisoformat(f"{DAY}T08:30:00+00:00").timestamp()
    # Both within 3 h: equal, earlier kickoff first; at 06:30 only the 09:00 one is "soon".
    assert [r.event.id for r in top.rank([soon, later], code_of, {}, now=at_9)] == [later.id, soon.id]
    early = datetime.fromisoformat(f"{DAY}T06:30:00+00:00").timestamp()
    ranked = top.rank([soon, later], code_of, {}, now=early)
    assert [r.event.id for r in ranked] == [later.id, soon.id] and ranked[0].score - ranked[1].score == W.kickoff_soon
    before = top.score(later, "PL", None, now=early).score
    kicked = top.score(replace(later, status="live", score=(0, 0), minute=1), "PL", None,
                       now=datetime.fromisoformat(f"{DAY}T09:01:00+00:00").timestamp()).score
    assert before == kicked
    # Past the scheduled kickoff but still "upcoming" at the source: same place too.
    overdue = top.score(later, "PL", None, now=datetime.fromisoformat(f"{DAY}T09:04:00+00:00").timestamp()).score
    assert overdue == before


def test_at_most_five_and_ties_are_stable() -> None:
    many = [event(i, home=(i * 2, f"T{i} FC"), away=(i * 2 + 1, f"U{i} FC")) for i in range(10, 2, -1)]
    first = top.rank(many, code_of, {})
    assert len(first) == 5 and [r.event.id for r in first] == [r.event.id for r in top.rank(reversed(many), code_of, {})]
    assert [r.event.id for r in first] == sorted(r.event.id for r in first)


def test_one_competition_takes_at_most_two_places_while_others_have_matches() -> None:
    pl = [event(i, home=(i * 2, f"T{i} FC"), away=(i * 2 + 1, f"U{i} FC")) for i in range(1, 6)]
    pd = [event(i, code="PD", home=(i * 2, f"T{i} FC"), away=(i * 2 + 1, f"U{i} FC")) for i in range(10, 12)]
    elc = event(20, code="ELC", home=(40, "X FC"), away=(41, "Y FC"))
    ids = [r.event.id for r in top.rank([*pl, *pd, elc], code_of, {})]
    assert ids == ["fd:1", "fd:2", "fd:10", "fd:11", "fd:20"]
    # Nothing else that day: the remaining places go to the same competition.
    assert [r.event.id for r in top.rank(pl, code_of, {})] == ["fd:1", "fd:2", "fd:3", "fd:4", "fd:5"]


def test_weights_file_is_validated(tmp_path: Path) -> None:
    bad = tmp_path / "w.json"
    text = Path(top.__file__).with_name("top_weights.json").read_text()
    bad.write_text(text.replace('"maxMatches": 5', '"maxMatches": 9'))
    with pytest.raises(ValueError):
        top.load_weights(bad)
    bad.write_text(text.replace('"table": 12', '"table": -1'))
    with pytest.raises(ValueError):
        top.load_weights(bad)
    bad.write_text(text.replace('"live": 14', '"live": 20'))
    with pytest.raises(ValueError):
        top.load_weights(bad)


@pytest.fixture()
def api(tmp_path, clock: FakeClock):
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    service.football.client._sleep = no_sleep
    return TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000"), t, service


def test_endpoint_reuses_the_day_listing_and_reads_tables_only_from_cache(api, clock: FakeClock) -> None:
    client, t, service = api
    url = f"/api/sports/football/top?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z"
    assert client.get(url).status_code == 401 and t.calls == []
    login(client, fresh(clock))
    assert client.get(url + "&x=1").status_code == 400
    two_days = f"/api/sports/football/top?from={DAY}T00:00:00Z&to=2027-01-17T00:00:00Z"
    assert client.get(two_days).json()["error"] == "bad_range" and t.calls == []
    day = {"matches": [match(1), match(2, home=(1, "A FC"), away=(2, "B FC"), code="PD"),
                       match(3, "POSTPONED", home=(3, "C FC"), away=(4, "D FC"))]}
    t.script = [t.ok(day)]
    body = client.get(url).json()
    assert [x["event"]["id"] for x in body["top"]] == ["fd:1", "fd:2"] and body["top"][0]["leaders"] is False
    calls = len(t.calls)
    # A table already in the cache counts; the ranking itself never asks for one.
    six = table(block(*(row(i + 1, tid, f"T{tid} FC", 30 - i) for i, tid in enumerate((57, 61, 1, 2, 3, 4)))))
    t.script = [t.ok(six)]
    client.get("/api/sports/football/tournaments/fd:PL/standings")
    body = client.get(url).json()
    assert len(t.calls) == calls + 1 and body["top"][0]["leaders"] is True
    assert "stage" not in body["top"][0]["event"]
    narrowed = client.get(url + "&tournament=fd:PD").json()
    assert [x["event"]["id"] for x in narrowed["top"]] == ["fd:2"] and len(t.calls) == calls + 1
    assert client.get(url + "&tournament=fd:XX").json()["error"] == "unknown_tournament"


def test_the_days_table_is_fixed_once_known(api, clock: FakeClock) -> None:
    client, t, service = api
    login(client, fresh(clock))
    url = f"/api/sports/football/top?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z"
    t.script = [t.ok({"matches": [match(1)]})]
    assert client.get(url).json()["top"][0]["leaders"] is False  # no table known yet
    six = table(block(*(row(i + 1, tid, f"T{tid} FC", 30 - i) for i, tid in enumerate((57, 61, 1, 2, 3, 4)))))
    t.script = [t.ok(six)]
    client.get("/api/sports/football/tournaments/fd:PL/standings")
    assert client.get(url).json()["top"][0]["leaders"] is True
    service.cache._entries.pop("football:standings:PL")  # the table ages out of the cache
    assert client.get(url).json()["top"][0]["leaders"] is True


def test_snapshot_cleanup_drops_only_days_that_are_over(api, clock: FakeClock) -> None:
    client, t, service = api
    login(client, fresh(clock))
    url = f"/api/sports/football/top?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z"
    six = table(block(*(row(i + 1, tid, f"T{tid} FC", 30 - i) for i, tid in enumerate((57, 61, 1, 2, 3, 4)))))
    t.script = [t.ok({"matches": [match(1)]}), t.ok(six)]
    client.get(url)
    client.get("/api/sports/football/tournaments/fd:PL/standings")
    assert client.get(url).json()["top"][0]["leaders"] is True
    stale = top.Table({}, 6)
    service._top_tables.update({(f"2020-01-01T00:00:{i:05d}", "fd:PL"): stale for i in range(2100)})
    service._top_tables.move_to_end((f"{DAY}T00:00:00+00:00", "fd:PL"))
    # A new day's first table triggers the cleanup: finished days go, the active ones stay.
    service._day_table(datetime.fromisoformat("2027-01-16T00:00:00+00:00"), "fd:PL")
    assert all(not k[0].startswith("2020") for k in service._top_tables)
    assert (f"{DAY}T00:00:00+00:00", "fd:PL") in service._top_tables
    assert ("2027-01-16T00:00:00+00:00", "fd:PL") in service._top_tables


def test_snapshots_are_bounded_and_keyed_on_the_day_grid(api, clock: FakeClock) -> None:
    from miniapp.sports.service import TOP_TABLES_MAX

    client, t, service = api
    six = table(block(*(row(i + 1, tid, f"T{tid} FC", 30 - i) for i, tid in enumerate((57, 61, 1, 2, 3, 4)))))
    t.script = [t.ok(six)]
    login(client, fresh(clock))
    client.get("/api/sports/football/tournaments/fd:PL/standings")
    base = datetime.fromisoformat(f"{DAY}T00:00:00+00:00")
    # Instants inside one 15-minute slot share one snapshot.
    for seconds in (0, 59, 899):
        service._day_table(base + timedelta(seconds=seconds), "fd:PL")
    assert len(service._top_tables) == 1
    for i in range(TOP_TABLES_MAX + 50):  # a flood of distinct (future) day starts
        service._day_table(base + timedelta(minutes=15 * (i + 1)), "fd:PL")
    assert len(service._top_tables) == TOP_TABLES_MAX
