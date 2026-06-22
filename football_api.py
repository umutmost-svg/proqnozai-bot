import asyncio
import json
import logging
import re
from datetime import date

import httpx

from config import APIFOOTBALL_KEY, FOOTBALL_KEY

logger = logging.getLogger(__name__)

SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"


async def _normalize_names(t1: str, t2: str) -> tuple[str, str]:
    """Translate team/player names to English using Claude Haiku."""
    try:
        from claude_client import client
        prompt = (
            f'Translate these sport team/player names to their standard English spelling.\n'
            f'Name 1: "{t1}"\nName 2: "{t2}"\n'
            f'Return JSON only: {{"name1": "...", "name2": "..."}}\n'
            f'If already English or unknown, return as-is.'
        )
        r = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001", max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = r.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group(0))
            return d.get("name1", t1), d.get("name2", t2)
    except Exception as e:
        logger.warning(f"_normalize_names: {e}")
    return t1, t2


async def _sportsdb_last5(team_name: str, client: httpx.AsyncClient) -> str:
    """Fetch last 5 results for a team from TheSportsDB."""
    try:
        r = await client.get(f"{SPORTSDB_BASE}/searchteams.php", params={"t": team_name})
        if r.status_code != 200:
            return ""
        teams = (r.json().get("teams") or [])
        if not teams:
            return ""
        tid = teams[0]["idTeam"]
        tname = teams[0]["strTeam"]

        r2 = await client.get(f"{SPORTSDB_BASE}/eventslast.php", params={"id": tid})
        if r2.status_code != 200:
            return ""
        events = (r2.json().get("results") or [])[:5]
        if not events:
            return ""

        lines = []
        for e in events:
            d = (e.get("dateEvent") or "")
            h = e.get("strHomeTeam", "?")
            a = e.get("strAwayTeam", "?")
            hs = e.get("intHomeScore", "?")
            as_ = e.get("intAwayScore", "?")
            lines.append(f"{d}: {h} {hs}–{as_} {a}")
        return f"{tname} last 5:\n" + "\n".join(lines)
    except Exception as e:
        logger.warning(f"_sportsdb_last5 {team_name}: {e}")
        return ""


async def _sportsdb_h2h(t1: str, t2: str, client: httpx.AsyncClient) -> str:
    """Fetch H2H between two teams from TheSportsDB (search last events for team1, filter by team2)."""
    try:
        r = await client.get(f"{SPORTSDB_BASE}/searchteams.php", params={"t": t1})
        if r.status_code != 200:
            return ""
        teams = (r.json().get("teams") or [])
        if not teams:
            return ""
        tid = teams[0]["idTeam"]

        r2 = await client.get(f"{SPORTSDB_BASE}/eventslast.php", params={"id": tid})
        if r2.status_code != 200:
            return ""
        events = r2.json().get("results") or []

        t2_lower = t2.lower()
        h2h = [
            e for e in events
            if t2_lower in (e.get("strHomeTeam") or "").lower()
            or t2_lower in (e.get("strAwayTeam") or "").lower()
        ][:5]
        if not h2h:
            return ""

        lines = []
        for e in h2h:
            d = e.get("dateEvent", "")
            h = e.get("strHomeTeam", "?")
            a = e.get("strAwayTeam", "?")
            hs = e.get("intHomeScore", "?")
            as_ = e.get("intAwayScore", "?")
            lines.append(f"{d}: {h} {hs}–{as_} {a}")
        return "H2H last meetings:\n" + "\n".join(lines)
    except Exception as e:
        logger.warning(f"_sportsdb_h2h: {e}")
        return ""


async def fetch_real_data(team1: str, team2: str) -> str:
    """
    Fetch last 5 results + H2H for both teams.
    1. Normalize names to English via Haiku
    2. Try TheSportsDB (free, multi-sport)
    3. Try api-sports.io as fallback (football only)
    4. Try football-data.org as last resort (football only)
    """
    if not team1 or not team2:
        return ""

    t1_en, t2_en = await _normalize_names(team1, team2)
    logger.info(f"fetch_real_data: '{team1}'→'{t1_en}', '{team2}'→'{t2_en}'")

    parts = []

    # ── TheSportsDB (primary, multi-sport) ───────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            t1_res, t2_res, h2h_res = await asyncio.gather(
                _sportsdb_last5(t1_en, h),
                _sportsdb_last5(t2_en, h),
                _sportsdb_h2h(t1_en, t2_en, h),
            )
        if t1_res: parts.append(t1_res)
        if t2_res: parts.append(t2_res)
        if h2h_res: parts.append(h2h_res)
    except Exception as e:
        logger.error(f"fetch_real_data sportsdb: {e}")

    # ── api-sports.io fallback (football only) ────────────────────────────────
    if not parts and APIFOOTBALL_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                t1_id = t2_id = None
                for name, slot in [(t1_en, 1), (t2_en, 2)]:
                    r = await h.get("https://v3.football.api-sports.io/teams",
                        headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"name": name})
                    if r.status_code != 200:
                        continue
                    teams = r.json().get("response", [])
                    if not teams:
                        continue
                    tid = teams[0]["team"]["id"]
                    tname = teams[0]["team"]["name"]
                    if slot == 1: t1_id = tid
                    else:         t2_id = tid

                    r2 = await h.get("https://v3.football.api-sports.io/fixtures",
                        headers={"x-apisports-key": APIFOOTBALL_KEY},
                        params={"team": tid, "last": 5, "status": "FT"})
                    if r2.status_code == 200:
                        fixtures = r2.json().get("response", [])
                        if fixtures:
                            lines = [
                                f"{f['fixture']['date'][:10]}: "
                                f"{f['teams']['home']['name']} "
                                f"{f['goals']['home']}–{f['goals']['away']} "
                                f"{f['teams']['away']['name']}"
                                for f in fixtures
                            ]
                            parts.append(f"{tname} last 5:\n" + "\n".join(lines))

                if t1_id and t2_id:
                    r3 = await h.get("https://v3.football.api-sports.io/fixtures/headtohead",
                        headers={"x-apisports-key": APIFOOTBALL_KEY},
                        params={"h2h": f"{t1_id}-{t2_id}", "last": 5})
                    if r3.status_code == 200:
                        h2h = r3.json().get("response", [])
                        if h2h:
                            lines = [
                                f"{f['fixture']['date'][:10]}: "
                                f"{f['teams']['home']['name']} "
                                f"{f['goals']['home']}–{f['goals']['away']} "
                                f"{f['teams']['away']['name']}"
                                for f in h2h
                            ]
                            parts.append("H2H last 5:\n" + "\n".join(lines))
        except Exception as e:
            logger.error(f"fetch_real_data api-sports: {e}")

    # ── football-data.org last resort ─────────────────────────────────────────
    if not parts and FOOTBALL_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as h:
                for name in [t1_en, t2_en]:
                    r = await h.get("https://api.football-data.org/v4/teams",
                        headers={"X-Auth-Token": FOOTBALL_KEY}, params={"name": name, "limit": 1})
                    if r.status_code != 200:
                        continue
                    teams = r.json().get("teams", [])
                    if not teams:
                        continue
                    tid = teams[0]["id"]
                    r2 = await h.get(f"https://api.football-data.org/v4/teams/{tid}/matches",
                        headers={"X-Auth-Token": FOOTBALL_KEY},
                        params={"status": "FINISHED", "limit": 5})
                    if r2.status_code == 200:
                        ms = r2.json().get("matches", [])
                        if ms:
                            lines = [
                                f"{m['utcDate'][:10]}: {m['homeTeam']['name']} "
                                f"{m['score']['fullTime'].get('home','?')}–"
                                f"{m['score']['fullTime'].get('away','?')} "
                                f"{m['awayTeam']['name']}"
                                for m in ms
                            ]
                            parts.append(f"{teams[0]['name']} last 5:\n" + "\n".join(lines))
        except Exception as e:
            logger.error(f"fetch_real_data fd.org: {e}")

    if not parts:
        return ""
    return "REAL MATCH DATA (use for form analysis — do not invent results):\n\n" + "\n\n".join(parts)


# ─── Football-only helpers for live tracking ──────────────────────────────────

async def search_match(query):
    if not APIFOOTBALL_KEY: return []
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"live": "all"})
            if r.status_code == 200:
                out = []
                for f in r.json().get("response", []):
                    home = f["teams"]["home"]["name"]; away = f["teams"]["away"]["name"]
                    if query.lower() in home.lower() or query.lower() in away.lower():
                        out.append({"id": str(f["fixture"]["id"]), "name": f"{home} vs {away}",
                            "status": f["fixture"]["status"]["short"],
                            "minute": f["fixture"]["status"].get("elapsed", 0),
                            "score": f"{f['goals']['home']}-{f['goals']['away']}", "live": True})
                if out: return out[:3]
            r2 = await h.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"date": date.today().isoformat()})
            if r2.status_code == 200:
                out = []
                for f in r2.json().get("response", []):
                    home = f["teams"]["home"]["name"]; away = f["teams"]["away"]["name"]
                    if query.lower() in home.lower() or query.lower() in away.lower():
                        out.append({"id": str(f["fixture"]["id"]), "name": f"{home} vs {away}",
                            "status": f["fixture"]["status"]["short"], "minute": 0,
                            "score": "0-0", "live": False})
                return out[:3]
    except Exception as e: logger.error(f"search_match: {e}")
    return []


async def get_events(mid):
    if not APIFOOTBALL_KEY: return []
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get("https://v3.football.api-sports.io/fixtures/events",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"fixture": mid})
            if r.status_code == 200: return r.json().get("response", [])
    except Exception as e: logger.error(f"get_events: {e}")
    return []


async def get_status(mid):
    if not APIFOOTBALL_KEY: return None
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"id": mid})
            if r.status_code == 200:
                resp = r.json().get("response", [])
                if resp:
                    f = resp[0]
                    return {"status": f["fixture"]["status"]["short"],
                            "minute": f["fixture"]["status"].get("elapsed", 0),
                            "score": f"{f['goals']['home']}-{f['goals']['away']}",
                            "home": f["teams"]["home"]["name"],
                            "away": f["teams"]["away"]["name"]}
    except Exception as e: logger.error(f"get_status: {e}")
    return None
