"""M2.5: session cap and revocation, emergency switch, edge secret, same-origin
static files with CSP, healthcheck, admin CLI. Offline: temp DB and dirs only."""
from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miniapp import admin
from miniapp.app import EDGE_SECRET_HEADER, create_app
from miniapp.settings import Settings, SettingsError
from miniapp.store import SESSION_RETENTION_S, BurnWindow, Store
from miniapp.tests.conftest import NOW, ORIGIN, FakeClock, fresh, login
from miniapp.tests.signing import TEST_BOT_TOKEN

COOKIE = "sporai_session"
BURN = BurnWindow.for_policy(NOW, 300, 30)
EDGE_SECRET = "e" * 40


def device(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8000")


def sign_in(app, clock: FakeClock, user_id: int = 501) -> tuple[TestClient, str]:
    c = device(app)
    clock.advance(1)  # every launch has its own initData
    r = login(c, fresh(clock, {"id": user_id}))
    assert r.status_code == 200, r.text
    return c, r.json()["session"]["csrf_token"]


def post(c: TestClient, path: str, csrf: str | None = None, body: dict | None = None, origin: str = ORIGIN):
    headers = {"Origin": origin}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    if body is None:
        return c.post(path, headers=headers)
    return c.post(path, content=json.dumps(body), headers={**headers, "Content-Type": "application/json"})


# --- session cap ---------------------------------------------------------------

def test_sixth_session_evicts_the_oldest(settings: Settings, clock: FakeClock,
                                         caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(settings, clock=clock)
    devices = [sign_in(app, clock)[0] for _ in range(settings.max_sessions_per_user)]
    other_user, _ = sign_in(app, clock, user_id=777)
    caplog.set_level(logging.INFO)
    newest, _ = sign_in(app, clock)
    assert devices[0].get("/api/me").status_code == 401
    assert all(d.get("/api/me").status_code == 200 for d in devices[1:])
    assert newest.get("/api/me").status_code == 200
    assert other_user.get("/api/me").status_code == 200  # another user's budget is separate
    assert "auth.session_evicted user=501 count=1" in caplog.text
    assert app.state.store.stats(int(clock.now))["active_sessions"] == settings.max_sessions_per_user + 1


def test_cap_holds_under_parallel_logins(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.db")
    store.init_schema()
    barrier = threading.Barrier(12)
    results: list[object] = []

    def create(i: int) -> None:
        barrier.wait()
        try:
            results.append(store.create_session(f"token-{i}", 1, f"{i:064x}", now=NOW, ttl_s=60, max_active=5, burn=BURN))
        except Exception as exc:  # collected: an exception in a thread would be lost
            results.append(exc)

    with sqlite3.connect(tmp_path / "m.db") as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (1, 'ru', 'telegram', 0, 0)")
    conn.close()
    threads = [threading.Thread(target=create, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 12 and all(isinstance(r, tuple) for r in results)
    assert sum(evicted for _, evicted in results) == 7
    assert store.stats(NOW)["active_sessions"] == 5


def test_expired_and_revoked_sessions_do_not_count_toward_the_cap(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.db")
    store.init_schema()
    with sqlite3.connect(tmp_path / "m.db") as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (1, 'ru', 'telegram', 0, 0)")
    conn.close()
    store.create_session("old", 1, "0" * 64, now=NOW - 100, ttl_s=10, max_active=2, burn=BURN)  # expired by NOW
    store.create_session("a", 1, "1" * 64, now=NOW, ttl_s=60, max_active=2, burn=BURN)
    _, evicted = store.create_session("b", 1, "2" * 64, now=NOW, ttl_s=60, max_active=2, burn=BURN)
    assert evicted == 0
    assert store.get_session("a").revoked_at is None and store.get_session("b").revoked_at is None


# --- logout everywhere ------------------------------------------------------------

def test_logout_all_ends_every_session_of_the_user_only(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    phone, _ = sign_in(app, clock)
    laptop, csrf = sign_in(app, clock)
    stranger, _ = sign_in(app, clock, user_id=900)
    current = fresh(clock, {"id": 501})
    r = post(laptop, "/api/auth/logout-all", csrf, {"init_data": current})
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": 2}
    assert 'sporai_session=""' in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert phone.get("/api/me").status_code == 401
    assert stranger.get("/api/me").status_code == 200
    # Neither the current launch nor the launches of the ended sessions sign back in on reload.
    assert login(device(app), current).json() == {"error": "init_data_reused"}
    clock.advance(1)
    assert login(device(app), fresh(clock, {"id": 501})).status_code == 200  # reopening works


def test_logout_all_requires_origin_csrf_and_a_live_session(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    c, csrf = sign_in(app, clock)
    assert post(c, "/api/auth/logout-all", csrf, origin="https://evil.example").status_code == 403
    assert post(c, "/api/auth/logout-all", None).status_code == 403
    assert post(c, "/api/auth/logout-all", "wrong").status_code == 403
    assert c.get("/api/me").status_code == 200  # rejected requests changed nothing
    assert c.get("/api/auth/logout-all").status_code == 405
    assert post(device(app), "/api/auth/logout-all", csrf).status_code == 401  # no cookie
    clock.advance(settings.session_ttl_s + 1)
    assert post(c, "/api/auth/logout-all", csrf).status_code == 401  # expired session


def test_logout_all_ignores_another_users_launch(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    c, csrf = sign_in(app, clock)
    foreign = fresh(clock, {"id": 902})
    assert post(c, "/api/auth/logout-all", csrf, {"init_data": foreign}).status_code == 200
    assert login(device(app), foreign).status_code == 200


# --- emergency switch -------------------------------------------------------------

def test_logins_disabled_rejects_new_logins_but_keeps_sessions(settings: Settings, clock: FakeClock) -> None:
    store = Store(settings.db_path)
    on = create_app(settings, clock=clock, store=store)
    c, _ = sign_in(on, clock)
    off = create_app(dataclasses.replace(settings, logins_enabled=False), clock=clock, store=store)
    same_device = TestClient(off, base_url="http://127.0.0.1:8000", cookies=dict(c.cookies))
    assert same_device.get("/api/me").status_code == 200
    r = login(device(off), fresh(clock, {"id": 600}))
    assert r.status_code == 503 and r.json() == {"error": "logins_disabled"}
    assert login(device(off), fresh(clock), origin="https://evil.example").status_code == 403


def test_logins_switch_from_env(tmp_path: Path) -> None:
    env = {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
           "MINIAPP_TRUSTED_ORIGINS": "https://a.example.com", "MINIAPP_EDGE_SECRET": EDGE_SECRET}
    assert Settings.from_env(env).logins_enabled is True
    assert Settings.from_env({**env, "MINIAPP_LOGINS_ENABLED": "false"}).logins_enabled is False
    for bad in ("no", "0", "off", "False!"):
        with pytest.raises(SettingsError):
            Settings.from_env({**env, "MINIAPP_LOGINS_ENABLED": bad})
    for name, bad in (("MINIAPP_MAX_SESSIONS_PER_USER", "0"), ("MINIAPP_MAX_SESSIONS_PER_USER", "6"),
                      ("MINIAPP_CSP_MODE", "off"), ("MINIAPP_EDGE_SECRET", "short"),
                      ("MINIAPP_EDGE_SECRET", "x" * 31), ("MINIAPP_EDGE_SECRET", "x" * 20 + " " + "y" * 20),
                      ("MINIAPP_STATIC_DIR", str(tmp_path / "missing"))):
        with pytest.raises(SettingsError) as exc:
            Settings.from_env({**env, name: bad})
        assert "y" * 20 not in str(exc.value) and "x" * 31 not in str(exc.value)


# --- edge secret ------------------------------------------------------------------

def test_edge_secret_blocks_every_route_but_health(settings: Settings, clock: FakeClock, tmp_path: Path,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>x</title>")
    app = create_app(dataclasses.replace(settings, edge_secret=EDGE_SECRET, static_dir=static), clock=clock)
    c = device(app)
    caplog.set_level(logging.DEBUG)
    assert c.get("/api/health").status_code == 200  # platform healthcheck has no edge header
    for path in ("/", "/api/me", "/index.html"):
        r = c.get(path)
        assert r.status_code == 403 and r.json() == {"error": "forbidden"}
        assert r.headers["cache-control"] == "no-store"
    assert login(c, fresh(clock)).status_code == 403
    assert login(c, fresh(clock), **{EDGE_SECRET_HEADER: "e" * 39 + "f"}).status_code == 403
    assert c.post("/api/health").status_code == 403  # only GET health is exempt
    c.headers[EDGE_SECRET_HEADER] = EDGE_SECRET
    assert c.get("/").status_code == 200
    assert login(c, fresh(clock)).status_code == 200
    assert EDGE_SECRET not in caplog.text
    assert "event=edge_bypass_blocked" in caplog.text
    assert EDGE_SECRET not in repr(app.state.settings)


# --- same-origin static files, CSP and cache ---------------------------------------

@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>Spor AI</title>")
    (d / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside")
    return d


def test_static_files_share_the_api_origin(settings: Settings, clock: FakeClock, dist: Path) -> None:
    c = device(create_app(dataclasses.replace(settings, static_dir=dist), clock=clock))
    index = c.get("/")
    assert index.status_code == 200 and "Spor AI" in index.text
    assert index.headers["cache-control"] == "no-store"
    asset = c.get("/assets/index-abc123.js")
    assert asset.status_code == 200 and asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "pragma" not in asset.headers
    missing = c.get("/assets/nope.js")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    # API paths never fall through to static files; wrong methods keep 405.
    r = c.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"error": "not_found"}
    assert c.get("/api").json() == {"error": "not_found"}
    assert c.get("/api/auth/logout").status_code == 405
    assert c.get("/api/health").json() == {"status": "ok"}
    # No path traversal out of dist, no directory listing.
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt", "/assets/"):
        r = c.get(path)
        assert r.status_code == 404 and "outside" not in r.text


def test_symlink_out_of_dist_is_not_served(settings: Settings, clock: FakeClock, dist: Path, tmp_path: Path) -> None:
    (dist / "leak.txt").symlink_to(tmp_path / "secret.txt")
    c = device(create_app(dataclasses.replace(settings, static_dir=dist), clock=clock))
    r = c.get("/leak.txt")
    assert r.status_code == 404 and "outside" not in r.text


def test_without_static_dir_the_root_is_not_served(client: TestClient) -> None:
    assert client.get("/").status_code == 404


def test_csp_on_every_response(settings: Settings, clock: FakeClock, dist: Path) -> None:
    c = device(create_app(dataclasses.replace(settings, static_dir=dist), clock=clock))
    for path in ("/", "/api/health", "/api/me", "/assets/index-abc123.js"):
        csp = c.get(path).headers["content-security-policy"]
        directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
        assert directives["default-src"] == "'none'"
        assert directives["script-src"] == "'self' https://telegram.org"
        assert directives["frame-ancestors"] == "https://web.telegram.org"
        assert directives["object-src"] == "'none'" and directives["base-uri"] == "'none'"
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
    headers = c.get("/").headers
    assert "x-frame-options" not in headers  # would break Telegram Web, which embeds an iframe
    assert "strict-transport-security" not in headers  # development (http loopback)
    assert "upgrade-insecure-requests" not in headers["content-security-policy"]
    assert "camera=()" in headers["permissions-policy"]


def test_production_headers_and_report_only_mode(tmp_path: Path, clock: FakeClock, dist: Path) -> None:
    prod = Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "p.db", static_dir=dist,
                    trusted_origins=frozenset({"https://stand.example.com"}), environment="production")
    headers = TestClient(create_app(prod, clock=clock), base_url="https://stand.example.com").get("/").headers
    assert headers["strict-transport-security"] == "max-age=31536000"
    assert headers["content-security-policy"].endswith("upgrade-insecure-requests")
    report = dataclasses.replace(prod, db_path=tmp_path / "r.db", csp_mode="report-only")
    headers = TestClient(create_app(report, clock=clock), base_url="https://stand.example.com").get("/").headers
    assert "content-security-policy" not in headers
    assert "frame-ancestors" in headers["content-security-policy-report-only"]


# --- health -----------------------------------------------------------------------

def test_health_fails_when_the_database_cannot_be_read(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                                       caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> None:
        raise sqlite3.OperationalError("disk I/O error at /secret/path")

    monkeypatch.setattr(client.app.state.store, "ping", broken)
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}
    assert "event=health_db_error type=OperationalError" in caplog.text
    assert "/secret/path" not in caplog.text


# --- purge --------------------------------------------------------------------------

def test_purge_drops_long_ended_sessions_only(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.db")
    store.init_schema()
    with sqlite3.connect(tmp_path / "m.db") as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (1, 'ru', 'telegram', 0, 0)")
    conn.close()
    store.create_session("expired", 1, "1" * 64, now=NOW - SESSION_RETENTION_S - 100, ttl_s=10)
    store.create_session("revoked", 1, "2" * 64, now=NOW - SESSION_RETENTION_S - 50, ttl_s=10 ** 9)
    store.revoke_session("revoked", NOW - SESSION_RETENTION_S - 10, NOW)
    store.create_session("recently-ended", 1, "3" * 64, now=NOW - 100, ttl_s=10)
    store.create_session("live", 1, "4" * 64, now=NOW, ttl_s=60)
    store.purge_expired(NOW)
    assert store.get_session("expired") is None and store.get_session("revoked") is None
    assert store.get_session("recently-ended") is not None and store.get_session("live") is not None


def test_app_start_purges(settings: Settings, clock: FakeClock) -> None:
    store = Store(settings.db_path)
    store.init_schema()
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (1, 'ru', 'telegram', 0, 0)")
    conn.close()
    store.create_session("ancient", 1, "1" * 64, now=NOW - SESSION_RETENTION_S - 100, ttl_s=10)
    create_app(settings, clock=clock, store=store)
    assert store.get_session("ancient") is None


def test_revoke_burns_only_launches_that_may_still_be_fresh(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.db")
    store.init_schema()
    with sqlite3.connect(tmp_path / "m.db") as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (1, 'ru', 'telegram', 0, 0)")
    conn.close()
    store.create_session("old", 1, "a" * 64, now=NOW - 1000, ttl_s=3600)
    store.create_session("new", 1, "b" * 64, now=NOW - 10, ttl_s=3600)
    assert store.revoke_user_sessions(1, NOW, BurnWindow.for_policy(NOW, 300, 30)) == 2
    assert store.is_init_data_consumed("b" * 64, NOW)
    assert not store.is_init_data_consumed("a" * 64, NOW)  # long expired anyway: nothing stored
    assert store.revoke_user_sessions(1, NOW, BurnWindow.for_policy(NOW, 300, 30)) == 0


# --- admin CLI -------------------------------------------------------------------------

@pytest.fixture()
def cli_env(tmp_path: Path) -> dict[str, str]:
    return {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
            "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development"}


def run_cli(env: dict[str, str], *argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = admin.main(list(argv), env=env, clock=lambda: NOW + 100)
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_never_creates_a_database(cli_env: dict[str, str], tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_cli(cli_env, "stats", capsys=capsys)
    assert code == 2 and "not found" in err
    assert not (tmp_path / "m.db").exists()
    code, _, err = run_cli({**cli_env, "MINIAPP_DB_PATH": "/data/bot.db"}, "stats", capsys=capsys)
    assert code == 2 and "bot database" in err and TEST_BOT_TOKEN not in err


def test_cli_revocation_and_stats(cli_env: dict[str, str], settings: Settings, capsys: pytest.CaptureFixture[str],
                                  caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock(NOW)
    app = create_app(dataclasses.replace(settings, db_path=Path(cli_env["MINIAPP_DB_PATH"])), clock=clock)
    a, _ = sign_in(app, clock, 11)
    b, _ = sign_in(app, clock, 12)
    tokens = [a.cookies.get(COOKIE), b.cookies.get(COOKIE)]
    caplog.set_level(logging.INFO)

    code, out, _ = run_cli(cli_env, "stats", capsys=capsys)
    assert code == 0 and json.loads(out) == {"active_sessions": 2, "burned_launches": 0, "stored_sessions": 2,
                                             "users": 2}
    assert run_cli(cli_env, "revoke-user", "11", capsys=capsys)[:2] == (0, '{"revoked": 1}\n')
    assert a.get("/api/me").status_code == 401 and b.get("/api/me").status_code == 200

    code, _, err = run_cli(cli_env, "revoke-all", capsys=capsys)
    assert code == 2 and "--yes" in err and b.get("/api/me").status_code == 200  # nothing changed
    assert run_cli(cli_env, "revoke-all", "--yes", capsys=capsys)[:2] == (0, '{"revoked": 1}\n')
    assert b.get("/api/me").status_code == 401
    assert run_cli(cli_env, "revoke-user", "0", capsys=capsys)[0] == 2
    assert "admin.revoke_all revoked=1" in caplog.text
    for secret in (*tokens, TEST_BOT_TOKEN):
        assert secret not in caplog.text and secret not in out


def test_cli_backup(cli_env: dict[str, str], settings: Settings, tmp_path: Path,
                    capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock(NOW)
    app = create_app(dataclasses.replace(settings, db_path=Path(cli_env["MINIAPP_DB_PATH"])), clock=clock)
    sign_in(app, clock, 21)
    target = tmp_path / "backups" / "miniapp-1.db"
    assert run_cli(cli_env, "backup", str(target), capsys=capsys)[0] == 0
    copy = Store(target)
    copy.init_schema()
    assert copy.count_users() == 1
    assert run_cli(cli_env, "backup", str(target), capsys=capsys)[0] == 2  # never overwrites
    assert run_cli(cli_env, "backup", cli_env["MINIAPP_DB_PATH"], capsys=capsys)[0] == 2
    assert run_cli(cli_env, "backup", str(tmp_path / "bot.db"), capsys=capsys)[0] == 2
    assert not (tmp_path / "bot.db").exists()


def test_cli_refuses_a_foreign_database(cli_env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    with sqlite3.connect(cli_env["MINIAPP_DB_PATH"]) as conn:
        conn.execute("CREATE TABLE requests (id INTEGER)")
    conn.close()
    code, _, err = run_cli(cli_env, "revoke-all", "--yes", capsys=capsys)
    assert code == 2 and "refused" in err


def test_cli_restore_revives_no_session(cli_env: dict[str, str], settings: Settings, tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock(NOW)
    app = create_app(dataclasses.replace(settings, db_path=Path(cli_env["MINIAPP_DB_PATH"])), clock=clock)
    kept, csrf = sign_in(app, clock, 31)
    target = tmp_path / "b.db"
    assert run_cli(cli_env, "backup", str(target), capsys=capsys)[0] == 0
    # After the backup: this session is logged out, a new user appears.
    assert post(kept, "/api/auth/logout", csrf).status_code == 200
    sign_in(app, clock, 32)

    assert run_cli(cli_env, "restore", str(target), capsys=capsys)[0] == 2  # needs --yes
    code, out, _ = run_cli(cli_env, "restore", str(target), "--yes", capsys=capsys)
    assert code == 0 and json.loads(out) == {"restored": True, "revoked": 1}
    stats = json.loads(run_cli(cli_env, "stats", capsys=capsys)[1])
    assert stats["users"] == 1 and stats["active_sessions"] == 0  # the logged-out session stays dead
    assert kept.get("/api/me").status_code == 401


def test_cli_restore_refuses_foreign_or_missing_sources(cli_env: dict[str, str], settings: Settings,
                                                        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_app(dataclasses.replace(settings, db_path=Path(cli_env["MINIAPP_DB_PATH"])), clock=FakeClock(NOW))
    foreign = tmp_path / "renamed-bot.sqlite"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE requests (id INTEGER)")
    conn.close()
    assert run_cli(cli_env, "restore", str(foreign), "--yes", capsys=capsys)[0] == 2
    assert run_cli(cli_env, "restore", str(tmp_path / "missing.db"), "--yes", capsys=capsys)[0] == 2
    assert run_cli(cli_env, "restore", cli_env["MINIAPP_DB_PATH"], "--yes", capsys=capsys)[0] == 2
    with sqlite3.connect(foreign) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master")] == ["requests"]  # untouched
    conn.close()


def test_cli_refuses_root(cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    code, _, err = run_cli(cli_env, "stats", capsys=capsys)
    assert code == 2 and "root" in err


def test_evicted_device_cannot_sign_back_in_with_its_launch(settings: Settings, clock: FakeClock) -> None:
    app = create_app(settings, clock=clock)
    clock.advance(1)
    first_launch = fresh(clock, {"id": 501})
    first = device(app)
    assert login(first, first_launch).status_code == 200
    for _ in range(settings.max_sessions_per_user):
        sign_in(app, clock)  # the fifth new one evicts `first`
    assert first.get("/api/me").status_code == 401
    # Reload of the evicted WebView: its launch is burned, no ping-pong evictions.
    r = login(first, first_launch)
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}
    assert app.state.store.stats(int(clock.now))["active_sessions"] == settings.max_sessions_per_user


def test_bulk_revoke_burns_fresh_launches_of_ended_sessions_too(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.db")
    store.init_schema()
    with sqlite3.connect(tmp_path / "m.db") as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (1, 'ru', 'telegram', 0, 0)")
    conn.close()
    store.create_session("short", 1, "c" * 64, now=NOW - 20, ttl_s=5)  # fresh launch, session already expired
    assert store.revoke_user_sessions(1, NOW, BURN) == 0
    assert store.is_init_data_consumed("c" * 64, NOW)


def test_restore_keeps_newer_burns_and_is_atomic(cli_env: dict[str, str], settings: Settings, tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock(NOW)
    app = create_app(dataclasses.replace(settings, db_path=Path(cli_env["MINIAPP_DB_PATH"])), clock=clock)
    c, csrf = sign_in(app, clock, 41)
    target = tmp_path / "b.db"
    assert run_cli(cli_env, "backup", str(target), capsys=capsys)[0] == 0
    clock.advance(7)
    launch = fresh(clock, {"id": 41, "first_name": "Reopened"})  # a NEW launch, unknown to the backup
    assert post(c, "/api/auth/logout", csrf, {"init_data": launch}).status_code == 200  # burned after the backup
    assert run_cli(cli_env, "restore", str(target), "--yes", capsys=capsys)[0] == 0
    r = login(device(app), launch)
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}


def test_failed_restore_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = Store(tmp_path / "live.db")
    live.init_schema()
    with sqlite3.connect(tmp_path / "live.db") as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (7, 'ru', 'telegram', 0, 0)")
    conn.close()
    live.create_session("live-token", 7, "d" * 64, now=NOW, ttl_s=60)
    backup = Store(tmp_path / "b.db")
    backup.init_schema()

    def boom(*_: object) -> int:
        raise sqlite3.OperationalError("simulated failure inside the transaction")

    monkeypatch.setattr(Store, "_revoke_where", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        live.restore_from(tmp_path / "b.db", NOW, BURN)
    assert live.count_users() == 1 and live.get_session("live-token").revoked_at is None


def test_production_from_env_requires_the_edge_secret(tmp_path: Path) -> None:
    env = {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
           "MINIAPP_TRUSTED_ORIGINS": "https://a.example.com"}
    for extra in ({}, {"MINIAPP_ENV": "production"}, {"MINIAPP_EDGE_SECRET": "   "}):
        with pytest.raises(SettingsError, match="MINIAPP_EDGE_SECRET is required"):
            Settings.from_env({**env, **extra})
    assert Settings.from_env({**env, "MINIAPP_EDGE_SECRET": EDGE_SECRET}).edge_secret == EDGE_SECRET
    local = Settings.from_env({**env, "MINIAPP_ENV": "development", "MINIAPP_TRUSTED_ORIGINS": ORIGIN})
    assert local.edge_secret is None  # loopback development needs no edge


def test_health_probe_does_not_hit_the_database_on_every_request(client: TestClient, clock: FakeClock,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_ping = client.app.state.store.ping
    monkeypatch.setattr(client.app.state.store, "ping", lambda: calls.append(1) or real_ping())
    for _ in range(50):
        assert client.get("/api/health").status_code == 200
    assert len(calls) == 1
    clock.advance(6)
    assert client.get("/api/health").status_code == 200 and len(calls) == 2


def test_health_failure_is_not_cached(client: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"broken": True}

    def ping() -> None:
        if state["broken"]:
            raise sqlite3.OperationalError("down")

    monkeypatch.setattr(client.app.state.store, "ping", ping)
    assert client.get("/api/health").status_code == 503
    state["broken"] = False
    assert client.get("/api/health").status_code == 200  # recovery is seen immediately


def test_restore_burns_launches_of_sessions_created_after_the_backup(
        cli_env: dict[str, str], settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock(NOW)
    app = create_app(dataclasses.replace(settings, db_path=Path(cli_env["MINIAPP_DB_PATH"])), clock=clock)
    sign_in(app, clock, 51)
    target = tmp_path / "b.db"
    assert run_cli(cli_env, "backup", str(target), capsys=capsys)[0] == 0
    clock.advance(3)
    late_launch = fresh(clock, {"id": 52})
    late = device(app)
    assert login(late, late_launch).status_code == 200  # exists only in the live DB
    assert run_cli(cli_env, "restore", str(target), "--yes", capsys=capsys)[0] == 0
    assert late.get("/api/me").status_code == 401
    r = login(device(app), late_launch)  # reload right after the restore
    assert r.status_code == 401 and r.json() == {"error": "init_data_reused"}


def test_single_logout_also_purges(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.db")
    store.init_schema()
    with sqlite3.connect(tmp_path / "m.db") as conn:
        conn.execute("INSERT INTO users (telegram_user_id, lang, lang_source, created_at, last_login_at) "
                     "VALUES (1, 'ru', 'telegram', 0, 0)")
    conn.close()
    store.create_session("ancient", 1, "1" * 64, now=NOW - SESSION_RETENTION_S - 100, ttl_s=10)
    store.create_session("current", 1, "2" * 64, now=NOW, ttl_s=60)
    assert store.revoke_session("current", NOW, NOW + 330)
    assert store.get_session("ancient") is None
