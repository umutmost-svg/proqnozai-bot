"""Closed launch: only the listed Telegram users may sign in or use a session (MINIAPP_ALLOWED_USER_IDS)."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from miniapp import telegram_auth
from miniapp.app import create_app
from miniapp.settings import Settings, SettingsError
from miniapp.tests.conftest import NOW, ORIGIN, FakeClock, login
from miniapp.tests.signing import TEST_BOT_ID, TEST_ED25519_PUBLIC_HEX, make_init_data_ed25519

OWNER, OTHER = 4242, 777


@pytest.fixture(autouse=True)
def telegram_key(monkeypatch) -> None:
    monkeypatch.setitem(telegram_auth.TELEGRAM_PUBLIC_KEYS, "production", TEST_ED25519_PUBLIC_HEX)


def settings(tmp_path, allowed) -> Settings:
    return Settings(bot_token="", db_path=tmp_path / "m.db", trusted_origins=frozenset({ORIGIN}), environment="development",
                    session_ttl_s=3600, initdata_mode="ed25519", bot_id=TEST_BOT_ID, csrf_secret="c" * 40,
                    allowed_user_ids=allowed)


def client(tmp_path, allowed, clock) -> TestClient:
    return TestClient(create_app(settings(tmp_path, allowed), clock=clock), base_url="http://127.0.0.1:8000")


def test_only_the_owner_signs_in_and_nothing_is_stored_about_others(tmp_path) -> None:
    clock = FakeClock()
    c = client(tmp_path, frozenset({OWNER}), clock)
    assert login(c, make_init_data_ed25519({"id": OWNER, "first_name": "O"}, auth_date=int(NOW))).status_code == 200
    assert c.get("/api/me").status_code == 200
    other = TestClient(c.app, base_url="http://127.0.0.1:8000")
    r = login(other, make_init_data_ed25519({"id": OTHER, "first_name": "X"}, auth_date=int(NOW)))
    assert r.status_code == 403 and r.json() == {"error": "not_allowed"}
    ids = [row[0] for row in sqlite3.connect(tmp_path / "m.db").execute("SELECT telegram_user_id FROM users")]
    assert ids == [OWNER]


def test_taking_a_user_off_the_list_ends_their_access_at_once(tmp_path) -> None:
    clock = FakeClock()
    first = client(tmp_path, frozenset({OWNER, OTHER}), clock)
    assert login(first, make_init_data_ed25519({"id": OTHER, "first_name": "X"}, auth_date=int(NOW))).status_code == 200
    cookies = dict(first.cookies)
    second = client(tmp_path, frozenset({OWNER}), clock)  # redeployed with a shorter list, same database
    for k, v in cookies.items():
        second.cookies.set(k, v)
    assert second.get("/api/me").status_code == 401


def test_without_a_list_everyone_with_a_valid_launch_signs_in(tmp_path) -> None:
    c = client(tmp_path, None, FakeClock())
    assert login(c, make_init_data_ed25519({"id": OTHER, "first_name": "X"}, auth_date=int(NOW))).status_code == 200


@pytest.mark.parametrize("raw,ok", [("4242", True), ("4242, 777", True), ("", None), ("abc", False), ("0", False),
                                    ("-5", False)])
def test_allowed_ids_from_env(tmp_path, raw, ok) -> None:
    env = {"MINIAPP_INITDATA_MODE": "ed25519", "MINIAPP_BOT_ID": str(TEST_BOT_ID), "MINIAPP_CSRF_SECRET": "c" * 40,
           "MINIAPP_DB_PATH": str(tmp_path / "m.db"), "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development",
           "MINIAPP_ALLOWED_USER_IDS": raw}
    if ok is False:
        with pytest.raises(SettingsError):
            Settings.from_env(env)
        return
    s = Settings.from_env(env)
    assert s.allowed_user_ids == (None if ok is None else frozenset(int(x) for x in raw.split(",")))


def test_ai_unavailability_is_logged_with_its_code_only(tmp_path, caplog) -> None:
    import logging

    from miniapp.ai.analysis import AiError

    class Down:
        connected = True

        async def analyse(self, event_id, user_id, *, create=True):
            raise AiError("timeout")

    clock = FakeClock()
    app = create_app(settings(tmp_path, None), clock=clock, ai=Down())
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    r = login(c, make_init_data_ed25519({"id": OWNER, "first_name": "O"}, auth_date=int(NOW)))
    csrf = r.json()["session"]["csrf_token"]
    with caplog.at_level(logging.INFO, logger="miniapp"):
        assert c.get("/api/ai/analysis?event_id=fd:1").status_code == 503
        assert c.post("/api/ai/analysis", json={"event_id": "fd:1"},
                      headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 503
    assert "ai.unavailable path=read reason=timeout" in caplog.text
    assert "ai.unavailable path=create reason=timeout" in caplog.text
