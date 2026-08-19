"""Partners & promo managed from the dashboard.

Three layers, all offline:

  * the DB layer — partners CRUD, env bootstrap, promo lifecycle;
  * the worker's HTTP CRUD, over a real loopback socket (as in
    test_stats_server_auth.py), so routing and auth are genuinely exercised;
  * the dashboard proxy, with httpx replaced so no socket is opened.

The point of the whole feature is that an edit needs no restart, so several
tests below assert exactly that: mutate through the API, then read what the
bot's own render path would produce.
"""
import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

import dashboard as dash
import stats_server as ss

TOKEN = "offline-test-dashboard-token"


# ─── fixtures ─────────────────────────────────────────────────────────────────

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


# ══ PARTNERS — DB layer ═══════════════════════════════════════════════════════

def test_create_partner(clean):
    pid = clean.db_partner_add("Mostbet", "https://mostbet.com")
    row = clean.db_get_partner(pid)
    assert row["name"] == "Mostbet" and row["url"] == "https://mostbet.com"
    assert row["is_active"] is True and row["is_archived"] is False
    assert row["created_at"] and row["updated_at"]


def test_edit_name_moves_promo_and_clicks_with_it(clean):
    pid = clean.db_partner_add("Old", "https://a.example")
    clean.db_set_promo_code("Old", "CODE-1", 5)
    clean.db_log_partner_click(1, "Old")
    clean.db_partner_update(pid, name="New")
    assert clean.db_get_partner(pid)["name"] == "New"
    assert [c["partner"] for c in clean.db_list_promo_codes()] == ["New"]
    assert dict(clean.db_partner_clicks()["by_partner"] and
                {r[0]: r[1] for r in clean.db_partner_clicks()["by_partner"]}).get("New") == 1


def test_edit_url(clean):
    pid = clean.db_partner_add("Mostbet", "https://old.example")
    clean.db_partner_update(pid, url="https://new.example")
    assert clean.db_get_partner(pid)["url"] == "https://new.example"


def test_enable_and_disable(clean):
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_partner_update(pid, is_active=False)
    assert clean.db_active_partners() == []
    clean.db_partner_update(pid, is_active=True)
    assert clean.db_active_partners() == [("Mostbet", "https://a.example")]


def test_archive_is_a_soft_delete(clean):
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    assert clean.db_partner_archive(pid) is True
    assert clean.db_active_partners() == []
    assert clean.db_list_partners() == []
    # The row survives, so click history recorded against the name still reads.
    archived = clean.db_list_partners(include_archived=True)
    assert [p["name"] for p in archived] == ["Mostbet"]
    assert clean.db_partner_archive(pid) is False       # idempotent


def test_active_list_is_ordered_and_filtered(clean):
    clean.db_partner_add("A", "https://a.example")
    b = clean.db_partner_add("B", "https://b.example")
    clean.db_partner_add("C", "https://c.example")
    clean.db_partner_update(b, is_active=False)
    assert clean.db_active_partners() == [
        ("A", "https://a.example"), ("C", "https://c.example")]


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<h1>x",
    "file:///etc/passwd", "ftp://x.example", "not a url", "", "   ",
    "https://", None, 42,
])
def test_unsafe_or_malformed_urls_are_refused(clean, bad):
    with pytest.raises(ValueError):
        clean.db_partner_add("Bad", bad)


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 65])
def test_invalid_names_are_refused(clean, bad):
    with pytest.raises(ValueError):
        clean.db_partner_add(bad, "https://a.example")


def test_duplicate_live_name_is_refused(clean):
    clean.db_partner_add("Mostbet", "https://a.example")
    with pytest.raises(ValueError):
        clean.db_partner_add("Mostbet", "https://b.example")


def test_archived_name_can_be_reused(clean):
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_partner_archive(pid)
    assert clean.db_partner_add("Mostbet", "https://b.example") != pid


# ══ ENV bootstrap ═════════════════════════════════════════════════════════════

def _rearm_bootstrap(db):
    with db.con() as c:
        c.execute("DELETE FROM _migrations WHERE key='partners_env_bootstrap'")


def test_env_bootstrap_imports_partners(clean, monkeypatch):
    import config
    monkeypatch.setattr(config, "PARTNERS", [
        ("Mostbet", "https://mostbet.com"), ("Topaz", "https://topaz.example")])
    _rearm_bootstrap(clean)
    assert clean._bootstrap_partners_from_env() == 2
    assert clean.db_active_partners() == [
        ("Mostbet", "https://mostbet.com"), ("Topaz", "https://topaz.example")]


def test_env_bootstrap_falls_back_to_legacy_single_url(clean, monkeypatch):
    """PARTNERS_URL is folded into config.PARTNERS by _parse_partners, so a
    deployment that only ever set the legacy variable still gets a row."""
    import config
    monkeypatch.setattr(config, "PARTNERS",
                        config._parse_partners("", "https://legacy.example"))
    _rearm_bootstrap(clean)
    assert clean._bootstrap_partners_from_env() == 1
    assert clean.db_active_partners() == [("legacy.example", "https://legacy.example")]


def test_env_bootstrap_is_idempotent(clean, monkeypatch):
    import config
    monkeypatch.setattr(config, "PARTNERS", [("Mostbet", "https://mostbet.com")])
    _rearm_bootstrap(clean)
    clean._bootstrap_partners_from_env()
    assert clean._bootstrap_partners_from_env() == 0
    assert clean._bootstrap_partners_from_env() == 0
    assert len(clean.db_active_partners()) == 1


def test_db_wins_over_env_after_an_edit(clean, monkeypatch):
    """The regression this guards: an admin fixes a URL in the dashboard, the
    worker restarts, and the stale env value comes back."""
    import config
    monkeypatch.setattr(config, "PARTNERS", [("Mostbet", "https://from-env.example")])
    _rearm_bootstrap(clean)
    clean._bootstrap_partners_from_env()
    pid = clean.db_list_partners()[0]["id"]
    clean.db_partner_update(pid, url="https://edited-in-dashboard.example")

    clean.db_init()                                  # a restart re-runs this
    assert clean.db_active_partners() == [
        ("Mostbet", "https://edited-in-dashboard.example")]


def test_env_does_not_resurrect_removed_partners(clean, monkeypatch):
    import config
    monkeypatch.setattr(config, "PARTNERS", [("Mostbet", "https://from-env.example")])
    _rearm_bootstrap(clean)
    clean._bootstrap_partners_from_env()
    clean.db_partner_archive(clean.db_list_partners()[0]["id"])

    clean.db_init()
    assert clean.db_active_partners() == []


def test_bootstrap_skips_invalid_env_entries(clean, monkeypatch):
    import config
    monkeypatch.setattr(config, "PARTNERS", [("x" * 200, "https://a.example"),
                                             ("Good", "https://b.example")])
    _rearm_bootstrap(clean)
    assert clean._bootstrap_partners_from_env() == 1
    assert clean.db_active_partners() == [("Good", "https://b.example")]


# ══ MIGRATIONS ════════════════════════════════════════════════════════════════

def test_db_init_twice_is_safe(clean):
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_set_promo_code("Mostbet", "CODE", 3)
    clean.db_claim_promos(991001)
    clean.db_init()
    clean.db_init()
    assert clean.db_get_partner(pid)["url"] == "https://a.example"
    assert clean.db_list_promo_codes()[0]["claimed"] == 1


def test_fresh_db_has_the_partners_table(tmp_path, monkeypatch):
    """A brand-new database file must come up complete."""
    import importlib
    import db as _db
    monkeypatch.setenv("BOT_DB_DIR", str(tmp_path))
    fresh = importlib.reload(_db)
    try:
        fresh.db_init()
        assert fresh.db_list_partners() == []
        pid = fresh.db_partner_add("Mostbet", "https://a.example")
        assert fresh.db_get_partner(pid)["is_active"] is True
    finally:
        monkeypatch.undo()
        importlib.reload(_db)


def test_legacy_db_gains_the_new_columns(tmp_path, monkeypatch):
    """A database written before this feature: pre-existing users and promo
    claims must survive, and the new columns must appear with safe defaults."""
    import importlib
    import sqlite3
    import db as _db

    path = tmp_path / "bot.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT,
            display_name TEXT, lang TEXT DEFAULT 'az', is_registered INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0, sports TEXT DEFAULT '',
            experience TEXT DEFAULT '', onboarding_done INTEGER DEFAULT 0,
            total_requests INTEGER DEFAULT 0, last_active TEXT DEFAULT '',
            joined_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE promo_campaign (code TEXT, max_uses INTEGER,
            updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE promo_claims (user_id INTEGER, code TEXT,
            claimed_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (user_id, code));
    """)
    legacy.execute("INSERT INTO users (user_id, username) VALUES (4242, 'legacy')")
    legacy.execute("INSERT INTO promo_campaign (code, max_uses) VALUES ('LEGACY', 7)")
    legacy.execute("INSERT INTO promo_claims (user_id, code) VALUES (4242, 'LEGACY')")
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("BOT_DB_DIR", str(tmp_path))
    migrated = importlib.reload(_db)
    try:
        migrated.db_init()
        # Nothing lost.
        with migrated.con() as c:
            assert c.execute("SELECT username FROM users WHERE user_id=4242"
                             ).fetchone()[0] == "legacy"
        # The legacy campaign keeps working and is active by default, i.e. the
        # old "a row exists ⇒ it is live" behaviour.
        codes = migrated.db_list_promo_codes()
        assert len(codes) == 1
        assert codes[0]["code"] == "LEGACY" and codes[0]["claimed"] == 1
        assert codes[0]["is_active"] is True and codes[0]["is_archived"] is False
        # And the new table exists.
        assert migrated.db_list_partners() == []
    finally:
        monkeypatch.undo()
        importlib.reload(_db)


# ══ PROMO ═════════════════════════════════════════════════════════════════════

def test_promo_create_and_edit_cap(clean):
    clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_set_promo_code("Mostbet", "WELCOME", 10)
    assert clean.db_promo_edit("Mostbet", max_uses=25) is True
    row = clean.db_list_promo_codes()[0]
    assert row["max_uses"] == 25 and row["code"] == "WELCOME"


def test_used_and_remaining_track_claims(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 3)
    clean.db_claim_promos(992001)
    clean.db_claim_promos(992002)
    row = clean.db_list_promo_codes()[0]
    assert row["claimed"] == 2 and row["available"] == 1


def test_repeated_claim_does_not_consume_a_second_use(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    first = clean.db_claim_promos(992010)
    assert clean.db_claim_promos(992010) == first
    assert clean.db_list_promo_codes()[0]["claimed"] == 1


def test_editing_the_code_preserves_used(clean):
    """A dashboard edit is a correction, not a new campaign: the count moves
    across with the code, so a typo fix does not hand out the cap twice."""
    clean.db_set_promo_code("Mostbet", "TYPPO", 5)
    clean.db_claim_promos(992020)
    clean.db_claim_promos(992021)
    assert clean.db_list_promo_codes()[0]["claimed"] == 2

    clean.db_promo_edit("Mostbet", code="TYPO-FIXED")
    row = clean.db_list_promo_codes()[0]
    assert row["code"] == "TYPO-FIXED" and row["claimed"] == 2 and row["available"] == 3


def test_editing_the_code_keeps_existing_holders_idempotent(clean):
    """Someone who already holds the code must not be able to take a second."""
    clean.db_set_promo_code("Mostbet", "OLD", 5)
    clean.db_claim_promos(992030)
    clean.db_promo_edit("Mostbet", code="NEW")
    assert clean.db_claim_promos(992030)[0]["code"] == "NEW"
    assert clean.db_list_promo_codes()[0]["claimed"] == 1


def test_deactivated_promo_is_not_issued(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    assert clean.db_promo_set_active("Mostbet", False) is True
    assert clean.db_claim_promos(992040) == []
    # …and it is hidden from every runtime reader, but still manageable.
    assert clean.db_list_promo_codes() == []
    assert len(clean.db_list_promo_codes(include_inactive=True)) == 1


def test_reactivated_promo_is_issued_again(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    clean.db_promo_set_active("Mostbet", False)
    clean.db_promo_set_active("Mostbet", True)
    assert clean.db_claim_promos(992050)[0]["code"] == "CODE"


def test_deactivating_preserves_existing_claims(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    clean.db_claim_promos(992060)
    clean.db_promo_set_active("Mostbet", False)
    assert clean.db_list_promo_codes(include_inactive=True)[0]["claimed"] == 1


def test_archiving_a_promo_keeps_claims_and_stops_issuing(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    clean.db_claim_promos(992070)
    assert clean.db_promo_archive("Mostbet") is True
    assert clean.db_claim_promos(992071) == []
    archived = clean.db_list_promo_codes(include_inactive=True)[0]
    assert archived["is_archived"] is True and archived["claimed"] == 1


def test_rotating_a_code_does_not_silently_reactivate(clean):
    """/setpromo on a campaign the admin switched off must leave it off."""
    clean.db_set_promo_code("Mostbet", "OLD", 5)
    clean.db_promo_set_active("Mostbet", False)
    clean.db_set_promo_code("Mostbet", "NEW", 5)
    assert clean.db_list_promo_codes() == []
    assert clean.db_list_promo_codes(include_inactive=True)[0]["is_active"] is False


def test_exhausted_promo_is_not_issued(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 1)
    clean.db_claim_promos(992080)
    assert clean.db_claim_promos(992081) == []
    assert clean.db_list_promo_codes()[0]["available"] == 0


@pytest.mark.parametrize("bad", ["", "   ", 12345, "x" * 65])
def test_invalid_promo_code_is_refused(clean, bad):
    """None is not in this list on purpose: it means "leave the code alone"."""
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    with pytest.raises(ValueError):
        clean.db_promo_edit("Mostbet", code=bad)


@pytest.mark.parametrize("bad", [-1, "abc", "", 10_000_001])
def test_invalid_cap_is_refused(clean, bad):
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    with pytest.raises(ValueError):
        clean.db_promo_edit("Mostbet", max_uses=bad)


def test_cap_of_zero_is_allowed_and_issues_nothing(clean):
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    clean.db_promo_edit("Mostbet", max_uses=0)
    assert clean.db_claim_promos(992090) == []


# ══ WORKER HTTP API ═══════════════════════════════════════════════════════════

def test_create_partner_over_http(server, clean):
    r = httpx.post(server + "/partners", headers=_hdr(),
                   json={"name": "Mostbet", "url": "https://mostbet.com"}, timeout=5)
    assert r.status_code == 201
    assert clean.db_active_partners() == [("Mostbet", "https://mostbet.com")]


def test_create_partner_with_a_campaign_over_http(server, clean):
    r = httpx.post(server + "/partners", headers=_hdr(), json={
        "name": "Mostbet", "url": "https://mostbet.com",
        "promo_code": "WELCOME", "promo_limit": 100}, timeout=5)
    assert r.status_code == 201
    assert clean.db_list_promo_codes()[0]["code"] == "WELCOME"


def test_get_partners_joins_promo_and_clicks(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://mostbet.com")
    clean.db_set_promo_code("Mostbet", "WELCOME", 10)
    clean.db_claim_promos(993001)
    clean.db_log_partner_click(993001, "Mostbet")

    body = httpx.get(server + "/partners", headers=_hdr(), timeout=5).json()
    row = next(p for p in body["partners"] if p["id"] == pid)
    assert row["url"] == "https://mostbet.com" and row["clicks"] == 1
    assert row["promo"]["code"] == "WELCOME"
    assert row["promo"]["claimed"] == 1 and row["promo"]["available"] == 9


def test_patch_updates_url_over_http(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://old.example")
    r = httpx.patch(f"{server}/partners/{pid}", headers=_hdr(),
                    json={"url": "https://new.example"}, timeout=5)
    assert r.status_code == 200
    assert clean.db_active_partners() == [("Mostbet", "https://new.example")]


def test_patch_can_disable_and_enable(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    httpx.patch(f"{server}/partners/{pid}", headers=_hdr(),
                json={"is_active": False}, timeout=5)
    assert clean.db_active_partners() == []
    httpx.patch(f"{server}/partners/{pid}", headers=_hdr(),
                json={"is_active": True}, timeout=5)
    assert clean.db_active_partners() == [("Mostbet", "https://a.example")]


def test_delete_archives_partner_and_its_campaign(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    r = httpx.delete(f"{server}/partners/{pid}", headers=_hdr(), timeout=5)
    assert r.status_code == 200
    assert clean.db_active_partners() == []
    assert clean.db_claim_promos(993010) == []


def test_delete_promo_only_keeps_the_partner(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    r = httpx.delete(f"{server}/partners/{pid}/promo", headers=_hdr(), timeout=5)
    assert r.status_code == 200
    assert clean.db_active_partners() == [("Mostbet", "https://a.example")]
    assert clean.db_list_promo_codes() == []


def test_unsafe_url_is_rejected_by_the_api(server, clean):
    r = httpx.post(server + "/partners", headers=_hdr(),
                   json={"name": "Bad", "url": "javascript:alert(1)"}, timeout=5)
    assert r.status_code == 400
    assert "http" in r.json()["error"]
    assert clean.db_list_partners() == []


def test_used_is_not_writable_through_the_api(server, clean):
    clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    clean.db_claim_promos(993020)
    pid = clean.db_list_partners()[0]["id"]
    httpx.patch(f"{server}/partners/{pid}", headers=_hdr(),
                json={"promo_claimed": 0, "claimed": 0}, timeout=5)
    assert clean.db_list_promo_codes()[0]["claimed"] == 1


def test_unknown_partner_is_404(server):
    assert httpx.patch(f"{server}/partners/999999", headers=_hdr(),
                       json={"url": "https://a.example"}, timeout=5).status_code == 404


# ── auth on every partner route ───────────────────────────────────────────────

_ROUTES = [("GET", "/partners"), ("POST", "/partners"),
           ("PATCH", "/partners/1"), ("DELETE", "/partners/1"),
           ("DELETE", "/partners/1/promo")]


@pytest.mark.parametrize("method,path", _ROUTES)
def test_missing_token_is_401(server, method, path):
    r = httpx.request(method, server + path, json={}, timeout=5)
    assert r.status_code == 401


@pytest.mark.parametrize("method,path", _ROUTES)
def test_wrong_token_is_401(server, method, path):
    r = httpx.request(method, server + path, headers=_hdr("nope"), json={}, timeout=5)
    assert r.status_code == 401


@pytest.mark.parametrize("method,path", _ROUTES)
def test_query_string_token_is_401(server, method, path):
    """The token must never be usable from the URL, where proxies log it."""
    r = httpx.request(method, f"{server}{path}?token={TOKEN}", json={}, timeout=5)
    assert r.status_code == 401


def test_correct_token_succeeds(server):
    assert httpx.get(server + "/partners", headers=_hdr(), timeout=5).status_code == 200


def test_write_is_refused_before_it_touches_the_db(server, clean):
    httpx.post(server + "/partners", json={"name": "X", "url": "https://x.example"},
               timeout=5)
    assert clean.db_list_partners() == []


# ══ RUNTIME CONSISTENCY — the acceptance criterion ════════════════════════════

def test_saved_url_reaches_the_bot_without_a_restart(server, clean):
    """Dashboard shows URL B ⇒ Telegram must not still hand out URL A."""
    import handlers.forecast as fc
    import config

    pid = clean.db_partner_add("Mostbet", "https://old.example")
    assert fc._partner_list_kb(1).inline_keyboard[0][0].url == "https://old.example"

    httpx.patch(f"{server}/partners/{pid}", headers=_hdr(),
                json={"url": "https://new.example"}, timeout=5)

    assert config.PARTNERS != [("Mostbet", "https://new.example")]   # env is stale
    assert fc._partner_list_kb(1).inline_keyboard[0][0].url == "https://new.example"


def test_disabling_removes_the_button_without_a_restart(server, clean):
    import handlers.forecast as fc
    pid = clean.db_partner_add("Mostbet", "https://a.example")
    httpx.patch(f"{server}/partners/{pid}", headers=_hdr(),
                json={"is_active": False}, timeout=5)
    assert list(fc._partner_list_kb(1).inline_keyboard) == []


async def test_disabled_promo_is_not_offered_in_the_menu(server, clean, monkeypatch):
    """The menu button is gated on there being an issuable code."""
    import handlers.utils as hu
    from translations import T
    monkeypatch.setattr(hu, "PROMO_CHANNEL", "@somechannel")
    clean.db_ensure(993100, "u", "ru")
    clean.db_partner_add("Mostbet", "https://a.example")
    clean.db_set_promo_code("Mostbet", "CODE", 5)
    assert T["ru"]["menu_get_promo"] in [
        b.text for row in hu.main_menu(993100).keyboard for b in row]

    clean.db_promo_set_active("Mostbet", False)
    assert T["ru"]["menu_get_promo"] not in [
        b.text for row in hu.main_menu(993100).keyboard for b in row]


# ══ DASHBOARD PROXY ═══════════════════════════════════════════════════════════

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
def dash_client(monkeypatch):
    monkeypatch.setattr(dash, "STATS_TOKEN", TOKEN)
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


@pytest.fixture
def proxied(monkeypatch):
    """Capture what the dashboard forwards to the worker."""
    sent = []

    def _request(method, url, **kw):
        sent.append((method, url, kw))
        return _Resp({"partners": [], "orphan_promos": []})

    monkeypatch.setattr(dash.httpx, "request", _request)
    return sent


def _basic():
    import base64
    return {"Authorization": "Basic " + base64.b64encode(
        f"admin:{TOKEN}".encode()).decode()}


def test_partners_page_renders(dash_client):
    r = dash_client.get("/partners", headers=_basic())
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Партнёры и промокоды" in body and "/api/partners" in body


def test_partners_page_needs_login(dash_client):
    assert dash_client.get("/partners").status_code == 401


def test_partner_data_is_fetched_with_the_header(dash_client, proxied):
    r = dash_client.get("/api/partners", headers=_basic())
    assert r.status_code == 200
    method, url, kw = proxied[-1]
    assert method == "GET" and url.endswith("/partners")
    assert kw["headers"]["X-Dashboard-Token"] == TOKEN


def test_save_sends_the_correct_payload(dash_client, proxied):
    payload = {"name": "Mostbet", "url": "https://new.example",
               "is_active": True, "promo_code": "WELCOME", "promo_limit": 50,
               "promo_active": True}
    r = dash_client.patch("/api/partners/7", json=payload,
                          headers={**_basic(), "X-CSRF-Token": dash.csrf_token()})
    assert r.status_code == 200
    method, url, kw = proxied[-1]
    assert method == "PATCH" and url.endswith("/partners/7")
    assert kw["json"] == payload


def test_create_sends_a_post(dash_client, proxied):
    r = dash_client.post("/api/partners", json={"name": "A", "url": "https://a.example"},
                         headers={**_basic(), "X-CSRF-Token": dash.csrf_token()})
    assert r.status_code == 200
    assert proxied[-1][0] == "POST"


def test_archive_sends_a_delete(dash_client, proxied):
    r = dash_client.delete("/api/partners/7",
                           headers={**_basic(), "X-CSRF-Token": dash.csrf_token()})
    assert r.status_code == 200
    assert proxied[-1][0] == "DELETE" and proxied[-1][1].endswith("/partners/7")


def test_writes_need_csrf(dash_client, proxied):
    r = dash_client.post("/api/partners", json={"name": "A", "url": "https://a.example"},
                         headers=_basic())
    assert r.status_code == 403
    assert not proxied


def test_error_from_the_worker_is_passed_through(dash_client, monkeypatch):
    monkeypatch.setattr(dash.httpx, "request",
                        lambda *a, **k: _Resp({"error": "bad url"}, status=400))
    r = dash_client.post("/api/partners", json={"name": "A", "url": "javascript:x"},
                         headers={**_basic(), "X-CSRF-Token": dash.csrf_token()})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad url"


def test_dead_worker_degrades_without_leaking(dash_client, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError(f"worker at http://internal:8888 said {TOKEN}")

    monkeypatch.setattr(dash.httpx, "request", _boom)
    r = dash_client.get("/api/partners", headers=_basic())
    assert r.status_code == 503
    body = r.get_data(as_text=True)
    assert TOKEN not in body and "internal" not in body


def test_saving_invalidates_the_redirect_map(dash_client, proxied, monkeypatch):
    """Without this, /r/<partner> would keep sending users to the old URL until
    the TTL expired."""
    import time as _time
    monkeypatch.setattr(dash, "_PARTNER_TARGETS", {"Mostbet": "https://old.example"})
    monkeypatch.setattr(dash, "_PARTNER_TARGETS_AT", _time.monotonic())
    dash_client.patch("/api/partners/7", json={"url": "https://new.example"},
                      headers={**_basic(), "X-CSRF-Token": dash.csrf_token()})
    assert dash._PARTNER_TARGETS == {}


def test_reading_does_not_invalidate_the_redirect_map(dash_client, proxied, monkeypatch):
    import time as _time
    monkeypatch.setattr(dash, "_PARTNER_TARGETS", {"Mostbet": "https://a.example"})
    monkeypatch.setattr(dash, "_PARTNER_TARGETS_AT", _time.monotonic())
    dash_client.get("/api/partners", headers=_basic())
    assert dash._PARTNER_TARGETS == {"Mostbet": "https://a.example"}


def test_redirect_map_comes_from_the_worker(monkeypatch):
    monkeypatch.setattr(dash, "STATS_TOKEN", TOKEN)
    monkeypatch.setattr(dash, "_PARTNER_TARGETS", {})
    monkeypatch.setattr(dash, "_PARTNER_TARGETS_AT", 0.0)
    monkeypatch.setattr(dash.httpx, "get", lambda *a, **k: _Resp({"partners": [
        {"name": "Mostbet", "url": "https://mostbet.com", "is_archived": False},
        {"name": "Gone", "url": "https://gone.example", "is_archived": True},
        {"name": "Bad", "url": "javascript:alert(1)", "is_archived": False},
    ]}))
    targets = dash._partner_targets()
    # Archived partners stay resolvable — their buttons live on in old chats —
    # but an unsafe URL is dropped even if it somehow reached the DB.
    assert targets == {"Mostbet": "https://mostbet.com", "Gone": "https://gone.example"}


def test_redirect_map_survives_a_dead_worker(monkeypatch):
    import time as _time
    monkeypatch.setattr(dash, "_PARTNER_TARGETS", {"Mostbet": "https://a.example"})
    monkeypatch.setattr(dash, "_PARTNER_TARGETS_AT", _time.monotonic() - 999)

    def _boom(*a, **kw):
        raise RuntimeError("worker unreachable")

    monkeypatch.setattr(dash.httpx, "get", _boom)
    assert dash._partner_targets() == {"Mostbet": "https://a.example"}


# ══ REGRESSIONS FROM THE ADVERSARIAL REVIEW ═══════════════════════════════════
# Each of these reproduces a defect the 849-test suite did not catch.

def test_rename_keeps_old_redirect_links_alive(clean):
    """CRITICAL. /r/<name> URLs are in Telegram messages forever. Renaming a
    partner used to make every already-sent button 404."""
    pid = clean.db_partner_add("Mostbet", "https://mostbet.com")
    clean.db_partner_update(pid, name="MOSTBET AZ")
    targets = clean.db_partner_link_targets()
    assert targets["Mostbet"] == "https://mostbet.com"       # the old link
    assert targets["MOSTBET AZ"] == "https://mostbet.com"    # and the new one


def test_old_link_follows_a_later_url_edit(clean):
    pid = clean.db_partner_add("Mostbet", "https://old.example")
    clean.db_partner_update(pid, name="MOSTBET AZ")
    clean.db_partner_update(pid, url="https://new.example")
    assert clean.db_partner_link_targets()["Mostbet"] == "https://new.example"


def test_archived_partner_still_resolves_old_links(clean):
    pid = clean.db_partner_add("Mostbet", "https://mostbet.com")
    clean.db_partner_archive(pid)
    assert clean.db_partner_link_targets()["Mostbet"] == "https://mostbet.com"


def test_a_reused_name_belongs_to_the_new_partner(clean):
    """Whoever is called X *now* owns /r/X."""
    old = clean.db_partner_add("Mostbet", "https://old.example")
    clean.db_partner_update(old, name="Mostbet Legacy")
    clean.db_partner_add("Mostbet", "https://new.example")
    assert clean.db_partner_link_targets()["Mostbet"] == "https://new.example"
    assert clean.db_partner_link_targets()["Mostbet Legacy"] == "https://old.example"


def test_aliases_are_backfilled_for_existing_rows(clean):
    """A database that already had partners before partner_aliases existed."""
    with clean.con() as c:
        c.execute("DELETE FROM partner_aliases")
        c.execute("INSERT INTO partners (name, url) VALUES ('Legacy', 'https://l.example')")
    clean.db_init()
    assert clean.db_partner_link_targets()["Legacy"] == "https://l.example"


def test_targets_are_served_over_http(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://mostbet.com")
    clean.db_partner_update(pid, name="MOSTBET AZ")
    body = httpx.get(server + "/partners", headers=_hdr(), timeout=5).json()
    assert body["targets"]["Mostbet"] == "https://mostbet.com"


def test_dashboard_redirect_map_includes_former_names(monkeypatch):
    monkeypatch.setattr(dash, "_PARTNER_TARGETS", {})
    monkeypatch.setattr(dash, "_PARTNER_TARGETS_AT", 0.0)
    monkeypatch.setattr(dash.httpx, "get", lambda *a, **k: _Resp({
        "partners": [{"name": "MOSTBET AZ", "url": "https://m.example"}],
        "targets": {"Mostbet": "https://m.example", "MOSTBET AZ": "https://m.example"},
    }))
    assert dash._partner_targets() == {"Mostbet": "https://m.example",
                                       "MOSTBET AZ": "https://m.example"}


def test_dashboard_falls_back_when_worker_has_no_targets(monkeypatch):
    """web and worker deploy independently; an older worker sends no `targets`."""
    monkeypatch.setattr(dash, "_PARTNER_TARGETS", {})
    monkeypatch.setattr(dash, "_PARTNER_TARGETS_AT", 0.0)
    monkeypatch.setattr(dash.httpx, "get", lambda *a, **k: _Resp({
        "partners": [{"name": "Mostbet", "url": "https://m.example"}]}))
    assert dash._partner_targets() == {"Mostbet": "https://m.example"}


@pytest.mark.parametrize("body,reason", [
    ({"promo_code": "", "promo_limit": 5}, "empty"),
    ({"promo_code": "   ", "promo_limit": 5}, "empty"),
    ({"promo_code": "OK", "promo_limit": -1}, "0 or more"),
    ({"promo_code": "OK", "promo_limit": 10_000_001}, "at most"),
    ({"promo_code": "OK", "promo_limit": "abc"}, "whole number"),
    ({"promo_code": "x" * 65, "promo_limit": 5}, "at most"),
])
def test_invalid_promo_input_is_refused_on_create(clean, body, reason):
    """HIGH. The create path went straight to db_set_promo_code, so an empty
    code or a negative cap reached the database."""
    clean.db_partner_add("V", "https://v.example")
    with pytest.raises(ValueError) as e:
        ss._apply_promo_patch("V", body)
    assert reason in str(e.value)
    assert clean.db_list_promo_codes(include_inactive=True) == []


def test_invalid_promo_is_rejected_over_http(server, clean):
    r = httpx.post(server + "/partners", headers=_hdr(), json={
        "name": "V", "url": "https://v.example",
        "promo_code": "", "promo_limit": 5}, timeout=5)
    assert r.status_code == 400
    assert clean.db_list_promo_codes(include_inactive=True) == []


def test_set_promo_code_itself_refuses_bad_input(clean):
    """The DB layer is the real gate — /setpromo goes through it too."""
    with pytest.raises(ValueError):
        clean.db_set_promo_code("X", "", 5)
    with pytest.raises(ValueError):
        clean.db_set_promo_code("X", "CODE", -1)


def test_disabling_a_campaign_keeps_it_in_the_funnel(clean):
    """HIGH. db_promo_funnel counted only live campaigns, so switching one off
    erased its historical claims from the report."""
    clean.db_set_promo_code("F", "FUN", 10)
    clean.db_claim_promos(994001)
    clean.db_claim_promos(994002)
    before = clean.db_promo_funnel()["claimed"]
    clean.db_promo_set_active("F", False)
    assert clean.db_promo_funnel()["claimed"] == before == 2
    clean.db_promo_archive("F")
    assert clean.db_promo_funnel()["claimed"] == 2


def test_rename_is_atomic_across_tables(clean, monkeypatch):
    """A crash between the partners and promo_campaign writes must leave
    neither applied — never name=NEW with campaign still on OLD."""
    import contextlib
    import sqlite3

    pid = clean.db_partner_add("Old", "https://a.example")
    clean.db_set_promo_code("Old", "C1", 5)

    class _Boom(sqlite3.Connection):
        def execute(self, sql, params=()):
            if "UPDATE promo_campaign" in sql:
                raise RuntimeError("simulated crash mid-rename")
            return super().execute(sql, params)

    @contextlib.contextmanager
    def _failing_con():
        c = sqlite3.connect(clean.DB, timeout=10, factory=_Boom)
        try:
            with c:
                yield c
        finally:
            c.close()

    monkeypatch.setattr(clean, "con", _failing_con)
    with pytest.raises(RuntimeError):
        clean.db_partner_update(pid, name="NEW")
    monkeypatch.undo()

    assert clean.db_get_partner(pid)["name"] == "Old"
    assert clean.db_list_promo_codes()[0]["partner"] == "Old"


def test_bootstrap_does_not_return_after_archiving_every_partner(clean, monkeypatch):
    """ENV bootstrap → archive ALL → restart → the env partners stay gone."""
    import config
    monkeypatch.setattr(config, "PARTNERS", [("Mostbet", "https://from-env.example"),
                                             ("Topaz", "https://topaz.example")])
    _rearm_bootstrap(clean)
    assert clean._bootstrap_partners_from_env() == 2
    for p in clean.db_list_partners():
        clean.db_partner_archive(p["id"])
    assert clean.db_active_partners() == []

    clean.db_init()
    clean.db_init()
    assert clean.db_active_partners() == []
    assert clean.db_list_partners() == []


def test_bootstrap_does_not_return_after_hard_deleting_every_partner(clean, monkeypatch):
    """Same, but the rows were physically removed rather than archived."""
    import config
    monkeypatch.setattr(config, "PARTNERS", [("Mostbet", "https://from-env.example")])
    _rearm_bootstrap(clean)
    clean._bootstrap_partners_from_env()
    with clean.con() as c:
        c.execute("DELETE FROM partners")

    clean.db_init()
    assert clean.db_active_partners() == []


# ══ SECOND ADVERSARIAL PASS — defects found in the FIRST pass's own fixes ═════

def test_restart_does_not_hand_an_alias_to_an_archived_partner(clean):
    """HIGH regression in the alias fix itself. The startup backfill re-ran with
    OR REPLACE, so a name held by both a live partner (via rename) and an older
    archived partner was reassigned to whichever sorted last. After a restart
    /r/<name> silently pointed at the archived partner's URL — users sent to the
    wrong site, and only after a restart, which makes it near-undiagnosable."""
    live = clean.db_partner_add("Q", "https://live.example")
    archived = clean.db_partner_add("Z", "https://archived.example")
    clean.db_partner_archive(archived)
    clean.db_partner_update(live, name="Z")
    assert clean.db_partner_link_targets()["Z"] == "https://live.example"

    clean.db_init()
    clean.db_init()
    assert clean.db_partner_link_targets()["Z"] == "https://live.example"


def test_backfill_prefers_the_live_partner_on_a_duplicate_name(clean):
    """A first fill on a database that predates partner_aliases."""
    live = clean.db_partner_add("Q", "https://live.example")
    archived = clean.db_partner_add("Z", "https://archived.example")
    clean.db_partner_archive(archived)
    clean.db_partner_update(live, name="Z")
    with clean.con() as c:
        c.execute("DELETE FROM partner_aliases")

    clean.db_init()
    assert clean.db_partner_link_targets()["Z"] == "https://live.example"


def test_rename_alias_is_stable_across_repeated_inits(clean):
    pid = clean.db_partner_add("Mostbet", "https://m.example")
    clean.db_partner_update(pid, name="MOSTBET AZ")
    before = clean.db_partner_link_targets()
    clean.db_init()
    clean.db_init()
    assert clean.db_partner_link_targets() == before
    assert before["Mostbet"] == "https://m.example"


@pytest.mark.parametrize("bad", [{"a": 1}, ["x"], 42, True])
def test_non_text_promo_code_is_refused_on_edit(clean, bad):
    """MEDIUM. The edit path ran str() on the value first, so a JSON object
    became the literal promo code "{'a': 1}"."""
    clean.db_partner_add("E", "https://e.example")
    clean.db_set_promo_code("E", "GOOD", 5)
    with pytest.raises(ValueError):
        ss._apply_promo_patch("E", {"promo_code": bad})
    assert clean.db_list_promo_codes()[0]["code"] == "GOOD"


def test_non_text_promo_code_is_rejected_over_http(server, clean):
    pid = clean.db_partner_add("E", "https://e.example")
    clean.db_set_promo_code("E", "GOOD", 5)
    r = httpx.patch(f"{server}/partners/{pid}", headers=_hdr(),
                    json={"promo_code": {"a": 1}}, timeout=5)
    assert r.status_code == 400
    assert clean.db_list_promo_codes()[0]["code"] == "GOOD"
