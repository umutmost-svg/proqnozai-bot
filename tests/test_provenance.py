"""Offline tests for provenance/freshness metadata (no network)."""
import re

from provenance import Provenance


def test_header_contains_all_fields():
    p = Provenance(source="api-football", fetched_at="2026-07-11T12:00:00+00:00")
    h = p.header()
    assert "SOURCE: api-football" in h
    assert "fetched_at: 2026-07-11T12:00:00+00:00" in h
    assert "stale: no" in h
    assert "missing: none" in h


def test_missing_fields_listed():
    p = Provenance(source="api-football", missing=["injuries", "lineups"])
    assert "missing: injuries, lineups" in p.header()


def test_stale_flag_rendered():
    assert "stale: yes" in Provenance(source="football-data", stale=True).header()


def test_default_fetched_at_is_iso_utc():
    p = Provenance(source="x")
    # Parses as ISO 8601 and carries an offset (UTC).
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", p.fetched_at)
    assert p.fetched_at.endswith("+00:00")


def test_wrap_prepends_header_above_block():
    p = Provenance(source="api-football", fetched_at="2026-07-11T00:00:00+00:00")
    wrapped = p.wrap("Arsenal last 5: ...")
    lines = wrapped.splitlines()
    assert lines[0].startswith("[SOURCE:")
    assert lines[1] == "Arsenal last 5: ..."


def test_wrap_empty_block_returns_header_only():
    p = Provenance(source="api-football")
    assert p.wrap("") == p.header()
