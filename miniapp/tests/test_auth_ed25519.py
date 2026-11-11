"""Token-free login: initData checked with Telegram's Ed25519 key (test key pair here), no bot token."""
from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode

import pytest
from fastapi.testclient import TestClient

from miniapp import telegram_auth
from miniapp.app import create_app
from miniapp.settings import Settings, SettingsError
from miniapp.tests.conftest import NOW, ORIGIN, FakeClock, login
from miniapp.tests.signing import TEST_BOT_ID, TEST_BOT_TOKEN, TEST_ED25519_PUBLIC_HEX, make_init_data_ed25519

CSRF_SECRET = "c" * 40


@pytest.fixture(autouse=True)
def telegram_key(monkeypatch) -> None:
    monkeypatch.setitem(telegram_auth.TELEGRAM_PUBLIC_KEYS, "production", TEST_ED25519_PUBLIC_HEX)


def ed_settings(tmp_path, **kw) -> Settings:
    return Settings(bot_token="", db_path=tmp_path / "m.db", trusted_origins=frozenset({ORIGIN}),
                    environment="development", session_ttl_s=3600, initdata_mode="ed25519",
                    bot_id=TEST_BOT_ID, csrf_secret=CSRF_SECRET, **kw)


def ed_client(tmp_path, clock: FakeClock) -> TestClient:
    return TestClient(create_app(ed_settings(tmp_path), clock=clock), base_url="http://127.0.0.1:8000")


def test_login_logout_and_burn_without_a_bot_token(tmp_path) -> None:
    clock = FakeClock()
    client = ed_client(tmp_path, clock)
    launch = make_init_data_ed25519({"id": 77, "first_name": "Ann"}, auth_date=int(NOW))
    r = login(client, launch)
    assert r.status_code == 200 and r.json()["user"]["id"] == 77
    csrf = r.json()["session"]["csrf_token"]
    out = client.post("/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert out.status_code == 200
    clock.advance(5)
    client.cookies.clear()
    assert login(client, launch).json() == {"error": "init_data_reused"}
    # The unsigned `hash` cannot be edited to dodge the burn.
    forged = dict(parse_qsl(launch, keep_blank_values=True)) | {"hash": "f" * 64}
    assert login(client, urlencode(forged)).json() == {"error": "init_data_reused"}


def test_hmac_only_launch_is_rejected_in_ed25519_mode(tmp_path) -> None:
    from miniapp.tests.signing import make_init_data
    client = ed_client(tmp_path, FakeClock())
    assert login(client, make_init_data(auth_date=int(NOW))).json() == {"error": "init_data_invalid"}


def test_csrf_comes_from_its_own_secret(tmp_path) -> None:
    from miniapp.security import CsrfTokens
    assert ed_settings(tmp_path).csrf_key_material == CSRF_SECRET
    assert CsrfTokens(CSRF_SECRET).for_session("t") != CsrfTokens(TEST_BOT_TOKEN).for_session("t")


@pytest.mark.parametrize("kw,message", [
    ({"bot_token": TEST_BOT_TOKEN}, "without the bot token"),
    ({"bot_id": None}, "requires MINIAPP_BOT_ID"),
    ({"csrf_secret": None}, "requires MINIAPP_BOT_ID and MINIAPP_CSRF_SECRET"),
    ({"csrf_secret": "short"}, "csrf_secret"),
    ({"telegram_env": "staging"}, "telegram_env"),
])
def test_ed25519_settings_are_strict(tmp_path, kw, message) -> None:
    base = dict(bot_token="", db_path=tmp_path / "m.db", trusted_origins=frozenset({ORIGIN}),
                environment="development", initdata_mode="ed25519", bot_id=TEST_BOT_ID, csrf_secret=CSRF_SECRET)
    with pytest.raises(SettingsError, match=message):
        Settings(**(base | kw))


def test_from_env_runs_without_the_token(tmp_path) -> None:
    s = Settings.from_env({"MINIAPP_INITDATA_MODE": "ed25519", "MINIAPP_BOT_ID": str(TEST_BOT_ID),
                           "MINIAPP_CSRF_SECRET": CSRF_SECRET, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
                           "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development"})
    assert s.initdata_mode == "ed25519" and s.bot_token == "" and s.bot_id == TEST_BOT_ID
    assert CSRF_SECRET not in repr(s)


def test_hmac_mode_measures_ed25519_readiness_and_checks_the_bot_id(tmp_path, caplog) -> None:
    with pytest.raises(SettingsError, match="does not match"):
        Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "x.db", trusted_origins=frozenset({ORIGIN}),
                 environment="development", bot_id=TEST_BOT_ID + 1)
    s = Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "m.db", trusted_origins=frozenset({ORIGIN}),
                 environment="development", bot_id=TEST_BOT_ID)
    client = TestClient(create_app(s, clock=FakeClock()), base_url="http://127.0.0.1:8000")
    with caplog.at_level(logging.INFO, logger="miniapp"):
        assert login(client, make_init_data_ed25519(auth_date=int(NOW))).status_code == 200
    assert "auth.ed25519_shadow result=ok" in caplog.text
    assert "signature=" not in caplog.text and "user=" not in caplog.text.split("auth.ed25519_shadow")[1][:40]


@pytest.mark.parametrize("first", ["hmac", "ed25519"])
def test_a_burned_launch_stays_burned_across_a_mode_switch(tmp_path, first) -> None:
    clock = FakeClock()
    hmac_settings = Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "m.db", trusted_origins=frozenset({ORIGIN}),
                             environment="development", session_ttl_s=3600, bot_id=TEST_BOT_ID, csrf_secret=CSRF_SECRET)
    apps = {"hmac": hmac_settings, "ed25519": ed_settings(tmp_path)}
    second = "ed25519" if first == "hmac" else "hmac"
    launch = make_init_data_ed25519({"id": 5, "first_name": "Ann"}, auth_date=int(NOW))
    a = TestClient(create_app(apps[first], clock=clock), base_url="http://127.0.0.1:8000")
    r = login(a, launch)
    a.post("/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": r.json()["session"]["csrf_token"]})
    clock.advance(5)
    b = TestClient(create_app(apps[second], clock=clock), base_url="http://127.0.0.1:8000")  # same database
    assert login(b, launch).json() == {"error": "init_data_reused"}
