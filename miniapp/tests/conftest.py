from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.settings import Settings
from miniapp.tests.signing import TEST_BOT_TOKEN, make_init_data

ORIGIN = "http://127.0.0.1:5173"
NOW = 1_800_000_000


class FakeClock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token=TEST_BOT_TOKEN,
        db_path=tmp_path / "miniapp-test.db",
        trusted_origins=frozenset({ORIGIN}),
        environment="development",
        session_ttl_s=3600,
    )


@pytest.fixture()
def client(settings: Settings, clock: FakeClock) -> TestClient:
    app = create_app(settings, clock=clock)
    return TestClient(app, base_url="http://127.0.0.1:8000")


def login(client: TestClient, init_data: str, origin: str | None = ORIGIN, **headers: str):
    hdrs = {"Content-Type": "application/json", **headers}
    if origin is not None:
        hdrs["Origin"] = origin
    return client.post("/api/auth/session", json=None, content=_body(init_data), headers=hdrs)


def _body(init_data: str) -> bytes:
    import json

    return json.dumps({"init_data": init_data}).encode()


def fresh(clock: FakeClock, user: dict | None = None, **kw) -> str:
    return make_init_data(user, auth_date=int(clock.now), **kw)
