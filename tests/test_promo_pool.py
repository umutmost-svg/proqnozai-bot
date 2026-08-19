"""A partner's pool of unique single-use promo codes.

The original campaign is one code many users share, capped by max_uses. A pool
is the mirror image — N distinct codes, one user each — which is what a partner
means when it hands over a list of vouchers. Both kinds coexist: the lifecycle
still lives on the campaign row, only the codes move to promo_pool.

Layers, as in test_partners_dashboard.py: the DB, the worker's HTTP routes over
a real loopback socket, and the dashboard proxy with httpx replaced.
"""
import json
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


def _batch(n, prefix="MB-"):
    return [f"{prefix}{i:04d}" for i in range(n)]


# ══ import ════════════════════════════════════════════════════════════════════

def test_import_creates_a_pool_campaign(clean):
    result = clean.db_promo_pool_import("Mostbet", _batch(200))
    assert result["added"] == 200
    assert result["total"] == 200
    assert result["available"] == 200
    assert clean.db_promo_mode("Mostbet") == "pool"


def test_the_headline_case_200_codes_one_activation_each(clean):
    """200 codes, 200 different users, every one of them a different string."""
    clean.db_promo_pool_import("Mostbet", _batch(200))
    handed = [clean.db_claim_promos(700000 + i)[0]["code"] for i in range(200)]
    assert len(set(handed)) == 200
    assert clean.db_claim_promos(700999) == []      # the 201st gets nothing
    assert clean.db_promo_pool_stats("Mostbet") == {
        "total": 200, "claimed": 200, "available": 0}


def test_import_accepts_pasted_text(clean):
    clean.db_promo_pool_import("Mostbet", "A-1\nA-2\n\n  A-3  \n")
    assert [c["code"] for c in clean.db_promo_pool_codes("Mostbet")] == ["A-1", "A-2", "A-3"]


def test_import_accepts_a_comma_separated_paste(clean):
    clean.db_promo_pool_import("Mostbet", "A-1, A-2,A-3")
    assert len(clean.db_promo_pool_codes("Mostbet")) == 3


def test_import_is_additive(clean):
    clean.db_promo_pool_import("Mostbet", ["A-1", "A-2"])
    second = clean.db_promo_pool_import("Mostbet", ["A-3"])
    assert second["added"] == 1
    assert second["total"] == 3


def test_reimporting_the_same_list_adds_nothing(clean):
    clean.db_promo_pool_import("Mostbet", _batch(10))
    again = clean.db_promo_pool_import("Mostbet", _batch(10))
    assert again["added"] == 0
    assert again["duplicates"] == 10
    assert again["total"] == 10


def test_duplicates_inside_one_paste_are_counted_once(clean):
    result = clean.db_promo_pool_import("Mostbet", ["A-1", "A-1", "A-2"])
    assert result["added"] == 2
    assert result["duplicates"] == 1


def test_a_code_held_by_another_partner_is_refused_as_a_duplicate(clean):
    """Claims are keyed by the code string, so one code in two pools would mean
    two partners sharing a holder."""
    clean.db_promo_pool_import("Mostbet", ["SHARED-1"])
    result = clean.db_promo_pool_import("Topaz", ["SHARED-1", "TZ-1"])
    assert result["added"] == 1
    assert result["duplicates"] == 1
    assert [c["code"] for c in clean.db_promo_pool_codes("Topaz")] == ["TZ-1"]


@pytest.mark.parametrize("bad", [None, 42, ["nested"], 3.5])
def test_invalid_codes_are_refused(clean, bad):
    with pytest.raises(ValueError):
        clean.db_promo_pool_import("Mostbet", ["GOOD-1", bad])


def test_blank_lines_are_skipped_not_refused(clean):
    """A pasted list carries stray blank lines; they are not input."""
    result = clean.db_promo_pool_import("Mostbet", ["GOOD-1", "", "   ", "GOOD-2"])
    assert result["added"] == 2


def test_a_bad_code_rejects_the_whole_batch(clean):
    """All-or-nothing: a partial import of a 200-line paste is far harder to
    reason about than a refusal."""
    with pytest.raises(ValueError):
        clean.db_promo_pool_import("Mostbet", ["GOOD-1", "GOOD-2", 42])
    assert clean.db_promo_pool_codes("Mostbet") == []
    assert clean.db_promo_mode("Mostbet") is None


def test_an_empty_list_is_refused(clean):
    with pytest.raises(ValueError):
        clean.db_promo_pool_import("Mostbet", ["", "  "])


def test_an_oversized_batch_is_refused(clean):
    with pytest.raises(ValueError):
        clean.db_promo_pool_import("Mostbet", _batch(clean.PROMO_POOL_BATCH_MAX + 1))


def test_import_needs_a_partner(clean):
    with pytest.raises(ValueError):
        clean.db_promo_pool_import("  ", ["A-1"])


# ══ claiming ══════════════════════════════════════════════════════════════════

def test_each_user_gets_a_different_code(clean):
    clean.db_promo_pool_import("Mostbet", _batch(3))
    first = clean.db_claim_promos(710001)[0]["code"]
    second = clean.db_claim_promos(710002)[0]["code"]
    assert first != second


def test_claiming_twice_returns_the_same_code(clean):
    """Idempotent per PARTNER, not per code: the user does not know which of the
    200 is theirs, so asking again must not burn a second one."""
    clean.db_promo_pool_import("Mostbet", _batch(5))
    first = clean.db_claim_promos(710010)
    assert clean.db_claim_promos(710010) == first
    assert clean.db_promo_pool_stats("Mostbet")["claimed"] == 1


def test_an_exhausted_pool_is_silently_skipped(clean):
    """Same outcome the bot already renders for a spent cap: the partner drops
    out of the reply, with no error."""
    clean.db_promo_pool_import("Mostbet", ["ONLY-1"])
    clean.db_claim_promos(710020)
    assert clean.db_claim_promos(710021) == []


def test_an_exhausted_pool_does_not_hide_another_partner(clean):
    clean.db_promo_pool_import("Mostbet", ["ONLY-1"])
    clean.db_set_promo_code("Topaz", "TZ", 5)
    clean.db_claim_promos(710030)
    assert [g["partner"] for g in clean.db_claim_promos(710031)] == ["Topaz"]


def test_pool_and_shared_partners_are_served_together(clean):
    clean.db_promo_pool_import("Mostbet", _batch(2))
    clean.db_set_promo_code("Topaz", "TZ", 5)
    granted = {g["partner"]: g["code"] for g in clean.db_claim_promos(710040)}
    assert granted["Topaz"] == "TZ"
    assert granted["Mostbet"].startswith("MB-")


def test_a_disabled_pool_is_not_issued(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    clean.db_promo_set_active("Mostbet", False)
    assert clean.db_claim_promos(710050) == []
    assert clean.db_promo_pool_stats("Mostbet")["claimed"] == 0


def test_a_reenabled_pool_issues_again(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    clean.db_promo_set_active("Mostbet", False)
    clean.db_promo_set_active("Mostbet", True)
    assert clean.db_claim_promos(710060)[0]["code"].startswith("MB-")


def test_archiving_stops_issuing_but_keeps_the_holders(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    mine = clean.db_claim_promos(710070)[0]["code"]
    clean.db_promo_archive("Mostbet")
    assert clean.db_claim_promos(710071) == []
    held = [c for c in clean.db_promo_pool_codes("Mostbet") if c["user_id"] == 710070]
    assert [c["code"] for c in held] == [mine]


def test_a_pooled_claim_is_recorded_in_the_funnel(clean):
    """Mirrored into promo_claims so the 7-day count and the distinct-user
    metric see a pooled claim exactly like a shared one."""
    clean.db_promo_pool_import("Mostbet", _batch(5))
    clean.db_claim_promos(710080)
    clean.db_claim_promos(710081)
    funnel = clean.db_promo_funnel()
    assert funnel["users"] == 2
    assert funnel["claimed"] == 2
    assert funnel["max_uses"] == 5


def test_the_pool_survives_a_rename(clean):
    pid = clean.db_partner_add("Old", "https://old.example")
    clean.db_promo_pool_import("Old", _batch(3))
    clean.db_claim_promos(710090)
    clean.db_partner_update(pid, name="New")
    assert clean.db_promo_pool_stats("New") == {"total": 3, "claimed": 1, "available": 2}
    assert clean.db_claim_promos(710091)[0]["partner"] == "New"


def test_a_pool_claim_holds_under_parallel_claimers(clean):
    """The BEGIN IMMEDIATE guarantee, in pool form: no two users may ever be
    handed the same code, and the pool cannot go past its size."""
    clean.db_promo_pool_import("Mostbet", _batch(20))
    handed, errors = [], []

    def grab(uid):
        try:
            handed.extend(g["code"] for g in clean.db_claim_promos(uid))
        except Exception as e:                          # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=grab, args=(711000 + i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert len(handed) == 20
    assert len(set(handed)) == 20
    assert clean.db_promo_pool_stats("Mostbet") == {
        "total": 20, "claimed": 20, "available": 0}


# ══ reporting ═════════════════════════════════════════════════════════════════

def test_listing_reports_the_pool_size_as_the_cap(clean):
    clean.db_promo_pool_import("Mostbet", _batch(200))
    clean.db_claim_promos(720001)
    row = clean.db_list_promo_codes()[0]
    assert row["mode"] == "pool"
    assert row["code"] == ""            # every holder has a different string
    assert (row["max_uses"], row["claimed"], row["available"]) == (200, 1, 199)


def test_a_shared_campaign_still_reports_mode_shared(clean):
    clean.db_set_promo_code("Topaz", "TZ", 5)
    assert clean.db_list_promo_codes()[0]["mode"] == "shared"


def test_removing_free_codes_keeps_the_issued_ones(clean):
    clean.db_promo_pool_import("Mostbet", _batch(10))
    clean.db_claim_promos(720010)
    assert clean.db_promo_pool_remove_free("Mostbet") == 9
    assert clean.db_promo_pool_stats("Mostbet") == {"total": 1, "claimed": 1, "available": 0}
    assert clean.db_claim_promos(720011) == []


def test_a_user_who_already_holds_a_code_keeps_it_after_a_purge(clean):
    clean.db_promo_pool_import("Mostbet", _batch(10))
    mine = clean.db_claim_promos(720020)
    clean.db_promo_pool_remove_free("Mostbet")
    assert clean.db_claim_promos(720020) == mine


def test_only_free_codes_can_be_listed(clean):
    clean.db_promo_pool_import("Mostbet", _batch(3))
    clean.db_claim_promos(720030)
    assert len(clean.db_promo_pool_codes("Mostbet", only_free=True)) == 2


# ══ the two modes do not corrupt each other ═══════════════════════════════════

def test_a_shared_code_cannot_be_set_over_a_pool(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    with pytest.raises(ValueError):
        clean.db_set_promo_code("Mostbet", "SINGLE", 100)
    assert clean.db_promo_mode("Mostbet") == "pool"


def test_a_pool_cannot_be_imported_over_a_shared_code(clean):
    clean.db_set_promo_code("Mostbet", "SINGLE", 100)
    with pytest.raises(ValueError):
        clean.db_promo_pool_import("Mostbet", _batch(5))
    assert clean.db_promo_pool_codes("Mostbet") == []


def test_editing_a_pools_code_or_cap_is_refused(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    with pytest.raises(ValueError):
        clean.db_promo_edit("Mostbet", code="SINGLE")
    with pytest.raises(ValueError):
        clean.db_promo_edit("Mostbet", max_uses=99)


def test_a_pool_can_still_be_switched_off_through_edit(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    assert clean.db_promo_edit("Mostbet", is_active=False) is True
    assert clean.db_claim_promos(730001) == []


def test_deleting_a_pool_campaign_drops_only_the_free_codes(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    clean.db_claim_promos(730010)
    assert clean.db_delete_promo_code("Mostbet") is True
    assert clean.db_promo_mode("Mostbet") is None
    assert len(clean.db_promo_pool_codes("Mostbet")) == 1     # the issued one


def test_a_shared_code_can_be_set_after_the_pool_is_deleted(clean):
    clean.db_promo_pool_import("Mostbet", _batch(5))
    clean.db_delete_promo_code("Mostbet")
    clean.db_set_promo_code("Mostbet", "SINGLE", 10)
    assert clean.db_list_promo_codes()[0]["mode"] == "shared"


# ══ worker HTTP routes ════════════════════════════════════════════════════════

def test_import_over_http(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    r = httpx.post(f"{server}/partners/{pid}/promo/pool",
                   json={"codes": "\n".join(_batch(200))}, headers=_hdr(), timeout=10)
    assert r.status_code == 200
    assert r.json()["added"] == 200
    assert clean.db_promo_mode("Mostbet") == "pool"


def test_import_is_reported_back_in_the_partner_list(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    httpx.post(f"{server}/partners/{pid}/promo/pool",
               json={"codes": _batch(50)}, headers=_hdr(), timeout=10)
    promo = httpx.get(f"{server}/partners", headers=_hdr(),
                      timeout=10).json()["partners"][0]["promo"]
    assert promo["mode"] == "pool"
    assert promo["max_uses"] == 50
    assert promo["code"] == ""


def test_clearing_free_codes_over_http(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    httpx.post(f"{server}/partners/{pid}/promo/pool",
               json={"codes": _batch(10)}, headers=_hdr(), timeout=10)
    clean.db_claim_promos(740001)
    r = httpx.request("DELETE", f"{server}/partners/{pid}/promo/pool",
                      headers=_hdr(), timeout=10)
    assert r.status_code == 200
    assert r.json()["removed"] == 9


def test_clearing_the_pool_does_not_archive_the_partner(server, clean):
    """The pool routes sit in front of the generic DELETE, which archives."""
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    httpx.post(f"{server}/partners/{pid}/promo/pool",
               json={"codes": _batch(4)}, headers=_hdr(), timeout=10)
    httpx.request("DELETE", f"{server}/partners/{pid}/promo/pool",
                  headers=_hdr(), timeout=10)
    assert [name for name, _url in clean.db_active_partners()] == ["Mostbet"]
    assert clean.db_promo_mode("Mostbet") == "pool"


def test_a_bad_import_is_a_400_with_the_reason(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    r = httpx.post(f"{server}/partners/{pid}/promo/pool",
                   json={"codes": ""}, headers=_hdr(), timeout=10)
    assert r.status_code == 400
    assert "error" in r.json()


def test_import_for_an_unknown_partner_is_404(server):
    r = httpx.post(f"{server}/partners/999999/promo/pool",
                   json={"codes": ["A-1"]}, headers=_hdr(), timeout=10)
    assert r.status_code == 404


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_pool_routes_need_the_token(server, clean, method):
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    r = httpx.request(method, f"{server}/partners/{pid}/promo/pool",
                      json={"codes": ["A-1"]}, timeout=10)
    assert r.status_code == 401


def test_query_string_token_is_refused_on_the_pool_routes(server, clean):
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    r = httpx.post(f"{server}/partners/{pid}/promo/pool?token={TOKEN}",
                   json={"codes": ["A-1"]}, timeout=10)
    assert r.status_code == 401


def test_the_saved_pool_reaches_the_bot_without_a_restart(server, clean):
    """The whole point of the feature: import, then read what the bot's own
    claim path would hand the next user."""
    pid = clean.db_partner_add("Mostbet", "https://mb.example")
    httpx.post(f"{server}/partners/{pid}/promo/pool",
               json={"codes": ["FRESH-1"]}, headers=_hdr(), timeout=10)
    assert clean.db_claim_promos(750001) == [{"partner": "Mostbet", "code": "FRESH-1"}]


# ══ dashboard proxy ═══════════════════════════════════════════════════════════

@pytest.fixture
def dash_client(monkeypatch):
    monkeypatch.setattr(dash, "STATS_TOKEN", TOKEN)
    dash.app.config["TESTING"] = True
    with dash.app.test_client() as client:
        yield client


def _login(client):
    import base64
    raw = base64.b64encode(f"admin:{TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def proxied(monkeypatch):
    """Capture what the dashboard would send the worker; open no socket."""
    sent = {}

    def fake_request(method, url, **kw):
        sent.update(method=method, url=url, json=kw.get("json"),
                    headers=kw.get("headers") or {})
        return httpx.Response(200, json={"added": 200, "duplicates": 0,
                                         "total": 200, "available": 200})

    monkeypatch.setattr(dash.httpx, "request", fake_request)
    return sent


def test_pool_import_is_proxied_to_the_worker(dash_client, proxied):
    r = dash_client.post("/api/partners/7/promo/pool",
                         json={"codes": "A-1\nA-2"},
                         headers={**_login(dash_client),
                                  "X-CSRF-Token": dash.csrf_token()})
    assert r.status_code == 200
    assert proxied["method"] == "POST"
    assert proxied["url"].endswith("/partners/7/promo/pool")
    assert proxied["json"] == {"codes": "A-1\nA-2"}


def test_pool_import_needs_csrf(dash_client, proxied):
    r = dash_client.post("/api/partners/7/promo/pool", json={"codes": "A-1"},
                         headers=_login(dash_client))
    assert r.status_code == 403
    assert proxied == {}                       # refused before touching the worker


def test_pool_clear_needs_csrf(dash_client, proxied):
    r = dash_client.delete("/api/partners/7/promo/pool", headers=_login(dash_client))
    assert r.status_code == 403
    assert proxied == {}


def test_pool_routes_need_login(dash_client):
    assert dash_client.post("/api/partners/7/promo/pool", json={"codes": "A"}).status_code == 401
    assert dash_client.delete("/api/partners/7/promo/pool").status_code == 401


def test_the_token_never_reaches_the_browser(dash_client, proxied):
    dash_client.post("/api/partners/7/promo/pool", json={"codes": "A-1"},
                     headers={**_login(dash_client), "X-CSRF-Token": dash.csrf_token()})
    assert proxied["headers"].get("X-Dashboard-Token") == TOKEN
    page = dash_client.get("/partners", headers=_login(dash_client)).get_data(as_text=True)
    assert TOKEN not in page


def test_the_partners_page_offers_the_import(dash_client):
    page = dash_client.get("/partners", headers=_login(dash_client)).get_data(as_text=True)
    assert "Загрузить коды" in page
    assert "promo/pool" in page


def test_importing_invalidates_the_redirect_map(dash_client, proxied, monkeypatch):
    """A write must drop the cached redirect map, as every other write does."""
    called = []
    monkeypatch.setattr(dash, "_invalidate_partner_targets",
                        lambda: called.append(True))
    dash_client.post("/api/partners/7/promo/pool", json={"codes": "A-1"},
                     headers={**_login(dash_client), "X-CSRF-Token": dash.csrf_token()})
    assert called == [True]


def test_a_worker_error_is_passed_through(dash_client, monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("worker down")
    monkeypatch.setattr(dash.httpx, "request", boom)
    r = dash_client.post("/api/partners/7/promo/pool", json={"codes": "A-1"},
                         headers={**_login(dash_client),
                                  "X-CSRF-Token": dash.csrf_token()})
    assert r.status_code == 503
    body = r.get_data(as_text=True)
    assert TOKEN not in body and "worker down" not in body
    assert json.loads(body)["error"] == "stats backend unavailable"


# ══ migration ═════════════════════════════════════════════════════════════════

def test_a_legacy_db_gains_the_pool_and_stays_shared(tmp_path, monkeypatch):
    """A database written before pools: its campaign keeps working untouched and
    is reported as 'shared', the new table appears, and a pool can be imported
    for a different partner alongside it."""
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
    legacy.execute("INSERT INTO promo_campaign (code, max_uses) VALUES ('LEGACY', 7)")
    legacy.execute("INSERT INTO promo_claims (user_id, code) VALUES (4242, 'LEGACY')")
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("BOT_DB_DIR", str(tmp_path))
    migrated = importlib.reload(_db)
    try:
        migrated.db_init()
        row = migrated.db_list_promo_codes()[0]
        assert row["mode"] == "shared"
        assert row["code"] == "LEGACY" and row["claimed"] == 1
        # The pool table is there and usable for another partner.
        assert migrated.db_promo_pool_import("Topaz", ["TZ-1", "TZ-2"])["added"] == 2
        granted = {g["partner"]: g["code"] for g in migrated.db_claim_promos(4243)}
        assert granted == {"": "LEGACY", "Topaz": "TZ-1"}
    finally:
        monkeypatch.undo()
        importlib.reload(_db)
