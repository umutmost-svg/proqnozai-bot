"""Mostbet Odds Checker API, read by the Mini App itself (docs/miniapp/odds-catalog.md).

The bot is going to be retired, so the Mini App fetches the line list and the prices on its own. The API is
IP-whitelisted (the same server as the bot). The list's kickoff times are in UTC+3 (SRC_TZ_HOURS) and are converted
to UTC here. The outcome parsing is the bot's (mostbet.py, release/bot-odds), proven in production, into the same
feed keys `book.read_book` already reads. Every request is paced by the caller; any 429 is reported as such so
the caller pauses (the bot's users keep the API's budget first while both run).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

from miniapp.sports.client import _OPENER, _read_with_deadline

logger = logging.getLogger("miniapp.odds")

BASE = "https://mostbet2.com/api/v3/advertiser/oddschecker"
SRC_TZ_HOURS = 3  # the list gives "DD.MM.YYYY HH:MM:SS" in UTC+3
PAGE_LIMIT = 100
MAX_LIST_PAGES = 60
MAX_OUTCOME_PAGES = 10
TIMEOUT_S = 15.0
MAX_PAGE_BYTES = 4 * 1024 * 1024
_THREADS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mostbet")

# Virtual / e-sports feeds sit in the football category (the bot's list, unchanged).
_VIRTUAL = ("electronic game", "electronic games", "esports", "esport", "esportsbattle", "e-sports", "efootball",
            "cyber football", "cyberfootball", "fc 24", "fc 25", "fc 26", "2x3 min", "2x4 min", "2x5 min", "h2h liga")
_NON_MAIN_GROUPS = ("corner", "card", "booking", "yellow", "individual", "team total", "player", "penalt", "offside",
                    "throw", "goal kick", "shot", "foul", "substitut")


class RateLimited(Exception):
    """The API answered 429: the caller pauses all its requests."""


class SourceError(Exception):
    """Any other failure (network, status, unreadable page): the step is skipped, nothing partial is used."""


Fetch = Callable[[str], Awaitable[tuple[int, bytes]]]


def _get_sync(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"})
    try:
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:
            return response.status, _read_with_deadline(response, time.monotonic() + TIMEOUT_S, MAX_PAGE_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SourceError(type(exc).__name__) from None


async def http_get(url: str) -> tuple[int, bytes]:
    return await asyncio.get_running_loop().run_in_executor(_THREADS, _get_sync, url)


def _json(status: int, body: bytes, field: str) -> list:
    if status == 429:
        raise RateLimited()
    if status != 200:
        raise SourceError(f"status_{status}")
    try:
        data = json.loads(body)
    except ValueError:
        raise SourceError("bad_json") from None
    items = data.get(field) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SourceError("no_list")
    return items


def parse_kickoff(raw: object) -> float | None:
    """UTC timestamp of the list's kickoff: ISO (UTC unless zoned) or 'DD.MM.YYYY HH:MM:SS' in UTC+3."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
        if "." in text:
            dt = datetime.strptime(text[:19], "%d.%m.%Y %H:%M:%S")
            return dt.replace(tzinfo=timezone(timedelta(hours=SRC_TZ_HOURS))).timestamp()
    except (ValueError, TypeError):
        return None
    return None


def is_real_match(m: dict) -> bool:
    """A football head-to-head line: not virtual / e-sports, not an outright ('?' as the second team)."""
    if str(m.get("lineCategory") or "").strip().lower() != "football":
        return False
    text = " ".join(str(m.get(k) or "") for k in ("lineCategory", "lineSuperCategory", "lineSubCategory",
                                                   "team1Title", "team2Title", "matchTitle")).lower()
    if any(k in text for k in _VIRTUAL):
        return False
    return str(m.get("team2Title") or "").strip() not in ("", "?")


async def fetch_list(fetch: Fetch = http_get, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                     spacing_s: float = 2.0) -> list[dict]:
    """Every line of the list, read to the cursor's end; a page that fails ends the read with SourceError (a
    partial list is never used: a missing line would look 'not listed')."""
    lines: list[dict] = []
    last_id = 0
    for page in range(MAX_LIST_PAGES):
        if page:
            await sleep(spacing_s)
        query = urllib.parse.urlencode({"lastId": last_id, "locale": "en", "limit": PAGE_LIMIT})
        items = _json(*await fetch(f"{BASE}/line/list?{query}"), "lineMatches")
        lines.extend(m for m in items if isinstance(m, dict))
        if len(items) < PAGE_LIMIT:
            return lines
        last = items[-1].get("id") if isinstance(items[-1], dict) else None
        if not isinstance(last, int) or isinstance(last, bool) or last <= last_id:
            raise SourceError("bad_cursor")
        last_id = last
    raise SourceError("page_cap")


async def fetch_odds(line_id: int, fetch: Fetch = http_get, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                     spacing_s: float = 2.0) -> dict[str, float]:
    """The line's prices in feed keys (w1, x, w2, dc_1x, ...), read in full; partial reads raise."""
    outcomes: list[dict] = []
    last_id = 0
    for page in range(MAX_OUTCOME_PAGES):
        if page:
            await sleep(spacing_s)
        query = urllib.parse.urlencode({"lastId": last_id, "locale": "en", "limit": 100})
        items = _json(*await fetch(f"{BASE}/line/{int(line_id)}/outcomes/list?{query}"), "lineMatchOutcomes")
        outcomes.extend(o for o in items if isinstance(o, dict))
        if len(items) < 100:
            return parse_outcomes(outcomes)
        last_id = items[-1].get("outcomeId", 0) if isinstance(items[-1], dict) else 0
        if not last_id:
            raise SourceError("bad_cursor")
    raise SourceError("page_cap")


def parse_outcomes(outcomes: list[dict]) -> dict[str, float]:
    """The bot's parsing (release/bot-odds mostbet.mostbet_get_odds), main markets only."""
    result: dict[str, float] = {}
    handicaps: dict[float, dict[str, tuple[float, float]]] = {}
    for o in outcomes:
        title = str(o.get("outcomeTitle") or "").lower().strip()
        group = str(o.get("groupTitle") or "").lower().strip()
        try:
            odd = float(o.get("odd", ""))
        except (TypeError, ValueError):
            continue
        if any(k in group for k in _NON_MAIN_GROUPS):
            continue
        is_half = any(k in group for k in ("1st half", "first half", "halftime", "half time", "ht ", "1st period"))
        if is_half:
            continue  # half-time markets are not served
        if any(k in group for k in ("winner", "match result", "1x2", "result", "match winner")):
            if title in ("1", "w1", "home") or "(1)" in title:
                result["w1"] = odd
            elif title in ("x", "draw", "tie"):
                result["x"] = odd
            elif title in ("2", "w2", "away") or "(2)" in title:
                result["w2"] = odd
        elif any(k in group for k in ("double chance", "double result")):
            if "1x" in title or "home or draw" in title:
                result["dc_1x"] = odd
            elif "12" in title or "home or away" in title:
                result["dc_12"] = odd
            elif "x2" in title or "draw or away" in title:
                result["dc_x2"] = odd
        elif any(k in group for k in ("draw no bet", "dnb", "moneyline")):
            if title in ("1", "w1", "home"):
                result["dnb_w1"] = odd
            elif title in ("2", "w2", "away"):
                result["dnb_w2"] = odd
        elif any(k in group for k in ("handicap", "asian handicap", "european handicap")):
            m = re.search(r"\(([+-]?\d+\.?\d*)\)", title)
            if m and float(m.group(1)) != 0:
                val = float(m.group(1))
                slot = handicaps.setdefault(abs(val), {})
                if title.startswith("1") or "home" in title or "w1" in title:
                    slot["w1"] = (val, odd)
                elif title.startswith("2") or "away" in title or "w2" in title:
                    slot["w2"] = (val, odd)
        elif any(k in group for k in ("total", "goals", "total goals", "over/under")):
            is_over = any(k in title for k in ("over", "more", "больше")) or "(+)" in title
            is_under = any(k in title for k in ("under", "less", "меньше")) or "(-)" in title
            for line, over_key, under_key in (("1.5", "over15", "under15"), ("2.5", "over25", "under25"),
                                              ("3.5", "over35", "under35")):
                if re.search(rf"(?<!\d){re.escape(line)}", title):
                    if is_over:
                        result[over_key] = odd
                    elif is_under:
                        result[under_key] = odd
                    break
        elif any(k in group for k in ("both", "btts", "gg", "обе", "goals both")):
            if any(k in title for k in ("yes", "да", "gg")):
                result["btts_yes"] = odd
            elif any(k in title for k in ("no", "нет", "ng")):
                result["btts_no"] = odd
    both = {v: s for v, s in handicaps.items() if "w1" in s and "w2" in s}
    pool = both or {v: s for v, s in handicaps.items() if "w1" in s}
    if pool:
        slot = pool[min(pool)]
        result["hcp_val"] = slot["w1"][0]
        result["hcp_w1"] = slot["w1"][1]
        if "w2" in slot:
            result["hcp_w2"] = slot["w2"][1]
    return result
