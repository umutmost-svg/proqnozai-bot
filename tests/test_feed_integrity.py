"""Offline tests for feed-generation integrity: a partial paginated fetch must
never replace the previous full generation outright — the fresh head is merged
with the previous tail, and a suspiciously shrunken "complete" result is
treated as silent truncation. No network."""
import asyncio
import json


import mostbet
from mostbet import _merge_generations, _publish_generation, _mostbet_load_matches


def _gen(ids, src):
    return [{"id": i, "src": src} for i in ids]


# ─── Pure publication logic ───────────────────────────────────────────────────

def test_merge_takes_fresh_head_and_prev_tail():
    fresh = _gen(range(1, 101), "fresh")
    prev = _gen(range(1, 151), "prev")
    merged = _merge_generations(fresh, 100, prev)
    assert len(merged) == 150
    assert all(m["src"] == "fresh" for m in merged[:100])   # head is fresh
    assert all(m["src"] == "prev" for m in merged[100:])    # tail preserved
    assert len({m["id"] for m in merged}) == 150            # no duplicate ids


def test_partial_with_previous_generation_merges():
    fresh = _gen(range(1, 51), "fresh")
    prev = _gen(range(1, 201), "prev")
    out = _publish_generation(fresh, 50, complete=False, prev=prev)
    assert len(out) == 200                                   # nothing lost
    assert out[0]["src"] == "fresh"


def test_partial_without_previous_publishes_what_exists():
    fresh = _gen(range(1, 51), "fresh")
    assert _publish_generation(fresh, 50, complete=False, prev=[]) == fresh


def test_complete_normal_shrink_publishes_fresh_only():
    # Feeds legitimately shrink a little between cycles — no merge, no ghosts.
    fresh = _gen(range(1, 81), "fresh")
    prev = _gen(range(1, 101), "prev")
    out = _publish_generation(fresh, 80, complete=True, prev=prev)
    assert out == fresh                                      # 80% of prev: fine


def test_complete_but_sharply_shrunken_is_suspect_and_merged():
    # "Complete"-looking result at <60% of the previous generation = suspected
    # silent truncation (e.g. 200-with-empty-body mid-stream) → keep the tail.
    fresh = _gen(range(1, 51), "fresh")
    prev = _gen(range(1, 201), "prev")
    out = _publish_generation(fresh, 50, complete=True, prev=prev)
    assert len(out) == 200


def test_empty_fresh_with_previous_serves_previous():
    prev = _gen(range(1, 101), "prev")
    out = _publish_generation([], 0, complete=False, prev=prev)
    assert out == prev


# ─── Integration: pagination break mid-fetch ──────────────────────────────────

class _SeqResponse:
    def __init__(self, items):
        self.status_code = 200
        self.headers: dict = {}
        self._items = items
        self.text = json.dumps({"lineMatches": items})

    def json(self):
        return {"lineMatches": self._items}


class _SeqClient:
    """Serves queued pages; then raises — simulates a network drop mid-fetch."""

    def __init__(self, pages):
        self._pages = list(pages)

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        if not self._pages:
            raise mostbet.httpx.ConnectError("network drop")
        return _SeqResponse(self._pages.pop(0))


def test_pagination_break_merges_previous_tail(monkeypatch, clean_mostbet_cache):
    """One full page arrives, then the network drops. Previously the 100-item
    head was cached as THE list for 15 minutes; now the previous generation's
    tail survives, so no tournament silently disappears."""
    prev = _gen(range(1, 151), "prev")
    clean_mostbet_cache["all_matches"] = (0, prev)          # expired → refetch

    page1 = _gen(range(1, 101), "fresh")                    # full page, then drop
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", _SeqClient([page1]))

    result = asyncio.run(_mostbet_load_matches())

    assert len(result) == 150
    assert result[0]["src"] == "fresh"                      # head refreshed
    assert result[-1] == {"id": 150, "src": "prev"}         # tail preserved
    ts, cached = clean_mostbet_cache["all_matches"]
    assert cached == result                                  # merged gen cached


def test_natural_end_publishes_fresh_generation(monkeypatch, clean_mostbet_cache):
    prev = _gen(range(1, 121), "prev")
    clean_mostbet_cache["all_matches"] = (0, prev)

    page1 = _gen(range(1, 101), "fresh")
    page2 = _gen(range(101, 111), "fresh")                  # short page = end
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", _SeqClient([page1, page2]))

    result = asyncio.run(_mostbet_load_matches())

    assert len(result) == 110                                # 110/120 ≥ 60%: fresh wins
    assert all(m["src"] == "fresh" for m in result)
