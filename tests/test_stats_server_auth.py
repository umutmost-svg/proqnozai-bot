"""End-to-end auth for the internal stats server.

Exercises the real ThreadingHTTPServer over a loopback socket, so the checks
cover request parsing and routing rather than just the helper. No external
network: the server under test is started in-process on port 0.
"""
import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

import stats_server as ss

TOKEN = "offline-test-dashboard-token"
PROTECTED_GET = ["/stats", "/broadcast/status", "/broadcast/list",
                 "/segment/size?s=all", "/users/search?q=nobody"]


@pytest.fixture
def server(monkeypatch, temp_db):
    monkeypatch.setattr(ss, "STATS_TOKEN", TOKEN)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ss._Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _hdr(token=TOKEN):
    return {"X-Dashboard-Token": token}


# ─── header auth works everywhere ─────────────────────────────────────────────

@pytest.mark.parametrize("path", PROTECTED_GET)
def test_header_auth_accepted_on_every_protected_get(server, path):
    r = httpx.get(server + path, headers=_hdr(), timeout=5)
    assert r.status_code == 200


def test_header_auth_accepted_on_users_block(server, temp_db):
    temp_db.db_ensure(860001, "u", "ru")
    r = httpx.post(server + "/users/block", headers=_hdr(),
                   json={"user_id": 860001, "blocked": 1}, timeout=5)
    assert r.status_code == 200
    assert temp_db.db_is_blocked(860001) is True


# ─── the query-string token is dead ───────────────────────────────────────────

@pytest.mark.parametrize("path", ["/stats", "/broadcast/status", "/broadcast/list"])
def test_query_token_is_rejected(server, path):
    """Regression: the token must not be usable from the URL, where proxies
    would log it."""
    r = httpx.get(f"{server}{path}?token={TOKEN}", timeout=5)
    assert r.status_code == 401


def test_query_token_rejected_on_user_search(server):
    r = httpx.get(f"{server}/users/search?q=x&token={TOKEN}", timeout=5)
    assert r.status_code == 401


def test_body_token_is_rejected_on_post(server):
    r = httpx.post(server + "/users/block",
                   json={"token": TOKEN, "user_id": 1, "blocked": 1}, timeout=5)
    assert r.status_code == 401


# ─── missing / wrong credentials ──────────────────────────────────────────────

@pytest.mark.parametrize("path", PROTECTED_GET)
def test_no_token_is_rejected(server, path):
    assert httpx.get(server + path, timeout=5).status_code == 401


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_wrong_token_is_rejected(server, path):
    r = httpx.get(server + path, headers=_hdr("nope"), timeout=5)
    assert r.status_code == 401


def test_broadcast_requires_auth(server):
    r = httpx.post(server + "/broadcast", json={"text": "hi", "segment": "all"}, timeout=5)
    assert r.status_code == 401


def test_unconfigured_token_locks_everything(monkeypatch, temp_db):
    """An empty DASHBOARD_TOKEN must fail closed, not open."""
    monkeypatch.setattr(ss, "STATS_TOKEN", "")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ss._Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert httpx.get(base + "/stats", headers=_hdr(""), timeout=5).status_code == 503
        assert httpx.get(base + "/stats", headers=_hdr(), timeout=5).status_code == 503
    finally:
        srv.shutdown(); srv.server_close(); thread.join(timeout=5)


# ─── unauthenticated surface stays minimal ────────────────────────────────────

def test_health_needs_no_token(server):
    r = httpx.get(server + "/health", timeout=5)
    assert r.status_code == 200 and r.text == "ok"


def test_unknown_path_is_404_not_a_leak(server):
    r = httpx.get(server + "/../secrets", headers=_hdr(), timeout=5)
    assert r.status_code == 404


def test_stats_payload_carries_no_token(server):
    r = httpx.get(server + "/stats", headers=_hdr(), timeout=5)
    body = json.dumps(r.json())
    assert TOKEN not in body
