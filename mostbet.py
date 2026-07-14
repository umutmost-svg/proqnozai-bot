import asyncio
import logging
import re as _re
import time
from datetime import datetime, timedelta, timezone

import httpx

from config import (MOSTBET_BASE, MOSTBET_CACHE_TTL, MOSTBET_ODDS_TTL,
                    MOSTBET_ODDS_EMPTY_TTL, MOSTBET_SRC_TZ, mostbet_cache,
                    _mostbet_lock)

logger = logging.getLogger(__name__)

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


# Virtual football/eSports feeds appear in Mostbet's football category and can
# otherwise crowd out real matches in the user-facing tournament list.
#
# NOTE: no bare "fifa 2" token here on purpose. EA dropped the "FIFA" brand
# after the 2023 game (current esports feeds use "FC 24"/"FC 25"/"FC 26",
# already covered below), and "fifa 2" as a 6-char substring matches "FIFA"
# immediately followed by ANY year starting with 2 — i.e. every FIFA World
# Cup from 2000 to 2999 ("FIFA 2026", "FIFA 2030", ...). That silently wiped
# real World Cup tournaments from the match list.
_VIRTUAL_MATCH_KEYWORDS = (
    "electronic game", "electronic games", "esports", "esport",
    "esportsbattle", "e-sports", "efootball", "cyber football",
    "cyberfootball", "fc 24", "fc 25", "fc 26",
    "2x3 min", "2x4 min", "2x5 min", "h2h liga",
)


def _is_virtual_match(m: dict) -> bool:
    text = " ".join(str(m.get(k) or "") for k in (
        "lineCategory", "lineSuperCategory", "lineSubCategory",
        "team1Title", "team2Title", "matchTitle",
    )).lower()
    return any(kw in text for kw in _VIRTUAL_MATCH_KEYWORDS)


# Market groups that must never feed the MAIN 1X2/totals/BTTS/DC fields: they
# share keywords ("total", "winner", "both"…) with the main markets but price a
# different thing entirely (corners, cards, individual/team totals, players…).
_NON_MAIN_GROUPS = (
    "corner", "card", "booking", "yellow", "individual", "team total",
    "player", "penalt", "offside", "throw", "goal kick", "shot", "foul",
    "substitut",
)


def _is_outright_market(m: dict) -> bool:
    """Tournament outright / futures markets ("Cup. Winner", top scorer, etc.)
    list a placeholder second team ("?") instead of a real opponent — per the
    Oddschecker API docs these are not head-to-head matches and must never
    reach match-selection UI or be treated as a team name."""
    t2 = (m.get("team2Title") or "").strip()
    return t2 in ("", "?")


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
        _LOAD_TIMEOUT = 300       # max seconds for the entire paginated fetch
        _PAGE_LIMIT = 100         # matches per page (API max is 100)
        _PAGE_SLEEP = 1.0         # pause between pages to avoid 429
        _MAX_429 = 6              # give up after this many consecutive rate-limits
        try:
            deadline = asyncio.get_event_loop().time() + _LOAD_TIMEOUT
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
                last_id = 0
                page = 0
                rate_hits = 0
                while True:
                    if asyncio.get_event_loop().time() > deadline:
                        logger.warning(f"Mostbet load deadline reached at page {page} "
                                       f"({len(all_matches)} matches so far) — stopping")
                        break
                    if page > 0:
                        await asyncio.sleep(_PAGE_SLEEP)
                    page += 1
                    r = await h.get(
                        f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                        headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                        params={"lastId": last_id, "locale": "en", "limit": _PAGE_LIMIT}
                    )
                    if r.status_code == 429:
                        rate_hits += 1
                        retry_after = min(int(r.headers.get("Retry-After", 15)), 30)
                        logger.warning(f"Mostbet 429 on page {page} (hit {rate_hits}/{_MAX_429}) "
                                       f"— waiting {retry_after}s, kept {len(all_matches)} so far")
                        # Don't throw away fresh progress: only bail to stale cache if
                        # we have collected nothing yet AND keep hitting the limit.
                        if not all_matches and rate_hits >= _MAX_429 and cache_key in mostbet_cache:
                            _, stale = mostbet_cache[cache_key]
                            logger.info(f"Returning stale cache: {len(stale)} matches")
                            return stale
                        if rate_hits >= _MAX_429:
                            logger.warning("Mostbet 429 limit reached — stopping with partial data")
                            break
                        page -= 1  # retry the same page
                        await asyncio.sleep(retry_after)
                        continue
                    rate_hits = 0
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
                    last_id = matches[-1]["id"]
                    all_matches.extend(matches)
                    logger.info(f"Mostbet page {page}: {len(matches)} matches "
                                f"(total: {len(all_matches)})")
                    if len(matches) < _PAGE_LIMIT:
                        break
        except Exception as e:
            logger.error(f"_mostbet_load_matches: {e}")
            if not all_matches and cache_key in mostbet_cache:
                _, stale = mostbet_cache[cache_key]
                return stale

        if all_matches:
            mostbet_cache[cache_key] = (time.time(), all_matches)
            logger.info(f"Mostbet cache updated: {len(all_matches)} total matches")
        return all_matches


def _is_within_week(match_date_str: str, days: int = 7) -> bool:
    """Check if match is within the next `days` days (default 7) or live."""
    if not match_date_str:
        return True  # unknown date - include
    try:
        # Format: "01.06.2025 19:00:00" or "2025-06-01T19:00:00"
        ds = match_date_str.strip()
        if "T" in ds:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)  # assume UTC, same as _fmt_dt
        elif "." in ds:
            # Same source zone as display formatting (_fmt_dt), or the window
            # would be shifted by MOSTBET_SRC_TZ hours relative to shown times.
            dt = datetime.strptime(ds[:16], "%d.%m.%Y %H:%M")
            dt = dt.replace(tzinfo=timezone(timedelta(hours=MOSTBET_SRC_TZ)))
        else:
            return True
        now_utc = datetime.now(timezone.utc)
        delta = (dt - now_utc).total_seconds()
        return -3600 <= delta <= days * 24 * 3600  # from 1hr ago to `days` ahead
    except Exception:
        return True  # parse error - include


async def mostbet_find_match(team1: str, team2: str) -> dict | None:
    """Search match in Mostbet by team names using fuzzy token matching."""
    try:
        all_matches = await _mostbet_load_matches()
        matches = [m for m in all_matches
                   if not _is_virtual_match(m) and not _is_outright_market(m)
                   and (m.get("isLive") or _is_within_week(m.get("matchBeginAt", "")))]
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
        # Freshness depends on content: real values live MOSTBET_ODDS_TTL (odds
        # move much faster than the match list); an EMPTY result (failed fetch
        # or no oddschecker outcomes) expires quickly, so one network hiccup
        # can never pin "no odds" on a match for the full list TTL.
        ttl = (MOSTBET_ODDS_TTL if any(v is not None for v in data.values())
               else MOSTBET_ODDS_EMPTY_TTL)
        if now - ts < ttl:
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
    # Collect all outcomes across pages. API caps limit at 100 and paginates
    # via lastId (cursor = last outcomeId); only "Oddschecker"-labelled
    # outcomes are returned, so the full set is usually small.
    outcomes: list = []
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as h:
            last_id = 0
            for _page in range(10):  # safety cap: up to 1000 outcomes
                r = None
                for attempt in range(3):
                    r = await h.get(
                        f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/{line_id}/outcomes/list",
                        headers={"Accept": "application/json"},
                        params={"lastId": last_id, "locale": "en", "limit": 100}
                    )
                    if r.status_code == 429:
                        retry_after = min(int(r.headers.get("Retry-After", 10)), 30)
                        logger.warning(f"Mostbet odds 429 (attempt {attempt+1}) — waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    break
                if not r or r.status_code != 200:
                    logger.error(f"Mostbet odds error: {r.status_code if r else 'no response'}")
                    break
                try:
                    page = r.json().get("lineMatchOutcomes", [])
                except Exception:
                    logger.error(f"Mostbet odds invalid JSON for line_id={line_id}")
                    break
                if not page:
                    break
                outcomes.extend(page)
                if len(page) < 100:
                    break
                last_id = page[-1].get("outcomeId", 0)
                if not last_id:
                    break

        if outcomes:
            # Handicap lines collected as {abs_value: {"w1": (val, odd), "w2": (val, odd)}}
            # so the W1/W2 pair is always read from the SAME line.
            handicaps: dict = {}

            for o in outcomes:
                title = (o.get("outcomeTitle") or "").lower().strip()
                group = (o.get("groupTitle") or "").lower().strip()
                try:
                    odd_f = float(o.get("odd", ""))
                except Exception:
                    continue

                # Non-goal side markets share keywords with the main ones
                # ("Total corners" contains "total", "Individual total" too…)
                # and used to OVERWRITE the real 1X2/totals fields — the bot
                # then showed a line that differed from the site. Skip them.
                if any(k in group for k in _NON_MAIN_GROUPS):
                    continue

                # Half-time markets must be detected first so the generic "result"
                # / "1x2" keywords below don't swallow them into full-time fields.
                is_half = any(k in group for k in ("1st half", "first half", "halftime",
                                                   "half time", "ht ", "1st period"))

                # ── 1st-half 1X2 ──────────────────────────────────────────────
                if is_half and any(k in group for k in ("result", "1x2", "winner", "1x 2")):
                    if title in ("1", "w1", "home") or "(1)" in title:
                        result["h1_w1"] = odd_f
                    elif title in ("x", "draw"):
                        result["h1_x"] = odd_f
                    elif title in ("2", "w2", "away") or "(2)" in title:
                        result["h1_w2"] = odd_f

                # ── Full-time 1X2 ─────────────────────────────────────────────
                elif (not is_half) and any(k in group for k in
                                           ("winner", "match result", "1x2", "result", "match winner")):
                    if title in ("1", "w1", "home") or "(1)" in title:
                        result["w1"] = odd_f
                    elif title in ("x", "draw", "tie"):
                        result["x"] = odd_f
                    elif title in ("2", "w2", "away") or "(2)" in title:
                        result["w2"] = odd_f

                # ── Double chance ─────────────────────────────────────────────
                elif (not is_half) and any(k in group for k in ("double chance", "double result")):
                    if "1x" in title or "home or draw" in title:
                        result["dc_1x"] = odd_f
                    elif "12" in title or "home or away" in title:
                        result["dc_12"] = odd_f
                    elif "x2" in title or "draw or away" in title:
                        result["dc_x2"] = odd_f

                # ── Draw no bet ───────────────────────────────────────────────
                elif (not is_half) and any(k in group for k in ("draw no bet", "dnb", "moneyline")):
                    if title in ("1", "w1", "home"):
                        result["dnb_w1"] = odd_f
                    elif title in ("2", "w2", "away"):
                        result["dnb_w2"] = odd_f

                # ── Handicap ──────────────────────────────────────────────────
                elif (not is_half) and any(k in group for k in
                                           ("handicap", "asian handicap", "european handicap")):
                    m = _re.search(r'\(([+-]?\d+\.?\d*)\)', title)
                    if m:
                        val = float(m.group(1))
                        if val != 0:
                            slot = handicaps.setdefault(abs(val), {})
                            if title.startswith("1") or "home" in title or "w1" in title:
                                slot["w1"] = (val, odd_f)
                            elif title.startswith("2") or "away" in title or "w2" in title:
                                slot["w2"] = (val, odd_f)

                # ── Totals ────────────────────────────────────────────────────
                elif any(k in group for k in ("total", "goals", "total goals", "over/under")):
                    is_over = any(k in title for k in ("over", "more", "больше")) or "(+)" in title
                    is_under = any(k in title for k in ("under", "less", "меньше")) or "(-)" in title
                    for line, over_key, under_key in [
                        ("0.5",  "h1_over05" if is_half else None, "h1_under05" if is_half else None),
                        ("1.5",  "h1_over15" if is_half else "over15", "h1_under15" if is_half else "under15"),
                        ("2.5",  None if is_half else "over25", None if is_half else "under25"),
                        ("3.5",  None if is_half else "over35", None if is_half else "under35"),
                    ]:
                        # Match the line as a whole token, so "0.5" doesn't match "10.5".
                        if _re.search(rf'(?<!\d){_re.escape(line)}', title):
                            if is_over and over_key:
                                result[over_key] = odd_f
                            elif is_under and under_key:
                                result[under_key] = odd_f
                            break

                # ── BTTS ──────────────────────────────────────────────────────
                elif (not is_half) and any(k in group for k in ("both", "btts", "gg", "обе", "goals both")):
                    if any(k in title for k in ("yes", "да", "gg")):
                        result["btts_yes"] = odd_f
                    elif any(k in title for k in ("no", "нет", "ng")):
                        result["btts_no"] = odd_f

            # Resolve handicap: pick the most balanced line (smallest |value|) that
            # has BOTH sides, falling back to the smallest line with at least W1.
            if handicaps:
                both = {v: s for v, s in handicaps.items() if "w1" in s and "w2" in s}
                pool = both or {v: s for v, s in handicaps.items() if "w1" in s}
                if pool:
                    slot = pool[min(pool)]
                    result["hcp_val"] = slot["w1"][0]
                    result["hcp_w1"] = slot["w1"][1]
                    if "w2" in slot:
                        result["hcp_w2"] = slot["w2"][1]

    except Exception as e:
        logger.error(f"mostbet_get_odds: {e}")
    mostbet_cache[f"odds_{line_id}"] = (time.time(), result)
    return result


def format_odds_compact(odds: dict) -> str:
    """One-line, language-neutral summary of a match's REAL odds for prompts
    (express flow). Emits only markets actually present; "" when none are.
    The model translates market names itself — values are passed as data."""
    parts = []
    if odds.get("w1"):
        seg = f"1X2: {odds['w1']}"
        if odds.get("x"):
            seg += f"/{odds['x']}"
        if odds.get("w2"):
            seg += f"/{odds['w2']}"
        parts.append(seg)
    if odds.get("over25") and odds.get("under25"):
        parts.append(f"O/U 2.5: {odds['over25']}/{odds['under25']}")
    if odds.get("btts_yes") and odds.get("btts_no"):
        parts.append(f"BTTS Y/N: {odds['btts_yes']}/{odds['btts_no']}")
    if odds.get("dc_1x") or odds.get("dc_12") or odds.get("dc_x2"):
        dc = "/".join(str(odds[k]) if odds.get(k) else "-"
                      for k in ("dc_1x", "dc_12", "dc_x2"))
        parts.append(f"DC 1X/12/X2: {dc}")
    if odds.get("hcp_w1") and odds.get("hcp_val") is not None:
        sign = "+" if odds["hcp_val"] > 0 else ""
        seg = f"H1({sign}{odds['hcp_val']}): {odds['hcp_w1']}"
        if odds.get("hcp_w2"):
            seg += f" | H2({-odds['hcp_val']:g}): {odds['hcp_w2']}"
        parts.append(seg)
    return " · ".join(parts)


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
