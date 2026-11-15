"""Manual live check of the AI analysis — NOT part of pytest or CI. Costs one Claude call.

    MINIAPP_FOOTBALL_DATA_KEY=... MINIAPP_ANTHROPIC_API_KEY=... \\
        miniapp/.venv/bin/python -m miniapp.ai.live_check

Picks the first upcoming match of the next 48 hours (the API's longest range), builds the verified basis
(table + recent form) and asks the model once. Prints readiness, the numbers the
model was given and its answer — never keys or URLs. Keys come only from the
MINIAPP_* variables (not the bot's .env).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from miniapp.settings import DEFAULT_COMPETITIONS
from miniapp.sports.client import UpstreamError
from miniapp.sports.models import FootballEvent
from miniapp.sports.service import build_service

from .analysis import AiError, build_ai_service


async def main() -> int:
    football_key = os.environ.get("MINIAPP_FOOTBALL_DATA_KEY", "").strip()
    anthropic_key = os.environ.get("MINIAPP_ANTHROPIC_API_KEY", "").strip()
    if not football_key or not anthropic_key:
        print("MINIAPP_FOOTBALL_DATA_KEY and MINIAPP_ANTHROPIC_API_KEY are required: no live call made.")
        return 2
    codes = tuple(c.strip().upper() for c in os.environ.get("MINIAPP_FOOTBALL_DATA_COMPETITIONS", DEFAULT_COMPETITIONS).split(","))
    settings = SimpleNamespace(
        football_data_key=football_key, football_data_competitions=codes, football_data_per_minute=8,
        football_data_per_day=50, football_data_live_plan=False, sports_unknown_lookups_per_hour=5,
        anthropic_api_key=anthropic_key, ai_model=os.environ.get("MINIAPP_AI_MODEL", "claude-opus-5-5"),
        ai_per_user_daily=1, ai_daily_budget=1,
    )
    sports = build_service(settings, clock=time.time)
    ai = build_ai_service(settings, sports, clock=time.time)
    now = datetime.now(UTC)
    try:
        listing = await sports.events("football", now, now + timedelta(hours=48), None)
        event = next((e for e in listing.events if isinstance(e, FootballEvent) and e.status == "upcoming"), None)
        if event is None:
            print("no upcoming football match in the next 48 hours")
            return 1
        print(f"event {event.id} {event.starts_at} {event.home.name} – {event.away.name}")
        result = await ai.analyse(event.id, user_id=0)
    except (AiError, UpstreamError) as exc:
        print(f"error: {exc.code}")
        return 1
    if result is None:
        print("event not found")
        return 1
    print(f"readiness: {result.kind} · reasons {list(result.reasons)} · estimated {result.estimated} · model {result.model}")
    for side in ("home", "away"):
        if result.basis:
            print(f"  basis {side}: {result.basis[side]}")
    if result.probabilities:
        print(f"probabilities: {result.probabilities}")
        print(f"factors: {list(result.factors)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
