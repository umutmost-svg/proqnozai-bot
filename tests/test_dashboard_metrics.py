"""Dashboard metrics: what /stats measures, and how the page renders it.

Offline — the stats server talks to the temp DB, the dashboard's HTTP calls are
mocked.
"""
import base64
import json

import pytest

import dashboard as dash
import stats_server as ss
from db import REQ_FORECAST, REQ_TEXT


TOKEN = dash.STATS_TOKEN
AUTH = {"Authorization": "Basic " + base64.b64encode(f"admin:{TOKEN}".encode()).decode()}


@pytest.fixture()
def seeded(temp_db):
    ss._stats_cache.update(at=0.0, data=None)   # never reuse another test's snapshot
    uid = 920001
    temp_db.db_ensure(uid, "seed", "ru")
    temp_db.db_set(uid, "is_registered", 1)
    temp_db.db_log_req(uid, REQ_FORECAST, ok=True, ms=1200)
    temp_db.db_log_req(uid, REQ_TEXT)
    return temp_db


# ─── /stats content ───────────────────────────────────────────────────────────

def test_forecast_volume_comes_from_the_event_log(seeded):
    """forecast_history keeps only 10 rows per user, so charting it understated
    every busy day. The daily series must come from `requests`."""
    data = ss._collect()
    assert data["forecasts_daily"], "forecast series must not be empty after a forecast"
    assert sum(r[1] for r in data["forecasts_daily"]) == data["forecasts_real_total"]


def test_week_over_week_baselines_are_present(seeded):
    data = ss._collect()
    for key in ("users_prev_week", "reqs_prev_week", "forecasts_prev_week",
                "users_active_yday", "forecasts_week"):
        assert key in data


def test_repeat_rate_is_reported(seeded):
    data = ss._collect()
    assert data["users_with_activity"] >= 1
    assert 0 <= data["repeat_pct"] <= 100


def test_action_breakdown_counts_event_types(seeded):
    """A dead entry point should be visible as a missing/So-low row here."""
    before = dict(ss._collect()["by_action"])
    seeded.db_log_req(920001, REQ_FORECAST, ok=True, ms=900)
    after = dict(ss._collect()["by_action"])
    assert after[REQ_FORECAST] == before.get(REQ_FORECAST, 0) + 1
    assert after.get(REQ_TEXT, 0) == before.get(REQ_TEXT, 0)


def test_broadcast_metrics_are_part_of_stats(seeded):
    assert "broadcasts" in ss._collect()


def test_stats_are_cached_between_polls(seeded, monkeypatch):
    """Several open tabs poll independently; one collection pass should serve
    them all within the TTL."""
    calls = []
    monkeypatch.setattr(ss, "_collect", lambda: calls.append(1) or {"x": 1})
    ss._stats_cache.update(at=0.0, data=None)
    ss._stats_payload()
    ss._stats_payload()
    assert len(calls) == 1


def test_cache_expires(seeded, monkeypatch):
    calls = []
    monkeypatch.setattr(ss, "_collect", lambda: calls.append(1) or {"x": 1})
    monkeypatch.setattr(ss, "STATS_TTL", -1)
    ss._stats_cache.update(at=0.0, data=None)
    ss._stats_payload()
    ss._stats_payload()
    assert len(calls) == 2


# ─── Page rendering ───────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_page_renders_with_a_partial_payload(monkeypatch):
    """The worker and the web process deploy independently, so an older worker
    can omit the newest keys. That must not blank the dashboard."""
    monkeypatch.setattr(dash.httpx, "get", lambda *a, **k: _Resp({"fb_total": 0}))
    r = dash.app.test_client().get("/", headers=AUTH)
    assert r.status_code == 200


def test_forecast_chart_gets_its_own_series(monkeypatch):
    """The bar chart used to plot the request series twice, so "forecasts per
    day" showed request volume."""
    import re
    payload = {"fb_total": 0, "fb_wins": 0,
               "daily": [["2026-08-18", 7]],
               "forecasts_daily": [["2026-08-18", 2]]}
    monkeypatch.setattr(dash.httpx, "get", lambda *a, **k: _Resp(payload))
    body = dash.app.test_client().get("/", headers=AUTH).get_data(as_text=True)
    assert re.search(r"dailyData\s*=\s*\[7\]", body)
    assert re.search(r"fcData\s*=\s*\[2\]", body)


def test_delta_chip_hidden_without_a_baseline():
    assert dash._delta(10, 0) == {"show": False}


def test_delta_chip_reports_direction():
    up = dash._delta(12, 10)
    down = dash._delta(8, 10)
    assert (up["pct"], up["cls"]) == (20, "d-up")
    assert (down["pct"], down["cls"]) == (20, "d-down")


def test_moscow_conversion_for_the_operator():
    assert dash._msk("2026-08-19 07:30:00") == "19.08 10:30"
