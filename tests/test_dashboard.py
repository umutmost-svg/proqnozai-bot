"""Offline tests for the web dashboard (dashboard.py).

No network: every call to the stats backend goes through httpx, which is mocked
here. conftest sets DASHBOARD_TOKEN before import, so auth is exercisable.
"""
import base64
import importlib

import httpx
import pytest

import dashboard

TOKEN = dashboard.STATS_TOKEN  # set by conftest to the offline test token
GOOD_AUTH = {"Authorization": "Basic " + base64.b64encode(
    f"admin:{TOKEN}".encode()).decode()}


@pytest.fixture()
def client():
    return dashboard.app.test_client()


class _FakeResp:
    def __init__(self, payload, status=200, text=None):
        self._payload = payload
        self.status_code = status
        self.text = text if text is not None else "{}"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


_STATS = {"fb_total": 10, "fb_wins": 6, "daily": [], "langs": [],
          "forecasts_total": 20, "winrate_daily": []}


@pytest.fixture()
def mock_backend(monkeypatch):
    """Stats backend reachable, returning valid data."""
    monkeypatch.setattr(dashboard.httpx, "get",
                        lambda *a, **k: _FakeResp(_STATS, text="{\"ok\":true}"))


@pytest.fixture()
def down_backend(monkeypatch):
    """Stats backend unreachable — httpx raises, no real network touched."""
    def _boom(*a, **k):
        raise httpx.ConnectError("worker unreachable")
    monkeypatch.setattr(dashboard.httpx, "get", _boom)
    monkeypatch.setattr(dashboard.httpx, "post", _boom)


# ─── App / import ─────────────────────────────────────────────────────────────
def test_dashboard_imports():
    mod = importlib.import_module("dashboard")
    assert mod.app is not None


def test_port_config_reads_env(monkeypatch):
    monkeypatch.setenv("PORT", "7777")
    assert dashboard._port() == 7777
    monkeypatch.delenv("PORT", raising=False)
    assert dashboard._port() == 5000  # local default
    monkeypatch.setenv("PORT", "not-a-number")
    assert dashboard._port() == 5000  # invalid value handled safely


# ─── Health ───────────────────────────────────────────────────────────────────
def test_health_route_open(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_data(as_text=True) == "ok"


# ─── Auth ─────────────────────────────────────────────────────────────────────
def test_root_requires_auth(client):
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_auth_failure_bad_credentials(client):
    bad = base64.b64encode(b"admin:wrong-password").decode()
    r = client.get("/", headers={"Authorization": "Basic " + bad})
    assert r.status_code == 401


def test_auth_success_renders_root(client, mock_backend):
    r = client.get("/", headers=GOOD_AUTH)
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.get_data(as_text=True)


# ─── API ──────────────────────────────────────────────────────────────────────
def test_api_data_success(client, mock_backend):
    r = client.get("/api/data", headers=GOOD_AUTH)
    assert r.status_code == 200
    assert r.mimetype == "application/json"


# ─── Graceful failure & no secret leak ────────────────────────────────────────
def test_root_backend_down_is_graceful(client, down_backend):
    r = client.get("/", headers=GOOD_AUTH)
    assert r.status_code == 503
    body = r.get_data(as_text=True)
    assert "railway.internal" not in body      # internal URL not leaked
    assert TOKEN not in body                    # token not leaked
    assert "Traceback" not in body


def test_api_backend_down_is_graceful(client, down_backend):
    r = client.get("/api/data", headers=GOOD_AUTH)
    assert r.status_code == 503
    body = r.get_data(as_text=True)
    assert body == '{"error": "stats backend unavailable"}'
    assert "railway.internal" not in body
    assert TOKEN not in body


# ─── Missing configuration handled safely ─────────────────────────────────────
def test_missing_token_returns_503(client, monkeypatch):
    # No DASHBOARD_TOKEN configured → authed routes fail closed with 503,
    # never a crash and never open access.
    monkeypatch.setattr(dashboard, "STATS_TOKEN", "")
    r = client.get("/", headers=GOOD_AUTH)
    assert r.status_code == 503


def test_health_works_without_token(client, monkeypatch):
    # Health must stay up even with no token, so the platform health check passes.
    monkeypatch.setattr(dashboard, "STATS_TOKEN", "")
    r = client.get("/health")
    assert r.status_code == 200
