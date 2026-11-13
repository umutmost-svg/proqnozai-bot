# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Dialogs only where explicitly allowed: a per-admin grant (default deny), owner-granted with 2FA, journalled."""
from __future__ import annotations

from miniapp.tests.test_admin_web import code, env, login, post  # noqa: F401 — env is a fixture


def grant(client, csrf, secrets_, clock, target, granted, key, email="owner@x.test", permission="view_dialogs"):
    clock.t += 30
    return post(client, f"/api/admin/admins/{target}/grants", csrf, {
        "permission": permission, "granted": granted, "reason": "доступ к диалогам", "idempotencyKey": key,
        "code": code(secrets_[email], clock)})


def ids(accounts):
    return {a["email"]: a["id"] for a in accounts.admins()}


def test_dialogs_are_denied_until_an_owner_grants_them(env):
    _, clock, client, accounts, secrets_ = env
    admin_id = ids(accounts)["admin@x.test"]
    csrf = login(client, clock, secrets_, "owner@x.test")
    assert client.get("/api/admin/dialogs?demo=1").status_code == 403  # the owner too: never implied by a role
    r = grant(client, csrf, secrets_, clock, admin_id, True, "grant-key-1")
    assert r.status_code == 200 and r.json()["after"] == {"permission": "view_dialogs", "granted": True}
    assert grant(client, csrf, secrets_, clock, admin_id, True, "grant-key-1").json()["replayed"] is True
    assert next(a for a in client.get("/api/admin/admins").json()["items"] if a["id"] == admin_id)["grants"] == [
        "view_dialogs"]
    client.cookies.clear()
    csrf_admin = login(client, clock, secrets_, "admin@x.test")
    assert client.get("/api/admin/auth/me").json()["admin"]["grants"] == ["view_dialogs"]
    assert client.get("/api/admin/dialogs?demo=1").json()["source"] == "demo"
    # an admin cannot grant (owner only)
    assert grant(client, csrf_admin, secrets_, clock, admin_id, False, "grant-key-2", email="admin@x.test").status_code == 403
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "owner@x.test")
    grant(client, csrf, secrets_, clock, admin_id, False, "grant-key-3")
    assert not accounts.has_grant(admin_id, "view_dialogs")
    events = accounts.events(action="admin.grant")
    assert [e["after"]["granted"] for e in events] == [False, True] and all(e["reason"] for e in events)


def test_analysts_cannot_hold_the_grant_and_a_demotion_ends_it(env):
    _, clock, client, accounts, secrets_ = env
    people = ids(accounts)
    csrf = login(client, clock, secrets_, "owner@x.test")
    r = grant(client, csrf, secrets_, clock, people["analyst@x.test"], True, "grant-key-1")
    assert r.status_code == 409 and r.json()["error"] == "role_cannot_hold_grant"
    assert grant(client, csrf, secrets_, clock, people["admin@x.test"], True, "grant-key-2").status_code == 200
    clock.t += 30
    post(client, f"/api/admin/admins/{people['admin@x.test']}", csrf, {
        "role": "analyst", "reason": "смена роли", "idempotencyKey": "role-key-1",
        "code": code(secrets_["owner@x.test"], clock)})
    assert not accounts.has_grant(people["admin@x.test"], "view_dialogs")
    assert grant(client, csrf, secrets_, clock, people["admin@x.test"], True, "grant-key-3",
                 permission="read_everything").json()["error"] == "invalid_grant"
