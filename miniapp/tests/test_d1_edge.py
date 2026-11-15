"""D1 collection for the admin's geography / platform / hour cards: the sign-in country (the edge's CF-IPCountry,
trusted only behind the edge secret, never an IP), the Telegram client's platform name, and the fact of activity per
UTC hour; the admin reads aggregates only."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from miniapp.admin_web.insights import UserInsights
from miniapp.ads.telemetry import Telemetry, telemetry_path
from miniapp.app import create_app
from miniapp.tests.conftest import NOW, ORIGIN, FakeClock
from miniapp.tests.signing import make_init_data
from miniapp.tests.test_ads_miniapp import EDGE, USER, settings


def sign_in(app, uid: int, body_extra: dict | None = None, country: str | None = "AZ", edge: bool = True):
    headers = {"Content-Type": "application/json", "Origin": ORIGIN}
    if edge:
        headers["X-Edge-Auth"] = EDGE
    if country:
        headers["CF-IPCountry"] = country
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    body = {"init_data": make_init_data({"id": uid, "first_name": "U"}, auth_date=int(NOW)), **(body_extra or {})}
    return c.post("/api/auth/session", content=json.dumps(body).encode(), headers=headers)


def edge_rows(tmp: Path) -> dict[int, tuple]:
    conn = sqlite3.connect(telemetry_path(tmp / "miniapp.db"))
    try:
        return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT user_id, country, platform FROM user_edge")}
    finally:
        conn.close()


def test_sign_in_records_country_and_platform(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), clock=FakeClock())
    assert sign_in(app, USER, {"platform": "ios"}).status_code == 200
    assert sign_in(app, 2, {"platform": "Weird-Client"}, country="T1").status_code == 200   # Tor: no country
    assert sign_in(app, 3, country="XX").status_code == 200                               # an older client: no platform
    rows = edge_rows(tmp_path)
    assert rows[USER] == ("AZ", "ios") and rows[2] == (None, "other") and 3 not in rows


def test_a_later_sign_in_keeps_what_it_does_not_report(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), clock=FakeClock())
    sign_in(app, USER, {"platform": "android"}, country="TR")
    TestClient(app).cookies.clear()
    sign_in(app, USER, country="AZ")                                                      # no platform this time
    assert edge_rows(tmp_path)[USER] == ("AZ", "android")


def test_the_country_is_trusted_only_behind_the_edge_secret(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path, edge_secret=None), clock=FakeClock())
    assert sign_in(app, USER, {"platform": "ios"}, country="AZ", edge=False).status_code == 200
    assert edge_rows(tmp_path)[USER] == (None, "ios")


def test_the_sign_in_body_stays_strict(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), clock=FakeClock())
    assert sign_in(app, USER, {"device": "x"}).status_code == 400                         # unknown key
    assert sign_in(app, USER, {"platform": 5}).status_code == 400                          # wrong type
    assert sign_in(app, USER, {"platform": "x" * 33}).status_code == 400                   # too long


def test_activity_is_kept_per_utc_hour_without_counts(tmp_path: Path) -> None:
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    for offset in (0, 60, 3600):
        t.record_activity(USER, int(NOW) + offset)
    conn = sqlite3.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM activity_hours").fetchone()[0] == 2         # two distinct hours
    t.purge(int(NOW) + 500 * 86400)
    assert conn.execute("SELECT COUNT(*) FROM activity_hours").fetchone()[0] == 0         # same retention as activity


def test_the_admin_sees_aggregates_by_country_platform_and_hour(tmp_path: Path) -> None:
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    for uid, country, platform in ((1, "AZ", "ios"), (2, "AZ", "android"), (3, "TR", "tdesktop")):
        t.record_activity(uid, int(NOW))
        t.record_edge(uid, country, platform, int(NOW))
    t.record_edge(9, "RU", "ios", int(NOW))                                                # never active in the period
    out = UserInsights(tmp_path / "t.db").edge_overview(int(NOW), 30)
    assert out["source"] == "telemetry" and out["activeUsers"] == 3
    assert out["geo"] == [{"code": "AZ", "count": 2}, {"code": "TR", "count": 1}]
    assert sorted(p["name"] for p in out["platforms"]) == ["android", "ios", "tdesktop"]
    assert sum(map(sum, out["activityHeat"])) == 3
    assert UserInsights(tmp_path / "t.db").edge_countries([1, 9]) == {1: "AZ", 9: "RU"}
    assert UserInsights(tmp_path / "missing.db").edge_overview(int(NOW), 30) == {"source": "not_collected"}


def test_country_codes_are_ascii_letters_only() -> None:
    from starlette.requests import Request

    from miniapp.app import _edge_country

    def req(value: bytes) -> Request:
        return Request({"type": "http", "headers": [(b"cf-ipcountry", value)]})
    assert _edge_country(req("ÄÖ".encode("latin-1")), True) is None
    assert _edge_country(req("ß".encode("latin-1") + b" "), True) is None
    assert _edge_country(req(b"az"), True) == "AZ"
    assert _edge_country(req(b"AZ"), False) is None


def test_the_users_edge_record_expires_with_their_activity(tmp_path: Path) -> None:
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    t.record_edge(USER, "AZ", "ios", int(NOW))
    t.purge(int(NOW) + 300 * 86400)
    assert sqlite3.connect(tmp_path / "t.db").execute("SELECT COUNT(*) FROM user_edge").fetchone()[0] == 1
    t.purge(int(NOW) + 401 * 86400)
    assert sqlite3.connect(tmp_path / "t.db").execute("SELECT COUNT(*) FROM user_edge").fetchone()[0] == 0


def test_the_period_is_rolling_not_whole_utc_days(tmp_path: Path) -> None:
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    t.record_activity(1, int(NOW) - 30 * 3600)                                            # 30 h ago: outside "1 day"
    t.record_activity(2, int(NOW) - 3600)
    t.record_activity(3, int(NOW) + 5 * 3600)                                             # dated ahead: not counted
    out = UserInsights(tmp_path / "t.db").edge_overview(int(NOW), 1)
    assert out["activeUsers"] == 1
    assert UserInsights(tmp_path / "t.db").edge_overview(int(NOW), 7)["activeUsers"] == 2
