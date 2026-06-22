import asyncio
import json
import logging
import re
from datetime import date

import httpx

from config import APIFOOTBALL_KEY, FOOTBALL_KEY

logger = logging.getLogger(__name__)


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


async def _fetch_apifootball(t1_en: str, t2_en: str) -> list[str]:
    """Fetch last 5 results + H2H from api-sports.io (football only, 100 req/day free)."""
    parts = []
    if not APIFOOTBALL_KEY:
        return parts
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
        logger.error(f"_fetch_apifootball: {e}")
    return parts


async def _fetch_footballdata(t1_en: str, t2_en: str) -> list[str]:
    """Fetch last 5 from football-data.org (football only, free tier)."""
    parts = []
    if not FOOTBALL_KEY:
        return parts
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
        logger.error(f"_fetch_footballdata: {e}")
    return parts


async def _haiku_form_estimate(t1: str, t2: str, t1_en: str, t2_en: str) -> str:
    """
    Ask Claude Haiku to recall team form from training knowledge.
    Used only when real APIs return no data.
    Result is marked as estimated so Claude Sonnet uses it as such.
    """
    try:
        from claude_client import client
        prompt = (
            f"Describe the recent form (last 5-10 matches) and playing style for these teams/players:\n"
            f"Team 1: {t1_en} (also known as: {t1})\n"
            f"Team 2: {t2_en} (also known as: {t2})\n\n"
            f"For each team provide:\n"
            f"- Recent form trend (winning/losing streak, draws)\n"
            f"- Key strengths and weaknesses\n"
            f"- Head-to-head tendency if known\n\n"
            f"Be concise. If you have no reliable knowledge about a team, say so clearly.\n"
            f"Format: plain text, one paragraph per team."
        )
        r = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = r.content[0].text.strip()
        if len(text) < 30:
            return ""
        return f"FORM ANALYSIS (AI knowledge, may not reflect latest matches):\n\n{text}"
    except Exception as e:
        logger.warning(f"_haiku_form_estimate: {e}")
        return ""


async def fetch_real_data(team1: str, team2: str) -> str:
    """
    Fetch form + H2H data for both teams.
    1. Normalize names to English via Haiku
    2. Try api-sports.io (football, 100 req/day free)
    3. Try football-data.org (football free tier)
    4. Fall back to Haiku knowledge estimate (all sports, marked as estimated)
    """
    if not team1 or not team2:
        return ""

    t1_en, t2_en = await _normalize_names(team1, team2)
    logger.info(f"fetch_real_data: '{team1}'→'{t1_en}', '{team2}'→'{t2_en}'")

    # Try real APIs in parallel
    api_parts, fd_parts = await asyncio.gather(
        _fetch_apifootball(t1_en, t2_en),
        _fetch_footballdata(t1_en, t2_en),
    )

    parts = api_parts or fd_parts
    if parts:
        return "REAL MATCH DATA (use for form analysis — do not invent results):\n\n" + "\n\n".join(parts)

    # No real data — use Haiku knowledge
    estimated = await _haiku_form_estimate(team1, team2, t1_en, t2_en)
    return estimated


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
