"""A campaign with no partner name, left behind by a migration.

`ALTER TABLE promo_campaign ADD COLUMN partner TEXT DEFAULT ''` gave every
pre-existing campaign an empty name. Such a row is handed to every user like
any other, but the dashboard's orphan list filtered on a truthy name and
/delpromo needs a name to pass — so it could be neither seen nor switched off.
"""
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

import dashboard as dash
import stats_server as ss

TOKEN = "offline-test-dashboard-token"


@pytest.fixture
def server(monkeypatch, clean):
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


def _production_state(clean):
    """What /promodiag reported: an unnamed legacy campaign next to a pool."""
    clean.db_set_promo_code("", "BOT200", 300)
    clean.db_partner_add("Mostbet", "https://mb.example")
    clean.db_promo_pool_import("Mostbet", ["MB-0001", "MB-0002"])


# ── what it does today ───────────────────────────────────────────────────────

def test_an_unnamed_campaign_is_handed_out_like_any_other(clean):
    _production_state(clean)
    granted = clean.db_claim_promos(994001)
    assert {g["code"] for g in granted} == {"BOT200", "MB-0001"}


# ── it must be visible ───────────────────────────────────────────────────────

def test_it_appears_in_the_orphan_list(clean):
    _production_state(clean)
    orphans = ss._partners_payload()["orphan_promos"]
    assert [o["code"] for o in orphans] == ["BOT200"]


def test_a_named_orphan_still_appears(clean):
    """The existing case must not regress: a code set for a partner that was
    never added."""
    clean.db_set_promo_code("Ghost", "GH-1", 5)
    orphans = ss._partners_payload()["orphan_promos"]
    assert [o["partner"] for o in orphans] == ["Ghost"]


def test_a_campaign_attached_to_a_partner_is_not_an_orphan(clean):
    _production_state(clean)
    orphans = ss._partners_payload()["orphan_promos"]
    assert "Mostbet" not in [o["partner"] for o in orphans]


def test_an_archived_campaign_is_not_listed(clean):
    clean.db_set_promo_code("", "BOT200", 300)
    clean.db_promo_archive("")
    assert ss._partners_payload()["orphan_promos"] == []


# ── it must be switchable off ────────────────────────────────────────────────

def test_archiving_by_empty_name_over_http(server, clean):
    _production_state(clean)
    r = httpx.post(f"{server}/promo/archive", json={"partner": ""},
                   headers=_hdr(), timeout=10)
    assert r.status_code == 200
    assert r.json()["archived"] is True
    # The pool is untouched; only the unnamed campaign stops being issued.
    assert [g["code"] for g in clean.db_claim_promos(994010)] == ["MB-0001"]


def test_archiving_keeps_the_claims(clean):
    clean.db_set_promo_code("", "BOT200", 300)
    clean.db_claim_promos(994020)
    clean.db_promo_archive("")
    with clean.con() as c:
        assert c.execute("SELECT COUNT(*) FROM promo_claims WHERE code='BOT200'"
                         ).fetchone()[0] == 1


def test_archiving_a_named_orphan_over_http(server, clean):
    clean.db_set_promo_code("Ghost", "GH-1", 5)
    r = httpx.post(f"{server}/promo/archive", json={"partner": "Ghost"},
                   headers=_hdr(), timeout=10)
    assert r.json()["archived"] is True
    assert clean.db_claim_promos(994030) == []


def test_a_missing_name_is_a_400(server, clean):
    r = httpx.post(f"{server}/promo/archive", json={}, headers=_hdr(), timeout=10)
    assert r.status_code == 400


def test_the_route_needs_the_token(server, clean):
    r = httpx.post(f"{server}/promo/archive", json={"partner": ""}, timeout=10)
    assert r.status_code == 401


def test_archiving_something_that_is_not_there_is_not_an_error(server, clean):
    r = httpx.post(f"{server}/promo/archive", json={"partner": "nope"},
                   headers=_hdr(), timeout=10)
    assert r.status_code == 200
    assert r.json()["archived"] is False


# ── the dashboard proxy ──────────────────────────────────────────────────────

@pytest.fixture
def dash_client(monkeypatch):
    monkeypatch.setattr(dash, "STATS_TOKEN", TOKEN)
    dash.app.config["TESTING"] = True
    with dash.app.test_client() as client:
        yield client


def _login():
    import base64
    raw = base64.b64encode(f"admin:{TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def proxied(monkeypatch):
    sent = {}

    def fake_request(method, url, **kw):
        sent.update(method=method, url=url, json=kw.get("json"))
        return httpx.Response(200, json={"archived": True})

    monkeypatch.setattr(dash.httpx, "request", fake_request)
    return sent


def test_the_empty_name_survives_the_proxy(dash_client, proxied):
    """An empty string must reach the worker as an empty string — anything that
    treats it as "missing" puts the campaign back out of reach."""
    r = dash_client.post("/api/promo/archive", json={"partner": ""},
                         headers={**_login(), "X-CSRF-Token": dash.csrf_token()})
    assert r.status_code == 200
    assert proxied["json"] == {"partner": ""}
    assert proxied["url"].endswith("/promo/archive")


def test_it_needs_csrf(dash_client, proxied):
    r = dash_client.post("/api/promo/archive", json={"partner": ""},
                         headers=_login())
    assert r.status_code == 403
    assert proxied == {}


def test_it_needs_login(dash_client):
    assert dash_client.post("/api/promo/archive", json={"partner": ""}).status_code == 401


def test_the_page_offers_the_button(dash_client):
    page = dash_client.get("/partners", headers=_login()).get_data(as_text=True)
    assert "archiveOrphan" in page
    assert "Без названия" in page
