"""Manual live check of the sports provider — NOT part of pytest or CI.

    MINIAPP_FOOTBALL_DATA_KEY=... miniapp/.venv/bin/python -m miniapp.live_check [YYYY-MM-DD]

At most two upstream requests (one day of matches, then one match by id).
Prints only normalized results and quota counters: never the key, never URLs.
The key is read from MINIAPP_FOOTBALL_DATA_KEY only (not the bot's .env).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import UTC, date, datetime

from miniapp.sports import football_data
from miniapp.sports.client import ClientLimits, ProviderClient, UpstreamError
from miniapp.sports.football_data import FootballDataAdapter


async def main(day: date) -> int:
    key = os.environ.get("MINIAPP_FOOTBALL_DATA_KEY", "").strip()
    if not key:
        print("MINIAPP_FOOTBALL_DATA_KEY is not set: no live call made.")
        return 2
    codes = tuple(c.strip().upper() for c in os.environ.get("MINIAPP_FOOTBALL_DATA_COMPETITIONS", "PL,PD").split(","))
    client = ProviderClient(football_data.PROVIDER, football_data.BASE_URL, {"X-Auth-Token": key},
                            ClientLimits(per_minute=2, per_day=2, retries=0))
    adapter = FootballDataAdapter(client, codes, lambda: datetime.now(UTC).timestamp())
    try:
        events, ttl = await adapter.day(day)
    except UpstreamError as exc:
        print(f"provider error: {exc.code} (HTTP {exc.status})")
        return 1
    print(f"{football_data.PROVIDER} · {day.isoformat()} UTC · competitions {','.join(codes)}")
    print(f"events: {len(events)} · statuses: {dict(Counter(e.status for e in events))} · cache ttl {int(ttl)} s")
    for e in events[:10]:
        score = f"{e.score[0]}:{e.score[1]}" if e.score else "–"
        minute = f" {e.minute}'" if e.minute is not None else ""
        print(f"  {e.id} {e.tournament_id} {e.starts_at} {e.status:9} {e.home.name} – {e.away.name} {score}{minute}")
    if events:
        detail, _ = await adapter.match(int(events[0].id.split(":")[1]))
        print(f"detail {events[0].id}: {'ok' if detail else 'not found'}"
              f" · minute field {'present' if detail and detail.minute is not None else 'absent'}"
              f" · statistics {detail.stats_availability if detail else '-'}")
    print(f"quota: {client.usage()}")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else datetime.now(UTC).date().isoformat()
    sys.exit(asyncio.run(main(date.fromisoformat(arg))))
