"""Dashboard routes against a mocked worker API.

The dashboard is a thin proxy: Basic-auth at the edge, then an authenticated
call to the worker's stats server. These tests replace httpx so no socket is
opened, and assert the two things that matter — the secret travels in a header,
and a dead backend degrades instead of leaking internals.
"""
import base64
import json

import pytest

import dashboard as dash

TOKEN = "offline-test-dashboard-token"

_STATS_PAYLOAD = {
    "users_total": 3, "users_today": 1, "users_week": 2, "users_blocked": 0,
    "users_active_today": 1, "users_active_week": 2,
    "reqs_total": 9, "reqs_today": 2, "reqs_week": 5,
    "forecasts_total": 4, "forecasts_today": 1, "fb_total": 2, "fb_wins": 1,
    "live_subs": 0, "live_matches": 0,
    "langs": [["ru", 2], ["az", 1]], "top_users": [], "daily": [],
    "forecasts_daily": [], "winrate_daily": [], "recent_users": [],
    "recent_forecasts": [],
}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dash, "STATS_TOKEN", TOKEN)
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


@pytest.fixture
def calls(monkeypatch):
    """Capture outbound worker calls; default to a healthy stats response."""
    recorded = []

    def _get(url, **kw):
        recorded.append(("GET", url, kw))
        return _Resp(_STATS_PAYLOAD)

    def _post(url, **kw):
        recorded.append(("POST", url, kw))
        return _Resp({"started": 1})

    monkeypatch.setattr(dash.httpx, "get", _get)
    monkeypatch.setattr(dash.httpx, "post", _post)
    return recorded


def _auth():
    raw = base64.b64encode(f"admin:{TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# ─── edge auth ────────────────────────────────────────────────────────────────

def test_dashboard_requires_basic_auth(client, calls):
    r = client.get("/")
    assert r.status_code == 401
    assert not calls                      # nothing forwarded to the worker


def test_wrong_password_is_rejected(client, calls):
    raw = base64.b64encode(b"admin:wrong").decode()
    r = client.get("/", headers={"Authorization": f"Basic {raw}"})
    assert r.status_code == 401
    assert not calls


def test_authenticated_index_renders(client, calls):
    r = client.get("/", headers=_auth())
    assert r.status_code == 200
    assert calls and calls[0][0] == "GET"


# ─── the secret travels in a header, never in the URL ─────────────────────────

def test_stats_call_uses_the_header(client, calls):
    client.get("/api/data", headers=_auth())
    _, url, kw = calls[-1]
    assert kw["headers"]["X-Dashboard-Token"] == TOKEN
    assert "token=" not in url


def test_user_search_uses_the_header(client, calls):
    client.get("/api/users/search?q=alice", headers=_auth())
    _, url, kw = calls[-1]
    assert kw["headers"]["X-Dashboard-Token"] == TOKEN
    assert kw["params"] == {"q": "alice"}
    assert "token=" not in url


def test_block_call_uses_the_header_not_the_body(client, calls):
    client.post("/api/users/block", headers=_auth(),
                json={"user_id": 5, "blocked": 1})
    method, url, kw = calls[-1]
    assert method == "POST"
    assert kw["headers"]["X-Dashboard-Token"] == TOKEN
    assert "token" not in kw["json"]


def test_broadcast_status_uses_the_header(client, calls):
    client.get("/api/broadcast/status", headers=_auth())
    _, url, kw = calls[-1]
    assert kw["headers"]["X-Dashboard-Token"] == TOKEN
    assert "token=" not in url


# ─── backend failures degrade safely ──────────────────────────────────────────

def test_dead_backend_returns_a_page_not_a_traceback(client, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("connection refused to http://worker.internal:8888/stats")

    monkeypatch.setattr(dash.httpx, "get", _boom)
    r = client.get("/", headers=_auth())
    body = r.get_data(as_text=True)
    # 503 with a readable placeholder page — not a 500, not a stack trace.
    assert r.status_code == 503
    assert "Dashboard temporarily unavailable" in body
    assert "Traceback" not in body
    assert TOKEN not in body
    assert "worker.internal" not in body


def test_dead_backend_api_route_returns_503_json(client, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(dash.httpx, "get", _boom)
    r = client.get("/api/data", headers=_auth())
    assert r.status_code == 503
    assert r.get_json() == {"error": "stats backend unavailable"}


def test_malformed_backend_payload_degrades(client, monkeypatch):
    monkeypatch.setattr(dash.httpx, "get", lambda *a, **kw: _Resp(["not", "a", "dict"]))
    r = client.get("/", headers=_auth())
    assert r.status_code == 503
    body = r.get_data(as_text=True)
    assert "Dashboard temporarily unavailable" in body
    assert "Traceback" not in body


def test_error_summary_never_includes_the_message():
    """_safe_err is the only thing that reaches the log."""
    class _E(Exception):
        pass
    assert dash._safe_err(_E(f"boom ?token={TOKEN}")) == "_E"
