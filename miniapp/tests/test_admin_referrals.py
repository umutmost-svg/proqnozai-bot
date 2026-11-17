# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Referral links in the admin: create / archive (journalled without names), per-link funnel, user source."""
from __future__ import annotations

from pathlib import Path

import pytest

from miniapp.ads.telemetry import Telemetry
from miniapp.admin_web.bot_crm import BotCrmFile
from miniapp.admin_web.config import AdminWebConfig, ConfigError
from miniapp.admin_web.referrals import link_url
from miniapp.store import Store
from miniapp.telegram_auth import TelegramUser
from miniapp.tests.test_admin_crm import U1, U2
from miniapp.tests.test_admin_web import env, login, post  # noqa: F401 — env is a fixture


def _bot_start(config, user_id: int, code: str, at: int) -> None:
    f = BotCrmFile(config.bot_crm_db)
    f.init_schema()
    with f._connect() as conn:
        conn.execute("INSERT INTO referral_starts (user_id, code, at) VALUES (?, ?, ?)", (user_id, code, at))


def test_link_urls():
    assert link_url("bot", "abc", "sporbot", None) == "https://t.me/sporbot?start=ref_abc"
    assert link_url("miniapp", "abc", "sporbot", "app") == "https://t.me/sporbot/app?startapp=ref_abc"
    assert link_url("miniapp", "abc", "sporbot", None) == "https://t.me/sporbot?startapp=ref_abc"  # main Mini App
    assert link_url("bot", "abc", None, None) is None


def test_config_validates_public_names(tmp_path):
    with pytest.raises(ConfigError):
        AdminWebConfig.from_env({"ADMIN_PILOT_DIR": str(tmp_path / "p"), "ADMIN_BOT_USERNAME": "bad name"})
    cfg = AdminWebConfig.from_env({"ADMIN_PILOT_DIR": str(tmp_path / "p"), "ADMIN_BOT_USERNAME": "@SporBot"})
    assert cfg.bot_username == "SporBot" and cfg.miniapp_short_name is None


def test_create_archive_and_funnel(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, "/api/admin/referrals", csrf, {"name": "x", "channel": "sms"}).json()["error"] == "invalid_channel"
    assert post(client, "/api/admin/referrals", csrf, {"name": "x", "channel": "bot", "code": "Bad!"}).status_code == 400
    link = post(client, "/api/admin/referrals", csrf, {"name": "Инстаграм ноябрь", "channel": "bot", "code": "insta01"}).json()
    assert post(client, "/api/admin/referrals", csrf, {"name": "dup", "channel": "bot", "code": "insta01"}).status_code == 409
    auto = post(client, "/api/admin/referrals", csrf, {"name": "Авто", "channel": "miniapp"}).json()
    assert len(auto["code"]) == 8
    now = int(clock())
    # Bot starts BEFORE the users' Mini App accounts existed (a start after the account would not be an acquisition).
    _bot_start(config, U1, "insta01", 1)               # bot start, later also a Mini App user (demo set)
    _bot_start(config, 55, "insta01", 1)               # bot-only user: a start, nothing else
    _bot_start(config, U2, "ghost99", 1)               # a code with no link: shown as unknown
    Store(config.miniapp_db).upsert_user(TelegramUser(66, "N", None, None, "ru"), now - 50, auto["code"])
    t = Telemetry(config.telemetry_db)
    t.init_schema()
    t.record_activity(U1, now, "quick_single_ok")
    page = client.get("/api/admin/referrals").json()
    insta = next(i for i in page["items"] if i["id"] == link["id"])
    assert insta["stats"] == {"starts": 2, "botStarts": 2, "miniappStarts": 0, "inMiniApp": 1, "active7d": 1, "converted": 1}
    assert insta["param"] == "ref_insta01" and insta["url"] is None  # no bot username configured
    mini = next(i for i in page["items"] if i["id"] == auto["id"])
    assert mini["stats"]["miniappStarts"] == 1 and mini["stats"]["inMiniApp"] == 1
    assert page["unknownCodes"][0]["code"] == "ghost99" and page["funnel"]["starts"] == 4
    assert post(client, f"/api/admin/referrals/{link['id']}/archive", csrf, {"archived": True}).json() == {"archived": True}
    assert next(i for i in client.get("/api/admin/referrals").json()["items"] if i["id"] == link["id"])["archivedAt"]
    dump = " ".join(str(e) for e in accounts.events())
    assert "Инстаграм" not in dump and "referral.create" in dump and "referral.archive" in dump
    # The user's source in the list and the card, and the drill-down by code.
    row = client.get(f"/api/admin/users?q={U1}").json()["items"][0]
    assert row["source"]["code"] == "insta01" and row["source"]["channel"] == "bot" and row["source"]["name"] == "Инстаграм ноябрь"
    assert [u["id"] for u in client.get("/api/admin/users?ref=insta01").json()["items"]] == [U1]
    assert client.get(f"/api/admin/users/{U2}").json()["source"]["linkId"] is None


def test_the_earlier_start_wins_across_bot_and_miniapp(env):
    config, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "admin@x.test")
    now = int(clock())
    Store(config.miniapp_db).upsert_user(TelegramUser(77, "M", None, None, "ru"), now - 10, "later01")
    _bot_start(config, 77, "first01", now - 500)
    assert client.get("/api/admin/users/77").json()["source"]["code"] == "first01"


def test_analyst_reads_stats_but_cannot_create_and_sees_no_personal_data(env):
    config, clock, client, _, secrets_ = env
    owner = login(client, clock, secrets_, "owner@x.test")
    post(client, "/api/admin/referrals", owner, {"name": "x", "channel": "bot", "code": "abc123"})
    _bot_start(config, U1, "abc123", int(clock()))
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "analyst@x.test")
    r = client.get("/api/admin/referrals")
    assert r.status_code == 200 and "@x.test" not in r.text and str(U1) not in r.text
    assert post(client, "/api/admin/referrals", csrf, {"name": "x", "channel": "bot"}).status_code == 403


def test_an_older_miniapp_database_without_the_table_keeps_the_admin_working(env):
    import sqlite3
    config, clock, client, _, secrets_ = env
    with sqlite3.connect(config.miniapp_db) as conn:
        conn.execute("DROP TABLE IF EXISTS user_attribution")
    login(client, clock, secrets_, "admin@x.test")
    assert client.get("/api/admin/users").status_code == 200 and client.get(f"/api/admin/users/{U1}").status_code == 200
    assert client.get("/api/admin/referrals").json()["funnel"]["converted"] is None  # no telemetry: unknown, not 0


def test_admin_schema_equals_the_bot_side():
    from miniapp.admin_web import bot_crm as admin_side
    text = (Path(__file__).resolve().parents[2] / "bot_crm.py").read_text()  # the bot package is not importable here
    assert f'SCHEMA = """{admin_side.SCHEMA}"""' in text


def test_links_carry_platform_geo_pid_and_are_summarised(env):
    """Labels of a link (owner 16.11): validated, returned with the link, summed per value ("—" = not set)."""
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    base = {"name": "x", "channel": "miniapp"}
    for bad, err in (({"platform": "myspace"}, "invalid_platform"), ({"geo": "az"}, "invalid_geo"),
                     ({"geo": "AZE"}, "invalid_geo"), ({"pid": "a b"}, "invalid_pid"), ({"pid": "x" * 41}, "invalid_pid")):
        assert post(client, "/api/admin/referrals", csrf, {**base, **bad}).json()["error"] == err
    post(client, "/api/admin/referrals", csrf, {**base, "code": "tt1", "platform": "tiktok", "geo": "AZ", "pid": "p-1"})
    post(client, "/api/admin/referrals", csrf, {**base, "code": "tt2", "platform": "tiktok", "geo": "TR"})
    post(client, "/api/admin/referrals", csrf, {**base, "code": "plain"})
    now = int(clock())
    store = Store(config.miniapp_db)
    for uid, code in ((701, "tt1"), (702, "tt1"), (703, "tt2"), (704, "plain")):
        store.upsert_user(TelegramUser(uid, "N", None, None, "ru"), now - 50, code)
    page = client.get("/api/admin/referrals").json()
    tt1 = next(i for i in page["items"] if i["code"] == "tt1")
    assert (tt1["platform"], tt1["geo"], tt1["pid"]) == ("tiktok", "AZ", "p-1")
    by_platform = {g["value"]: (g["links"], g["starts"]) for g in page["breakdown"]["platform"]}
    assert by_platform == {"tiktok": (2, 3), "—": (1, 1)}
    assert {g["value"]: g["starts"] for g in page["breakdown"]["geo"]} == {"AZ": 2, "TR": 1, "—": 1}
    assert {g["value"]: g["starts"] for g in page["breakdown"]["pid"]} == {"p-1": 2, "—": 2}


def test_a_bot_start_of_an_existing_mini_app_user_is_not_an_acquisition(env):
    """CRM audit B3: a link posted to the existing audience counted Mini App users who then started the bot as new."""
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    link = post(client, "/api/admin/referrals", csrf, {"name": "Пост", "channel": "bot", "code": "post01"}).json()
    now = int(clock())
    Store(config.miniapp_db).upsert_user(TelegramUser(81, "A", None, None, "ru"), now - 500)  # in the Mini App first
    _bot_start(config, 81, "post01", now - 100)
    _bot_start(config, 82, "post01", now - 100)                                                 # new through the link
    Store(config.miniapp_db).upsert_user(TelegramUser(82, "B", None, None, "ru"), now - 50)
    stats = next(i for i in client.get("/api/admin/referrals").json()["items"] if i["id"] == link["id"])["stats"]
    assert (stats["starts"], stats["botStarts"], stats["inMiniApp"]) == (1, 1, 1)
    assert client.get("/api/admin/users/81").json()["source"] is None
