"""Admin panel SERVER mode (docs/ops/admin-server.md): the edge secret on every request, one public host over
HTTPS, `__Host-`/`__Secure-` Secure cookies, the Mini App's own data directory, no demo data, no 2FA-code CLI."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miniapp.admin_web import __main__ as cli
from miniapp.admin_web import totp
from miniapp.admin_web.accounts import AdminStore
from miniapp.admin_web.app import create_admin_app
from miniapp.admin_web.config import EDGE_SECRET_HEADER, AdminWebConfig, ConfigError
from miniapp.store import Store

HOST = "admin.example.com"
ORIGIN = f"https://{HOST}"
SECRET = "e" * 40


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def server_env(data: Path, **over) -> dict[str, str]:
    return {"ADMIN_WEB_MODE": "server", "ADMIN_DATA_DIR": str(data), "ADMIN_STATE_DIR": str(data.parent / "state"),
            "ADMIN_PUBLIC_HOST": HOST,
            "ADMIN_EDGE_SECRET": SECRET, **over}


@pytest.fixture()
def server(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    Store(data / "miniapp.db").init_schema()             # the Mini App's own database
    config = AdminWebConfig.from_env(server_env(data))
    clock = Clock()
    app = create_admin_app(config, clock=clock)
    accounts = AdminStore(config.admin_db)
    _, secret = accounts.create_admin("owner@x.test", "owner", "a-long-owner-password", int(clock()))
    client = TestClient(app, base_url=ORIGIN, headers={EDGE_SECRET_HEADER: SECRET})
    return config, clock, client, secret


def sign_in(client: TestClient, clock: Clock, secret: str):
    h = {"Origin": ORIGIN}
    r = client.post("/api/admin/auth/login", json={"email": "owner@x.test", "password": "a-long-owner-password"}, headers=h)
    assert r.status_code == 200, r.text
    pre_cookie = r.headers["set-cookie"]
    r = client.post("/api/admin/auth/totp", json={"code": totp.code_at(secret, int(clock() // 30))}, headers=h)
    assert r.status_code == 200, r.text
    session = next(c for c in r.headers.get_list("set-cookie") if "_session=" in c)
    return pre_cookie, session


def test_every_request_needs_the_edge_secret(server) -> None:
    _, _, client, _ = server
    for path in ("/", "/api/admin/health", "/api/admin/auth/me", "/assets/x.js"):
        assert client.get(path, headers={EDGE_SECRET_HEADER: ""}).status_code == 403
        assert client.get(path, headers={EDGE_SECRET_HEADER: SECRET[:-1] + "x"}).status_code == 403
    bare = TestClient(client.app, base_url=ORIGIN)       # no header at all: the server's public :80 with a forged Host
    assert bare.get("/api/admin/health").status_code == 403
    assert bare.post("/api/admin/auth/login", json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/api/admin/health").status_code == 200


def test_only_the_public_host_and_its_https_origin(server) -> None:
    _, _, client, _ = server
    assert client.get("/api/admin/health", headers={"Host": "127.0.0.1:8795"}).status_code == 421
    assert client.post("/api/admin/auth/login", json={"email": "a", "password": "b"},
                       headers={"Origin": f"http://{HOST}"}).status_code == 403
    assert client.get("/api/admin/health").headers["strict-transport-security"].startswith("max-age=")


def test_sign_in_needs_both_factors_and_sets_secure_prefixed_cookies(server) -> None:
    _, clock, client, secret = server
    pre, session = sign_in(client, clock, secret)
    assert pre.startswith("__Secure-sporbot_admin_pre=") and "Secure" in pre and "HttpOnly" in pre
    assert session.startswith("__Host-sporbot_admin_session=") and "Secure" in session and "Path=/" in session
    assert "Domain" not in session
    me = client.get("/api/admin/auth/me").json()
    assert me["admin"]["role"] == "owner" and me["pilot"] == {"dataset": "production", "demoTelemetry": False}


def test_no_demo_data_on_the_server_whatever_is_asked(server) -> None:
    _, clock, client, secret = server
    sign_in(client, clock, secret)
    body = client.get("/api/admin/overview?demo=1").json()
    assert body["telemetry"] == {"source": "not_collected"}


def test_server_config_is_strict(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(ConfigError):
        AdminWebConfig.from_env({"ADMIN_WEB_MODE": "server", "ADMIN_PUBLIC_HOST": HOST, "ADMIN_EDGE_SECRET": SECRET})
    for over in ({"ADMIN_EDGE_SECRET": ""}, {"ADMIN_EDGE_SECRET": "short"}, {"ADMIN_EDGE_SECRET": "x" * 31 + " y"},
                 {"ADMIN_PUBLIC_HOST": ""}, {"ADMIN_PUBLIC_HOST": "Admin.Example.com"},
                 {"ADMIN_PUBLIC_HOST": "admin.example.com:8080"}, {"ADMIN_WEB_MODE": "cloud"}):
        with pytest.raises(ConfigError):
            AdminWebConfig.from_env(server_env(data, **over))
    config = AdminWebConfig.from_env(server_env(data))
    assert config.bind == "0.0.0.0" and "secret" not in repr(config).lower().replace("edge_secret", "")
    with pytest.raises(ConfigError):                      # no Mini App database: not its data directory
        config.require_data()
    Store(data / "miniapp.db").init_schema()
    config.require_data()
    assert (config.admin_dir.stat().st_mode & 0o777) == 0o700
    inside = AdminWebConfig.from_env(server_env(data, ADMIN_STATE_DIR=str(data / "admin")))
    with pytest.raises(ConfigError):                      # the admin's secrets never on the Mini App's volume
        inside.require_data()
    (data / "pilot.json").write_text('{"dataset": "demo"}')
    with pytest.raises(ConfigError):                      # never a pilot directory
        config.require_data()


def test_the_pilot_cannot_listen_beyond_loopback(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        AdminWebConfig(data_dir=tmp_path / "p", port=8795, static_dir=None, bind="0.0.0.0").validate()


def test_cli_keeps_pilot_and_server_commands_apart(tmp_path: Path, monkeypatch, capsys) -> None:
    data = tmp_path / "data"
    data.mkdir()
    Store(data / "miniapp.db").init_schema()
    monkeypatch.setattr(os, "environ", server_env(data))
    assert cli.main(["code", "owner@x.test"]) == 2         # a printed code would bypass the 2FA
    assert cli.main(["init"]) == 2
    assert cli.main(["create-owner", "owner@x.test"]) == 2  # not a terminal (pytest): refused, nothing created
    assert "interactive terminal" in capsys.readouterr().err
    assert not (tmp_path / "state" / "admin.db").exists()
    monkeypatch.setattr(os, "environ", {"ADMIN_PILOT_DIR": str(tmp_path / "pilot")})
    assert cli.main(["create-owner", "owner@x.test"]) == 2


def test_create_owner_once_on_a_terminal(tmp_path: Path, monkeypatch, capsys) -> None:
    data = tmp_path / "data"
    data.mkdir()
    Store(data / "miniapp.db").init_schema()
    monkeypatch.setattr(os, "environ", server_env(data))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    answers = iter(["short", "short"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["create-owner", "Owner@X.test"]) == 2   # too short
    answers = iter(["a-long-owner-password", "a-long-owner-password"])
    assert cli.main(["create-owner", "Owner@X.test"]) == 0
    out = capsys.readouterr().out
    assert "2FA secret:" in out and "otpauth://" in out and "a-long-owner-password" not in out
    admins = AdminStore(tmp_path / "state" / "admin.db").admins()
    assert not (data / "admin").exists()                      # nothing of the admin's on the Mini App's volume
    assert [(a["email"], a["role"]) for a in admins] == [("owner@x.test", "owner")]
    answers = iter(["a-long-owner-password", "a-long-owner-password"])
    assert cli.main(["create-owner", "second@x.test"]) == 2  # an owner exists: more admins come from the panel


def test_health_needs_the_mini_app_database(server) -> None:
    config, _, client, _ = server
    assert client.get("/api/admin/health").status_code == 200
    config.miniapp_db.unlink()
    config.miniapp_db.mkdir()                              # unreadable as a database
    assert client.get("/api/admin/health").status_code == 503
