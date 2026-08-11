"""Two failure modes the audit flagged as untested: a rate-limited Mostbet feed
and a database that cannot be read. Offline — httpx and the DB path are stubbed.
"""
import asyncio
import json
import sqlite3

import pytest

import mostbet
from mostbet import _mostbet_load_matches


def _gen(ids, src):
    return [{"id": i, "src": src} for i in ids]


class _Response:
    def __init__(self, items=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._items = items or []
        self.text = json.dumps({"lineMatches": self._items})

    def json(self):
        return {"lineMatches": self._items}


class _ScriptedClient:
    """Serves a scripted sequence of responses (status codes included)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.calls += 1
        if not self._responses:
            raise mostbet.httpx.ConnectError("no more scripted responses")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _no_page_sleep(monkeypatch):
    """The loader sleeps between pages and after a 429; tests shouldn't."""
    async def _instant(_):
        return None
    monkeypatch.setattr(mostbet.asyncio, "sleep", _instant)


# ─── Mostbet: rate limiting ───────────────────────────────────────────────────

def test_rate_limited_feed_serves_the_previous_generation(monkeypatch, clean_mostbet_cache):
    """A 429 wall on the first page must not publish an empty match list —
    users would see "no matches" for a full TTL."""
    prev = _gen(range(1, 121), "prev")
    clean_mostbet_cache["all_matches"] = (0, prev)          # expired → refetch

    monkeypatch.setattr(mostbet.httpx, "AsyncClient",
                        _ScriptedClient([_Response(status=429) for _ in range(8)]))

    result = asyncio.run(_mostbet_load_matches())
    assert result == prev


def test_rate_limit_after_a_good_page_keeps_the_tail(monkeypatch, clean_mostbet_cache):
    """Partial fetch cut short by 429s behaves like any other partial fetch:
    fresh head, previous tail, nothing silently dropped."""
    prev = _gen(range(1, 151), "prev")
    clean_mostbet_cache["all_matches"] = (0, prev)

    page1 = _Response(_gen(range(1, 101), "fresh"))
    responses = [page1] + [_Response(status=429) for _ in range(8)]
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", _ScriptedClient(responses))

    result = asyncio.run(_mostbet_load_matches())
    assert len(result) == 150
    assert result[0]["src"] == "fresh"
    assert result[-1] == {"id": 150, "src": "prev"}


def test_server_error_does_not_raise_out_of_the_loader(monkeypatch, clean_mostbet_cache):
    """A 500 is handled like any other bad page — the caller gets a list, and
    main._preload_mostbet never sees an exception."""
    prev = _gen(range(1, 21), "prev")
    clean_mostbet_cache["all_matches"] = (0, prev)
    monkeypatch.setattr(mostbet.httpx, "AsyncClient",
                        _ScriptedClient([_Response(status=500)]))

    result = asyncio.run(_mostbet_load_matches())
    assert isinstance(result, list)


# ─── Corrupted / unreadable database ──────────────────────────────────────────

def test_corrupt_conversation_json_returns_empty(temp_db):
    uid = 850001
    with temp_db.con() as c:
        c.execute("INSERT OR REPLACE INTO conversation (user_id, messages, fixture_key) "
                  "VALUES (?,?,'')", (uid, "{not json"))
    assert temp_db.db_get_conv(uid) == []


def test_db_get_survives_an_unreadable_database(temp_db, monkeypatch):
    """db_get is on the hot path of every forecast; a DB error must degrade to
    None rather than take the handler down."""
    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(temp_db.sqlite3, "connect", _boom)
    assert temp_db.db_get(850002) is None


def test_db_all_uids_survives_an_unreadable_database(temp_db, monkeypatch):
    """The broadcast path: a failure here must return no recipients, not raise
    inside the background task."""
    def _boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(temp_db.sqlite3, "connect", _boom)
    assert temp_db.db_all_uids() == []


def test_db_log_req_swallows_write_failures(temp_db, monkeypatch):
    """Request logging is telemetry; it must never break a user's forecast."""
    def _boom(*a, **kw):
        raise sqlite3.OperationalError("readonly database")

    monkeypatch.setattr(temp_db.sqlite3, "connect", _boom)
    temp_db.db_log_req(850003, "text")      # must not raise


def test_lang_lookup_falls_back_when_the_row_is_junk(temp_db):
    uid = 850004
    temp_db.db_ensure(uid, "u", "ru")
    temp_db.db_set(uid, "lang", "!!!not-a-lang!!!")
    assert temp_db.db_lang(uid) == temp_db.DEFAULT_LANG
