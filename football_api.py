import asyncio
import json
import logging
import re
import time
from datetime import date

import httpx

from config import APIFOOTBALL_KEY, FOOTBALL_KEY
from metrics import compute_team_metrics, format_metrics_block
from provenance import Provenance

logger = logging.getLogger(__name__)

# Simple in-memory TTL cache to stay under football-data's 10 req/min limit.
_fd_cache: dict = {}  # key -> (expires_at, value)


def _cache_get(key: str):
    item = _fd_cache.get(key)
    if item and item[0] > time.time():
        return item[1]
    return None


def _cache_set(key: str, value, ttl: int):
    _fd_cache[key] = (time.time() + ttl, value)


async def _normalize_names(t1: str, t2: str) -> tuple[str, str]:
    """Translate team/player names to English using Claude Haiku."""
    try:
        from claude_client import _create_with_retry
        prompt = (
            f'Translate these sport team/player names to their standard English spelling.\n'
            f'Name 1: "{t1}"\nName 2: "{t2}"\n'
            f'Return JSON only: {{"name1": "...", "name2": "..."}}\n'
            f'If already English or unknown, return as-is.'
        )
        r = await _create_with_retry(
            model="claude-haiku-4-5-20251001", max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        if not r.content:
            return t1, t2
        raw = r.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group(0))
            return d.get("name1", t1), d.get("name2", t2)
    except Exception as e:
        logger.warning(f"_normalize_names: {e}")
    return t1, t2


def _team_results(fixtures: list, team_id: int) -> list[dict]:
    """Normalize api-sports fixtures into {scored, conceded} from team_id's view,
    for the deterministic metrics engine. Missing goals stay None. Fixtures are
    de-duplicated by fixture id so a repeated fixture is never double-counted."""
    out, seen = [], set()
    for f in fixtures:
        fid = (f.get("fixture") or {}).get("id")
        if fid is not None:
            if fid in seen:
                continue
            seen.add(fid)
        is_home = f["teams"]["home"]["id"] == team_id
        g_home = f["goals"]["home"]
        g_away = f["goals"]["away"]
        out.append({
            "scored": g_home if is_home else g_away,
            "conceded": g_away if is_home else g_home,
        })
    return out


async def _fetch_injuries(h, hdrs, team_id: int, team_name: str) -> str:
    """Current-season injuries/suspensions for a team (api-sports /injuries)."""
    from datetime import date as _date
    year = _date.today().year
    for season in (year, year - 1):  # cover split-year seasons
        r = await _api_get(h, "https://v3.football.api-sports.io/injuries",
            headers=hdrs, params={"team": team_id, "season": season})
        if not r or r.status_code != 200:
            continue
        rows = r.json().get("response", [])
        if not rows:
            continue
        # Most recent unique players (latest fixtures come last in the feed).
        seen = {}
        for it in rows:
            p = (it.get("player") or {})
            name = p.get("name")
            if not name:
                continue
            reason = p.get("reason") or p.get("type") or "—"
            seen[name] = reason  # later entries overwrite → keep most recent
        if not seen:
            continue
        players = list(seen.items())[-8:]
        lines = [f"  - {nm}: {rs}" for nm, rs in players]
        return f"{team_name} injuries/out ({len(seen)}):\n" + "\n".join(lines)
    return ""


async def _api_get(h: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response | None:
    """GET with up to 2 retries on 429/5xx and connection errors."""
    delay = 2.0
    for attempt in range(3):
        try:
            r = await h.get(url, **kwargs)
            if r.status_code == 429:
                wait = min(int(r.headers.get("Retry-After", delay)), 30)
                logger.warning(f"football API 429 {url} — waiting {wait}s")
                await asyncio.sleep(wait)
                delay *= 2
                continue
            if r.status_code >= 500:
                logger.warning(f"football API {r.status_code} {url} (attempt {attempt+1})")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return r
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(f"football API network error {url} (attempt {attempt+1}): {e}")
            await asyncio.sleep(delay)
            delay *= 2
    return None


def _fixture_lines(fixtures: list) -> list[str]:
    return [
        f"{f['fixture']['date'][:10]}: "
        f"{f['teams']['home']['name']} "
        f"{f['goals']['home']}–{f['goals']['away']} "
        f"{f['teams']['away']['name']}"
        for f in fixtures
    ]


def _finished_recent(fixtures: list, limit: int = 5) -> list:
    """Finished fixtures, most recent first, capped at `limit`."""
    ft = [f for f in fixtures if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]
    ft.sort(key=lambda f: f["fixture"]["date"], reverse=True)
    return ft[:limit]


async def _team_recent(h, hdrs, tid: int) -> list:
    """Recent finished fixtures for a team. The free plan blocks the `last`
    parameter, so we query by season (current, then previous year) and trim."""
    from datetime import date as _date
    yr = _date.today().year
    for season in (yr, yr - 1):
        r = await _api_get(h, "https://v3.football.api-sports.io/fixtures",
            headers=hdrs, params={"team": tid, "season": season})
        if not r or r.status_code != 200:
            continue
        recent = _finished_recent(r.json().get("response", []))
        if recent:
            return recent
    return []


async def _fetch_apifootball(t1_en: str, t2_en: str) -> list[str]:
    """Fetch last-5 results + H2H + injuries from api-sports.io (football only)."""
    parts = []
    if not APIFOOTBALL_KEY:
        return parts
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            hdrs = {"x-apisports-key": APIFOOTBALL_KEY}
            t1_id = t2_id = None
            for name, slot in [(t1_en, 1), (t2_en, 2)]:
                r = await _api_get(h, "https://v3.football.api-sports.io/teams",
                    headers=hdrs, params={"name": name})
                if not r or r.status_code != 200:
                    continue
                teams = r.json().get("response", [])
                if not teams:
                    continue
                tid   = teams[0]["team"]["id"]
                tname = teams[0]["team"]["name"]
                if slot == 1: t1_id = tid
                else:         t2_id = tid

                fixtures = await _team_recent(h, hdrs, tid)
                if fixtures:
                    metrics = compute_team_metrics(_team_results(fixtures, tid))
                    block = (f"{tname} last {len(fixtures)}:\n"
                             + "\n".join(_fixture_lines(fixtures))
                             + "\n" + format_metrics_block(tname, metrics))
                    parts.append(Provenance("api-football").wrap(block))

                # Injuries: an empty feed means "no information", NOT "no
                # injuries". Emit the players only when the feed returned some;
                # otherwise record injuries as a missing field so Claude states
                # it as unavailable rather than asserting a clean bill of health.
                inj = await _fetch_injuries(h, hdrs, tid, tname)
                if inj:
                    parts.append(Provenance("api-football").wrap(inj))
                else:
                    parts.append(Provenance("api-football", missing=["injuries"]).wrap(
                        f"{tname} injuries/suspensions: data unavailable (feed returned nothing)"))

            if t1_id and t2_id:
                # H2H also can't use `last` on the free plan — fetch all and trim.
                r3 = await _api_get(h, "https://v3.football.api-sports.io/fixtures/headtohead",
                    headers=hdrs, params={"h2h": f"{t1_id}-{t2_id}"})
                if r3 and r3.status_code == 200:
                    h2h = _finished_recent(r3.json().get("response", []))
                    if h2h:
                        parts.append(Provenance("api-football").wrap(
                            "H2H last 5:\n" + "\n".join(_fixture_lines(h2h))))
    except Exception as e:
        logger.error(f"_fetch_apifootball: {e}")
    return parts


# Map a tournament name (Mostbet lineSubCategory) → football-data.org competition
# code. Only free-tier competitions; substring match, first hit wins.
_FD_COMP = {
    "world cup": "WC", "fifa world cup": "WC", "mundial": "WC",
    "european championship": "EC", "euro ": "EC",
    "champions league": "CL",
    "premier league": "PL", "epl": "PL",
    "la liga": "PD", "laliga": "PD",
    "bundesliga": "BL1",
    "serie a": "SA",
    "ligue 1": "FL1",
    "eredivisie": "DED",
    "primeira liga": "PPL",
    "championship": "ELC",
    "brasileir": "BSA",
}


def _fd_comp_code(league_hint: str) -> str:
    h = (league_hint or "").lower()
    for kw, code in _FD_COMP.items():
        if kw in h:
            return code
    return ""


def _fd_match_team(teams: list, name: str) -> dict | None:
    """Pick the football-data team best matching `name` (token overlap)."""
    q = name.lower().strip()
    qtok = {t for t in re.sub(r"[^\w\s]", " ", q).split() if t}
    best, best_score = None, 0.0
    for t in teams:
        for cand in (t.get("name", ""), t.get("shortName", ""), t.get("tla", "")):
            c = (cand or "").lower().strip()
            if not c:
                continue
            if c == q:
                return t
            ctok = {x for x in re.sub(r"[^\w\s]", " ", c).split() if x}
            if not ctok:
                continue
            score = len(qtok & ctok) / max(len(qtok), len(ctok))
            if score > best_score:
                best, best_score = t, score
    return best if best_score >= 0.5 else None


async def _fd_resolve_ai(roster_names: list, t1: str, t2: str) -> dict:
    """Map two Mostbet team names onto exact football-data roster names via Haiku.
    Bridges naming differences (e.g. 'Ivory Coast' ↔ 'Côte d'Ivoire')."""
    try:
        from claude_client import _create_with_retry
        prompt = (
            "Match each input team to the EXACT closest entry in the roster list. "
            "Account for spelling/language differences and common aliases.\n"
            f"Roster: {json.dumps(roster_names, ensure_ascii=False)}\n"
            f'Input1: "{t1}"\nInput2: "{t2}"\n'
            'Return JSON only: {"team1": "<exact roster name or null>", '
            '"team2": "<exact roster name or null>"}'
        )
        r = await _create_with_retry(
            model="claude-haiku-4-5-20251001", max_tokens=120,
            messages=[{"role": "user", "content": prompt}])
        if r.content:
            m = re.search(r"\{.*\}", r.content[0].text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"_fd_resolve_ai: {e}")
    return {}


async def _fetch_footballdata(t1_en: str, t2_en: str, league_hint: str = "") -> list[str]:
    """Form + avg goals from football-data.org. Resolves teams via the COMPETITION
    roster (their /teams?name= search is unreliable) so national teams work."""
    parts = []
    code = _fd_comp_code(league_hint)
    if not FOOTBALL_KEY or not code:
        return parts
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            hdrs = {"X-Auth-Token": FOOTBALL_KEY}
            # Competition roster — changes rarely, cache 24h.
            comp_teams = _cache_get(f"comp:{code}")
            if comp_teams is None:
                rc = await _api_get(h, f"https://api.football-data.org/v4/competitions/{code}/teams",
                                    headers=hdrs)
                if not rc or rc.status_code != 200:
                    return parts
                comp_teams = rc.json().get("teams", [])
                _cache_set(f"comp:{code}", comp_teams, 24 * 3600)

            # Resolve both Mostbet names → roster teams. Token match first;
            # if either fails, ask Haiku to bridge the naming difference.
            resolved = {name: _fd_match_team(comp_teams, name) for name in (t1_en, t2_en)}
            if not all(resolved.values()):
                roster_names = [t.get("name", "") for t in comp_teams]
                ai = await _fd_resolve_ai(roster_names, t1_en, t2_en)
                by_name = {t.get("name", "").lower(): t for t in comp_teams}
                for orig, key in ((t1_en, "team1"), (t2_en, "team2")):
                    if not resolved.get(orig):
                        match = by_name.get(str(ai.get(key) or "").lower())
                        if match:
                            resolved[orig] = match

            for name in (t1_en, t2_en):
                t = resolved.get(name)
                if not t:
                    continue
                tid, tname = t["id"], t.get("name", name)
                # Team form — cache 6h.
                ms = _cache_get(f"matches:{tid}")
                if ms is None:
                    r2 = await _api_get(h, f"https://api.football-data.org/v4/teams/{tid}/matches",
                                        headers=hdrs, params={"status": "FINISHED", "limit": 8})
                    if not r2 or r2.status_code != 200:
                        continue
                    ms = sorted(r2.json().get("matches", []),
                                key=lambda m: m.get("utcDate", ""), reverse=True)[:6]
                    _cache_set(f"matches:{tid}", ms, 6 * 3600)
                if not ms:
                    continue
                lines, results, seen = [], [], set()
                for m in ms:
                    # Belt-and-suspenders: the query already asks for FINISHED,
                    # but never let a postponed/cancelled fixture into metrics.
                    if m.get("status") not in (None, "FINISHED"):
                        continue
                    mid_ = m.get("id")
                    if mid_ is not None:
                        if mid_ in seen:
                            continue
                        seen.add(mid_)
                    gh = m["score"]["fullTime"].get("home")
                    ga = m["score"]["fullTime"].get("away")
                    lines.append(f"{m['utcDate'][:10]}: {m['homeTeam']['name']} "
                                 f"{gh}–{ga} {m['awayTeam']['name']}")
                    is_home = m["homeTeam"]["id"] == tid
                    results.append({"scored": gh if is_home else ga,
                                    "conceded": ga if is_home else gh})
                if not results:
                    continue
                metrics = compute_team_metrics(results)
                block = (f"{tname} last {len(results)}:\n" + "\n".join(lines)
                         + "\n" + format_metrics_block(tname, metrics))
                parts.append(Provenance("football-data").wrap(block))
    except Exception as e:
        logger.error(f"_fetch_footballdata: {e}")
    return parts


async def fetch_real_data(team1: str, team2: str, league_hint: str = "") -> str:
    """
    Fetch real provider form + goals data for both teams. FACTUAL DATA ONLY —
    this function never asks the LLM to invent form, results, injuries or stats.
    1. Normalize names to English via Haiku (translation only, not facts).
    2. Prefer football-data.org (free tier has CURRENT seasons incl. World Cup);
       teams are resolved via the competition roster, so league_hint is needed.
    3. Fall back to api-sports (only covers 2022-2024 on free — rarely useful).
    4. If no provider returns data, return "" — the caller flags the forecast as
       having no real data and the prompt tells Claude to state it as unavailable.
    """
    if not team1 or not team2:
        return ""

    t1_en, t2_en = await _normalize_names(team1, team2)
    logger.info(f"fetch_real_data: '{team1}'→'{t1_en}', '{team2}'→'{t2_en}' | league='{league_hint}'")

    header = "REAL MATCH DATA (use for form analysis — do not invent results):\n\n"

    # Primary: football-data.org (current-season data on the free tier).
    if FOOTBALL_KEY:
        try:
            fd_parts = await asyncio.wait_for(
                _fetch_footballdata(t1_en, t2_en, league_hint), timeout=20)
        except asyncio.TimeoutError:
            fd_parts = []
        if fd_parts:
            return header + "\n\n".join(fd_parts)

    # Secondary: api-sports (only call if it might help — old seasons).
    if APIFOOTBALL_KEY:
        try:
            api_parts = await asyncio.wait_for(_fetch_apifootball(t1_en, t2_en), timeout=20)
        except asyncio.TimeoutError:
            api_parts = []
        if api_parts:
            return header + "\n\n".join(api_parts)

    # No real provider data. Return empty so the caller sets has_real_data=False;
    # the forecast prompt then requires Claude to state form/H2H as unavailable
    # instead of fabricating it. We never invent factual sports data.
    logger.info(f"fetch_real_data: no provider data for '{team1}' vs '{team2}'")
    return ""


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
                            "home": home, "away": away,
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
                            "home": home, "away": away,
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
