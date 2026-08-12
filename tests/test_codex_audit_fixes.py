"""Regressions found by an independent audit of the release.

One per finding, so each stays fixed. Offline.
"""
import base64

import pytest

import dashboard as dash

TOKEN = "offline-test-dashboard-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dash, "STATS_TOKEN", TOKEN)
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


def _auth():
    raw = base64.b64encode(f"admin:{TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# ─── CSRF on state-changing routes ────────────────────────────────────────────

def test_broadcast_rejects_a_cross_site_post(client, monkeypatch):
    """Basic Auth alone doesn't stop CSRF: the browser replays credentials on a
    cross-site POST, which would fire a broadcast to the whole user base."""
    sent = []
    monkeypatch.setattr(dash.httpx, "post", lambda *a, **k: sent.append(a) or None)

    r = client.post("/broadcast", headers={**_auth(), "Origin": "https://evil.example"},
                    data={"text": "spam", "segment": "all"})
    assert r.status_code == 403
    assert not sent                     # nothing reached the worker


def test_broadcast_rejects_a_post_with_no_origin_and_no_token(client, monkeypatch):
    sent = []
    monkeypatch.setattr(dash.httpx, "post", lambda *a, **k: sent.append(a) or None)
    r = client.post("/broadcast", headers=_auth(), data={"text": "x", "segment": "all"})
    assert r.status_code == 403
    assert not sent


def test_broadcast_accepts_the_form_token(client, monkeypatch):
    calls = []

    class _R:
        status_code = 200
        def json(self):
            return {"started": 3}

    monkeypatch.setattr(dash.httpx, "post", lambda *a, **k: calls.append(k) or _R())
    r = client.post("/broadcast", headers=_auth(),
                    data={"text": "hi", "segment": "all", "csrf": dash.csrf_token()})
    assert r.status_code == 200
    assert calls and calls[-1]["json"]["text"] == "hi"


def test_broadcast_accepts_a_same_origin_post(client, monkeypatch):
    class _R:
        status_code = 200
        def json(self):
            return {"started": 1}

    monkeypatch.setattr(dash.httpx, "post", lambda *a, **k: _R())
    r = client.post("/broadcast",
                    headers={**_auth(), "Origin": "http://localhost"},
                    data={"text": "hi", "segment": "all"})
    assert r.status_code == 200


def test_block_endpoint_is_csrf_protected_too(client, monkeypatch):
    sent = []
    monkeypatch.setattr(dash.httpx, "post", lambda *a, **k: sent.append(a) or None)
    r = client.post("/api/users/block", headers=_auth(), json={"user_id": 1, "blocked": 1})
    assert r.status_code == 403
    assert not sent


def test_the_form_carries_the_token(client, monkeypatch):
    monkeypatch.setattr(dash.httpx, "get", lambda *a, **k: None)
    body = client.get("/broadcast", headers=_auth()).get_data(as_text=True)
    assert f'name="csrf" value="{dash.csrf_token()}"' in body


def test_token_is_derived_from_the_dashboard_secret(monkeypatch):
    monkeypatch.setattr(dash, "STATS_TOKEN", "one")
    a = dash.csrf_token()
    monkeypatch.setattr(dash, "STATS_TOKEN", "two")
    assert dash.csrf_token() != a
    assert TOKEN not in a               # the secret itself never ships to the page


# ─── the promo card reads the current payload shape ───────────────────────────

def test_promo_card_renders_configured_campaigns(temp_db):
    """The funnel switched to per-partner codes and stopped returning `code`;
    the template still tested `pr.code`, so the card always claimed there was
    no campaign."""
    from jinja2 import Environment
    payload = temp_db.db_promo_funnel()
    assert "partners" in payload and "code" not in payload

    temp_db.db_set_promo_code("Auditor", "AUD-1", 10)
    temp_db.db_claim_promos(760001)
    pr = temp_db.db_promo_funnel()
    assert pr["partners"], "a configured campaign must be visible to the template"

    # Render the card's condition the way Jinja does.
    env = Environment()
    tpl = env.from_string("{% if pr.partners %}shown{% else %}empty{% endif %}")
    assert tpl.render(pr=pr) == "shown"


# ─── one code cannot belong to two partners ───────────────────────────────────

def test_same_code_for_two_partners_is_refused(temp_db):
    """Claims are keyed by the code string, so sharing one would merge two
    partners' caps and counts."""
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_campaign")
    temp_db.db_set_promo_code("First", "SHARED", 5)
    with pytest.raises(ValueError):
        temp_db.db_set_promo_code("Second", "SHARED", 5)
    # Re-setting the SAME partner's own code is still fine.
    temp_db.db_set_promo_code("First", "SHARED", 9)
    assert temp_db.db_list_promo_codes()[0]["max_uses"] == 9


# ─── feedback coverage counts every forecast ──────────────────────────────────

def test_coverage_denominator_is_not_the_capped_history(temp_db):
    """forecast_history keeps ten rows per user; using it as the denominator
    would shrink it over time and overstate coverage."""
    uid = 760100
    temp_db.db_ensure(uid, "u", "ru")
    for i in range(15):
        temp_db.db_save_history(uid, f"q{i}", "f")
        temp_db.db_log_req(uid, temp_db.REQ_FORECAST, ok=True, ms=10)
    cov = temp_db.db_feedback_coverage()
    with temp_db.con() as c:
        events = c.execute("SELECT COUNT(*) FROM requests "
                           "WHERE msg_type='FORECAST'").fetchone()[0]
    assert cov["total"] == events
    assert cov["pct"] <= 100
