# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Inquiries: grant-only access, journalled views, merged history, status, owner-only retention with purge."""
from __future__ import annotations

from miniapp.admin_web.bot_crm import DAY, BotCrmFile
from miniapp.tests.test_admin_crm import U1, U2
from miniapp.tests.test_admin_grants import grant, ids
from miniapp.tests.test_admin_web import code, env, login, post  # noqa: F401 — env is a fixture


def _inquiry(config, user_id: int, text: str, at: int) -> None:
    f = BotCrmFile(config.bot_crm_db)
    f.init_schema()
    with f._connect() as conn:
        conn.execute("INSERT OR IGNORE INTO inquiry_notices (user_id, at, days) VALUES (?, ?, 365)", (user_id, at))
        conn.execute("INSERT INTO inquiries (user_id, at, text) VALUES (?, ?, ?)", (user_id, at, text))


def test_nothing_collected_and_access_needs_the_grant(env):
    config, clock, client, accounts, secrets_ = env
    owner = login(client, clock, secrets_, "owner@x.test")
    assert client.get("/api/admin/inquiries").json()["error"] == "dialogs_not_allowed"  # the owner too
    r = grant(client, owner, secrets_, clock, ids(accounts)["owner@x.test"], True, "grant-own-1")
    assert r.status_code == 200, r.text
    page = client.get("/api/admin/inquiries").json()
    assert page == {"collected": False, "retentionDays": 90, "items": []}
    assert not config.bot_crm_db.exists()  # reads never create the bot's file


def test_threads_history_status_and_view_journal(env):
    config, clock, client, accounts, secrets_ = env
    now = int(clock())
    _inquiry(config, U1, "Не приходит прогноз", now - 100)
    _inquiry(config, U1, "Уже второй день", now - 50)
    _inquiry(config, U2, "Как поменять язык?", now - 10)
    owner = login(client, clock, secrets_, "owner@x.test")
    grant(client, owner, secrets_, clock, ids(accounts)["admin@x.test"], True, "grant-adm-1")
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "admin@x.test")
    page = client.get("/api/admin/inquiries").json()
    assert [i["userId"] for i in page["items"]] == [U2, U1] and page["items"][1]["count"] == 2
    assert page["collected"] is True and "Уже второй день" not in str(page)  # the list never carries texts
    post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "Проверим!", "idempotencyKey": "reply-0001"})
    thread = client.get(f"/api/admin/inquiries/{U1}").json()
    assert [(h["from"], h["text"]) for h in thread["history"]] == [
        ("user", "Не приходит прогноз"), ("user", "Уже второй день"), ("admin", "Проверим!")]
    assert thread["noticeAt"] == now - 100 and thread["history"][-1]["status"] == "queued"
    views = accounts.events(action="inquiry.view")
    assert views and views[0]["targetId"] == str(U1) and "прогноз" not in str(views[0])
    assert post(client, f"/api/admin/inquiries/{U1}/status", csrf, {"status": "closed"}).json() == {"status": "closed"}
    assert client.get("/api/admin/inquiries?status=closed").json()["items"][0]["userId"] == U1
    assert client.get(f"/api/admin/inquiries/{U1}").json()["status"] == "closed"
    _inquiry(config, U1, "Ещё вопрос", int(clock()))  # same second as the close: still reopens (by message id)
    assert {i["userId"]: i["status"] for i in client.get("/api/admin/inquiries").json()["items"]}[U1] == "open"
    assert client.get(f"/api/admin/inquiries/{U1}").json()["status"] == "open"
    assert post(client, f"/api/admin/inquiries/{U1}/status", csrf, {"status": "x"}).status_code == 400


def test_settings_need_the_grant_too(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "admin@x.test")
    assert client.get("/api/admin/inquiries-settings").json()["error"] == "dialogs_not_allowed"


def test_analyst_never_reads_inquiries(env):
    config, clock, client, _, secrets_ = env
    _inquiry(config, U1, "секрет", int(clock()))
    login(client, clock, secrets_, "analyst@x.test")
    assert client.get("/api/admin/inquiries").status_code == 403
    assert client.get(f"/api/admin/inquiries/{U1}").status_code == 403


def test_retention_is_owner_only_with_step_up_and_purges(env):
    config, clock, client, accounts, secrets_ = env
    now = int(clock())
    _inquiry(config, U1, "старое", now - 40 * DAY)
    _inquiry(config, U1, "новое", now - DAY)
    admin = login(client, clock, secrets_, "admin@x.test")
    body = {"retentionDays": 30, "reason": "политика", "idempotencyKey": "ret-0001"}
    assert post(client, "/api/admin/inquiries-settings", admin, {**body, "code": code(secrets_["admin@x.test"], clock)}).status_code == 403
    client.cookies.clear()
    owner = login(client, clock, secrets_, "owner@x.test")
    clock.t += 30
    assert post(client, "/api/admin/inquiries-settings", owner, {**body, "retentionDays": 0,
                                                                 "code": code(secrets_["owner@x.test"], clock)}).status_code == 400
    r = post(client, "/api/admin/inquiries-settings", owner, {**body, "code": code(secrets_["owner@x.test"], clock)})
    assert r.status_code == 200 and r.json()["removed"] == 1
    grant(client, owner, secrets_, clock, ids(accounts)["owner@x.test"], True, "grant-own-2")
    settings = client.get("/api/admin/inquiries-settings").json()
    assert (settings["retentionDays"], settings["applied"]) == (30, True)
    assert [m["text"] for m in BotCrmFile(config.bot_crm_db).messages(U1)] == ["новое"]
    ev = accounts.events(action="inquiry.retention")[0]
    assert ev["before"] == {"retentionDays": 90} and ev["after"] == {"retentionDays": 30}


def test_a_failed_file_write_is_reported_and_applied_on_retry(env, monkeypatch):
    config, clock, client, accounts, secrets_ = env
    now = int(clock())
    _inquiry(config, U1, "старое", now - 40 * DAY)
    owner = login(client, clock, secrets_, "owner@x.test")
    clock.t += 30
    body = {"retentionDays": 30, "reason": "политика", "idempotencyKey": "ret-fail-01", "code": code(secrets_["owner@x.test"], clock)}

    def broken(self, days, now):
        raise OSError("read-only volume")

    monkeypatch.setattr(BotCrmFile, "apply_retention", broken)
    r = post(client, "/api/admin/inquiries-settings", owner, body)
    assert r.status_code == 503 and r.json()["error"] == "retention_not_applied"
    assert accounts.events(action="inquiry.retention")  # recorded in admin.db: the source of truth
    assert BotCrmFile(config.bot_crm_db).retention_days() == 90  # the file is unchanged for now
    grant(client, owner, secrets_, clock, ids(accounts)["owner@x.test"], True, "grant-own-3")
    # Reads refuse while the file holds texts past the owner's setting.
    assert client.get("/api/admin/inquiries").json()["error"] == "retention_not_applied"
    assert client.get(f"/api/admin/inquiries/{U1}").status_code == 503
    assert client.get("/api/admin/inquiries-settings").status_code == 503
    monkeypatch.undo()
    assert client.get(f"/api/admin/inquiries/{U1}").json()["history"] == []  # applied on the read: purged
    again = post(client, "/api/admin/inquiries-settings", owner, body)  # same request: replayed, nothing left to do
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert BotCrmFile(config.bot_crm_db).retention_days() == 30


def test_long_histories_say_they_are_truncated(env):
    config, clock, client, accounts, secrets_ = env
    now = int(clock())
    for i in range(201):
        _inquiry(config, U2, f"m{i}", now - 1000 + i)
    owner = login(client, clock, secrets_, "owner@x.test")
    grant(client, owner, secrets_, clock, ids(accounts)["owner@x.test"], True, "grant-own-4")
    thread = client.get(f"/api/admin/inquiries/{U2}").json()
    assert len(thread["history"]) == 200 and thread["truncated"] == {"inbound": True, "outbound": False}
    assert thread["history"][-1]["text"] == "m200"


def test_outbound_truncation_is_exact_at_the_boundary(env):
    _, clock, client, accounts, secrets_ = env
    owner = login(client, clock, secrets_, "owner@x.test")
    grant(client, owner, secrets_, clock, ids(accounts)["owner@x.test"], True, "grant-own-5")
    for i in range(50):
        post(client, f"/api/admin/users/{U1}/messages", owner, {"body": f"r{i}", "idempotencyKey": f"reply-key-{i:03d}"})
    assert client.get(f"/api/admin/inquiries/{U1}").json()["truncated"]["outbound"] is False
    post(client, f"/api/admin/users/{U1}/messages", owner, {"body": "r50", "idempotencyKey": "reply-key-050"})
    assert client.get(f"/api/admin/inquiries/{U1}").json()["truncated"]["outbound"] is True


def test_admin_side_creates_and_tightens_the_file(tmp_path, monkeypatch):
    import os

    import pytest

    from miniapp.admin_web import bot_crm as admin_side
    old = os.umask(0o022)
    try:
        BotCrmFile(tmp_path / "new.db").init_schema()
        loose = tmp_path / "loose.db"
        loose.touch()
        os.chmod(loose, 0o666)
        BotCrmFile(loose).init_schema()
    finally:
        os.umask(old)
    assert os.stat(tmp_path / "new.db").st_mode & 0o777 == 0o660 and os.stat(loose).st_mode & 0o777 == 0o660
    theirs = tmp_path / "theirs.db"
    theirs.touch()
    os.chmod(theirs, 0o644)

    def not_owner(*_a, **_k):
        raise PermissionError("not the owner")

    monkeypatch.setattr(admin_side.os, "chmod", not_owner)
    with pytest.raises(PermissionError):
        BotCrmFile(theirs).init_schema()


def test_admin_purge_tightens_a_loose_file_first(tmp_path):
    import os
    f = BotCrmFile(tmp_path / "bot-crm.db")
    f.init_schema()
    os.chmod(f.path, 0o666)
    f.purge(10**9)
    assert os.stat(f.path).st_mode & 0o777 == 0o660


def test_an_unreadable_history_is_an_error_not_an_empty_thread(env, monkeypatch):
    import sqlite3
    config, clock, client, accounts, secrets_ = env
    _inquiry(config, U1, "текст", int(clock()))
    owner = login(client, clock, secrets_, "owner@x.test")
    grant(client, owner, secrets_, clock, ids(accounts)["owner@x.test"], True, "grant-own-6")

    def broken(self, *a, **k):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(BotCrmFile, "messages", broken)
    before = len(accounts.events(action="inquiry.view"))
    assert client.get(f"/api/admin/inquiries/{U1}").json()["error"] == "inquiries_unavailable"
    assert len(accounts.events(action="inquiry.view")) == before  # nothing was shown: no view recorded


def test_a_directory_at_the_file_path_is_an_error(env):
    config, clock, client, accounts, secrets_ = env
    config.bot_crm_db.mkdir()
    owner = login(client, clock, secrets_, "owner@x.test")
    grant(client, owner, secrets_, clock, ids(accounts)["owner@x.test"], True, "grant-own-7")
    assert client.get("/api/admin/inquiries").status_code == 503
