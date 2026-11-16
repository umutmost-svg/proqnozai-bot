# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Queue view (no texts), role matrix, product analytics (DAU/WAU/MAU, cohorts, forecasts, AI, referral funnel)."""
from __future__ import annotations

import re
from pathlib import Path

from miniapp.ads.telemetry import Telemetry
from miniapp.admin_web.outbox import BOT_LIMITS
from miniapp.store import Store
from miniapp.tests.test_admin_crm import U1, U2
from miniapp.tests.test_admin_web import env, login, post  # noqa: F401 — env is a fixture


def test_queue_view_shows_statuses_never_texts(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "Секретный текст", "idempotencyKey": "msg-q-0001"})
    page = client.get("/api/admin/outbox")
    data = page.json()
    assert page.status_code == 200 and "Секретный текст" not in page.text
    assert data["summary"]["byStatus"] == {"queued": 1} and data["items"][0]["kind"] == "message"
    assert data["items"][0]["userId"] == U1 and data["limits"]["maxAttempts"] == 3
    assert any(e["action"] == "user.message" for e in data["log"])
    assert client.get("/api/admin/outbox?status=sent").json()["items"] == []


def test_queue_log_carries_no_free_text(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "n", "body": "Тело рассылки"}).json()["id"]
    from miniapp.tests.test_admin_crm import dangerous
    post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, {**dangerous(secrets_, clock), "reason": "секретная причина"})
    page = client.get("/api/admin/outbox")
    assert "секретная причина" not in page.text and any(e["action"] == "broadcast.cancel" for e in page.json()["log"])


def test_queue_view_is_not_for_analysts(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "analyst@x.test")
    assert client.get("/api/admin/outbox").status_code == 403


def test_bot_limits_match_the_bot_side():
    text = (Path(__file__).resolve().parents[2] / "bot_outbox.py").read_text()
    for name, key in (("BATCH", "batchPerRun"), ("MAX_ATTEMPTS", "maxAttempts"), ("STUCK_S", "stuckS"),
                      ("TEXT_RETENTION_DAYS", "textRetentionDays"), ("ROW_RETENTION_DAYS", "rowRetentionDays")):
        assert int(re.search(rf"^{name} = (\d+)", text, re.M).group(1)) == BOT_LIMITS[key], name


def test_role_matrix(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "analyst@x.test")
    data = client.get("/api/admin/roles").json()
    perms = {p["permission"]: p["roles"] for p in data["permissions"]}
    assert perms["read"] == ["owner", "admin", "analyst", "promoter"] and perms["manage_inquiry_settings"] == ["owner"]
    assert data["grants"] == [{"permission": "view_dialogs", "roles": ["owner", "admin"]}]


def test_product_analytics_not_collected_without_telemetry(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "analyst@x.test")
    data = client.get("/api/admin/product?days=7").json()
    assert data["product"] == {"source": "not_collected"} and data["oddsCoverage"]["source"] == "not_collected"


def test_product_analytics_from_telemetry(env):
    config, clock, client, _, secrets_ = env
    now = int(clock())
    t = Telemetry(config.telemetry_db)
    t.init_schema()
    t.record_activity(U1, now)
    t.record_activity(U1, now, "quick_single_ok")
    t.record_activity(U2, now, "ai_created")
    t.record_activity(U2, now - 3 * 86400, "ai_opened")
    t.record_activity(U2, now - 20 * 86400)
    t.record_activity(4242, now + 2 * 86400)  # a future-dated row never counts
    Store(config.miniapp_db)  # demo users registered over the last months
    login(client, clock, secrets_, "analyst@x.test")
    p = client.get("/api/admin/product?days=7").json()["product"]
    assert p["source"] == "telemetry" and len(p["series"]) == 7
    assert p["dau"] == 2 and p["wau"] == 2 and p["mau"] == 2
    assert p["totals"] == {"quick": 1, "coupons": 0, "aiCreated": 1, "aiOpened": 1, "quickFailed": 0, "forecasts": 2}
    assert len(p["cohorts"]) == 8 and all(len(c["retention"]) == 8 for c in p["cohorts"])
    assert p["cohorts"][-1]["retention"][1] is None  # the current week has no week 1 yet
    assert "990" not in str(p["cohorts"])  # aggregates only


def test_cohort_retention_counts_weeks_from_each_users_registration(tmp_path):
    from datetime import UTC, datetime, timedelta

    from miniapp.admin_web.insights import UserInsights, product
    now = int(datetime(2026, 11, 13, 12, tzinfo=UTC).timestamp())
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    reg = now - 10 * 86400                      # Tue 3 Nov
    t.record_activity(1, reg)                    # week 0
    t.record_activity(1, reg + 8 * 86400)        # week 1 (days 7..13 after registration)
    users = [{"id": 1, "createdAt": reg}, {"id": 2, "createdAt": reg + 3600}]
    cohorts = product(UserInsights(tmp_path / "t.db"), now, 7, users)["cohorts"]
    week = next(c for c in cohorts if c["week"] == (datetime.fromtimestamp(reg, UTC).date()
                                                    - timedelta(days=datetime.fromtimestamp(reg, UTC).weekday())).isoformat())
    # Week 0 (3..9 Nov) is over for both, week 1 (10..16 Nov) is not yet: empty, never a zero.
    assert week["size"] == 2 and week["retention"][:2] == [0.5, None]


def test_a_late_week_registration_is_not_counted_before_its_week_is_over(tmp_path):
    from datetime import UTC, datetime

    from miniapp.admin_web.insights import UserInsights, product
    now = int(datetime(2026, 11, 13, 12, tzinfo=UTC).timestamp())        # Friday
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    sunday = int(datetime(2026, 11, 8, 12, tzinfo=UTC).timestamp())      # its week 0 (8..14 Nov) is not over
    monday = int(datetime(2026, 11, 2, 12, tzinfo=UTC).timestamp())      # week 0 (2..8 Nov) over, week 1 (9..15) not
    t.record_activity(1, monday)
    users = [{"id": 1, "createdAt": monday}, {"id": 2, "createdAt": sunday}]
    cohort = next(c for c in product(UserInsights(tmp_path / "t.db"), now, 7, users)["cohorts"] if c["week"] == "2026-11-02")
    # Only the Monday member counts for week 0 (the Sunday one is not counted as inactive).
    assert cohort["size"] == 2 and cohort["retention"][0] == 1.0 and cohort["retention"][1] is None


def test_cohorts_are_not_capped_by_the_users_list(env, monkeypatch):
    config, clock, client, _, secrets_ = env
    from miniapp.admin_web import crm_routes
    monkeypatch.setattr(crm_routes, "MAX_SCAN", 1)
    t = Telemetry(config.telemetry_db)
    t.init_schema()
    login(client, clock, secrets_, "analyst@x.test")
    sizes = sum(c["size"] for c in client.get("/api/admin/product?days=7").json()["product"]["cohorts"])
    recent = Store(config.miniapp_db).registrations_since(0)
    from miniapp.admin_web.insights import cohort_since
    assert sizes == sum(1 for u in recent if u["createdAt"] >= cohort_since(int(clock())))


def test_an_older_telemetry_schema_without_quick_failed_is_not_collected(tmp_path):
    import sqlite3

    from miniapp.admin_web.insights import UserInsights, product
    path = tmp_path / "telemetry.db"
    with sqlite3.connect(path) as conn:  # every column read here except quick_failed
        conn.execute("CREATE TABLE user_activity (day TEXT, user_id INTEGER, first_at INTEGER, last_at INTEGER, "
                     "requests INTEGER, ai_opened INTEGER, ai_created INTEGER, quick_single_ok INTEGER, "
                     "quick_coupon_ok INTEGER)")
    assert product(UserInsights(path), 1_800_000_000, 7, []) == {"source": "not_collected"}


def test_last_24h_counts_outcomes_by_when_they_happened(tmp_path):
    import sqlite3

    from miniapp.admin_web.outbox import Outbox
    now = 1_800_000_000
    box = Outbox(tmp_path / "outbox.db")
    box.init_schema()
    box.enqueue([("a", 1, "x", []), ("b", 2, "x", []), ("c", 3, "x", [])], batch_id=None, not_before=now - 2 * 86400,
                admin_event_id=None, now=now - 2 * 86400)  # all created two days ago
    with sqlite3.connect(box.path) as conn:
        conn.execute("UPDATE jobs SET status = 'failed', claimed_at = ? WHERE idempotency_key = 'a'", (now - 3600,))
        conn.execute("UPDATE jobs SET status = 'expired', expires_at = ? WHERE idempotency_key = 'b'", (now - 60,))
        conn.execute("UPDATE jobs SET status = 'failed', claimed_at = ? WHERE idempotency_key = 'c'", (now - 2 * 86400,))
    recent = box.summary(now)["last24h"]
    assert recent.get("failed") == 1 and recent.get("expired") == 1


def test_held_jobs_are_not_the_next_due(tmp_path):
    from miniapp.admin_web.outbox import Outbox
    box = Outbox(tmp_path / "outbox.db")
    box.init_schema()
    box.enqueue([("h", 1, "x", [])], batch_id="bc:1", not_before=100, admin_event_id=None, now=100, hold=True)
    assert box.summary(200)["nextDueAt"] is None
    assert box.release_batch("bc:1") == 1 and box.summary(200)["nextDueAt"] == 100


def test_a_failed_telemetry_read_is_unavailable_not_zero(tmp_path, monkeypatch):
    import sqlite3

    from miniapp.admin_web.insights import UserInsights, product
    path = tmp_path / "telemetry.db"
    Telemetry(path).init_schema()  # the full schema
    ins = UserInsights(path)
    assert product(ins, 1_800_000_000, 7, [])["source"] == "telemetry"

    def locked(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(UserInsights, "week_offsets", locked)
    assert product(ins, 1_800_000_000, 7, [{"id": 1, "createdAt": 1_800_000_000}]) == {"source": "unavailable"}


def test_a_telemetry_file_that_cannot_be_opened_is_unavailable(tmp_path, monkeypatch):
    import sqlite3

    from miniapp.admin_web import insights
    from miniapp.admin_web.insights import UserInsights, product
    path = tmp_path / "telemetry.db"
    Telemetry(path).init_schema()
    real = sqlite3.connect

    def refuse(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(insights.sqlite3, "connect", refuse)
    assert product(UserInsights(path), 1_800_000_000, 7, []) == {"source": "unavailable"}
    monkeypatch.setattr(insights.sqlite3, "connect", real)
    assert product(UserInsights(tmp_path / "missing.db"), 1_800_000_000, 7, []) == {"source": "not_collected"}


def test_a_failed_activity_read_leaves_the_conversion_unknown(env, monkeypatch):
    import sqlite3

    from miniapp.admin_web.insights import UserInsights
    config, clock, client, _, secrets_ = env
    Telemetry(config.telemetry_db).init_schema()

    def locked(self, *a, strict=False, **k):
        if strict:
            raise sqlite3.OperationalError("database is locked")
        return True
    monkeypatch.setattr(UserInsights, "available", locked)
    login(client, clock, secrets_, "analyst@x.test")
    data = client.get("/api/admin/product?days=7").json()
    assert data["referrals"]["converted"] is None and data["product"] == {"source": "unavailable"}
