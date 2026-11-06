from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.settings import Settings
from miniapp.tests.conftest import ORIGIN, FakeClock, fresh, login
from miniapp.tests.signing import TEST_BOT_TOKEN

COOKIE = "sporai_session"


def logged_in(client: TestClient, clock: FakeClock, user: dict | None = None) -> dict:
    response = login(client, fresh(clock, user))
    assert response.status_code == 200, response.text
    return response.json()


def logout(client: TestClient, csrf: str | None, origin: str | None = ORIGIN):
    headers = {}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/api/auth/logout", headers=headers)


# --- health and basics -------------------------------------------------------

def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["cache-control"] == "no-store"


def test_docs_disabled(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- login -------------------------------------------------------------------

def test_login_creates_session_and_me_returns_profile(client: TestClient, clock: FakeClock) -> None:
    body = logged_in(client, clock, {"id": 501, "first_name": "Ayan", "username": "ayan", "language_code": "az"})
    assert body["created"] is True
    assert body["user"] == {
        "id": 501, "first_name": "Ayan", "last_name": None, "username": "ayan", "lang": "az",
        "lang_source": "telegram",
    }
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"]["id"] == 501
    assert me.json()["session"]["csrf_token"] == body["session"]["csrf_token"]
    assert me.headers["cache-control"] == "no-store"


def test_response_never_contains_session_token(client: TestClient, clock: FakeClock) -> None:
    r = login(client, fresh(clock))
    token = client.cookies.get(COOKIE)
    assert token and len(token) >= 43  # 32 random bytes, base64url
    assert token not in r.text
    assert token not in client.get("/api/me").text


def test_cookie_flags_development(client: TestClient, clock: FakeClock) -> None:
    r = login(client, fresh(clock))
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Max-Age=3600" in cookie
    assert "Domain" not in cookie
    assert "Secure" not in cookie  # only in explicit development mode on loopback


def test_cookie_flags_production(tmp_path, clock: FakeClock) -> None:
    settings = Settings(
        bot_token=TEST_BOT_TOKEN,
        db_path=tmp_path / "prod.db",
        trusted_origins=frozenset({"https://app.example.com"}),
        environment="production",
    )
    client = TestClient(create_app(settings, clock=clock), base_url="https://app.example.com")
    r = login(client, fresh(clock), origin="https://app.example.com")
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-sporai_session=")
    for flag in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert flag in cookie
    assert "Domain" not in cookie
    assert client.get("/api/me").status_code == 200


def test_login_requires_trusted_origin(client: TestClient, clock: FakeClock) -> None:
    for origin in (None, "null", "http://127.0.0.1:5174", "https://127.0.0.1:5173", "http://localhost:5173",
                   "http://127.0.0.1:5173.evil.com", "http://evil.com"):
        r = login(client, fresh(clock), origin=origin)
        assert r.status_code == 403, origin
        assert r.json() == {"error": "origin_not_allowed"}
    assert COOKIE not in client.cookies


def test_forwarded_headers_are_not_trusted(client: TestClient, clock: FakeClock) -> None:
    r = login(client, fresh(clock), origin="http://evil.com",
              **{"X-Forwarded-Host": "127.0.0.1:5173", "X-Forwarded-Proto": "http"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("content_type", "body", "status"),
    [
        ("text/plain", '{"init_data":"a=1"}', 415),
        ("application/x-www-form-urlencoded", "init_data=a%3D1", 415),
        ("application/json", "not json", 400),
        ("application/json", '["a=1"]', 400),
        ("application/json", '{"init_data":"a=1","user_id":1}', 400),
        ("application/json", '{"init_data":1}', 400),
        ("application/json", '{"init_data":"a","init_data":"b"}', 400),
        ("application/json", "{}", 400),
    ],
)
def test_login_strict_json_contract(client: TestClient, content_type: str, body: str, status: int) -> None:
    r = client.post("/api/auth/session", content=body.encode(),
                    headers={"Content-Type": content_type, "Origin": ORIGIN})
    assert r.status_code == status
    assert "init_data" not in r.text  # nothing echoed back


def test_login_body_size_limit(client: TestClient) -> None:
    big = json.dumps({"init_data": "a=" + "x" * 9000}).encode()
    r = client.post("/api/auth/session", content=big, headers={"Content-Type": "application/json", "Origin": ORIGIN})
    assert r.status_code == 413


def test_login_body_size_limit_without_content_length(client: TestClient) -> None:
    def chunks():
        yield b'{"init_data":"'
        for _ in range(20):
            yield b"x" * 1000
        yield b'"}'

    r = client.post("/api/auth/session", content=chunks(), headers={"Content-Type": "application/json", "Origin": ORIGIN})
    assert r.status_code == 413


def test_invalid_and_expired_init_data(client: TestClient, clock: FakeClock) -> None:
    r = login(client, fresh(clock, token="7000000002:other-bot"))
    assert r.status_code == 401 and r.json() == {"error": "init_data_invalid"}
    from miniapp.tests.signing import make_init_data

    old = make_init_data(None, auth_date=int(clock.now) - 301)
    r = login(client, old)
    assert r.status_code == 401 and r.json() == {"error": "init_data_expired"}
    assert COOKIE not in client.cookies


def test_user_id_comes_only_from_signed_data(client: TestClient, clock: FakeClock) -> None:
    logged_in(client, clock, {"id": 700})
    assert client.get("/api/me?user_id=701").json()["user"]["id"] == 700
    assert client.get("/api/me", headers={"X-User-Id": "701"}).json()["user"]["id"] == 700


# --- /api/me -----------------------------------------------------------------

def test_me_without_cookie(client: TestClient) -> None:
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json() == {"error": "unauthenticated"}
    assert r.headers["cache-control"] == "no-store"


def test_me_with_forged_cookie(client: TestClient, clock: FakeClock) -> None:
    logged_in(client, clock)
    client.cookies.set(COOKIE, "forged-" + "A" * 40)
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json() == {"error": "unauthenticated"}
    assert 'sporai_session=""' in r.headers["set-cookie"]  # cleared


def test_session_expiry(client: TestClient, clock: FakeClock) -> None:
    logged_in(client, clock)
    clock.advance(3599)
    assert client.get("/api/me").status_code == 200
    clock.advance(1)
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json() == {"error": "session_expired"}


def test_cannot_read_other_users_profile(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    alice, bob = TestClient(app), TestClient(app)
    logged_in(alice, clock, {"id": 1001, "first_name": "Alice"})
    logged_in(bob, clock, {"id": 1002, "first_name": "Bob"})
    assert alice.get("/api/me").json()["user"]["first_name"] == "Alice"
    assert bob.get("/api/me").json()["user"]["first_name"] == "Bob"
    assert alice.get("/api/me?user_id=1002").json()["user"]["id"] == 1001


# --- repeated / concurrent login ---------------------------------------------

def test_repeat_login_with_live_session_is_idempotent(client: TestClient, clock: FakeClock) -> None:
    first = logged_in(client, clock)
    token = client.cookies.get(COOKIE)
    clock.advance(10)
    second = logged_in(client, clock)
    assert second["created"] is False
    assert client.cookies.get(COOKIE) == token
    assert second["session"] == first["session"]


def test_login_as_other_user_replaces_session(client: TestClient, clock: FakeClock) -> None:
    logged_in(client, clock, {"id": 1})
    old = client.cookies.get(COOKIE)
    body = logged_in(client, clock, {"id": 2})
    assert body["created"] is True and body["user"]["id"] == 2
    assert client.cookies.get(COOKIE) != old
    client.cookies.set(COOKIE, old)
    assert client.get("/api/me").status_code == 401  # old session revoked


def test_concurrent_logins_create_one_user(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    init_data = fresh(clock, {"id": 3003})

    def attempt(_: int) -> int:
        return login(TestClient(app), init_data).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(8)))
    assert statuses == [200] * 8
    assert app.state.store.count_users() == 1


# --- logout ------------------------------------------------------------------

def test_logout_revokes_session_and_clears_cookie(client: TestClient, clock: FakeClock) -> None:
    body = logged_in(client, clock)
    token = client.cookies.get(COOKIE)
    r = logout(client, body["session"]["csrf_token"])
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert 'sporai_session=""' in r.headers["set-cookie"]
    client.cookies.set(COOKIE, token)  # a stolen copy of the cookie is useless now
    assert client.get("/api/me").json() == {"error": "unauthenticated"}


def test_logout_requires_csrf(client: TestClient, clock: FakeClock) -> None:
    logged_in(client, clock)
    assert logout(client, None).status_code == 403
    assert logout(client, "0" * 64).json() == {"error": "csrf_failed"}
    assert client.get("/api/me").status_code == 200  # still logged in


def test_logout_requires_origin(client: TestClient, clock: FakeClock) -> None:
    body = logged_in(client, clock)
    assert logout(client, body["session"]["csrf_token"], origin="https://evil.example").status_code == 403
    assert logout(client, body["session"]["csrf_token"], origin=None).status_code == 403
    assert client.get("/api/me").status_code == 200


def test_csrf_token_is_bound_to_session(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    a, b = TestClient(app), TestClient(app)
    csrf_a = logged_in(a, clock, {"id": 1})["session"]["csrf_token"]
    logged_in(b, clock, {"id": 2})
    assert logout(b, csrf_a).status_code == 403


def test_same_init_data_cannot_log_in_again_after_logout(client: TestClient, clock: FakeClock) -> None:
    init_data = fresh(clock)
    body = login(client, init_data).json()
    logout(client, body["session"]["csrf_token"])
    r = login(client, init_data)
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}
    # A newly signed launch (new auth_date → new hash) works.
    clock.advance(5)
    assert login(client, fresh(clock)).status_code == 200


def test_logout_without_session_is_harmless(client: TestClient) -> None:
    r = logout(client, None)
    assert r.status_code == 200


def test_get_never_changes_state(client: TestClient, clock: FakeClock) -> None:
    body = logged_in(client, clock)
    assert client.get("/api/auth/logout").status_code == 405
    assert client.get("/api/auth/session").status_code == 405
    assert client.get("/api/me").status_code == 200
    assert body


# --- CORS ----------------------------------------------------------------------

def test_no_cors_headers_for_foreign_origin(client: TestClient, clock: FakeClock) -> None:
    pre = client.options("/api/auth/session", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert "access-control-allow-origin" not in pre.headers
    logged_in(client, clock)
    me = client.get("/api/me", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in me.headers
    assert "access-control-allow-credentials" not in me.headers


# --- language ------------------------------------------------------------------

def test_explicit_language_survives_relogin(client: TestClient, clock: FakeClock) -> None:
    body = logged_in(client, clock, {"id": 9, "language_code": "ru"})
    assert body["user"]["lang"] == "ru"
    r = client.post("/api/me/language", json={"lang": "en"},
                    headers={"Origin": ORIGIN, "X-CSRF-Token": body["session"]["csrf_token"]})
    assert r.status_code == 200
    client.cookies.clear()
    clock.advance(60)
    again = logged_in(client, clock, {"id": 9, "language_code": "az"})
    assert again["user"]["lang"] == "en" and again["user"]["lang_source"] == "user"


def test_telegram_language_followed_until_explicit_choice(client: TestClient, clock: FakeClock) -> None:
    logged_in(client, clock, {"id": 10, "language_code": "ru"})
    client.cookies.clear()
    assert logged_in(client, clock, {"id": 10, "language_code": "az-AZ"})["user"]["lang"] == "az"
    client.cookies.clear()
    assert logged_in(client, clock, {"id": 10, "language_code": "de"})["user"]["lang"] == "ru"


def test_language_change_requires_csrf_and_valid_value(client: TestClient, clock: FakeClock) -> None:
    body = logged_in(client, clock)
    assert client.post("/api/me/language", json={"lang": "en"}, headers={"Origin": ORIGIN}).status_code == 403
    csrf = body["session"]["csrf_token"]
    assert client.post("/api/me/language", json={"lang": "de"},
                       headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 400


# --- rate limit ---------------------------------------------------------------

def test_login_rate_limit(settings: Settings, clock: FakeClock) -> None:
    client = TestClient(create_app(settings, clock=clock))
    bad = "a=1&hash=" + "0" * 64
    for _ in range(settings.login_rate_limit):
        assert login(client, bad).status_code == 401
    r = login(client, fresh(clock))
    assert r.status_code == 429 and r.json() == {"error": "rate_limited"}
    assert int(r.headers["retry-after"]) >= 1
    clock.advance(settings.login_rate_window_s)
    assert login(client, fresh(clock)).status_code == 200


# --- secrets in logs ----------------------------------------------------------

def test_logs_contain_no_secrets(client: TestClient, clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    init_data = fresh(clock, {"id": 4242, "first_name": "LogProbe"})
    body = login(client, init_data).json()
    token = client.cookies.get(COOKIE)
    login(client, fresh(clock, token="7000000002:other-bot"))
    logout(client, body["session"]["csrf_token"])
    text = caplog.text
    assert "auth.session user=4242" in text and "auth.rejected reason=bad_signature" in text
    for secret in (init_data, token, body["session"]["csrf_token"], TEST_BOT_TOKEN, "LogProbe"):
        assert secret not in text
