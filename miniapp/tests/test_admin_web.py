"""Admin panel local pilot: auth (password + TOTP), sessions, CSRF/Origin/Host, roles, journal,
dangerous actions, demo isolation, and the read methods it adds to Store / ForecastHistory."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miniapp.admin_web import totp
from miniapp.admin_web.accounts import (ABSOLUTE_S, IDLE_S, ActionError, AdminStore, AuthError, hash_password,
                                        verify_password)
from miniapp.admin_web.app import CSRF_HEADER, SESSION_COOKIE, create_admin_app
from miniapp.admin_web.config import AdminWebConfig, ConfigError
from miniapp.admin_web.demo import DEMO_ID_BASE, seed
from miniapp.forecast.history import ForecastHistory
from miniapp.store import Store

PORT = 8799
ORIGIN = f"http://127.0.0.1:{PORT}"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def env(tmp_path: Path):
    config = AdminWebConfig(data_dir=tmp_path / "pilot", port=PORT, static_dir=None)
    config.data_dir.mkdir(mode=0o700)
    seed(config.data_dir)
    clock = Clock()
    app = create_admin_app(config, clock=clock)
    accounts = AdminStore(config.admin_db)
    secrets_ = {}
    for email, role in (("owner@x.test", "owner"), ("admin@x.test", "admin"), ("analyst@x.test", "analyst")):
        _, secret = accounts.create_admin(email, role, "pw-" + role, int(clock()))
        secrets_[email] = secret
    client = TestClient(app, base_url=ORIGIN)
    return config, clock, client, accounts, secrets_


def code(secret: str, clock: Clock, shift: int = 0) -> str:
    return totp.code_at(secret, int(clock() // 30) + shift)


def login(client: TestClient, clock: Clock, secrets_: dict, email: str) -> str:
    role = email.split("@")[0]
    r = client.post("/api/admin/auth/login", json={"email": email, "password": "pw-" + role}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    r = client.post("/api/admin/auth/totp", json={"code": code(secrets_[email], clock)}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    csrf = client.get("/api/admin/auth/me").json()["csrf"]
    clock.t += 30  # the next action needs a fresh TOTP step (codes work once)
    return csrf


def post(client: TestClient, path: str, csrf: str, body: dict):
    return client.post(path, json=body, headers={"Origin": ORIGIN, CSRF_HEADER: csrf})


def test_totp_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 SHA-1 key "12345678901234567890"
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.matching_step(secret, "287082", 59) == 1
    assert totp.matching_step(secret, "28708", 59) is None


def test_password_hash_roundtrip():
    stored = hash_password("s3cret")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert verify_password("s3cret", stored) and not verify_password("other", stored)
    assert not verify_password("x", None)


def test_config_pilot_only_and_bot_db_refused(tmp_path):
    with pytest.raises(ConfigError):
        AdminWebConfig.from_env({"ADMIN_WEB_MODE": "server"})
    with pytest.raises(ConfigError):
        AdminWebConfig(data_dir=tmp_path, port=80, static_dir=None).validate()


def test_login_requires_both_factors_and_same_error_for_unknown_email(env):
    _, clock, client, _, secrets_ = env
    h = {"Origin": ORIGIN}
    assert client.get("/api/admin/overview").status_code == 401
    unknown = client.post("/api/admin/auth/login", json={"email": "nobody@x.test", "password": "x"}, headers=h)
    wrong = client.post("/api/admin/auth/login", json={"email": "owner@x.test", "password": "x"}, headers=h)
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"] == wrong.json()["error"] == "invalid_credentials"
    assert client.post("/api/admin/auth/login", json={"email": "owner@x.test", "password": "pw-owner"},
                       headers=h).status_code == 200
    assert client.get("/api/admin/overview").status_code == 401  # password alone is not a session
    assert client.post("/api/admin/auth/totp", json={"code": "000000"}, headers=h).json()["error"] == "invalid_code"
    ok = client.post("/api/admin/auth/totp", json={"code": code(secrets_["owner@x.test"], clock)}, headers=h)
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"]
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/admin/overview").status_code == 200


def test_totp_code_is_single_use(env):
    _, clock, client, _, secrets_ = env
    h = {"Origin": ORIGIN}
    c = code(secrets_["owner@x.test"], clock)
    client.post("/api/admin/auth/login", json={"email": "owner@x.test", "password": "pw-owner"}, headers=h)
    assert client.post("/api/admin/auth/totp", json={"code": c}, headers=h).status_code == 200
    other = TestClient(client.app, base_url=ORIGIN)
    other.post("/api/admin/auth/login", json={"email": "owner@x.test", "password": "pw-owner"}, headers=h)
    assert other.post("/api/admin/auth/totp", json={"code": c}, headers=h).json()["error"] == "invalid_code"


def test_lockout_survives_restart(env):
    config, clock, client, accounts, _ = env
    for _ in range(5):
        client.post("/api/admin/auth/login", json={"email": "admin@x.test", "password": "bad"}, headers={"Origin": ORIGIN})
    fresh = AdminStore(config.admin_db)  # a new process sees the same lock
    with pytest.raises(AuthError, match="invalid_credentials"):
        fresh.login_password("admin@x.test", "pw-admin", int(clock()))
    clock.t += 15 * 60 + 1
    assert fresh.login_password("admin@x.test", "pw-admin", int(clock()))


def test_session_idle_and_absolute_expiry(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    clock.t += IDLE_S + 1
    assert client.get("/api/admin/overview").status_code == 401
    login(client, clock, secrets_, "owner@x.test")
    for _ in range(ABSOLUTE_S // (IDLE_S - 60) + 1):
        clock.t += IDLE_S - 60
        client.get("/api/admin/auth/me")
    assert client.get("/api/admin/overview").status_code == 401


def test_host_origin_and_csrf_guards(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    assert client.get("/api/admin/health", headers={"Host": "evil.example"}).status_code == 421
    body = {"value": False, "reason": "test", "code": "123456", "idempotencyKey": "k" * 10}
    assert client.post("/api/admin/settings/ai.enabled", json=body,
                       headers={"Origin": "http://evil.example", CSRF_HEADER: csrf}).json()["error"] == "bad_origin"
    assert client.post("/api/admin/settings/ai.enabled", json=body,
                       headers={"Origin": ORIGIN}).json()["error"] == "csrf"
    assert client.post("/api/admin/auth/logout", headers={"Origin": ORIGIN}).json()["error"] == "csrf"


def test_security_headers(env):
    _, _, client, _, _ = env
    r = client.get("/api/admin/health")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


def test_roles_enforced_on_server(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "analyst@x.test")
    assert client.get("/api/admin/users").json()["error"] == "forbidden"
    assert client.get(f"/api/admin/users/{DEMO_ID_BASE + 1000}").status_code == 403
    assert client.get("/api/admin/forecasts").status_code == 200
    r = post(client, "/api/admin/settings/ai.enabled", csrf, {"value": False, "reason": "try", "idempotencyKey": "k" * 10,
                                                             "code": code(secrets_["analyst@x.test"], clock)})
    assert r.status_code == 403
    r = post(client, "/api/admin/admins", csrf, {"email": "a@b.c", "role": "owner", "reason": "try",
                                                 "idempotencyKey": "k" * 10, "code": "000000"})
    assert r.status_code == 403


def test_setting_change_needs_step_up_reason_and_is_journalled_once(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    secret = secrets_["owner@x.test"]
    base = {"value": False, "idempotencyKey": "idem-setting-1"}
    assert post(client, "/api/admin/settings/ai.enabled", csrf, {**base, "reason": "", "code": code(secret, clock)}
                ).json()["error"] == "reason_required"
    assert post(client, "/api/admin/settings/ai.enabled", csrf, {**base, "reason": "budget", "code": "000000"}
                ).json()["error"] == "invalid_code"
    assert post(client, "/api/admin/settings/ai.per_user_daily", csrf,
                {"value": 9999, "idempotencyKey": "idem-x-123", "reason": "r", "code": code(secret, clock)}
                ).json()["error"] in ("invalid_value", "reason_required")
    ok = post(client, "/api/admin/settings/ai.enabled", csrf, {**base, "reason": "budget", "code": code(secret, clock)})
    assert ok.status_code == 200 and ok.json()["replayed"] is False
    clock.t += 30
    again = post(client, "/api/admin/settings/ai.enabled", csrf, {**base, "reason": "budget", "code": code(secret, clock)})
    assert again.json()["replayed"] is True
    assert accounts.settings()["ai.enabled"]["value"] is False
    events = [e for e in accounts.events() if e["action"] == "settings.update"]
    assert len(events) == 1 and events[0]["before"] == {"key": "ai.enabled", "value": True}
    with sqlite3.connect(config.admin_db) as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM admin_events")


def test_journal_never_holds_secrets(env):
    config, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    with sqlite3.connect(config.admin_db) as conn:
        dump = " ".join(str(r) for r in conn.execute("SELECT * FROM admin_events"))
    assert "pw-owner" not in dump and all(s not in dump for s in secrets_.values()) and "scrypt$" not in dump


def test_last_owner_cannot_be_demoted_or_disabled(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    owner_id = next(a["id"] for a in accounts.admins() if a["role"] == "owner")
    r = post(client, f"/api/admin/admins/{owner_id}", csrf, {"role": "admin", "reason": "test", "idempotencyKey": "k" * 12,
                                                            "code": code(secrets_["owner@x.test"], clock)})
    assert r.status_code == 409 and r.json()["error"] == "last_owner"


def test_invite_and_disable_admin_revokes_their_sessions(env):
    _, clock, client, accounts, secrets_ = env
    admin_client = TestClient(client.app, base_url=ORIGIN)
    login(admin_client, clock, secrets_, "admin@x.test")
    csrf = login(client, clock, secrets_, "owner@x.test")
    r = post(client, "/api/admin/admins", csrf, {"email": "new@x.test", "role": "analyst", "reason": "hire",
                                                 "idempotencyKey": "inv-1-xxxx", "code": code(secrets_["owner@x.test"], clock)})
    assert r.status_code == 200 and any(a["email"] == "new@x.test" and a["state"] == "invited" for a in accounts.admins())
    clock.t += 30
    admin_id = next(a["id"] for a in accounts.admins() if a["email"] == "admin@x.test")
    r = post(client, f"/api/admin/admins/{admin_id}", csrf, {"state": "disabled", "reason": "left", "idempotencyKey": "dis-1-xxxx",
                                                            "code": code(secrets_["owner@x.test"], clock)})
    assert r.status_code == 200
    assert admin_client.get("/api/admin/overview").status_code == 401


def test_revoke_user_sessions_requested_then_completed(env):
    config, clock, client, accounts, secrets_ = env
    store = Store(config.miniapp_db)
    store.create_session("tok-a", DEMO_ID_BASE + 1000, "init-a", int(clock()), 86400)
    csrf = login(client, clock, secrets_, "admin@x.test")
    user = DEMO_ID_BASE + 1000
    r = post(client, f"/api/admin/users/{user}/sessions/revoke", csrf,
             {"reason": "support", "idempotencyKey": "rev-1-xxxx", "code": code(secrets_["admin@x.test"], clock)})
    assert r.status_code == 200 and r.json()["after"]["revoked"] >= 1
    assert store.get_session("tok-a").revoked_at is not None
    kinds = [e["result"] for e in accounts.events(target=("user", str(user)))]
    assert kinds == ["completed", "requested"]
    clock.t += 30
    replay = post(client, f"/api/admin/users/{user}/sessions/revoke", csrf,
                  {"reason": "support", "idempotencyKey": "rev-1-xxxx", "code": code(secrets_["admin@x.test"], clock)})
    assert replay.json()["replayed"] is True


def test_demo_telemetry_only_on_explicit_request(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    assert client.get("/api/admin/overview?days=30").json()["telemetry"] == {"source": "not_collected"}
    # Real cost accounting (billed tokens from the Mini App), never the demo set unless asked for.
    assert client.get("/api/admin/ai?days=7").json()["source"] == "tokens"
    assert client.get("/api/admin/sports-api?days=7").json() == {"source": "not_collected"}
    assert client.get("/api/admin/system").json()["source"] == "not_collected"
    demo = client.get("/api/admin/ai?days=7&demo=1").json()
    assert demo["source"] == "demo" and demo["calls"]
    assert client.get("/api/admin/overview?days=5").json()["error"] == "bad_period"


def test_analyst_demo_calls_and_journal_have_no_user_ids(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "analyst@x.test")
    calls = client.get("/api/admin/ai?days=7&demo=1").json()["calls"]
    assert calls and all(c["userId"] is None for c in calls)


def test_overview_and_forecasts_use_the_real_history(env):
    config, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    history = ForecastHistory(config.history_db, read_only=True)
    overview = client.get("/api/admin/overview?days=30").json()
    assert overview["forecasts"]["total"] == history.count()
    assert overview["users"]["total"] == 64
    page = client.get("/api/admin/forecasts?status=settled&size=5").json()
    assert page["total"] == history.status_counts()["settled"] and len(page["items"]) == 5
    key = page["items"][0]["key"]
    detail = client.get(f"/api/admin/forecasts/{key}").json()
    assert detail["settlements"] and detail["match"]
    found = client.get("/api/admin/forecasts?q=барселона").json()
    assert all("Барселона" in r["match"] for r in found["items"])


def test_store_list_users_search_sort_and_like_escape(tmp_path):
    seed(tmp_path)
    store = Store(tmp_path / "miniapp.db")
    total, rows = store.list_users(1_900_000_000, sort="id", descending=False, limit=5)
    assert total == 64 and [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert store.list_users(1_900_000_000, query="%")[0] == 0  # LIKE wildcards are escaped
    uid = rows[0]["id"]
    assert store.list_users(1_900_000_000, query=str(uid))[1][0]["id"] == uid
    profile = store.user_profile(uid, 1_900_000_000)
    assert "token_hash" not in str(profile) and "init_hash" not in str(profile)


def test_seed_refuses_existing_data(tmp_path):
    seed(tmp_path)
    with pytest.raises(SystemExit):
        seed(tmp_path)


def test_step_up_failures_count_toward_lockout(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    for i in range(5):
        post(client, "/api/admin/settings/ai.enabled", csrf,
             {"value": False, "reason": "r-x", "idempotencyKey": f"lock-{i}-xxxx", "code": "000000"})
    assert client.get("/api/admin/overview").status_code == 401  # locked: sessions revoked
    with pytest.raises(ActionError):
        accounts.update_admin(accounts_admin(accounts), 1, None, None, "r", "k" * 9, "000000", int(clock()))


def accounts_admin(accounts: AdminStore):
    from miniapp.admin_web.accounts import Admin
    a = accounts.admins()[0]
    return Admin(a["id"], a["email"], a["role"], a["state"])


def test_wrong_totp_codes_reach_the_lockout(env):
    _, clock, client, accounts, secrets_ = env
    h = {"Origin": ORIGIN}
    client.post("/api/admin/auth/login", json={"email": "owner@x.test", "password": "pw-owner"}, headers=h)
    errors = [client.post("/api/admin/auth/totp", json={"code": "000000"}, headers=h).json()["error"] for _ in range(5)]
    assert errors == ["invalid_code"] * 4 + ["locked"]
    assert any(e["action"] == "auth.locked" for e in accounts.events())
    with pytest.raises(AuthError, match="invalid_credentials"):
        accounts.login_password("owner@x.test", "pw-owner", int(clock()))


def test_unfinished_revocation_is_completed_on_retry(env, monkeypatch):
    config, clock, client, accounts, secrets_ = env
    store = Store(config.miniapp_db)
    user = DEMO_ID_BASE + 1000
    store.create_session("tok-b", user, "init-b", int(clock()), 86400)
    csrf = login(client, clock, secrets_, "admin@x.test")

    def crash(*_a, **_k):
        raise RuntimeError("crash between requested and the revocation")

    monkeypatch.setattr(Store, "revoke_user_sessions", crash)
    body = {"reason": "support", "idempotencyKey": "rev-crash-1", "code": code(secrets_["admin@x.test"], clock)}
    assert post(client, f"/api/admin/users/{user}/sessions/revoke", csrf, body).status_code == 500
    assert store.get_session("tok-b").revoked_at is None
    monkeypatch.undo()
    clock.t += 30
    retry = post(client, f"/api/admin/users/{user}/sessions/revoke", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)})
    assert retry.status_code == 200 and retry.json()["replayed"] is False and retry.json()["resumed"] is True
    assert store.get_session("tok-b").revoked_at is not None
    kinds = [e["result"] for e in accounts.events(target=("user", str(user)))]
    assert kinds == ["completed", "requested"]
    other = post(client, f"/api/admin/users/{user + 37}/sessions/revoke", csrf, {**body, "code": "000000"})
    assert other.status_code == 409  # one key cannot be reused for another user


def test_analyst_journal_hides_user_targets_and_reasons(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    user = DEMO_ID_BASE + 1000
    post(client, f"/api/admin/users/{user}/sessions/revoke", csrf,
         {"reason": "user @someone asked", "idempotencyKey": "rev-an-1x", "code": code(secrets_["admin@x.test"], clock)})
    analyst = TestClient(client.app, base_url=ORIGIN)
    login(analyst, clock, secrets_, "analyst@x.test")
    items = [e for e in analyst.get("/api/admin/events?days=7").json()["items"] if e["targetType"] == "user"]
    assert items and all(e["targetId"] is None and e["reason"] is None for e in items)


def test_quality_gate_is_per_model_version(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    q = client.get("/api/admin/forecasts/summary").json()["quality"]
    assert q["currentGateForecasts"] == q["versions"][q["currentVersion"]]["prospectiveGate"]["forecasts"]
    assert q["minimum"] == 300


def test_pilot_refuses_foreign_or_shared_directories(tmp_path, monkeypatch):
    from miniapp.admin_web.__main__ import main

    foreign = tmp_path / "data"
    foreign.mkdir()
    (foreign / "miniapp.db").write_bytes(b"")  # e.g. a stand data directory
    monkeypatch.setenv("ADMIN_PILOT_DIR", str(foreign))
    assert main(["init"]) == 2 and not (foreign / "admin").exists()
    assert main(["serve"]) == 2
    with pytest.raises(ConfigError):
        create_admin_app(AdminWebConfig(data_dir=foreign, port=PORT, static_dir=None))
    seeded = tmp_path / "open"
    seeded.mkdir(mode=0o755)
    seed(seeded)
    seeded.chmod(0o755)
    with pytest.raises(ConfigError, match="private"):
        create_admin_app(AdminWebConfig(data_dir=seeded, port=PORT, static_dir=None))


def test_idempotency_key_is_bound_to_action_and_value(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    secret = secrets_["owner@x.test"]
    body = {"value": False, "reason": "budget", "idempotencyKey": "idem-bound-1"}
    assert post(client, "/api/admin/settings/ai.enabled", csrf, {**body, "code": code(secret, clock)}).status_code == 200
    clock.t += 30
    other_key = post(client, "/api/admin/settings/quick.enabled", csrf, {**body, "code": code(secret, clock)})
    assert other_key.status_code == 409 and other_key.json()["error"] == "idempotency_key_reused"
    other_value = post(client, "/api/admin/settings/ai.enabled", csrf, {**body, "value": True, "code": code(secret, clock)})
    assert other_value.status_code == 409
    assert accounts.settings()["quick.enabled"]["value"] is True


def test_analyst_never_sees_free_text_reasons(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    post(client, "/api/admin/settings/ai.enabled", csrf, {"value": False, "reason": "asked by @someone",
                                                         "idempotencyKey": "idem-reason", "code": code(secrets_["owner@x.test"], clock)})
    analyst = TestClient(client.app, base_url=ORIGIN)
    login(analyst, clock, secrets_, "analyst@x.test")
    assert all(e["reason"] is None for e in analyst.get("/api/admin/events?days=7").json()["items"])
    assert any(e["reason"] for e in client.get("/api/admin/events?days=7").json()["items"])


def test_pilot_directory_inside_the_repository_is_refused():
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(ConfigError, match="outside the repository"):
        AdminWebConfig(data_dir=repo / "pilot-data", port=PORT, static_dir=None).validate()


def test_analyst_journal_has_no_people_values(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    post(client, "/api/admin/admins", csrf, {"email": "person@x.test", "role": "analyst", "reason": "hire",
                                             "idempotencyKey": "inv-redact-1", "code": code(secrets_["owner@x.test"], clock)})
    analyst = TestClient(client.app, base_url=ORIGIN)
    login(analyst, clock, secrets_, "analyst@x.test")
    body = analyst.get("/api/admin/events?days=7").text
    assert "person@x.test" not in body


def test_revocation_key_needs_the_same_admin_and_reason(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    user = DEMO_ID_BASE + 1000
    body = {"reason": "support", "idempotencyKey": "rev-same-1", "code": code(secrets_["admin@x.test"], clock)}
    assert post(client, f"/api/admin/users/{user}/sessions/revoke", csrf, body).status_code == 200
    clock.t += 30
    changed = post(client, f"/api/admin/users/{user}/sessions/revoke", csrf,
                   {**body, "reason": "other", "code": code(secrets_["admin@x.test"], clock)})
    assert changed.status_code == 409


def test_demo_activity_is_consistent():
    from datetime import UTC, datetime

    from miniapp.admin_web.demo import telemetry
    for days in (1, 7, 30, 90):
        t = telemetry(days, datetime(2026, 11, 10, 0, 10, tzinfo=UTC))
        assert t["series"][-1]["dau"] <= t["wau"] <= t["mau"]
    assert len(telemetry(1, datetime(2026, 11, 10, 0, 10, tzinfo=UTC))["series"]) == 2


def test_revocation_completion_is_recorded_once(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    user = DEMO_ID_BASE + 1000
    post(client, f"/api/admin/users/{user}/sessions/revoke", csrf,
         {"reason": "support", "idempotencyKey": "rev-once-1", "code": code(secrets_["admin@x.test"], clock)})
    done = [e for e in accounts.events(target=("user", str(user))) if e["result"] == "completed"]
    rid = sqlite3.connect(accounts.path).execute(
        "SELECT request_id FROM admin_events WHERE id = ?", (done[0]["id"],)).fetchone()[0]
    admin = accounts_admin(accounts)
    late = accounts.revoke_completed(admin, user, 0, int(clock()), rid)  # a concurrent retry finishing second
    assert late["replayed"] is True and late["after"] == done[0]["after"]
    with sqlite3.connect(accounts.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO admin_events (at, action, kind, request_id) VALUES (1, 'x', 'completed', ?)", (rid,))


def test_analyst_cannot_list_admins_or_see_actors(env):
    _, clock, client, _, secrets_ = env
    analyst = TestClient(client.app, base_url=ORIGIN)
    login(analyst, clock, secrets_, "analyst@x.test")
    assert analyst.get("/api/admin/admins").status_code == 403
    assert "@x.test" not in analyst.get("/api/admin/events?days=7").text


def test_admin_update_replay_must_match_the_whole_input(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    target = next(a["id"] for a in accounts.admins() if a["email"] == "analyst@x.test")
    body = {"role": "admin", "state": "disabled", "reason": "role change", "idempotencyKey": "upd-full-1"}
    assert post(client, f"/api/admin/admins/{target}", csrf, {**body, "code": code(secrets_["owner@x.test"], clock)}).status_code == 200
    clock.t += 30
    partial = post(client, f"/api/admin/admins/{target}", csrf,
                   {**body, "state": None, "code": code(secrets_["owner@x.test"], clock)})
    assert partial.status_code == 409


def test_actor_is_rechecked_inside_the_write_transaction(env):
    # The route saw an active admin; by the time the body arrived the actor was disabled.
    _, clock, _, accounts, secrets_ = env
    from miniapp.admin_web.accounts import Admin
    row = next(a for a in accounts.admins() if a["email"] == "admin@x.test")
    stale = Admin(row["id"], row["email"], "admin", "active")
    with sqlite3.connect(accounts.path) as conn:
        conn.execute("UPDATE admin_users SET state = 'disabled' WHERE id = ?", (row["id"],))
    with pytest.raises(ActionError, match="forbidden"):
        accounts.set_setting(stale, "ai.enabled", False, "late", "late-key-1", code(secrets_["admin@x.test"], clock),
                             int(clock()))
    assert accounts.settings()["ai.enabled"]["value"] is True
    with sqlite3.connect(accounts.path) as conn:
        conn.execute("UPDATE admin_users SET state = 'active', role = 'analyst' WHERE id = ?", (row["id"],))
    with pytest.raises(ActionError, match="forbidden"):
        accounts.revoke_requested(stale, DEMO_ID_BASE + 1000, "late", "late-key-2",
                                  code(secrets_["admin@x.test"], clock), int(clock()), "rid-late")


def test_resumed_revocation_needs_the_permission_now(env, monkeypatch):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    user = DEMO_ID_BASE + 1000

    def crash(*_a, **_k):
        raise RuntimeError("crash")

    monkeypatch.setattr(Store, "revoke_user_sessions", crash)
    body = {"reason": "support", "idempotencyKey": "rev-demoted-1", "code": code(secrets_["admin@x.test"], clock)}
    assert post(client, f"/api/admin/users/{user}/sessions/revoke", csrf, body).status_code == 500
    monkeypatch.undo()
    row = next(a for a in accounts.admins() if a["email"] == "admin@x.test")
    from miniapp.admin_web.accounts import Admin
    with sqlite3.connect(accounts.path) as conn:
        conn.execute("UPDATE admin_users SET role = 'analyst' WHERE id = ?", (row["id"],))
    with pytest.raises(ActionError, match="forbidden"):
        accounts.revoke_requested(Admin(row["id"], row["email"], "admin", "active"), user, "support", "rev-demoted-1",
                                  "000000", int(clock()), "rid-x")


def test_body_limit_holds_without_content_length(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")

    def chunks():
        yield b'{"reason": "'
        for _ in range(40):
            yield b"x" * 1024
        yield b'"}'

    r = client.post("/api/admin/settings/ai.enabled", content=chunks(),
                    headers={"Origin": ORIGIN, CSRF_HEADER: csrf, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_locked_account_looks_like_wrong_credentials(env):
    _, _, client, _, _ = env
    h = {"Origin": ORIGIN}
    for _ in range(5):
        client.post("/api/admin/auth/login", json={"email": "admin@x.test", "password": "bad"}, headers=h)
    locked = client.post("/api/admin/auth/login", json={"email": "admin@x.test", "password": "pw-admin"}, headers=h)
    unknown = client.post("/api/admin/auth/login", json={"email": "ghost@x.test", "password": "pw-admin"}, headers=h)
    assert (locked.status_code, locked.json()["error"]) == (unknown.status_code, unknown.json()["error"])


def test_upcoming_forecasts_are_filtered_in_sql(tmp_path):
    seed(tmp_path)
    history = ForecastHistory(tmp_path / "miniapp.forecasts.db", read_only=True)
    _, rows = history.list_forecasts(status="pending", kickoff_from="2000-01-01T00:00:00Z", sort="kickoff",
                                     descending=False, limit=500)
    later = rows[len(rows) // 2]["kickoffAt"]
    _, upcoming = history.list_forecasts(status="pending", kickoff_from=later, sort="kickoff", descending=False, limit=3)
    assert upcoming and all(r["kickoffAt"] >= later for r in upcoming)


def test_non_ascii_digits_are_a_wrong_code_not_a_crash(env):
    _, _, client, _, _ = env
    h = {"Origin": ORIGIN}
    client.post("/api/admin/auth/login", json={"email": "owner@x.test", "password": "pw-owner"}, headers=h)
    r = client.post("/api/admin/auth/totp", json={"code": "١٢٣٤٥٦"}, headers=h)  # Arabic-Indic digits
    assert r.status_code == 401 and r.json()["error"] == "invalid_code"
