"""User value and top users in the admin: the bot's hourly snapshot (checked field by field) and the Mini App's
telemetry (per active user); names only for roles that may read personal data."""
from __future__ import annotations

import json
from pathlib import Path

from miniapp.admin_web.value_routes import miniapp_value, read_bot_snapshot
from miniapp.ads.telemetry import Telemetry

NOW = 1_800_000_000


def snapshot(**over) -> dict:
    base = {"schema": 1, "generatedAt": "2027-01-15T08:00:00Z",
            "value": {"users": 10, "forecastsPerUser": 5.8, "clicksPerUser": 0.2, "clickConversion": 10,
                      "avgLifespanDays": 29.2, "promoConversion": 20, "cpa": 0, "revenuePerUser": None,
                      "revenueTotal": None},
            "topUsers": [{"userId": 7, "name": "Ann", "username": "ann", "requests": 40, "forecasts": 30,
                          "clicks": 1, "promo": True, "lastActive": "2027-01-14 10:00:00"}]}
    base.update(over)
    return base


def test_the_bot_snapshot_is_read_strictly(tmp_path: Path) -> None:
    f = tmp_path / "bot-snapshot.json"
    assert read_bot_snapshot(f) == {"source": "not_collected"}
    f.write_text(json.dumps(snapshot()))
    got = read_bot_snapshot(f)
    assert got["source"] == "snapshot" and got["value"]["avgLifespanDays"] == 29.2
    assert got["topUsers"][0]["name"] == "Ann" and got["value"]["revenuePerUser"] is None
    row = snapshot()["topUsers"][0]
    for bad in (snapshot(schema=2), snapshot(value={"users": "x"}), snapshot(topUsers=[{"userId": "7"}]), [1, 2],
                snapshot(topUsers=[{**row, "requests": "abc"}]), snapshot(topUsers=[{**row, "clicks": -1}]),
                snapshot(value={**snapshot()["value"], "users": -1}), snapshot(value={**snapshot()["value"], "users": 2.5}),
                snapshot(value={**snapshot()["value"], "clicksPerUser": float("nan")}),
                snapshot(value={**snapshot()["value"], "cpa": -1}), snapshot(value={**snapshot()["value"], "cpa": 2.0}),
                snapshot(value={**snapshot()["value"], "revenueTotal": "x"}),
                snapshot(value={k: v for k, v in snapshot()["value"].items() if k != "cpa"})):
        f.write_text(json.dumps(bad, allow_nan=True))
        assert read_bot_snapshot(f) == {"source": "unavailable"}
    f.write_text("not json")
    assert read_bot_snapshot(f) == {"source": "unavailable"}


def test_mini_app_value_is_per_active_user(tmp_path: Path) -> None:
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    for day in (0, 3):                                                     # user 1: two days apart
        t.record_activity(1, NOW + day * 86400)
        t.record_activity(1, NOW + day * 86400, "quick_single_ok")
    t.record_activity(2, NOW)                                              # user 2: one visit, no forecast
    got = miniapp_value(tmp_path / "t.db", total_users=693, now=NOW + 4 * 86400)
    assert got["activeUsers"] == 2 and got["totalUsers"] == 693
    assert got["value"]["forecastsPerUser"] == 1.0 and got["value"]["avgLifespanDays"] == 3.0
    assert [r["userId"] for r in got["topUsers"]] == [1, 2] and got["topUsers"][0]["forecasts"] == 2
    assert miniapp_value(tmp_path / "missing.db", 0, NOW) == {"source": "not_collected"}
    assert miniapp_value(tmp_path / "t.db", 693, NOW + 200 * 86400)["activeUsers"] == 0   # outside the 90-day window
    assert miniapp_value(tmp_path / "t.db", 693, NOW - 86400)["activeUsers"] == 0         # dated ahead: not counted


def test_names_are_hidden_from_roles_without_personal_access(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from miniapp.admin_web import totp
    from miniapp.admin_web.accounts import AdminStore
    from miniapp.admin_web.app import create_admin_app
    from miniapp.admin_web.config import AdminWebConfig
    from miniapp.admin_web.demo import seed

    config = AdminWebConfig(data_dir=tmp_path / "pilot", port=8799, static_dir=None)
    config.data_dir.mkdir(mode=0o700)
    seed(config.data_dir)
    (config.admin_dir).mkdir(exist_ok=True, mode=0o700)
    (config.admin_dir / "bot-snapshot.json").write_text(json.dumps(snapshot()))
    clock = lambda: float(NOW)  # noqa: E731
    app = create_admin_app(config, clock=clock)
    accounts = AdminStore(config.admin_db)
    _, secret = accounts.create_admin("analyst@x.test", "analyst", "pw-analyst", NOW)
    origin = "http://127.0.0.1:8799"
    c = TestClient(app, base_url=origin)
    assert c.post("/api/admin/auth/login", json={"email": "analyst@x.test", "password": "pw-analyst"},
                  headers={"Origin": origin}).status_code == 200
    assert c.post("/api/admin/auth/totp", json={"code": totp.code_at(secret, NOW // 30)},
                  headers={"Origin": origin}).status_code == 200
    body = c.get("/api/admin/value").json()
    first = body["bot"]["topUsers"][0]
    assert body["personal"] is False and first["userId"] is None and first["name"] == "" and first["username"] == ""
    assert first["requests"] == 40                                          # counters stay
