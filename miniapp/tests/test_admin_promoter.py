# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Promotion managers (owner 16.11): a role that sees analytics aggregates and only the referral links it created;
invited by a one-time link on which the invitee sets their own password and 2FA."""
from __future__ import annotations

import sqlite3

from miniapp.admin_web import totp
from miniapp.admin_web.accounts import AdminStore
from miniapp.store import Store
from miniapp.telegram_auth import TelegramUser
from miniapp.tests.test_admin_web import ORIGIN, code, env, login, post  # noqa: F401 — env is a fixture


def _promoter(env) -> str:
    config, clock, client, accounts, secrets_ = env
    _, secret = accounts.create_admin("promoter@x.test", "promoter", "pw-promoter", int(clock()))
    secrets_["promoter@x.test"] = secret
    return login(client, clock, secrets_, "promoter@x.test")


def _invite(client, clock, secrets_, csrf: str, email: str, key: str) -> dict:
    return post(client, "/api/admin/admins", csrf, {"email": email, "role": "promoter", "reason": "продвижение",
                                                    "code": code(secrets_["owner@x.test"], clock),
                                                    "idempotencyKey": key}).json()


def test_invitation_link_sets_password_and_2fa(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    token = _invite(client, clock, secrets_, csrf, "m1@x.test", "invite-m1-0001")["inviteToken"]
    assert token and token not in " ".join(str(e) for e in accounts.events())  # never journalled
    hdr = {"Origin": ORIGIN}  # opened in the owner's own signed-in browser: works, and ends that session
    assert client.post("/api/admin/auth/invite", json={"token": "nope"}, headers=hdr).status_code == 404
    info = client.post("/api/admin/auth/invite", json={"token": token}, headers=hdr).json()
    assert info["email"] == "m1@x.test" and info["role"] == "promoter" and info["otpauth"].startswith("otpauth://")
    assert client.post("/api/admin/auth/invite", json={"token": token}, headers=hdr).json()["secret"] == info["secret"]
    good = totp.code_at(info["secret"], int(clock() // 30))
    accept = "/api/admin/auth/invite/accept"
    assert client.post(accept, json={"token": token, "password": "short", "code": good},
                       headers=hdr).json()["error"] == "weak_password"
    assert client.post(accept, json={"token": token, "password": "long-enough-pw", "code": "000000"},
                       headers=hdr).json()["error"] == "invalid_code"
    assert client.post(accept, json={"token": token, "password": "long-enough-pw", "code": good},
                       headers=hdr).json() == {"ok": True}
    assert client.get("/api/admin/auth/me").status_code == 401  # the owner's session in this browser ended
    assert client.post(accept, json={"token": token, "password": "long-enough-pw", "code": good},
                       headers=hdr).status_code == 404  # spent
    clock.t += 30
    assert client.post("/api/admin/auth/login", json={"email": "m1@x.test", "password": "long-enough-pw"},
                       headers=hdr).status_code == 200
    r = client.post("/api/admin/auth/totp", json={"code": totp.code_at(info["secret"], int(clock() // 30))}, headers=hdr)
    assert r.status_code == 200
    assert client.get("/api/admin/auth/me").json()["admin"]["role"] == "promoter"


def test_wrong_codes_burn_the_link_and_a_new_link_revokes_the_old(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    first = _invite(client, clock, secrets_, csrf, "m2@x.test", "invite-m2-0001")
    target = next(a["id"] for a in client.get("/api/admin/admins").json()["items"] if a["email"] == "m2@x.test")
    clock.t += 30
    second = post(client, f"/api/admin/admins/{target}/invite", csrf, {
        "reason": "новая ссылка", "code": code(secrets_["owner@x.test"], clock), "idempotencyKey": "relink-m2-01"}).json()
    hdr = {"Origin": ORIGIN}
    assert client.post("/api/admin/auth/invite", json={"token": first["inviteToken"]}, headers=hdr).status_code == 404
    token = second["inviteToken"]
    client.post("/api/admin/auth/invite", json={"token": token}, headers=hdr)
    for _ in range(5):
        client.post("/api/admin/auth/invite/accept", json={"token": token, "password": "long-enough-pw",
                                                           "code": "000000"}, headers=hdr)
    assert client.post("/api/admin/auth/invite", json={"token": token}, headers=hdr).status_code == 404
    # An active account gets no link.
    clock.t += 30
    analyst = next(a["id"] for a in client.get("/api/admin/admins").json()["items"] if a["role"] == "analyst")
    assert post(client, f"/api/admin/admins/{analyst}/invite", csrf, {
        "reason": "x" * 5, "code": code(secrets_["owner@x.test"], clock),
        "idempotencyKey": "relink-an-01"}).json()["error"] == "not_invited"


def test_promoter_reaches_only_analytics_and_own_links(env):
    config, clock, client, accounts, secrets_ = env
    admin_csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, "/api/admin/referrals", admin_csrf, {"name": "чужая", "channel": "miniapp", "code": "theirs1"})
    client.cookies.clear()
    csrf = _promoter(env)
    for path in ("/api/admin/users", "/api/admin/admins", "/api/admin/events", "/api/admin/outbox",
                 "/api/admin/broadcasts", "/api/admin/forecasts", "/api/admin/users/1"):
        assert client.get(path).status_code == 403, path
    for path in ("/api/admin/overview", "/api/admin/analytics", "/api/admin/value", "/api/admin/product"):
        assert client.get(path).status_code == 200, path
    value = client.get("/api/admin/value").json()
    assert all(not b.get("topUsers") for b in (value["bot"], value["miniapp"]))
    assert post(client, "/api/admin/settings/ai.enabled", csrf, {"value": False}).status_code == 403
    mine = post(client, "/api/admin/referrals", csrf, {"name": "моя", "channel": "miniapp", "code": "mine01",
                                                       "platform": "tiktok"}).json()
    now = int(clock())
    store = Store(config.miniapp_db)
    store.upsert_user(TelegramUser(801, "N", None, None, "ru"), now - 50, "mine01")
    store.upsert_user(TelegramUser(802, "N", None, None, "ru"), now - 50, "theirs1")
    store.upsert_user(TelegramUser(803, "N", None, None, "ru"), now - 50, "ghost01")
    page = client.get("/api/admin/referrals").json()
    assert [i["code"] for i in page["items"]] == ["mine01"] and page["items"][0]["by"] is None
    assert page["funnel"]["starts"] == 1 and page["unknownCodes"] == []
    assert {g["value"] for g in page["breakdown"]["platform"]} == {"tiktok"}
    assert client.get("/api/admin/product?days=30").json()["referrals"]["starts"] == 1
    theirs = next(link for link in accounts_links(config) if link[1] == "theirs1")[0]
    assert post(client, f"/api/admin/referrals/{theirs}/archive", csrf, {"archived": True}).status_code == 404
    assert post(client, f"/api/admin/referrals/{mine['id']}/archive", csrf, {"archived": True}).json() == {"archived": True}


def accounts_links(config) -> list[tuple[int, str]]:
    with sqlite3.connect(config.admin_db) as conn:
        return conn.execute("SELECT id, code FROM crm_referral_links").fetchall()


def test_old_database_gets_the_promoter_role_keeping_rows(tmp_path):
    path = tmp_path / "admin.db"
    store = AdminStore(path)
    store.init_schema()
    with sqlite3.connect(path) as conn:  # rebuild the pre-16.11 table: CHECK without 'promoter'
        conn.execute("PRAGMA foreign_keys=OFF")
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'admin_users'").fetchone()[0]
        conn.execute("ALTER TABLE admin_users RENAME TO admin_users_tmp")
        conn.execute(sql.replace(", 'promoter'", ""))
        conn.execute("DROP TABLE admin_users_tmp")
    admin, _ = store.create_admin("o@x.test", "owner", "pw-owner-long", 100)
    with sqlite3.connect(path) as conn:
        try:
            conn.execute("INSERT INTO admin_users (email, role, state, created_at) VALUES ('p@x', 'promoter', 'invited', 1)")
            raise AssertionError("the old CHECK should refuse the role")
        except sqlite3.IntegrityError:
            pass
    store.init_schema()
    store.init_schema()  # idempotent
    promoter, _ = store.create_admin("p@x.test", "promoter", None, 200)
    assert {a["email"]: a["role"] for a in store.admins()} == {"o@x.test": "owner", "p@x.test": "promoter"}
    assert promoter.id > admin.id
