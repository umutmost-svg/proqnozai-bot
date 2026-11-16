"""Credits and MB ID (docs/miniapp/credits.md): welcome and monthly credits, one charge per generated forecast,
the owner's switch, admin commands applied once."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miniapp.admin_commands import CommandQueue
from miniapp.ai.analysis import AnalysisResult
from miniapp.app import create_app
from miniapp.credits import COST, MONTHLY, WELCOME, Credits, MbError, NoCredits
from miniapp.settings import Settings
from miniapp.store import Store
from miniapp.tests.conftest import ORIGIN, FakeClock, fresh, login

UID = 100


@pytest.fixture()
def credits(tmp_path: Path, clock: FakeClock) -> Credits:
    store = Store(tmp_path / "m.db")
    store.init_schema()
    return Credits(store, clock)


def on(c: Credits) -> None:
    assert c.apply("flag-on", "flag", None, {"key": "credits.enabled", "value": True}) == "done"


def test_welcome_then_one_charge_per_item_only_when_switched_on(credits: Credits) -> None:
    assert credits.summary(UID)["balance"] == WELCOME
    assert credits.charge(UID, "ai:fd:1") is False  # switch off: nothing is charged
    on(credits)
    assert credits.charge(UID, "ai:fd:1") is True and credits.charge(UID, "ai:fd:1") is False
    s = credits.summary(UID)
    assert s["enabled"] and s["balance"] == WELCOME - COST
    assert [h["kind"] for h in s["history"]] == ["spend", "welcome"]


def test_no_credits_and_the_monthly_top_up_once_a_month(credits: Credits, clock: FakeClock) -> None:
    on(credits)
    for i in range(WELCOME // COST):
        credits.charge(UID, f"ai:fd:{i}")
    # Balance 0: the monthly credits arrive at once (first touch this month), then not again this month.
    assert credits.summary(UID)["balance"] == MONTHLY
    for i in range(MONTHLY // COST):
        credits.charge(UID, f"quick:{i}")
    with pytest.raises(NoCredits):
        credits.precheck(UID, "ai:fd:new")
    assert credits.summary(UID)["monthly"]["nextAt"] is not None
    d = datetime.fromtimestamp(clock(), UTC)
    clock.now = datetime(d.year + (d.month == 12), d.month % 12 + 1, 2, tzinfo=UTC).timestamp()
    credits.precheck(UID, "ai:fd:new")  # a new month: topped up again
    assert credits.summary(UID)["balance"] == MONTHLY


def test_an_item_already_paid_is_free_and_unlimited_charges_nothing(credits: Credits) -> None:
    on(credits)
    credits.charge(UID, "ai:fd:1")
    assert credits.apply("k-debit", "debit", UID, {"amount": 10_000}) == "done"  # never below zero
    assert credits.summary(UID)["balance"] == MONTHLY  # 0 -> the month's top-up on the next touch
    credits.apply("k-unl", "unlimited", UID, {"days": 3})
    for i in range(10):
        assert credits.charge(UID, f"x:{i}") is False
    credits.precheck(UID, "ai:fd:1")  # paid before: free whatever the balance


def test_admin_commands_apply_once(tmp_path: Path, credits: Credits, clock: FakeClock) -> None:
    q = CommandQueue(tmp_path / "admin-commands.db")
    q.init_schema()
    assert q.enqueue("g1", "grant", UID, {"amount": 25}, int(clock()))
    assert not q.enqueue("g1", "grant", UID, {"amount": 25}, int(clock()))  # a retried admin action
    q.enqueue("bad", "grant", UID, {"amount": -5}, int(clock()))
    assert q.apply_pending(credits.apply, int(clock())) == 2
    assert q.apply_pending(credits.apply, int(clock())) == 0
    assert credits.summary(UID)["balance"] == WELCOME + 25
    assert credits.apply("g1", "grant", UID, {"amount": 25}) == "done"  # the Mini App's own record: no second grant
    assert credits.summary(UID)["balance"] == WELCOME + 25
    assert q.results(["g1", "bad"]) == {"g1": {"appliedAt": int(clock()), "result": "done"},
                                         "bad": {"appliedAt": int(clock()), "result": "invalid"}}


def test_mb_id_submit_decide_and_uniqueness(credits: Credits) -> None:
    with pytest.raises(MbError, match="invalid_mb_id"):
        credits.submit_mb(UID, "a b")
    assert credits.submit_mb(UID, "MB123") == {"id": "MB123", "state": "pending"}
    with pytest.raises(MbError, match="mb_taken"):
        credits.submit_mb(UID + 1, "mb123")
    assert credits.apply("d0", "mb_approve", UID, {"mbId": "OTHER"}) == "stale"  # not the ID the owner saw
    assert credits.apply("d1", "mb_reject", UID, {"mbId": "MB123", "reason": "нет такого"}) == "done"
    assert credits.summary(UID)["mb"]["state"] == "rejected"
    credits.submit_mb(UID + 1, "MB123")  # a rejected ID is free again
    credits.submit_mb(UID, "MB777")
    assert credits.apply("d2", "mb_approve", UID, {"mbId": "mb777"}) == "done"
    with pytest.raises(MbError, match="mb_locked"):
        credits.submit_mb(UID, "MB888")
    assert credits.apply("s1", "mb_set", UID + 2, {"mbId": "MB777"}) == "mb_taken"


class FakeAi:
    connected = True

    def __init__(self, kind: str = "ready") -> None:
        self.kind = kind
        self.calls = 0

    async def analyse(self, event_id: str, user_id: int, *, create: bool = True):
        self.calls += 1
        return AnalysisResult(event_id, self.kind, ())


def session(client: TestClient, clock: FakeClock, uid: int = UID) -> str:
    r = login(client, fresh(clock, {"id": uid, "first_name": "T"}))
    assert r.status_code == 200, r.text
    return r.json()["session"]["csrf_token"]


def test_api_balance_mb_and_ai_charging(settings: Settings, clock: FakeClock) -> None:
    ai = FakeAi()
    client = TestClient(create_app(settings, clock=clock, ai=ai), base_url="http://127.0.0.1:8000")
    csrf = session(client, clock)
    hdr = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    # Switched off: hidden from users entirely (no account is created, no MB ID is taken).
    assert client.get("/api/credits").json() == {"enabled": False}
    assert client.post("/api/credits/mb", json={"mbId": "X1"}, headers=hdr).json()["error"] == "credits_off"
    Credits(Store(settings.db_path), clock).apply("on", "flag", None, {"key": "credits.enabled", "value": True})
    assert client.get("/api/credits").json()["balance"] == WELCOME
    assert client.post("/api/credits/mb", json={"mbId": "X1"}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/credits/mb", json={"mbId": "X1"}, headers=hdr).json() == {"mb": {"id": "X1", "state": "pending"}}
    for i in range(WELCOME // COST):
        assert client.post("/api/ai/analysis", json={"event_id": f"fd:{i}"}, headers=hdr).status_code == 200
    assert client.post("/api/ai/analysis", json={"event_id": "fd:0"}, headers=hdr).status_code == 200  # paid already
    calls = ai.calls
    r = client.post("/api/ai/analysis", json={"event_id": "fd:new"}, headers=hdr)
    # Balance 0 -> this month's top-up arrives first, so the user can still generate.
    assert r.status_code == 200 and client.get("/api/credits").json()["balance"] == MONTHLY - COST
    for i in range(MONTHLY // COST - 1):
        client.post("/api/ai/analysis", json={"event_id": f"fd:m{i}"}, headers=hdr)
    r = client.post("/api/ai/analysis", json={"event_id": "fd:none"}, headers=hdr)
    assert r.status_code == 402 and r.json() == {"error": "no_credits", "balance": 0, "cost": COST}
    assert ai.calls == calls + MONTHLY // COST  # refused before generating
    ai.kind = "insufficient"  # nothing generated: free
    Credits(Store(settings.db_path), clock).apply("g", "grant", UID, {"amount": COST})
    client.post("/api/ai/analysis", json={"event_id": "fd:ins"}, headers=hdr)
    assert client.get("/api/credits").json()["balance"] == COST


def test_quick_pick_needs_the_csrf_header_once_credits_are_on(settings: Settings, clock: FakeClock) -> None:
    client = TestClient(create_app(settings, clock=clock, ai=FakeAi()), base_url="http://127.0.0.1:8000")
    csrf = session(client, clock)
    url = "/api/quick/pick?mode=single&from=2027-01-15T00:00:00Z&to=2027-01-16T00:00:00Z"
    assert client.get(url).status_code != 403  # switch off: as before
    Credits(Store(settings.db_path), clock).apply("on", "flag", None, {"key": "credits.enabled", "value": True})
    assert client.get(url).status_code == 403
    assert client.get(url, headers={"X-CSRF-Token": csrf}).status_code != 403
    Credits(Store(settings.db_path), clock).apply("d", "debit", UID, {"amount": WELCOME})
    # Balance 0 with this month's top-up already used: refused before computing.
    store = Store(settings.db_path)
    with store._write() as conn:
        conn.execute("UPDATE credit_accounts SET topup_month = ? WHERE user_id = ?",
                     (datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m"), UID))
    r = client.get(url, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 402 and r.json()["error"] == "no_credits"


def test_calculator_rate_follows_the_viewers_country(credits: Credits) -> None:
    from miniapp.credits import valid_rates
    rates = [{"country": "AZ", "currency": "AZN", "perForecast": 5}, {"country": "*", "currency": "USD", "perForecast": 3}]
    assert valid_rates(rates) == rates
    for bad in ([{"country": "az", "currency": "AZN", "perForecast": 5}], [{"country": "AZ", "currency": "AZN", "perForecast": 0}],
                rates + [{"country": "AZ", "currency": "TRY", "perForecast": 1}], "x"):
        assert valid_rates(bad) is None
    assert credits.summary(UID)["deposit"] is None  # no rates yet: the calculator is hidden
    assert credits.apply("r1", "flag", None, {"key": "credits.rates", "value": rates}) == "done"
    assert credits.summary(UID, "AZ")["deposit"] == {"currency": "AZN", "perForecast": 5}
    assert credits.summary(UID, "TR")["deposit"] == {"currency": "USD", "perForecast": 3}
    assert credits.summary(UID, None)["deposit"] == {"currency": "USD", "perForecast": 3}
    assert credits.apply("r2", "flag", None, {"key": "credits.rates", "value": [{"country": "AZ"}]}) == "invalid"
