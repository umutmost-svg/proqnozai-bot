import asyncio
import logging
from datetime import date

import httpx

from config import APIFOOTBALL_KEY, FOOTBALL_KEY

logger = logging.getLogger(__name__)


# ─── Football API ─────────────────────────────────────────────────────────────
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
                            "status": f["fixture"]["status"]["short"], "minute": 0, "score": "0-0", "live": False})
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
                            "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"]}
    except Exception as e: logger.error(f"get_status: {e}")
    return None


async def fetch_real_data(team1: str, team2: str) -> str:
    """Fetch last 5 results + H2H for both teams. Uses API-Sports first, fd.org as fallback."""
    if not team1 or not team2:
        return ""
    parts = []
    t1_id = t2_id = None

    if APIFOOTBALL_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                for name, slot in [(team1, 1), (team2, 2)]:
                    r = await h.get("https://v3.football.api-sports.io/teams",
                        headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"name": name})
                    if r.status_code != 200:
                        continue
                    teams = r.json().get("response", [])
                    if not teams:
                        continue
                    tid   = teams[0]["team"]["id"]
                    tname = teams[0]["team"]["name"]
                    if slot == 1: t1_id = tid
                    else:         t2_id = tid

                    r2 = await h.get("https://v3.football.api-sports.io/fixtures",
                        headers={"x-apisports-key": APIFOOTBALL_KEY},
                        params={"team": tid, "last": 5, "status": "FT"})
                    if r2.status_code == 200:
                        fixtures = r2.json().get("response", [])
                        if fixtures:
                            lines = []
                            for f in fixtures:
                                d    = f["fixture"]["date"][:10]
                                home = f["teams"]["home"]["name"]
                                away = f["teams"]["away"]["name"]
                                hg   = f["goals"]["home"]
                                ag   = f["goals"]["away"]
                                lines.append(f"{d}: {home} {hg}-{ag} {away}")
                            parts.append(f"{tname} last 5:\n" + "\n".join(lines))

                if t1_id and t2_id:
                    r3 = await h.get("https://v3.football.api-sports.io/fixtures/headtohead",
                        headers={"x-apisports-key": APIFOOTBALL_KEY},
                        params={"h2h": f"{t1_id}-{t2_id}", "last": 5})
                    if r3.status_code == 200:
                        h2h = r3.json().get("response", [])
                        if h2h:
                            lines = []
                            for f in h2h:
                                d    = f["fixture"]["date"][:10]
                                home = f["teams"]["home"]["name"]
                                away = f["teams"]["away"]["name"]
                                hg   = f["goals"]["home"]
                                ag   = f["goals"]["away"]
                                lines.append(f"{d}: {home} {hg}-{ag} {away}")
                            parts.append("H2H last 5:\n" + "\n".join(lines))
        except Exception as e:
            logger.error(f"fetch_real_data api-sports: {e}")

    if not parts and FOOTBALL_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as h:
                for name in [team1, team2]:
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
                                f"{m['score']['fullTime'].get('home','?')}-{m['score']['fullTime'].get('away','?')} "
                                f"{m['awayTeam']['name']}"
                                for m in ms
                            ]
                            parts.append(f"{teams[0]['name']} last 5:\n" + "\n".join(lines))
        except Exception as e:
            logger.error(f"fetch_real_data fd.org: {e}")

    if not parts:
        return ""
    return "REAL MATCH DATA (use this for form analysis — do not invent results):\n\n" + "\n\n".join(parts)
