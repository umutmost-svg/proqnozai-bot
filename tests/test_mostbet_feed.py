"""Odds export to the Mini App (mostbet_feed.py) — offline: no Mostbet, no HTTP."""

import asyncio
import json
import time

import pytest

import mostbet
import mostbet_feed as feed
from miniapp.odds.feed import FeedReceiver
from miniapp.sports.search import normalize as miniapp_normalize

NOW = 1_800_000_000.0
SECRET = b"k" * 40


def msk(ts: float) -> str:
    """Mostbet's own time format (UTC+3)."""
    return time.strftime("%d.%m.%Y %H:%M:%S", time.gmtime(ts + 3 * 3600))


def raw(fid=1, t1="Arsenal", t2="Chelsea", country="England", league="Premier League", start=NOW + 3600,
        live=False, sport="Football", **extra):
    m = {"id": fid, "team1Title": t1, "team2Title": t2, "lineCategory": sport, "lineSuperCategory": country,
         "lineSubCategory": league, "matchBeginAt": msk(start), "isLive": live}
    m.update(extra)
    return m


ODDS = {"w1": 1.8, "x": 3.8, "w2": 4.5, "over25": 1.75, "under25": 2.1, "hcp_val": None}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def env(monkeypatch):
    clock = Clock()
    posts: list = []
    fetched: list = []
    sleeps: list = []
    state = {"matches": [], "status": 200}

    async def get_odds(line_id, *, retry_on_429=True, page_spacing_s=0.0):
        assert retry_on_429 is False  # the export never retries a 429 itself
        assert page_spacing_s == feed.SPACING_S  # and keeps its pace per request, pages included
        fetched.append(line_id)
        mostbet.mostbet_cache[f"odds_{line_id}"] = (clock.now - 30, dict(ODDS))
        return dict(ODDS)

    async def post(url, body, headers):
        posts.append((url, body, headers))
        if isinstance(state["status"], Exception):
            raise state["status"]
        return state["status"]

    async def sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(mostbet, "cached_matches", lambda: listed())
    monkeypatch.setattr(mostbet, "mostbet_get_odds", get_odds)
    monkeypatch.setattr(mostbet, "last_rate_limited_at", 0.0)
    monkeypatch.setattr(mostbet, "mostbet_cache", {})
    monkeypatch.setattr(mostbet, "_partial_odds", set())
    seen: dict = {}
    monkeypatch.setattr(mostbet, "line_seen_at", seen)

    def listed():  # the lines as a just-fetched list page: confirmed now
        for m in state["matches"]:
            seen.setdefault(m["id"], clock.now)
        return list(state["matches"])
    export = feed.OddsExport("https://stand.example/api/internal/odds", SECRET, clock=clock, sleep=sleep, post=post)
    return export, clock, state, posts, fetched, sleeps


def run(coro):
    return asyncio.run(coro)


def test_normalization_is_the_mini_apps() -> None:
    for s in ["Premier League", "LaLiga", "Bayern München", "Brasileirão Série A", "1. FC Köln", "Ø Å ß ı"]:
        assert feed.normalize(s) == miniapp_normalize(s)


def test_only_known_competitions_upcoming_and_not_live_are_priced(env) -> None:
    export, clock, state, *_ = env
    lines = [raw(1), raw(2, country="Ukraine"), raw(3, live=True), raw(4, start=NOW - 60),
             raw(5, start=NOW + 49 * 3600), raw(6, t2="?"), raw(7, sport="Basketball"),
             raw(8, t1="Arsenal (esports)", league="Premier League"), raw(9, country="UEFA Champions League", league="League phase")]
    mostbet.line_seen_at.update({m["id"]: clock.now for m in lines})  # all in a fresh list page
    picked = [m["id"] for _, m in export.candidates(lines)]
    assert picked == [1, 9]
    assert export._skipped[("Ukraine", "Premier League")] == 1


def test_a_cycle_pushes_a_body_the_mini_app_accepts(env) -> None:
    export, clock, state, posts, fetched, sleeps = env
    state["matches"] = [raw(1), raw(2, t1="Liverpool", t2="Everton", start=NOW + 7200)]
    assert run(export.cycle()) == 2
    url, body, headers = posts[0]
    lines = FeedReceiver(SECRET, clock).verify(headers["X-Odds-Timestamp"], headers["X-Odds-Signature"], body)
    assert [ln.line_id for ln in lines] == [1, 2] and lines[0].book["1"] == 1.8
    sent = json.loads(body)["lines"][0]
    assert sent["kickoff"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 3600))
    assert sent["fetchedAt"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30))
    assert "hcp_val" not in sent["odds"]  # empty values are not sent
    assert sleeps == [feed.SPACING_S] and fetched == [1, 2]


def test_lines_are_refreshed_only_when_due(env) -> None:
    export, clock, state, posts, fetched, _ = env
    state["matches"] = [raw(1, start=NOW + 3600), raw(2, start=NOW + 10 * 3600)]
    run(export.cycle())
    clock.now += feed.REFRESH_NEAR_S
    run(export.cycle())  # the near match again, the far one not yet
    assert fetched == [1, 2, 1]
    clock.now += feed.REFRESH_FAR_S
    run(export.cycle())
    assert fetched[-2:] == [1, 2]


def test_at_most_a_bounded_number_of_lines_per_cycle_nearest_first(env) -> None:
    export, clock, state, _, fetched, _ = env
    state["matches"] = [raw(i, t1=f"T{i}", start=NOW + 60 * (100 - i)) for i in range(1, 61)]
    run(export.cycle())
    assert len(fetched) == feed.MAX_LINES_PER_CYCLE and fetched[0] == 60


def test_a_recent_429_pauses_the_export_before_and_during_a_cycle(env, monkeypatch) -> None:
    export, clock, state, posts, fetched, _ = env
    state["matches"] = [raw(1), raw(2, t1="Liverpool", t2="Everton")]
    monkeypatch.setattr(mostbet, "last_rate_limited_at", NOW - 10)
    assert run(export.cycle()) == 0 and fetched == [] and posts == []
    monkeypatch.setattr(mostbet, "last_rate_limited_at", 0.0)

    async def limited(line_id, *, retry_on_429=True, page_spacing_s=0.0):
        fetched.append(line_id)
        mostbet.last_rate_limited_at = clock.now  # the API said 429 to this request
        return {k: None for k in ODDS}

    monkeypatch.setattr(mostbet, "mostbet_get_odds", limited)
    assert run(export.cycle()) == 0 and fetched == [1]  # stopped right after it


@pytest.mark.parametrize("outcome", [503, 401, ConnectionError("down")])
def test_a_failed_push_is_retried_next_cycle(env, outcome) -> None:
    export, clock, state, posts, fetched, _ = env
    state["matches"] = [raw(1)]
    state["status"] = outcome
    assert run(export.cycle()) == 0
    state["status"] = 200
    assert run(export.cycle()) == 1 and fetched == [1, 1]


def test_a_line_without_any_price_is_not_sent(env, monkeypatch) -> None:
    export, clock, state, posts, *_ = env
    state["matches"] = [raw(1)]

    async def empty(line_id, *, retry_on_429=True, page_spacing_s=0.0):
        mostbet.mostbet_cache[f"odds_{line_id}"] = (clock.now, {"w1": None})
        return {"w1": None}

    monkeypatch.setattr(mostbet, "mostbet_get_odds", empty)
    assert run(export.cycle()) == 0 and posts == []


@pytest.mark.parametrize(("env_vars", "on"), [
    ({}, False),
    ({"MINIAPP_ODDS_URL": "https://x/api/internal/odds"}, False),
    ({"MINIAPP_ODDS_URL": "https://x/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "short"}, False),
    ({"MINIAPP_ODDS_URL": "http://x/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, False),
    ({"MINIAPP_ODDS_URL": "https://x/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, True),
    ({"MINIAPP_ODDS_URL": "http://127.0.0.1:8792/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, True),
    ({"MINIAPP_ODDS_URL": "http://127.0.0.1:80@evil.example/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, False),
    ({"MINIAPP_ODDS_URL": "https://user:pw@stand.example/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, False),
    ({"MINIAPP_ODDS_URL": "http://127.0.0.1/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, False),
    ({"MINIAPP_ODDS_URL": "http://127.0.0.1.evil.example:80/x", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, False),
    ({"MINIAPP_ODDS_URL": "https:///nohost", "MINIAPP_ODDS_FEED_SECRET": "k" * 40}, False),
])
def test_the_export_is_off_unless_safely_configured(env_vars, on) -> None:
    assert (feed.from_env(env_vars) is not None) is on


def test_the_shared_table_is_read_from_the_mini_app_package() -> None:
    pairs = feed.load_table()
    assert ("england", "premier league") in pairs and ("ukraine", "premier league") not in pairs
    assert ("uefa champions league", "league phase") in pairs
    assert not any(c == "uefa champions league" and lg in ("women", "u21", "") for c, lg in pairs)


# --- the saved API answer, end to end ------------------------------------------------------------

class _Answer:
    def __init__(self, payload: dict | None, status: int = 200) -> None:
        self._payload, self.status_code = payload, status
        self.headers = {"Retry-After": "1"}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    """httpx.AsyncClient stand-in serving scripted answers in order."""

    def __init__(self, answers) -> None:
        self.answers, self.requests = list(answers), []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        self.requests.append(url)
        return self.answers.pop(0)


def _saved_outcomes() -> dict:
    from pathlib import Path
    return json.loads((Path(__file__).parent / "fixtures" / "outcomes_full.json").read_text())


def test_a_saved_api_answer_goes_through_the_bot_parser_the_signed_push_and_the_mini_app(monkeypatch, clean_mostbet_cache) -> None:
    from miniapp.odds.value import compare

    clock = Clock()
    clock.now = time.time()
    client = _Client([_Answer(_saved_outcomes())])
    monkeypatch.setattr(mostbet, "line_seen_at", {777001: clock.now})  # in a fresh list page
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", client)
    monkeypatch.setattr(mostbet, "last_rate_limited_at", 0.0)
    monkeypatch.setattr(mostbet, "cached_matches", lambda: [raw(777001, start=clock.now + 3600)])
    posts = []

    async def post(url, body, headers):
        posts.append((body, headers))
        return 200

    export = feed.OddsExport("https://stand.example/api/internal/odds", SECRET, clock=clock, sleep=lambda s: asyncio.sleep(0), post=post)
    assert run(export.cycle()) == 1
    body, headers = posts[0]
    lines = FeedReceiver(SECRET, clock).verify(headers["X-Odds-Timestamp"], headers["X-Odds-Signature"], body)
    book = lines[0].book
    # The saved answer's prices (as the bot's own tests read them), in the Mini App's keys.
    assert (book["1"], book["X"], book["2"]) == (2.10, 3.40, 3.60)
    assert (book["1X"], book["12"], book["X2"]) == (1.30, 1.32, 1.75)
    quotes = compare(book, {"1": 0.5, "X": 0.25, "2": 0.25, "1X": 0.75, "X2": 0.5, "12": 0.75})
    assert abs(sum(quotes[k].market for k in ("1", "X", "2")) - 1) < 1e-9


def test_a_429_stops_a_background_fetch_at_once_and_caches_nothing(monkeypatch, clean_mostbet_cache) -> None:
    client = _Client([_Answer({}, 429), _Answer(_saved_outcomes())])
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", client)
    monkeypatch.setattr(mostbet, "last_rate_limited_at", 0.0)
    odds = run(mostbet.mostbet_get_odds(5, retry_on_429=False))
    assert not any(v is not None for v in odds.values())
    assert len(client.requests) == 1 and "odds_5" not in clean_mostbet_cache
    assert mostbet.last_rate_limited_at > 0
    # A bot user right after: a real fetch (the empty result was not pinned in the cache).
    odds = run(mostbet.mostbet_get_odds(5))
    assert odds["w1"] == 2.10 and len(client.requests) == 2


def test_the_bot_keeps_retrying_a_429_for_its_users(monkeypatch, clean_mostbet_cache) -> None:
    client = _Client([_Answer({}, 429), _Answer(_saved_outcomes())])
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", client)

    async def no_wait(_):
        return None

    monkeypatch.setattr(mostbet.asyncio, "sleep", no_wait)
    assert run(mostbet.mostbet_get_odds(6))["w1"] == 2.10 and len(client.requests) == 2


def test_lines_that_go_live_are_marked_without_a_request_and_repeated_only_when_due(env, monkeypatch) -> None:
    export, clock, state, posts, fetched, _ = env
    state["matches"] = [raw(1)]
    run(export.cycle())
    # 1 was priced by this process, 2 was not (e.g. before a restart): both are marked.
    state["matches"] = [raw(1, live=True, start=clock.now - 60), raw(2, live=True, t1="Liverpool", t2="Everton", start=clock.now - 60)]
    monkeypatch.setattr(mostbet, "last_rate_limited_at", clock.now)  # even while prices are paused
    assert run(export.cycle()) == 2 and fetched == [1]
    marker = json.loads(posts[-1][1])["lines"]
    assert sorted((m["id"], m["live"], m["odds"]) for m in marker) == [(1, True, {}), (2, True, {})]
    lines = FeedReceiver(SECRET, clock).verify(posts[-1][2]["X-Odds-Timestamp"], posts[-1][2]["X-Odds-Signature"], posts[-1][1])
    assert all(ln.live and ln.book == {} for ln in lines)
    clock.now += 60
    assert run(export.cycle()) == 0  # accepted a minute ago: not repeated yet
    clock.now += feed.MARKER_REPEAT_S
    assert run(export.cycle()) == 2  # still live: repeated (idempotent at the receiver)

def _full_first_page() -> dict:
    """The saved answer padded to exactly 100 outcomes, so the cursor asks for a second page."""
    outcomes = list(_saved_outcomes()["lineMatchOutcomes"])
    filler = [{"outcomeId": 900000 + i, "outcomeTitle": "x", "groupTitle": "corners", "odd": 2.0}
              for i in range(100 - len(outcomes))]
    return {"lineMatchOutcomes": outcomes + filler}


class _BadJson(_Answer):
    def json(self):
        raise ValueError("not json")


@pytest.mark.parametrize("second", [_Answer({}, 503), _BadJson({}), _Answer({}, 200),
                                    _Answer({"lineMatchOutcomes": None}, 200)])
def test_a_failed_later_page_is_never_exported(monkeypatch, clean_mostbet_cache, second) -> None:
    monkeypatch.setattr(mostbet, "_partial_odds", set())
    assert len(_full_first_page()["lineMatchOutcomes"]) == 100
    # A background fetch: nothing used, nothing cached.
    client = _Client([_Answer(_full_first_page()), second])
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", client)
    odds = run(mostbet.mostbet_get_odds(7, retry_on_429=False))
    assert not any(v is not None for v in odds.values()) and "odds_7" not in clean_mostbet_cache
    assert not mostbet.odds_complete(7)
    # A bot user's fetch keeps its behaviour (partial set cached) but stays marked partial ...
    client = _Client([_Answer(_full_first_page()), second])
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", client)
    assert run(mostbet.mostbet_get_odds(8))["w1"] == 2.10 and not mostbet.odds_complete(8)
    # ... so the export, served that cached set, does not push it.
    clock = Clock()
    clock.now = time.time()
    monkeypatch.setattr(mostbet, "line_seen_at", {8: clock.now})  # fresh: only the partial set stops it
    monkeypatch.setattr(mostbet, "cached_matches", lambda: [raw(8, start=clock.now + 3600)])
    monkeypatch.setattr(mostbet, "last_rate_limited_at", 0.0)
    posts = []

    async def post(url, body, headers):
        posts.append(body)
        return 200

    export = feed.OddsExport("https://stand.example/api/internal/odds", SECRET, clock=clock,
                             sleep=lambda s: asyncio.sleep(0), post=post)
    assert run(export.cycle()) == 0 and posts == []
    # A later complete fetch clears the mark.
    clean_mostbet_cache.clear()
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", _Client([_Answer(_saved_outcomes())]))
    run(mostbet.mostbet_get_odds(8))
    assert mostbet.odds_complete(8)



def test_lines_not_confirmed_by_a_recent_list_page_get_no_prices_but_live_markers_still_go(env) -> None:
    export, clock, state, posts, fetched, _ = env
    state["matches"] = [raw(1)]
    run(export.cycle())
    clock.now += feed.LIST_MAX_AGE_S + 1  # line 1 has not been in a fetched page since
    state["matches"] = [raw(1, live=True), raw(3, t1="Liverpool", t2="Everton", start=clock.now + 3600)]
    mostbet.line_seen_at[1] = clock.now - feed.LIST_MAX_AGE_S - 1  # only merged from an old tail
    assert run(export.cycle()) == 2 and fetched == [1, 3]  # the live marker of 1, fresh line 3 priced
    sent = {ln["id"]: ln["live"] for ln in json.loads(posts[-1][1])["lines"]}
    assert sent == {3: False, 1: True}
    # A line seen only in an old merged tail is not priced.
    clock.now += 600
    state["matches"] = [raw(4, t1="Spurs", t2="Fulham", start=clock.now + 3600)]
    mostbet.line_seen_at[4] = clock.now - feed.LIST_MAX_AGE_S - 1
    assert run(export.cycle()) == 0 and 4 not in fetched


def test_no_prices_are_fetched_for_the_last_minutes_before_kickoff(env) -> None:
    from miniapp.odds.store import CUTOFF_BEFORE_KICKOFF_S
    assert feed.STOP_BEFORE_KICKOFF_S == CUTOFF_BEFORE_KICKOFF_S
    export, clock, state, posts, fetched, _ = env
    state["matches"] = [raw(1, start=clock.now + feed.STOP_BEFORE_KICKOFF_S - 60), raw(2, t1="X", t2="Y", start=clock.now + 3600)]
    run(export.cycle())
    assert fetched == [2]


def test_the_list_records_when_each_line_was_really_fetched(monkeypatch, clean_mostbet_cache) -> None:
    monkeypatch.setattr(mostbet, "line_seen_at", {})
    mostbet._note_lines_seen([{"id": 5}, {"id": True}, {"id": "x"}], 1000.0)
    assert mostbet.line_seen_at == {5: 1000.0}
    mostbet._note_lines_seen([{"id": 6}], 1000.0 + 86401)
    assert mostbet.line_seen_at == {6: 1000.0 + 86401}



def test_a_lost_answer_or_a_restart_sends_the_live_markers_again(env, monkeypatch) -> None:
    export, clock, state, posts, fetched, _ = env
    monkeypatch.setattr(mostbet, "last_rate_limited_at", clock.now)  # markers only
    state["matches"] = [raw(1, live=True, start=clock.now - 60)]
    state["status"] = ConnectionError("answer lost")
    assert run(export.cycle()) == 0
    state["status"] = 200
    assert run(export.cycle()) == 1  # sent again at once: nothing was recorded
    restarted = feed.OddsExport(export.url, SECRET, clock=clock, sleep=export._sleep, post=export._post)
    assert run(restarted.cycle()) == 1  # a fresh process knows nothing: marks again
    assert len(posts) == 3


def test_markers_only_for_served_competitions_started_recently(env, monkeypatch) -> None:
    export, clock, state, *_ = env
    state["matches"] = [raw(1, live=True, start=clock.now - feed.MARKER_LOOKBACK_S - 60),
                        raw(2, live=True, country="Ukraine", start=clock.now - 60),
                        raw(3, live=True, t2="?", start=clock.now - 60),
                        raw(4, live=True, sport="Basketball", start=clock.now - 60)]
    assert export.went_live(state["matches"]) == []


def test_the_cycle_prices_at_most_the_configured_number_of_lines(env) -> None:
    export, clock, state, posts, fetched, _ = env
    small = feed.OddsExport(export.url, SECRET, clock=clock, sleep=export._sleep, post=export._post, max_lines=5)
    state["matches"] = [raw(i, t1=f"T{i}", start=clock.now + 3600 + i) for i in range(1, 21)]
    run(small.cycle())
    assert fetched == [1, 2, 3, 4, 5]
    with pytest.raises(ValueError):
        feed.OddsExport(export.url, SECRET, max_lines=41)


@pytest.mark.parametrize(("raw_max", "expected"), [("", 40), ("5", 5), ("40", 40), ("0", None), ("41", None), ("x", None)])
def test_max_lines_from_the_environment(raw_max, expected) -> None:
    env = {"MINIAPP_ODDS_URL": "https://x/api/internal/odds", "MINIAPP_ODDS_FEED_SECRET": "k" * 40,
           "MINIAPP_ODDS_MAX_LINES": raw_max}
    export = feed.from_env(env)
    assert (export.max_lines if export else None) == expected


def test_a_410_switches_the_export_off_until_an_empty_push_is_accepted_again(env) -> None:
    export, clock, state, posts, fetched, _ = env
    state["matches"] = [raw(1, start=clock.now + 6 * 3600)]  # still upcoming after the hour below
    state["status"] = 410
    assert run(export.cycle()) == 0 and fetched == [1]  # the cycle that learnt it
    clock.now += feed.REFRESH_NEAR_S
    assert run(export.cycle()) == 0 and fetched == [1] and len(posts) == 1  # off: no request, no push
    clock.now += feed.OFF_RECHECK_S
    assert run(export.cycle()) == 0 and fetched == [1]  # still 410: only the empty probe went out
    assert json.loads(posts[-1][1])["lines"] == []
    clock.now += feed.OFF_RECHECK_S
    state["status"] = 200
    mostbet.line_seen_at[1] = clock.now  # the list keeps refreshing meanwhile
    assert run(export.cycle()) == 1 and fetched == [1, 1]  # back on: probe accepted, then priced



def test_cycles_start_every_five_minutes_from_their_start_and_never_overlap() -> None:
    assert feed.next_delay(100.0, 100.0 + 78) == feed.CYCLE_S - 78  # a 40-line cycle of 78 s
    assert feed.next_delay(100.0, 100.0 + feed.CYCLE_S + 30) == 0.0  # overran: next at once


def test_the_loop_survives_a_failing_cycle_and_keeps_the_cadence() -> None:
    clock = Clock()
    clock.now = 0.0
    sleeps: list = []
    calls = {"n": 0}

    class _Export:
        async def cycle(self):
            calls["n"] += 1
            clock.now += 40  # the cycle takes 40 s
            if calls["n"] == 1:
                raise RuntimeError("one bad cycle")
            if calls["n"] == 3:
                raise asyncio.CancelledError

    async def sleep(s):
        sleeps.append(s)
        clock.now += s

    with pytest.raises(asyncio.CancelledError):
        run(feed.run_odds_export(_Export(), clock=clock, sleep=sleep))
    assert sleeps == [60, feed.CYCLE_S - 40, feed.CYCLE_S - 40] and calls["n"] == 3


def test_a_background_fetch_paces_every_page_not_only_every_line(monkeypatch, clean_mostbet_cache) -> None:
    # 10 lines x up to 10 pages each: without a pause per page the export could burst at the source.
    page = _saved_outcomes()
    full = {**page, "lineMatchOutcomes": (page["lineMatchOutcomes"] * 100)[:100]}  # a full page: there is a next one
    last = {**page, "lineMatchOutcomes": []}
    client = _Client([_Answer(full), _Answer(full), _Answer(last)])
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", client)
    pauses: list = []

    async def fake_sleep(s):
        pauses.append(s)
    monkeypatch.setattr(mostbet.asyncio, "sleep", fake_sleep)
    run(mostbet.mostbet_get_odds(9, retry_on_429=False, page_spacing_s=2.0))
    assert len(client.requests) == 3 and pauses == [2.0, 2.0]  # before page 2 and page 3, never before the first


def test_the_bots_own_fetch_keeps_its_speed(monkeypatch, clean_mostbet_cache) -> None:
    page = _saved_outcomes()
    full = {**page, "lineMatchOutcomes": (page["lineMatchOutcomes"] * 100)[:100]}
    client = _Client([_Answer(full), _Answer({**page, "lineMatchOutcomes": []})])
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", client)
    pauses: list = []

    async def fake_sleep(s):
        pauses.append(s)
    monkeypatch.setattr(mostbet.asyncio, "sleep", fake_sleep)
    run(mostbet.mostbet_get_odds(10))
    assert len(client.requests) == 2 and pauses == []


def test_a_broken_export_setup_never_stops_the_bot(monkeypatch, caplog) -> None:
    import main

    def broken(env=None):
        raise ValueError("unreadable leagues table")
    monkeypatch.setattr(feed, "from_env", broken)
    with caplog.at_level("ERROR"):
        main._start_odds_export()  # returns: polling and handlers start as usual
    assert "odds_export.disabled reason=setup_failed type=ValueError" in caplog.text
