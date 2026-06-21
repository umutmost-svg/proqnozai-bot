import asyncio
import logging
import time
from datetime import datetime

import httpx

from config import MOSTBET_BASE, MOSTBET_CACHE_TTL, mostbet_cache, _mostbet_lock

logger = logging.getLogger(__name__)


# ─── Mostbet Odds Checker API ─────────────────────────────────────────────────

async def _mostbet_load_matches() -> list:
    """Load all matches from Mostbet with caching (15 min TTL).
    Uses lock so only one concurrent request goes to Mostbet API."""
    cache_key = "all_matches"
    now = time.time()

    # Return fresh cache without acquiring lock
    if cache_key in mostbet_cache:
        ts, data = mostbet_cache[cache_key]
        if now - ts < MOSTBET_CACHE_TTL:
            return data

    # Only one coroutine fetches at a time; others wait and reuse result
    async with _mostbet_lock:
        # Re-check cache after acquiring lock (another coroutine may have filled it)
        if cache_key in mostbet_cache:
            ts, data = mostbet_cache[cache_key]
            if now - ts < MOSTBET_CACHE_TTL:
                return data

        all_matches = []
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
                last_id = 0
                page = 0
                while True:
                    if page > 0:
                        await asyncio.sleep(2.0)   # 2s between pages
                    page += 1
                    r = await h.get(
                        f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                        headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                        params={"lastId": last_id, "locale": "en", "limit": 100}
                    )
                    if r.status_code == 429:
                        logger.warning(f"Mostbet 429 on page {page}")
                        if cache_key in mostbet_cache:
                            _, stale = mostbet_cache[cache_key]
                            logger.info(f"Returning stale cache: {len(stale)} matches")
                            return stale
                        await asyncio.sleep(10)   # longer wait on 429
                        continue
                    if r.status_code != 200:
                        logger.error(f"Mostbet list error: {r.status_code} | {r.text[:100]}")
                        break
                    matches = r.json().get("lineMatches", [])
                    if not matches:
                        break
                    all_matches.extend(matches)
                    logger.info(f"Mostbet loaded page {page}: {len(matches)} matches (total: {len(all_matches)})")
                    if len(matches) < 100:
                        break
                    last_id = matches[-1]["id"]
        except Exception as e:
            logger.error(f"_mostbet_load_matches: {e}")
            if cache_key in mostbet_cache:
                _, stale = mostbet_cache[cache_key]
                return stale

        if all_matches:
            mostbet_cache[cache_key] = (time.time(), all_matches)
            logger.info(f"Mostbet cache updated: {len(all_matches)} total matches")
        return all_matches


def _is_within_week(match_date_str: str) -> bool:
    """Check if match is within next 7 days or live."""
    if not match_date_str:
        return True  # unknown date - include
    try:
        from datetime import timezone
        # Format: "01.06.2025 19:00:00" or "2025-06-01T19:00:00"
        ds = match_date_str.strip()
        if "T" in ds:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        elif "." in ds:
            dt = datetime.strptime(ds[:16], "%d.%m.%Y %H:%M")
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            return True
        now_utc = datetime.now(timezone.utc)
        delta = (dt - now_utc).total_seconds()
        return -3600 <= delta <= 7 * 24 * 3600  # from 1hr ago to 7 days ahead
    except Exception:
        return True  # parse error - include


async def mostbet_find_match(team1: str, team2: str) -> dict | None:
    """Search match in Mostbet by team names, only within next 7 days."""
    try:
        all_matches = await _mostbet_load_matches()
        # Filter to next 7 days + live matches
        matches = [m for m in all_matches
                   if m.get("isLive") or _is_within_week(m.get("matchBeginAt", ""))]
        logger.info(f"Mostbet filtered: {len(matches)}/{len(all_matches)} within 7 days")

        t1 = team1.lower().strip(); t2 = team2.lower().strip()
        if not t1 or not t2 or t1 == t2:
            return None
        for m in matches:
            t1m = m.get("team1Title", "").lower()
            t2m = m.get("team2Title", "").lower()
            mt  = m.get("matchTitle", "").lower()
            if (t1 in t1m or t1 in mt) and (t2 in t2m or t2 in mt):
                return m
            if (t2 in t1m or t2 in mt) and (t1 in t2m or t1 in mt):
                return m
            if t1 in mt and t2 in mt:
                return m
    except Exception as e:
        logger.error(f"mostbet_find_match: {e}")
    return None


async def mostbet_get_odds(line_id: int) -> dict:
    """Get odds for a match from Mostbet with caching."""
    cache_key = f"odds_{line_id}"
    now = time.time()
    if cache_key in mostbet_cache:
        ts, data = mostbet_cache[cache_key]
        if now - ts < MOSTBET_CACHE_TTL:
            return data
    result = {
        "w1": None, "x": None, "w2": None,
        "over25": None, "under25": None,
        "btts_yes": None, "btts_no": None,
        "url": f"{MOSTBET_BASE}/line/{line_id}"
    }
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as h:
            r = await h.get(
                f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/{line_id}/outcomes/list",
                headers={"Accept": "application/json"},
                params={"locale": "en", "limit": 100}
            )
            if r.status_code != 200:
                logger.error(f"Mostbet odds error: {r.status_code}")
                return result
            outcomes = r.json().get("lineMatchOutcomes", [])
            for o in outcomes:
                title = o.get("outcomeTitle", "").lower()
                group = o.get("groupTitle", "").lower()
                odd   = o.get("odd", "")
                try:
                    odd_f = float(odd)
                except Exception:
                    continue
                # 1X2
                if group in ("winner", "match result", "1x2", "result"):
                    if "1" == title or "w1" in title or "(1)" in title:
                        result["w1"] = odd_f
                    elif "x" == title or "draw" in title or "x" in title:
                        result["x"] = odd_f
                    elif "2" == title or "w2" in title or "(2)" in title:
                        result["w2"] = odd_f
                # Total over/under 2.5
                if "2.5" in title or "2.5" in group:
                    if "over" in title or "more" in title or "больше" in title or "(+)" in title:
                        result["over25"] = odd_f
                    elif "under" in title or "less" in title or "меньше" in title or "(-)" in title:
                        result["under25"] = odd_f
                # BTTS
                if "both" in group or "btts" in group or "gg" in group or "обе" in group:
                    if "yes" in title or "да" in title:
                        result["btts_yes"] = odd_f
                    elif "no" in title or "нет" in title:
                        result["btts_no"] = odd_f
    except Exception as e:
        logger.error(f"mostbet_get_odds: {e}")
    mostbet_cache[f"odds_{line_id}"] = (time.time(), result)
    return result


def format_mostbet_odds(odds: dict, lang: str) -> str:
    """Format Mostbet odds as a clean string to inject into Claude prompt."""
    if not any([odds["w1"], odds["over25"], odds["btts_yes"]]):
        return ""
    lines = []
    # Map new langs to existing formats
    if lang in ("kz", "uz", "tr"):
        lang = "ru"
    elif lang == "ar":
        lang = "en"
    if lang == "ru":
        lines.append("РЕАЛЬНЫЕ КОЭФФИЦИЕНТЫ MOSTBET:")
        if odds["w1"] and odds["x"] and odds["w2"]:
            lines.append(f"1X2: П1={odds['w1']} | X={odds['x']} | П2={odds['w2']}")
        if odds["over25"] and odds["under25"]:
            lines.append(f"Тотал 2.5: Больше={odds['over25']} | Меньше={odds['under25']}")
        if odds["btts_yes"] and odds["btts_no"]:
            lines.append(f"Обе забьют: Да={odds['btts_yes']} | Нет={odds['btts_no']}")
        lines.append(f"Ссылка: {odds['url']}")
        lines.append("ВАЖНО: используй ИМЕННО эти коэффициенты в прогнозе, не выдумывай свои.")
    elif lang == "az":
        lines.append("MOSTBET REAL KEFLƏRİ:")
        if odds["w1"] and odds["x"] and odds["w2"]:
            lines.append(f"1X2: Q1={odds['w1']} | X={odds['x']} | Q2={odds['w2']}")
        if odds["over25"] and odds["under25"]:
            lines.append(f"Total 2.5: Üstündə={odds['over25']} | Altında={odds['under25']}")
        if odds["btts_yes"] and odds["btts_no"]:
            lines.append(f"Hər ikisi qol: Bəli={odds['btts_yes']} | Xeyr={odds['btts_no']}")
        lines.append(f"Link: {odds['url']}")
        lines.append("VACIB: proqnozda MƏHZbu kefləri istifadə et.")
    else:
        lines.append("REAL MOSTBET ODDS:")
        if odds["w1"] and odds["x"] and odds["w2"]:
            lines.append(f"1X2: W1={odds['w1']} | X={odds['x']} | W2={odds['w2']}")
        if odds["over25"] and odds["under25"]:
            lines.append(f"Total 2.5: Over={odds['over25']} | Under={odds['under25']}")
        if odds["btts_yes"] and odds["btts_no"]:
            lines.append(f"BTTS: Yes={odds['btts_yes']} | No={odds['btts_no']}")
        lines.append(f"Link: {odds['url']}")
        lines.append("IMPORTANT: use THESE exact odds in the forecast, do not invent your own.")
    return "\n".join(lines)
