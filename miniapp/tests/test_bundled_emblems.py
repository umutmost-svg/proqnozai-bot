"""Competition emblems bundled with the app for competitions the provider gives none for (UNL, TSL, RFPL)."""
from __future__ import annotations

import asyncio

import pytest

from miniapp.sports.crests import BUNDLED_DIR, PNG_MAGIC, CrestStore, load_bundled


def test_the_bundled_emblems_are_valid_pngs() -> None:
    bundled = load_bundled()
    assert set(bundled) == {"UNL", "TSL", "RFPL"}
    assert all(body.startswith(PNG_MAGIC) for body in bundled.values())


def test_a_bundled_emblem_is_served_while_the_provider_has_none() -> None:
    store = CrestStore(bundled={"UNL": PNG_MAGIC + b"x"})
    assert store.path("competition", "UNL") == "/api/crests/competition/UNL"
    assert asyncio.run(store.image("competition", "UNL")) == PNG_MAGIC + b"x"
    assert store.path("competition", "PL") is None          # nothing bundled, nothing learned
    assert store.path("team", "UNL") is None                 # competitions only


def test_a_provider_emblem_wins_over_the_bundled_one() -> None:
    fetched = []

    async def fetch(url: str, timeout: float) -> bytes:
        fetched.append(url)
        return PNG_MAGIC + b"provider"

    store = CrestStore(fetch, bundled={"UNL": PNG_MAGIC + b"bundled"})
    assert store.learn("competition", "UNL", "https://crests.football-data.org/UNL.png")
    assert asyncio.run(store.image("competition", "UNL")) == PNG_MAGIC + b"provider"
    store.forget("competition", "UNL")                       # the provider withdrew it again
    assert asyncio.run(store.image("competition", "UNL")) == PNG_MAGIC + b"bundled"


def test_anything_but_a_png_named_by_a_code_is_refused(tmp_path) -> None:
    (tmp_path / "UNL.png").write_bytes(b"<svg/>")
    with pytest.raises(ValueError):
        load_bundled(tmp_path)
    good = tmp_path / "ok"
    good.mkdir()
    (good / "bad name.png").write_bytes(PNG_MAGIC)
    with pytest.raises(ValueError):
        load_bundled(good)


def test_the_tournament_list_and_the_crest_route_serve_it(tmp_path, clock) -> None:
    from fastapi.testclient import TestClient

    from miniapp.app import create_app
    from miniapp.tests.conftest import fresh, login
    from miniapp.tests.test_sports import KEY, FakeTransport, base_settings
    from miniapp.sports.service import build_service

    settings = base_settings(tmp_path, football_data_key=KEY, football_data_competitions=("PL", "UNL", "TSL", "RFPL"))
    service = build_service(settings, clock=clock, transport=FakeTransport(), warm_emblems=False)
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    assert login(client, fresh(clock)).status_code == 200
    items = {t["id"]: t for t in client.get("/api/sports/football/tournaments").json()["tournaments"]}
    assert items["fd:UNL"]["emblem"] == "/api/crests/competition/UNL"
    r = client.get("/api/crests/competition/UNL")
    assert r.status_code == 200 and r.content == (BUNDLED_DIR / "UNL.png").read_bytes()
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=3600"   # a later provider emblem replaces it within the hour
