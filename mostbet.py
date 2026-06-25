import asyncio
import logging
import re as _re
import time
from datetime import datetime

import httpx

from config import MOSTBET_BASE, MOSTBET_CACHE_TTL, mostbet_cache, _mostbet_lock

logger = logging.getLogger(__name__)

# ─── Tournament name normalisation ───────────────────────────────────────────

_TOURNAMENT_MAP = {
    # Football — club
    "epl": "Premier League (England)",
    "english premier league": "Premier League (England)",
    "premier league": "Premier League (England)",
    "laliga": "La Liga (Spain)",
    "la liga": "La Liga (Spain)",
    "bundesliga": "Bundesliga (Germany)",
    "serie a": "Serie A (Italy)",
    "ligue 1": "Ligue 1 (France)",
    "eredivisie": "Eredivisie (Netherlands)",
    "primeira liga": "Primeira Liga (Portugal)",
    "süper lig": "Süper Lig (Turkey)",
    "super lig": "Süper Lig (Turkey)",
    "championship": "Championship (England)",
    "liga nos": "Primeira Liga (Portugal)",
    "scottish premiership": "Scottish Premiership",
    "mls": "MLS (USA)",
    "brasileirao": "Brasileirão (Brazil)",
    "serie b": "Serie B (Italy)",
    "2. bundesliga": "2. Bundesliga (Germany)",
    "ligue 2": "Ligue 2 (France)",
    "liga 2": "Liga 2",
    # Football — European
    "ucl": "UEFA Champions League",
    "champions league": "UEFA Champions League",
    "uefa champions league": "UEFA Champions League",
    "uel": "UEFA Europa League",
    "europa league": "UEFA Europa League",
    "uefa europa league": "UEFA Europa League",
    "uecl": "UEFA Conference League",
    "conference league": "UEFA Conference League",
    # Football — national teams
    "world cup": "FIFA World Cup",
    "euro": "UEFA Euro",
    "copa america": "Copa América",
    "africa cup": "Africa Cup of Nations",
    "afcon": "Africa Cup of Nations",
    "nations league": "UEFA Nations League",
    # Basketball
    "nba": "NBA (USA)",
    "euroleague": "EuroLeague",
    "eurocup": "EuroCup",
    "vtb united league": "VTB United League",
    # Tennis
    "atp": "ATP Tour",
    "wta": "WTA Tour",
    "wimbledon": "Wimbledon",
    "us open": "US Open (Tennis)",
    "french open": "Roland Garros",
    "roland garros": "Roland Garros",
    "australian open": "Australian Open",
    # MMA / Boxing
    "ufc": "UFC",
    "bellator": "Bellator MMA",
    # Hockey
    "nhl": "NHL (USA/Canada)",
    "khl": "KHL",
    # Cybersport
    "cs2": "CS2",
    "csgo": "CS:GO",
    "dota 2": "Dota 2",
    "lol": "League of Legends",
    "valorant": "Valorant",
}

_tournament_norm_cache: dict[str, str] = {}

def normalize_tournament(raw: str) -> str:
    """Clean up a tournament name. Trusts Mostbet's name — only expands
    exact short abbreviations, never rewrites a descriptive name."""
    if not raw:
        return raw
    key = raw.strip().lower()
    # Only expand when the WHOLE name is a known abbreviation (exact match).
    # Substring matching is unsafe: "World Cup. CONCACAF" must NOT become
    # "FIFA World Cup" and lose the qualifier context.
    if key in _TOURNAMENT_MAP:
        return _TOURNAMENT_MAP[key]
    # Otherwise keep Mostbet's name as-is (lightly cleaned).
    return raw.strip()


async def normalize_tournament_ai(raw: str) -> str:
    """Normalize a tournament name. Mostbet names are already accurate, so we
    only translate to a clean English form, preserving qualifier/region context."""
    if not raw:
        return raw
    key = raw.strip().lower()
    # Exact-match abbreviations expand instantly
    if key in _TOURNAMENT_MAP:
        return _TOURNAMENT_MAP[key]
    # Already plain ASCII English? Trust it as-is, no API call needed.
    if raw.isascii():
        return raw.strip()
    # Cache check
    if key in _tournament_norm_cache:
        return _tournament_norm_cache[key]
    # Non-Latin name → translate to English, but KEEP all context (region, stage)
    try:
        from claude_client import _create_with_retry
        r = await _create_with_retry(
            model="claude-haiku-4-5-20251001", max_tokens=40,
            messages=[{"role": "user", "content":
                f'Translate this sports tournament name to standard English. '
                f'Keep ALL details (region, qualification stage, division). '
                f'Do NOT replace it with a different competition. '
                f'Return ONLY the name.\nInput: "{raw}"'}]
        )
        if r.content:
            result = r.content[0].text.strip().strip('"')
            if result:
                _tournament_norm_cache[key] = result
                return result
    except Exception as e:
        logger.warning(f"normalize_tournament_ai: {e}")
    _tournament_norm_cache[key] = raw.strip()
    return raw.strip()


_NOISE = {"fc", "cf", "ac", "sc", "afc", "fk", "sk", "bk", "rsc", "rc", "ud", "cd", "sd",
          "fútbol", "club", "sporting", "atletico", "atletik", "united", "city", "the"}

def _norm_tokens(name: str) -> set:
    name = name.lower()
    name = _re.sub(r"[^\w\s]", " ", name)
    return {t for t in name.split() if len(t) > 1 and t not in _NOISE}

def _fuzzy_score(q_tokens: set, cand: str) -> float:
    c_tokens = _norm_tokens(cand)
    if not q_tokens or not c_tokens:
        return 0.0
    common = q_tokens & c_tokens
    return len(common) / max(len(q_tokens), len(c_tokens))


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
        _LOAD_TIMEOUT = 120  # max seconds for the entire paginated fetch
        try:
            deadline = asyncio.get_event_loop().time() + _LOAD_TIMEOUT
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
                last_id = 0
                page = 0
                while True:
                    if asyncio.get_event_loop().time() > deadline:
                        logger.warning("Mostbet load deadline reached — stopping pagination")
                        break
                    if page > 0:
                        await asyncio.sleep(2.0)
                    page += 1
                    r = await h.get(
                        f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                        headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                        params={"lastId": last_id, "locale": "en", "limit": 100}
                    )
                    if r.status_code == 429:
                        # Honour Retry-After if present, otherwise fall back to 30s
                        retry_after = int(r.headers.get("Retry-After", 30))
                        retry_after = min(retry_after, 60)  # cap at 60s
                        logger.warning(f"Mostbet 429 on page {page} — waiting {retry_after}s")
                        if cache_key in mostbet_cache:
                            _, stale = mostbet_cache[cache_key]
                            logger.info(f"Returning stale cache: {len(stale)} matches")
                            return stale
                        await asyncio.sleep(retry_after)
                        continue
                    if r.status_code != 200:
                        logger.error(f"Mostbet list error: {r.status_code} | {r.text[:100]}")
                        break
                    try:
                        matches = r.json().get("lineMatches", [])
                    except Exception:
                        logger.error(f"Mostbet invalid JSON on page {page}")
                        break
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
    """Search match in Mostbet by team names using fuzzy token matching."""
    try:
        all_matches = await _mostbet_load_matches()
        matches = [m for m in all_matches
                   if m.get("isLive") or _is_within_week(m.get("matchBeginAt", ""))]
        logger.info(f"Mostbet filtered: {len(matches)}/{len(all_matches)} within 7 days")

        t1 = team1.strip(); t2 = team2.strip()
        if not t1 or not t2 or t1.lower() == t2.lower():
            return None

        t1_tok = _norm_tokens(t1); t2_tok = _norm_tokens(t2)
        best_score = 0.0; best_match = None

        for m in matches:
            m1 = m.get("team1Title", ""); m2 = m.get("team2Title", "")
            # Normal order
            s = min(_fuzzy_score(t1_tok, m1), _fuzzy_score(t2_tok, m2))
            # Reversed order
            sr = min(_fuzzy_score(t1_tok, m2), _fuzzy_score(t2_tok, m1))
            score = max(s, sr)
            if score > best_score:
                best_score = score; best_match = m

        if best_score >= 0.5:
            logger.info(f"Mostbet fuzzy match (score={best_score:.2f}): "
                        f"{best_match.get('team1Title')} vs {best_match.get('team2Title')}")
            return best_match
        logger.info(f"Mostbet no match for '{t1}' vs '{t2}' (best={best_score:.2f})")
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
        # 1X2
        "w1": None, "x": None, "w2": None,
        # Double chance
        "dc_1x": None, "dc_12": None, "dc_x2": None,
        # Handicap (main line, e.g. -1/+1)
        "hcp_w1": None, "hcp_w2": None, "hcp_val": None,
        # Totals
        "over15": None, "under15": None,
        "over25": None, "under25": None,
        "over35": None, "under35": None,
        # BTTS
        "btts_yes": None, "btts_no": None,
        # 1st half 1X2
        "h1_w1": None, "h1_x": None, "h1_w2": None,
        # 1st half total
        "h1_over05": None, "h1_under05": None,
        "h1_over15": None, "h1_under15": None,
        # Draw no bet
        "dnb_w1": None, "dnb_w2": None,
    }
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as h:
            for attempt in range(3):
                r = await h.get(
                    f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/{line_id}/outcomes/list",
                    headers={"Accept": "application/json"},
                    params={"locale": "en", "limit": 200}
                )
                if r.status_code == 429:
                    retry_after = min(int(r.headers.get("Retry-After", 10)), 30)
                    logger.warning(f"Mostbet odds 429 (attempt {attempt+1}) — waiting {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue
                break
            if r.status_code != 200:
                logger.error(f"Mostbet odds error: {r.status_code}")
                return result
            try:
                outcomes = r.json().get("lineMatchOutcomes", [])
            except Exception:
                logger.error(f"Mostbet odds invalid JSON for line_id={line_id}")
                return result

            for o in outcomes:
                title = (o.get("outcomeTitle") or "").lower().strip()
                group = (o.get("groupTitle") or "").lower().strip()
                try:
                    odd_f = float(o.get("odd", ""))
                except Exception:
                    continue

                # ── 1X2 ──────────────────────────────────────────────────────
                if any(k in group for k in ("winner", "match result", "1x2", "result", "match winner")):
                    if title in ("1", "w1", "home") or "(1)" in title:
                        result["w1"] = odd_f
                    elif title in ("x", "draw", "tie"):
                        result["x"] = odd_f
                    elif title in ("2", "w2", "away") or "(2)" in title:
                        result["w2"] = odd_f

                # ── Double chance ─────────────────────────────────────────────
                elif any(k in group for k in ("double chance", "double result")):
                    if "1x" in title or "home or draw" in title:
                        result["dc_1x"] = odd_f
                    elif "12" in title or "home or away" in title:
                        result["dc_12"] = odd_f
                    elif "x2" in title or "draw or away" in title:
                        result["dc_x2"] = odd_f

                # ── Draw no bet ───────────────────────────────────────────────
                elif any(k in group for k in ("draw no bet", "dnb", "moneyline")):
                    if title in ("1", "w1", "home"):
                        result["dnb_w1"] = odd_f
                    elif title in ("2", "w2", "away"):
                        result["dnb_w2"] = odd_f

                # ── Handicap ──────────────────────────────────────────────────
                elif any(k in group for k in ("handicap", "asian handicap", "european handicap")):
                    # Try to capture a non-zero handicap line
                    m = _re.search(r'\(([+-]?\d+\.?\d*)\)', title)
                    if m:
                        val = float(m.group(1))
                        if val != 0 and (result["hcp_val"] is None or abs(val) < abs(result["hcp_val"])):
                            # Store the smallest absolute handicap line (most balanced)
                            if title.startswith("1") or "home" in title or "w1" in title:
                                result["hcp_w1"] = odd_f
                                result["hcp_val"] = val
                            elif title.startswith("2") or "away" in title or "w2" in title:
                                result["hcp_w2"] = odd_f

                # ── Totals ────────────────────────────────────────────────────
                elif any(k in group for k in ("total", "goals", "total goals", "over/under")):
                    is_h1 = any(k in group for k in ("1st", "first half", "halftime", "half time", "ht"))
                    is_over = any(k in title for k in ("over", "more", "+", "больше"))
                    is_under = any(k in title for k in ("under", "less", "-", "меньше"))
                    for line, over_key, under_key in [
                        ("0.5",  "h1_over05" if is_h1 else None, "h1_under05" if is_h1 else None),
                        ("1.5",  "h1_over15" if is_h1 else "over15", "h1_under15" if is_h1 else "under15"),
                        ("2.5",  None if is_h1 else "over25", None if is_h1 else "under25"),
                        ("3.5",  None if is_h1 else "over35", None if is_h1 else "under35"),
                    ]:
                        if line in title:
                            if is_over and over_key:
                                result[over_key] = odd_f
                            elif is_under and under_key:
                                result[under_key] = odd_f
                            break

                # ── 1st half 1X2 ──────────────────────────────────────────────
                elif any(k in group for k in ("1st half", "first half", "halftime", "half time", "ht result")):
                    if title in ("1", "w1", "home") or "(1)" in title:
                        result["h1_w1"] = odd_f
                    elif title in ("x", "draw"):
                        result["h1_x"] = odd_f
                    elif title in ("2", "w2", "away") or "(2)" in title:
                        result["h1_w2"] = odd_f

                # ── BTTS ──────────────────────────────────────────────────────
                elif any(k in group for k in ("both", "btts", "gg", "обе", "goals both")):
                    if any(k in title for k in ("yes", "да", "gg")):
                        result["btts_yes"] = odd_f
                    elif any(k in title for k in ("no", "нет", "ng")):
                        result["btts_no"] = odd_f

    except Exception as e:
        logger.error(f"mostbet_get_odds: {e}")
    mostbet_cache[f"odds_{line_id}"] = (time.time(), result)
    return result


def format_mostbet_odds(odds: dict, lang: str) -> str:
    """Format Mostbet odds as a clean string to inject into Claude prompt."""
    if not any(odds.get(k) for k in ("w1", "over25", "btts_yes", "dc_1x", "hcp_w1")):
        return ""

    if lang in ("kz", "uz", "tr"):
        lang = "ru"
    elif lang == "ar":
        lang = "en"

    def _line(label, *vals):
        """Return 'label: v1 | v2 ...' only when all values are present."""
        if all(v is not None for v in vals):
            return f"{label}: {' | '.join(str(v) for v in vals)}"
        return None

    if lang == "ru":
        lines = ["РЕАЛЬНЫЕ КОЭФФИЦИЕНТЫ MOSTBET:"]
        if r := _line("1X2  П1/X/П2", odds.get("w1"), odds.get("x"), odds.get("w2")):
            lines.append(r)
        if odds.get("dc_1x") or odds.get("dc_12") or odds.get("dc_x2"):
            dc = []
            if odds.get("dc_1x"): dc.append(f"1X={odds['dc_1x']}")
            if odds.get("dc_12"): dc.append(f"12={odds['dc_12']}")
            if odds.get("dc_x2"): dc.append(f"X2={odds['dc_x2']}")
            lines.append("Двойной шанс: " + " | ".join(dc))
        if odds.get("hcp_w1") and odds.get("hcp_val") is not None:
            sign = "+" if odds["hcp_val"] > 0 else ""
            lines.append(f"Фора: П1({sign}{odds['hcp_val']})={odds['hcp_w1']}" +
                         (f" | П2({-odds['hcp_val']:.1f})={odds['hcp_w2']}" if odds.get("hcp_w2") else ""))
        if odds.get("over15") and odds.get("under15"):
            lines.append(f"Тотал 1.5: Б={odds['over15']} | М={odds['under15']}")
        if odds.get("over25") and odds.get("under25"):
            lines.append(f"Тотал 2.5: Б={odds['over25']} | М={odds['under25']}")
        if odds.get("over35") and odds.get("under35"):
            lines.append(f"Тотал 3.5: Б={odds['over35']} | М={odds['under35']}")
        if r := _line("Обе забьют Да/Нет", odds.get("btts_yes"), odds.get("btts_no")):
            lines.append(r)
        if r := _line("1-й тайм 1X2", odds.get("h1_w1"), odds.get("h1_x"), odds.get("h1_w2")):
            lines.append(r)
        if odds.get("h1_over15") and odds.get("h1_under15"):
            lines.append(f"Тотал 1-го тайма 1.5: Б={odds['h1_over15']} | М={odds['h1_under15']}")
        if r := _line("Победа без ничьей", odds.get("dnb_w1"), odds.get("dnb_w2")):
            lines.append(r)
        lines.append("ВАЖНО: используй ЭТИ коэффициенты в прогнозе — они отражают реальную оценку рынка.")

    elif lang == "az":
        lines = ["MOSTBET REAL KEFLƏRİ:"]
        if r := _line("1X2  Q1/X/Q2", odds.get("w1"), odds.get("x"), odds.get("w2")):
            lines.append(r)
        if odds.get("dc_1x") or odds.get("dc_12") or odds.get("dc_x2"):
            dc = []
            if odds.get("dc_1x"): dc.append(f"1X={odds['dc_1x']}")
            if odds.get("dc_12"): dc.append(f"12={odds['dc_12']}")
            if odds.get("dc_x2"): dc.append(f"X2={odds['dc_x2']}")
            lines.append("İkiqat şans: " + " | ".join(dc))
        if odds.get("hcp_w1") and odds.get("hcp_val") is not None:
            sign = "+" if odds["hcp_val"] > 0 else ""
            lines.append(f"Fora: Q1({sign}{odds['hcp_val']})={odds['hcp_w1']}" +
                         (f" | Q2({-odds['hcp_val']:.1f})={odds['hcp_w2']}" if odds.get("hcp_w2") else ""))
        if odds.get("over25") and odds.get("under25"):
            lines.append(f"Total 2.5: Üstündə={odds['over25']} | Altında={odds['under25']}")
        if odds.get("over35") and odds.get("under35"):
            lines.append(f"Total 3.5: Üstündə={odds['over35']} | Altında={odds['under35']}")
        if r := _line("Hər ikisi qol B/X", odds.get("btts_yes"), odds.get("btts_no")):
            lines.append(r)
        if r := _line("1-ci yarım 1X2", odds.get("h1_w1"), odds.get("h1_x"), odds.get("h1_w2")):
            lines.append(r)
        lines.append("VACİB: proqnozda MƏHZbu keflərdən istifadə et — bunlar bazarın real qiymətləndirilməsidir.")

    else:  # en and others
        lines = ["REAL MOSTBET ODDS:"]
        if r := _line("1X2  W1/X/W2", odds.get("w1"), odds.get("x"), odds.get("w2")):
            lines.append(r)
        if odds.get("dc_1x") or odds.get("dc_12") or odds.get("dc_x2"):
            dc = []
            if odds.get("dc_1x"): dc.append(f"1X={odds['dc_1x']}")
            if odds.get("dc_12"): dc.append(f"12={odds['dc_12']}")
            if odds.get("dc_x2"): dc.append(f"X2={odds['dc_x2']}")
            lines.append("Double chance: " + " | ".join(dc))
        if odds.get("hcp_w1") and odds.get("hcp_val") is not None:
            sign = "+" if odds["hcp_val"] > 0 else ""
            lines.append(f"Handicap: W1({sign}{odds['hcp_val']})={odds['hcp_w1']}" +
                         (f" | W2({-odds['hcp_val']:.1f})={odds['hcp_w2']}" if odds.get("hcp_w2") else ""))
        if odds.get("over15") and odds.get("under15"):
            lines.append(f"Total 1.5: Over={odds['over15']} | Under={odds['under15']}")
        if odds.get("over25") and odds.get("under25"):
            lines.append(f"Total 2.5: Over={odds['over25']} | Under={odds['under25']}")
        if odds.get("over35") and odds.get("under35"):
            lines.append(f"Total 3.5: Over={odds['over35']} | Under={odds['under35']}")
        if r := _line("BTTS Yes/No", odds.get("btts_yes"), odds.get("btts_no")):
            lines.append(r)
        if r := _line("1st Half 1X2", odds.get("h1_w1"), odds.get("h1_x"), odds.get("h1_w2")):
            lines.append(r)
        if odds.get("h1_over15") and odds.get("h1_under15"):
            lines.append(f"1st Half Total 1.5: Over={odds['h1_over15']} | Under={odds['h1_under15']}")
        if r := _line("Draw No Bet W1/W2", odds.get("dnb_w1"), odds.get("dnb_w2")):
            lines.append(r)
        lines.append("IMPORTANT: use THESE exact odds in the forecast — they reflect real market assessment.")

    return "\n".join(lines)
