"""Mini App side of referral links: `startapp=ref_<code>` attributes a user's FIRST start, only behind the flag."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.settings import Settings
from miniapp.store import BurnWindow, Store, referral_code
from miniapp.tests.conftest import FakeClock, fresh, login


def _client(settings: Settings, clock: FakeClock, enabled: bool) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings, referrals_enabled=enabled), clock=clock),
                      base_url="http://127.0.0.1:8000")


def test_referral_code_format():
    assert referral_code("ref_insta01") == "insta01"
    assert referral_code("ref_IN") is None and referral_code("promo") is None and referral_code(None) is None


@pytest.mark.parametrize("enabled", [False, True])
def test_first_start_with_startapp_is_attributed_only_with_the_flag(settings: Settings, clock: FakeClock, enabled):
    client = _client(settings, clock, enabled)
    user = {"id": 777001, "first_name": "R"}
    assert login(client, fresh(clock, user, extra={"start_param": "ref_insta01"})).status_code == 200
    clock.now += 5
    login(client, fresh(clock, user, extra={"start_param": "ref_other99"}))  # later starts never re-attribute
    expected = {777001: {"code": "insta01", "at": int(clock.now) - 5}} if enabled else {}
    assert Store(settings.db_path).attributions() == expected


def test_existing_users_and_bad_codes_are_not_attributed(settings: Settings, clock: FakeClock):
    client = _client(settings, clock, True)
    login(client, fresh(clock, {"id": 777002, "first_name": "Old"}))
    clock.now += 5
    login(client, fresh(clock, {"id": 777002, "first_name": "Old"}, extra={"start_param": "ref_insta01"}))
    login(client, fresh(clock, {"id": 777003, "first_name": "Bad"}, extra={"start_param": "ref_<script>"}))
    assert Store(settings.db_path).attributions() == {}


def test_restore_keeps_attribution_of_the_backup(tmp_path: Path):
    from miniapp.telegram_auth import TelegramUser

    store = Store(tmp_path / "a.db")
    store.init_schema()
    store.upsert_user(TelegramUser(5, "A", None, None, "ru"), 100, "insta01")
    store.backup(tmp_path / "backup.db")
    store.upsert_user(TelegramUser(6, "B", None, None, "ru"), 200, "later01")
    store.restore_from(tmp_path / "backup.db", 300, BurnWindow.for_policy(300, 60, 5))
    assert store.attributions() == {5: {"code": "insta01", "at": 100}}
