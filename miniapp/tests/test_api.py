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


def test_account_switch_burns_the_old_accounts_launch(client: TestClient, clock: FakeClock) -> None:
    old_launch = fresh(clock, {"id": 1})
    assert login(client, old_launch).status_code == 200
    logged_in(client, clock, {"id": 2})  # the device switches accounts: session 1 is revoked
    clock.advance(5)  # past the switch second, still inside initData max age
    client.cookies.clear()
    replay = login(client, old_launch)
    assert replay.status_code == 401 and replay.json() == {"error": "init_data_reused"}


def test_concurrent_logins_create_one_user(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    init_data = fresh(clock, {"id": 3003})

    def attempt(_: int) -> int:
        return login(TestClient(app), init_data).status_code

    # Within the session cap: beyond it, eviction burns the shared launch (test_stand.py).
    attempts = settings.max_sessions_per_user
    with ThreadPoolExecutor(max_workers=attempts) as pool:
        statuses = list(pool.map(attempt, range(attempts)))
    assert statuses == [200] * attempts
    assert app.state.store.count_users() == 1


# --- logout ------------------------------------------------------------------

def test_samesite_none_cookie_is_partitioned_and_works_on_this_python(tmp_path, clock: FakeClock) -> None:
    # Telegram Web iframe setting: Secure + SameSite=None + Partitioned (CHIPS).
    # Regression: Starlette's partitioned= raised ValueError before Python 3.14 → 500.
    origin = "https://app.example.com"
    settings = Settings(
        bot_token=TEST_BOT_TOKEN,
        db_path=tmp_path / "none.db",
        trusted_origins=frozenset({origin}),
        environment="production",
        cookie_samesite="none",
    )
    client = TestClient(create_app(settings, clock=clock), base_url=origin)
    r = login(client, fresh(clock), origin=origin)
    assert r.status_code == 200, r.text
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-sporai_session=")
    for flag in ("Secure", "HttpOnly", "SameSite=none", "Path=/"):
        assert flag in cookie
    assert cookie.endswith("; Partitioned") and cookie.count("Partitioned") == 1
    assert client.get("/api/me").status_code == 200

    out = logout(client, r.json()["session"]["csrf_token"], origin=origin)
    assert out.status_code == 200, out.text
    cleared = out.headers["set-cookie"]
    assert cleared.startswith('__Host-sporai_session=""') and cleared.endswith("; Partitioned")
    assert "SameSite=none" in cleared and "Secure" in cleared


def test_lax_cookie_is_not_partitioned(client: TestClient, clock: FakeClock) -> None:
    r = login(client, fresh(clock))
    assert "Partitioned" not in r.headers["set-cookie"]


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


def logout_with_launch(client: TestClient, csrf: str, init_data: str, **headers: str):
    return client.post("/api/auth/logout", content=json.dumps({"init_data": init_data}),
                       headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json", **headers})


def test_logout_burns_the_current_launch_not_only_the_first(client: TestClient, clock: FakeClock) -> None:
    first = fresh(clock)
    assert login(client, first).status_code == 200
    clock.advance(1800)  # Mini App reopened later (session TTL here is 1 h): new launch, cookie reused
    current = fresh(clock)
    csrf = client.get("/api/me").json()["session"]["csrf_token"]
    r = logout_with_launch(client, csrf, current)
    assert r.status_code == 200 and r.json() == {"ok": True}
    # A reload of the same WebView must not silently sign in with the current launch.
    r = login(client, current)
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}
    # Explicit sign-in again = reopening the Mini App, i.e. a new signed launch.
    clock.advance(5)
    assert login(client, fresh(clock)).status_code == 200


def test_logout_burn_is_bounded_in_time(client: TestClient, clock: FakeClock, settings: Settings) -> None:
    current = fresh(clock)
    csrf = login(client, current).json()["session"]["csrf_token"]
    logout_with_launch(client, csrf, current)
    clock.advance(settings.init_data_max_age_s + settings.init_data_future_skew_s + 1)
    # The burned launch has expired by itself; nothing blocks a new launch.
    assert login(client, current).json() == {"error": "init_data_expired"}
    assert login(client, fresh(clock)).status_code == 200


def test_logout_does_not_burn_another_users_launch(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    a = TestClient(app)
    csrf = logged_in(a, clock, {"id": 5001})["session"]["csrf_token"]
    others = fresh(clock, {"id": 5002})
    assert logout_with_launch(a, csrf, others).status_code == 200
    assert a.get("/api/me").status_code == 401  # the session itself is revoked
    assert login(TestClient(app), others).status_code == 200


def test_logout_with_unusable_launch_still_logs_out(client: TestClient, clock: FakeClock) -> None:
    csrf = logged_in(client, clock)["session"]["csrf_token"]
    assert logout_with_launch(client, csrf, "a=1&hash=" + "0" * 64).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_logout_body_is_strict(client: TestClient, clock: FakeClock) -> None:
    csrf = logged_in(client, clock)["session"]["csrf_token"]
    r = client.post("/api/auth/logout", content=b'{"init_data": 1}',
                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/auth/logout", content=b"init_data=x",
                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "text/plain"})
    assert r.status_code == 415
    assert client.get("/api/me").status_code == 200  # rejected requests change nothing


def test_login_racing_a_logout_cannot_use_the_burned_launch(client: TestClient, clock: FakeClock,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    launch = fresh(clock)
    csrf = login(client, launch).json()["session"]["csrf_token"]
    logout_with_launch(client, csrf, launch)
    # Simulate the race: the early check ran before the logout committed.
    monkeypatch.setattr(client.app.state.store, "is_init_data_consumed", lambda *_: False)
    r = login(client, launch)
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}
    assert COOKIE not in client.cookies
    assert client.get("/api/me").status_code == 401


def test_replaying_a_burned_launch_does_not_spend_the_login_budget(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    c = TestClient(app)
    launch = fresh(clock, {"id": 6100})
    csrf = login(c, launch).json()["session"]["csrf_token"]
    logout_with_launch(c, csrf, launch)
    for _ in range(3 * settings.login_rate_limit):
        assert login(TestClient(app), launch).json() == {"error": "init_data_reused"}
    clock.advance(1)
    assert login(TestClient(app), fresh(clock, {"id": 6100})).status_code == 200


def test_logout_with_an_expired_session_still_burns_the_current_launch(client: TestClient, clock: FakeClock,
                                                                       settings: Settings) -> None:
    csrf = logged_in(client, clock)["session"]["csrf_token"]
    token = client.cookies.get(COOKIE)
    clock.advance(settings.session_ttl_s + 1)  # session expired while the page stayed open
    current = fresh(clock)  # the launch the page runs in now (reopened), still fresh
    client.cookies.set(COOKIE, token)
    assert logout(client, None).status_code == 403  # CSRF still required
    client.cookies.set(COOKIE, token)
    assert logout_with_launch(client, csrf, current).status_code == 200
    r = login(client, current)
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}


def test_logout_after_the_browser_dropped_the_expired_cookie_burns_the_launch(
        client: TestClient, clock: FakeClock, settings: Settings) -> None:
    csrf = logged_in(client, clock)["session"]["csrf_token"]
    clock.advance(settings.session_ttl_s + 1)
    current = fresh(clock)
    client.cookies.clear()  # Max-Age passed: the browser no longer sends the cookie
    assert logout_with_launch(client, csrf, current).status_code == 200
    r = login(client, current)
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}
    clock.advance(1)
    assert login(client, fresh(clock)).status_code == 200  # a new launch is not blocked


def test_logout_without_session_burns_nothing_for_unsigned_or_foreign_origin(client: TestClient,
                                                                            clock: FakeClock) -> None:
    current = fresh(clock)
    assert logout_with_launch(client, "x", "a=1&hash=" + "0" * 64).status_code == 200
    assert logout_with_launch(client, "x", current, Origin="https://evil.example").status_code == 403
    assert login(client, current).status_code == 200


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

def _anonymous_login(app, init_data: str, **headers: str):
    """A login from a fresh device (no cookie) through the same TCP peer."""
    return login(TestClient(app), init_data, **headers)


def test_unsigned_flood_behind_one_proxy_does_not_block_valid_logins(settings: Settings, clock: FakeClock) -> None:
    # TestClient always connects from the same address, like every user behind one reverse proxy.
    app = create_app(settings, clock=clock)
    garbage = "a=1&hash=" + "0" * 64
    forged_user = 'user={"id":777}&auth_date=1&hash=' + "f" * 64  # unsigned user_id
    for _ in range(5 * settings.login_rate_limit):
        assert _anonymous_login(app, garbage).status_code == 401
        assert _anonymous_login(app, forged_user).status_code == 401
    assert _anonymous_login(app, fresh(clock, {"id": 777, "first_name": "Victim"})).status_code == 200
    assert _anonymous_login(app, fresh(clock, {"id": 778, "first_name": "Other"})).status_code == 200


def test_login_rate_limit_is_per_verified_user(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    user = {"id": 4100, "first_name": "Busy"}
    for i in range(settings.login_rate_limit):
        clock.advance(1)  # every launch has its own initData
        assert _anonymous_login(app, fresh(clock, user), **{"X-Forwarded-For": f"10.0.0.{i}"}).status_code == 200
    clock.advance(1)
    # A new X-Forwarded-For (or X-Real-IP) value is not a new client: the header is ignored.
    r = _anonymous_login(app, fresh(clock, user), **{"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.9"})
    assert r.status_code == 429 and r.json() == {"error": "rate_limited"}
    assert int(r.headers["retry-after"]) >= 1
    # Other users are not affected by this user's budget.
    assert _anonymous_login(app, fresh(clock, {"id": 4101})).status_code == 200
    clock.advance(settings.login_rate_window_s)
    assert _anonymous_login(app, fresh(clock, user)).status_code == 200


def test_forwarded_for_cannot_make_garbage_count_against_a_user(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    for i in range(3 * settings.login_rate_limit):
        r = _anonymous_login(app, f"user=%7B%22id%22%3A4200%7D&hash={'0' * 64}", **{"X-Forwarded-For": f"10.1.0.{i}"})
        assert r.status_code == 401
    assert _anonymous_login(app, fresh(clock, {"id": 4200})).status_code == 200


# --- unexpected errors -------------------------------------------------------

SENSITIVE = "initData=user%3D%7B%7D&hash=deadbeef token=7000000001:AAsecret cookie=abc"


def test_unhandled_error_is_contained(client: TestClient, clock: FakeClock, caplog: pytest.LogCaptureFixture,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    logged_in(client, clock)

    def boom(_: int) -> None:
        raise RuntimeError(SENSITIVE)

    monkeypatch.setattr(client.app.state.store, "get_user", boom)
    caplog.set_level(logging.DEBUG)
    # TestClient re-raises anything that reaches the server: getting a response
    # proves the exception did not propagate to uvicorn (which would log its text).
    r = client.get("/api/me", headers={"X-Request-ID": "client-chosen\nfake log line"})
    assert r.status_code == 500
    request_id = r.headers["x-request-id"]
    assert r.json() == {"error": "internal", "request_id": request_id}
    assert len(request_id) == 16 and request_id != "client-chosen"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert f"event=unhandled_error request_id={request_id} type=RuntimeError site=test_api.py:" in caplog.text
    for leaked in ("AAsecret", "deadbeef", "initData=", "Traceback", "fake log line"):
        assert leaked not in caplog.text and leaked not in r.text


def test_every_response_has_a_request_id(client: TestClient) -> None:
    a, b = client.get("/api/health"), client.get("/api/health")
    assert a.headers["x-request-id"] != b.headers["x-request-id"]
    assert client.get("/api/nope").headers["x-content-type-options"] == "nosniff"


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
