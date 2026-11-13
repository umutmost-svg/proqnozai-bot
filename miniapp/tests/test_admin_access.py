# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Block/unblock from the admin panel = Mini App access only (signed access-policy.json); no data deletion."""
from __future__ import annotations

import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from miniapp.ads import policy
from miniapp.admin_web.ads import load_signing_key
from miniapp.app import create_app
from miniapp.security import AccessPolicy, AccessPolicyError, parse_access_policy
from miniapp.settings import Settings
from miniapp.tests.conftest import ORIGIN, FakeClock, login as mini_login
from miniapp.tests.signing import TEST_BOT_TOKEN, make_init_data
from miniapp.tests.test_admin_crm import U1, U2, dangerous
from miniapp.tests.test_admin_web import env, login, post  # noqa: F401 — env is a fixture


def key_of(config):
    return load_signing_key(config.admin_db.parent / "ads-signing.key")


def snapshot(config) -> tuple[int, frozenset[int]]:
    return parse_access_policy(config.access_policy_file.read_bytes(), key_of(config).public_key())


def block(client, csrf, secrets_, clock, user_id, flag, key):
    clock.t += 30
    return post(client, f"/api/admin/users/{user_id}/block", csrf,
                {**dangerous(secrets_, clock), "idempotencyKey": key, "blocked": flag})


def test_block_and_unblock_publish_a_signed_list(env):
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert snapshot(config)[1] == frozenset()  # published at startup (empty)
    r = block(client, csrf, secrets_, clock, U1, True, "block-key-1").json()
    assert r["access"]["fileWritten"] is True
    v1, blocked = snapshot(config)
    assert blocked == {U1}
    block(client, csrf, secrets_, clock, U2, True, "block-key-2")
    block(client, csrf, secrets_, clock, U1, False, "block-key-3")
    v3, blocked = snapshot(config)
    assert blocked == {U2} and v3 > v1
    # no data deletion: the user and the CRM data are still there
    assert client.get(f"/api/admin/users/{U1}").status_code == 200


def test_lost_or_forged_file_is_repaired(env):
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    block(client, csrf, secrets_, clock, U1, True, "block-key-1")
    original = config.access_policy_file.read_bytes()
    config.access_policy_file.unlink()
    assert client.get("/api/admin/access/publication").json()["fileWritten"] is True
    assert config.access_policy_file.read_bytes() == original
    forged = json.loads(original)
    forged["blocked"] = []
    config.access_policy_file.write_bytes(policy.canonical(policy.sign(forged, Ed25519PrivateKey.generate())))
    client.get("/api/admin/access/publication")
    assert snapshot(config)[1] == {U1}


def test_reader_never_unblocks_by_deletion_replay_or_forgery(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "access-policy.json"
    clock = FakeClock()

    def write(version, blocked, signer=key):
        body = {"schema": 1, "kind": "access", "version": version, "generatedAt": 1, "blocked": blocked}
        policy.write_atomic(path, policy.canonical(policy.sign(body, signer)))

    reader = AccessPolicy(path, policy.public_key_hex(key), clock)
    assert reader.blocked() == frozenset()
    write(10, [5])
    clock.advance(31)
    assert reader.blocked() == {5}
    path.unlink()
    clock.advance(31)
    assert reader.blocked() == {5}
    write(9, [])                      # replay of an older file
    clock.advance(31)
    assert reader.blocked() == {5}
    write(11, [], Ed25519PrivateKey.generate())
    clock.advance(31)
    assert reader.blocked() == {5}
    write(12, [])
    clock.advance(31)
    assert reader.blocked() == frozenset()
    with pytest.raises(AccessPolicyError):
        parse_access_policy(b'{"schema": 1, "kind": "ads"}', key.public_key())


def test_mini_app_refuses_a_blocked_user_and_keeps_the_data(tmp_path):
    key = Ed25519PrivateKey.generate()
    clock = FakeClock()
    settings = Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "miniapp.db", trusted_origins=frozenset({ORIGIN}),
                        environment="development", access_policy_public_key=policy.public_key_hex(key))
    app = create_app(settings, clock=clock)
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    user = {"id": 4242, "first_name": "U"}
    assert mini_login(c, make_init_data(user, auth_date=int(clock.now))).status_code == 200
    body = {"schema": 1, "kind": "access", "version": 7, "generatedAt": 1, "blocked": [4242]}
    policy.write_atomic(tmp_path / "access-policy.json", policy.canonical(policy.sign(body, key)))
    clock.advance(31)
    assert c.get("/api/me").status_code == 401
    other = TestClient(app, base_url="http://127.0.0.1:8000")
    assert mini_login(other, make_init_data(user, auth_date=int(clock.now))).status_code == 403
    with sqlite3.connect(tmp_path / "miniapp.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE telegram_user_id = 4242").fetchone()[0] == 1
    body.update(version=8, blocked=[])
    policy.write_atomic(tmp_path / "access-policy.json", policy.canonical(policy.sign(body, key)))
    clock.advance(31)
    assert mini_login(other, make_init_data(user, auth_date=int(clock.now))).status_code == 200


def test_reader_refuses_a_replayed_older_file_after_a_restart(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "access-policy.json"
    clock = FakeClock()

    def write(version, blocked):
        body = {"schema": 1, "kind": "access", "version": version, "generatedAt": 1, "blocked": blocked}
        policy.write_atomic(path, policy.canonical(policy.sign(body, key)))

    write(20, [5])
    assert AccessPolicy(path, policy.public_key_hex(key), clock).blocked() == {5}
    write(19, [])
    assert AccessPolicy(path, policy.public_key_hex(key), clock).blocked() == frozenset()  # nothing applied yet
    assert AccessPolicy(path, policy.public_key_hex(key), clock)._version == 20  # the mark keeps the floor
    write(20, [5])
    restarted = AccessPolicy(path, policy.public_key_hex(key), clock)
    assert restarted.blocked() == {5}
