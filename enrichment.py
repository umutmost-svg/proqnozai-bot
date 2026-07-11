"""Verified API-Football enrichment for Mostbet football events.

Mostbet remains the authoritative source of the event list, the fixture
identity (``line_id``), the live flag, the markets and the odds. This module
adds *verified* football context (form, H2H, standings, injuries, lineups,
team statistics) from API-Football — and ONLY when we are deterministically
sure the API-Football fixture is the same match as the Mostbet event.

Identity is never fabricated:
    * ``mostbet_line_id`` stays authoritative for Mostbet odds.
    * ``api_football_*`` IDs are a SEPARATE identity, attached only when
      :func:`match_validation.validate_match` grades the candidate ``HIGH``.
    * ``MEDIUM`` is logged for diagnostics but never shown as verified data.
    * ``LOW`` is rejected.

Each enrichment block is independent: one provider failure degrades that block
to "unavailable" and never breaks the rest of the payload. Empty provider
responses mean "data unavailable", never a positive assertion (no injuries /
clean bill of health). Metrics are computed deterministically in Python; the
raw provider JSON is never forwarded to Claude — only normalized text.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx

from config import APIFOOTBALL_KEY
from match_validation import Confidence, MatchRef, validate_match
from metrics import compute_team_metrics, format_metrics_block
from provenance import Provenance

logger = logging.getLogger(__name__)
# Dedicated diagnostics channel (structured, PII/secret-free).
diag = logging.getLogger("enrichment")

API_BASE = "https://v3.football.api-sports.io"
SOURCE = "api-football"

# ─── Cache TTLs (seconds) ─────────────────────────────────────────────────────
# Suggested by the release plan; tuned per data volatility. Fixture identity is
# stable well ahead of kickoff but must refresh near/at kickoff (live score,
# status). Lineups only become meaningful minutes before kickoff.
TTL_FIXTURE = 3600            # 1h before match
TTL_FIXTURE_LIVE = 300       # 5min near kickoff / live
TTL_RECENT = 4 * 3600        # recent completed matches (3–6h)
TTL_H2H = 12 * 3600          # head-to-head
TTL_STANDINGS = 4 * 3600     # standings (3–6h)
TTL_INJURIES = 3 * 3600      # injuries/suspensions
TTL_LINEUPS = 300            # lineups near kickoff
TTL_STATS = 6 * 3600         # team statistics

# Near-kickoff window: within this many minutes of kickoff we treat the fixture
# as "live-ish" for TTL purposes (shorter cache).
NEAR_KICKOFF_MIN = 30

_LIVE_STATUSES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP"}
_FINISHED_STATUSES = {"FT", "AET", "PEN"}


# ─── Bounded TTL cache ────────────────────────────────────────────────────────
class TTLCache:
    """A tiny bounded TTL cache (LRU eviction). Not persisted; process-local.

    The key must include stable API-Football IDs where available so distinct
    fixtures/teams never collide. Expired entries are treated as a miss.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._maxsize = maxsize
        self._store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.time():
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (time.time() + ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()


_cache = TTLCache()


# ─── Result model ─────────────────────────────────────────────────────────────
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class EnrichmentBlock:
    """One independent enrichment block. ``available`` is False when the block
    could not be verified (provider failure or empty feed). ``missing`` names
    the normalized fields the provider did not supply; ``data`` holds already
    normalized, human-readable text — never raw provider JSON."""

    name: str
    available: bool
    source: str = SOURCE
    fetched_at: str = field(default_factory=_utc_now_iso)
    stale: bool = False
    missing: list[str] = field(default_factory=list)
    data: Optional[str] = None

    def to_text(self) -> Optional[str]:
        """Provenance-wrapped text for the prompt, or ``None`` when there is
        genuinely nothing to say (unavailable and no explicit note)."""
        prov = Provenance(self.source, fetched_at=self.fetched_at,
                          stale=self.stale, missing=self.missing)
        if self.available and self.data:
            return prov.wrap(self.data)
        if self.data:  # explicit "unavailable" note carries its own text
            return prov.wrap(self.data)
        return None


@dataclass
class EnrichmentResult:
    """Normalized enrichment outcome for one Mostbet football event."""

    mostbet_line_id: str
    api_football_fixture_id: Optional[int] = None
    api_football_home_team_id: Optional[int] = None
    api_football_away_team_id: Optional[int] = None
    api_football_league_id: Optional[int] = None
    match_confidence: str = Confidence.LOW.value  # high | medium | low
    match_confidence_reasons: list[str] = field(default_factory=list)
    source: str = SOURCE
    fetched_at: str = field(default_factory=_utc_now_iso)
    missing_fields: list[str] = field(default_factory=list)
    blocks: dict[str, EnrichmentBlock] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        """True only for a HIGH-confidence fixture with a real fixture id.
        Only verified results may be shown to the user as real data."""
        return (self.match_confidence == Confidence.HIGH.value
                and self.api_football_fixture_id is not None)

    def prompt_text(self) -> str:
        """Render verified blocks as normalized, provenance-wrapped text for
        Claude. Returns ``""`` when nothing is verified. Never emits raw JSON."""
        if not self.verified:
            return ""
        parts: list[str] = []
        for block in self.blocks.values():
            txt = block.to_text()
            if txt:
                parts.append(txt)
        if not parts:
            return ""
        header = ("VERIFIED FOOTBALL DATA (API-Football, fixture confirmed — "
                  "use for analysis, do not invent):\n\n")
        return header + "\n\n".join(parts)


# ─── HTTP ─────────────────────────────────────────────────────────────────────
async def _api_get(client: httpx.AsyncClient, path: str,
                   params: dict) -> Optional[list]:
    """GET an API-Football endpoint and return its ``response`` list, or None on
    failure. Rate-limit / error responses are logged (status only, never body)."""
    try:
        r = await client.get(f"{API_BASE}{path}", params=params)
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        diag.warning("api-football network error path=%s err=%s", path, type(e).__name__)
        return None
    if r.status_code == 429:
        diag.warning("api-football rate-limited path=%s retry_after=%s",
                     path, r.headers.get("Retry-After", "?"))
        return None
    if r.status_code != 200:
        diag.warning("api-football http path=%s status=%s", path, r.status_code)
        return None
    try:
        payload = r.json()
    except ValueError:
        diag.warning("api-football bad-json path=%s", path)
        return None
    # Surface quota headers for diagnostics without logging any secret.
    remaining = r.headers.get("x-ratelimit-requests-remaining")
    if remaining is not None:
        diag.debug("api-football quota path=%s remaining=%s", path, remaining)
    resp = payload.get("response")
    return resp if isinstance(resp, list) else []


async def _api_get_obj(client: httpx.AsyncClient, path: str,
                       params: dict) -> Optional[dict]:
    """Like :func:`_api_get` but for endpoints whose ``response`` is a dict
    (e.g. /teams/statistics)."""
    try:
        r = await client.get(f"{API_BASE}{path}", params=params)
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        diag.warning("api-football network error path=%s err=%s", path, type(e).__name__)
        return None
    if r.status_code != 200:
        diag.warning("api-football http path=%s status=%s", path, r.status_code)
        return None
    try:
        resp = r.json().get("response")
    except ValueError:
        return None
    return resp if isinstance(resp, dict) and resp else None


# ─── Fixture identification ───────────────────────────────────────────────────
def _seasons(now: datetime) -> tuple[int, int]:
    """Current and previous season years (split-year leagues)."""
    y = now.year
    return y, y - 1


def _fixture_is_live(fx: dict) -> bool:
    short = (((fx.get("fixture") or {}).get("status") or {}).get("short") or "")
    return short in _LIVE_STATUSES


def _fixture_ref(fx: dict) -> MatchRef:
    """Build a MatchRef from an API-Football fixture object."""
    teams = fx.get("teams") or {}
    home = ((teams.get("home") or {}).get("name")) or ""
    away = ((teams.get("away") or {}).get("name")) or ""
    league = ((fx.get("league") or {}).get("name")) or None
    kickoff = None
    raw_dt = (fx.get("fixture") or {}).get("date")
    if raw_dt:
        try:
            kickoff = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            kickoff = kickoff.astimezone(timezone.utc)
        except (ValueError, TypeError):
            kickoff = None
    return MatchRef(home=home, away=away, kickoff=kickoff, league=league,
                    is_live=_fixture_is_live(fx))


def _tokens(name: str) -> set[str]:
    name = re.sub(r"[^\w\s]", " ", (name or "").lower())
    return {t for t in name.split() if len(t) > 1}


async def _resolve_team_id(client: httpx.AsyncClient, name: str) -> Optional[int]:
    """Best-effort resolve a team name to an API-Football team id via /teams.
    Returns None when nothing overlaps clearly. This is only a way to *narrow*
    the fixture search; the final identity is always confirmed by
    :func:`validate_match`, so a wrong guess here yields no enrichment, never a
    mis-attached one."""
    q = (name or "").strip()
    if len(q) < 3:
        return None
    resp = await _api_get(client, "/teams", {"search": q})
    if not resp:
        return None
    want = _tokens(q)
    best_id, best_score = None, 0.0
    for row in resp:
        team = row.get("team") or {}
        cand = _tokens(team.get("name") or "")
        if not cand or not want:
            continue
        score = len(want & cand) / max(len(want), len(cand))
        if score > best_score:
            best_id, best_score = team.get("id"), score
    return best_id if best_score >= 0.5 else None


async def _candidate_fixtures(client: httpx.AsyncClient, requested: MatchRef,
                             home_id: Optional[int], now: datetime) -> list[dict]:
    """Fetch a small set of candidate fixtures to validate against. Prefer the
    home team's fixtures on the kickoff date; fall back to the live feed."""
    candidates: list[dict] = []
    if home_id is not None and requested.kickoff is not None:
        day = requested.kickoff.astimezone(timezone.utc).date().isoformat()
        resp = await _api_get(client, "/fixtures", {"team": home_id, "date": day})
        if resp:
            candidates.extend(resp)
    if home_id is not None and not candidates:
        # Season fallback (free plans block date/next on some tiers).
        for season in _seasons(now):
            resp = await _api_get(client, "/fixtures",
                                  {"team": home_id, "season": season})
            if resp:
                candidates.extend(resp)
                break
    if not candidates and requested.is_live:
        resp = await _api_get(client, "/fixtures", {"live": "all"})
        if resp:
            candidates.extend(resp)
    return candidates


async def _identify_fixture(client: httpx.AsyncClient, requested: MatchRef,
                            now: datetime) -> tuple[Optional[dict], Any]:
    """Find the API-Football fixture for a requested Mostbet match and grade it.

    Returns ``(fixture, confidence)`` where ``fixture`` is the best HIGH
    candidate (or None) and ``confidence`` is the best grading observed (for
    diagnostics). MEDIUM/LOW candidates never return a fixture.
    """
    home_id = await _resolve_team_id(client, requested.home)
    candidates = await _candidate_fixtures(client, requested, home_id, now)
    diag.info("candidates found=%d home_resolved=%s", len(candidates),
              home_id is not None)

    best_fx, best_conf = None, None
    for fx in candidates:
        conf = validate_match(requested, _fixture_ref(fx))
        if best_conf is None or conf.score > best_conf.score:
            best_conf = conf
        if conf.level is Confidence.HIGH:
            # Prefer the highest-scoring HIGH candidate.
            if best_fx is None or conf.score > (best_fx[1].score):
                best_fx = (fx, conf)

    if best_fx is not None:
        return best_fx[0], best_fx[1]
    return None, best_conf


# ─── Enrichment blocks ────────────────────────────────────────────────────────
def _finished(fixtures: list, limit: int = 5) -> list:
    ft = [f for f in fixtures
          if (((f.get("fixture") or {}).get("status") or {}).get("short")
              in _FINISHED_STATUSES)]
    ft.sort(key=lambda f: (f.get("fixture") or {}).get("date") or "", reverse=True)
    return ft[:limit]


def _results_for(fixtures: list, team_id: int) -> list[dict]:
    """Normalize fixtures into {scored, conceded} from a team's perspective,
    de-duplicated by fixture id. Missing goals stay None (excluded by metrics)."""
    out, seen = [], set()
    for f in fixtures:
        fid = (f.get("fixture") or {}).get("id")
        if fid is not None:
            if fid in seen:
                continue
            seen.add(fid)
        teams = f.get("teams") or {}
        goals = f.get("goals") or {}
        is_home = (teams.get("home") or {}).get("id") == team_id
        gh, ga = goals.get("home"), goals.get("away")
        out.append({"scored": gh if is_home else ga,
                    "conceded": ga if is_home else gh})
    return out


def _fixture_lines(fixtures: list) -> list[str]:
    lines = []
    for f in fixtures:
        fx = f.get("fixture") or {}
        teams = f.get("teams") or {}
        goals = f.get("goals") or {}
        lines.append(
            f"{str(fx.get('date') or '')[:10]}: "
            f"{(teams.get('home') or {}).get('name', '?')} "
            f"{goals.get('home')}–{goals.get('away')} "
            f"{(teams.get('away') or {}).get('name', '?')}")
    return lines


async def _block_recent(client, team_id: int, team_name: str,
                        now: datetime) -> EnrichmentBlock:
    key = f"recent:{team_id}"
    fixtures = _cache.get(key)
    if fixtures is None:
        fetched = []
        for season in _seasons(now):
            resp = await _api_get(client, "/fixtures",
                                  {"team": team_id, "season": season})
            if resp:
                fetched = _finished(resp)
                if fetched:
                    break
        fixtures = fetched
        _cache.set(key, fixtures, TTL_RECENT)
    if not fixtures:
        return EnrichmentBlock(f"recent_{team_id}", available=False,
                               missing=["recent_matches"],
                               data=f"{team_name} recent form: data unavailable")
    metrics = compute_team_metrics(_results_for(fixtures, team_id))
    block = (f"{team_name} last {len(fixtures)}:\n"
             + "\n".join(_fixture_lines(fixtures)) + "\n"
             + format_metrics_block(team_name, metrics))
    return EnrichmentBlock(f"recent_{team_id}", available=True, data=block)


async def _block_h2h(client, home_id: int, away_id: int) -> EnrichmentBlock:
    key = f"h2h:{min(home_id, away_id)}-{max(home_id, away_id)}"
    fixtures = _cache.get(key)
    if fixtures is None:
        resp = await _api_get(client, "/fixtures/headtohead",
                              {"h2h": f"{home_id}-{away_id}"})
        fixtures = _finished(resp or [])
        _cache.set(key, fixtures, TTL_H2H)
    if not fixtures:
        return EnrichmentBlock("h2h", available=False, missing=["h2h"],
                               data="Head-to-head: data unavailable")
    return EnrichmentBlock("h2h", available=True,
                           data="H2H last 5:\n" + "\n".join(_fixture_lines(fixtures)))


async def _block_standings(client, league_id: int, now: datetime) -> EnrichmentBlock:
    key = f"standings:{league_id}"
    rows = _cache.get(key)
    if rows is None:
        rows = []
        for season in _seasons(now):
            resp = await _api_get(client, "/standings",
                                  {"league": league_id, "season": season})
            if resp:
                league = (resp[0].get("league") or {})
                tables = league.get("standings") or []
                if tables:
                    rows = tables[0]
                    break
        _cache.set(key, rows, TTL_STANDINGS)
    if not rows:
        return EnrichmentBlock("standings", available=False, missing=["standings"],
                               data="League standings: data unavailable")
    lines = []
    for row in rows[:20]:
        team = (row.get("team") or {}).get("name", "?")
        lines.append(f"{row.get('rank', '?')}. {team} — "
                     f"{row.get('points', '?')} pts ({row.get('goalsDiff', '?')})")
    return EnrichmentBlock("standings", available=True,
                           data="Standings:\n" + "\n".join(lines))


async def _block_injuries(client, fixture_id: int) -> EnrichmentBlock:
    """Injuries/suspensions for the fixture. An EMPTY feed means data
    unavailable — never 'no injuries'. We only assert absences when present."""
    key = f"injuries:{fixture_id}"
    rows = _cache.get(key)
    if rows is None:
        rows = await _api_get(client, "/injuries", {"fixture": fixture_id}) or []
        _cache.set(key, rows, TTL_INJURIES)
    if not rows:
        return EnrichmentBlock("injuries", available=False, missing=["injuries"],
                               data=("Injuries/suspensions: data unavailable "
                                     "(feed returned nothing — NOT a clean bill of health)"))
    by_team: dict[str, list[str]] = {}
    for it in rows:
        player = (it.get("player") or {})
        name = player.get("name")
        if not name:
            continue
        team = (it.get("team") or {}).get("name", "?")
        reason = player.get("reason") or player.get("type") or "—"
        by_team.setdefault(team, []).append(f"  - {name}: {reason}")
    if not by_team:
        return EnrichmentBlock("injuries", available=False, missing=["injuries"],
                               data="Injuries/suspensions: data unavailable")
    parts = []
    for team, players in by_team.items():
        parts.append(f"{team} out ({len(players)}):\n" + "\n".join(players[:8]))
    return EnrichmentBlock("injuries", available=True,
                           data="Injuries/suspensions:\n" + "\n".join(parts))


async def _block_lineups(client, fixture_id: int) -> EnrichmentBlock:
    """Lineups for the fixture. API-Football does NOT flag confirmed vs
    predicted, so we mark the status 'unknown' and never claim 'confirmed'."""
    key = f"lineups:{fixture_id}"
    rows = _cache.get(key)
    if rows is None:
        rows = await _api_get(client, "/fixtures/lineups", {"fixture": fixture_id}) or []
        _cache.set(key, rows, TTL_LINEUPS)
    if not rows:
        return EnrichmentBlock("lineups", available=False, missing=["lineups"],
                               data="Lineups: not yet available")
    parts = []
    for lu in rows:
        team = (lu.get("team") or {}).get("name", "?")
        formation = lu.get("formation") or "?"
        starters = [((p.get("player") or {}).get("name") or "?")
                    for p in (lu.get("startXI") or [])]
        parts.append(f"{team} ({formation}) — lineup status: unknown "
                     f"(provider does not flag confirmed/predicted):\n  "
                     + ", ".join(starters[:11]))
    return EnrichmentBlock("lineups", available=True,
                           missing=["lineup_confirmation_status"],
                           data="Lineups:\n" + "\n".join(parts))


async def _block_team_stats(client, team_id: int, team_name: str,
                            league_id: int, now: datetime) -> EnrichmentBlock:
    key = f"stats:{team_id}:{league_id}"
    stats = _cache.get(key)
    if stats is None:
        stats = None
        for season in _seasons(now):
            resp = await _api_get_obj(client, "/teams/statistics",
                                      {"team": team_id, "league": league_id, "season": season})
            if resp:
                stats = resp
                break
        _cache.set(key, stats or {}, TTL_STATS)
    if not stats:
        return EnrichmentBlock(f"stats_{team_id}", available=False,
                               missing=["team_statistics"],
                               data=f"{team_name} season statistics: data unavailable")
    fixtures = (stats.get("fixtures") or {})
    played = ((fixtures.get("played") or {}).get("total"))
    wins = ((fixtures.get("wins") or {}).get("total"))
    draws = ((fixtures.get("draws") or {}).get("total"))
    loses = ((fixtures.get("loses") or {}).get("total"))
    form = stats.get("form") or ""
    line = (f"{team_name} season statistics (played {played}, "
            f"W/D/L {wins}/{draws}/{loses}, recent form {form[-5:]})")
    return EnrichmentBlock(f"stats_{team_id}", available=True, data=line)


# ─── Public entry point ───────────────────────────────────────────────────────
def _fixture_ttl(requested: MatchRef, now: datetime) -> int:
    if requested.is_live:
        return TTL_FIXTURE_LIVE
    if requested.kickoff is not None:
        gap_min = abs((requested.kickoff - now).total_seconds()) / 60.0
        if gap_min <= NEAR_KICKOFF_MIN:
            return TTL_FIXTURE_LIVE
    return TTL_FIXTURE


async def enrich_football_match(
    *,
    line_id: str,
    home: str,
    away: str,
    kickoff: Optional[datetime] = None,
    league: Optional[str] = None,
    is_live: bool = False,
    client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> EnrichmentResult:
    """Enrich a single Mostbet football event with verified API-Football data.

    Returns an :class:`EnrichmentResult`. Only a HIGH-confidence fixture yields
    ``verified is True`` and any usable blocks; MEDIUM/LOW/no-match return an
    unverified result (still safe to inspect for diagnostics). Never raises for
    provider failures — those degrade to unavailable blocks.
    """
    now = now or datetime.now(timezone.utc)
    result = EnrichmentResult(mostbet_line_id=str(line_id))
    requested = MatchRef(home=home, away=away, kickoff=kickoff,
                         league=league, is_live=is_live)
    diag.info("mostbet event selected line_id=%s live=%s", line_id, is_live)

    if not APIFOOTBALL_KEY:
        result.missing_fields = ["api_football (no key configured)"]
        return result

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=10, headers={"x-apisports-key": APIFOOTBALL_KEY})
    try:
        # Fixture identity — cached by the stable requested-match signature.
        fx_key = f"fixture:{line_id}"
        fixture = _cache.get(fx_key)
        if fixture is None:
            fixture, conf = await _identify_fixture(client, requested, now)
            _cache.set(fx_key, fixture or {}, _fixture_ttl(requested, now))
        else:
            fixture = fixture or None
            conf = None

        if not fixture:
            level = conf.level.value if conf else Confidence.LOW.value
            reasons = conf.reasons if conf else ["no_candidate"]
            result.match_confidence = level
            result.match_confidence_reasons = list(reasons)
            result.missing_fields = ["api_football_fixture"]
            diag.info("fixture rejected line_id=%s confidence=%s reasons=%s",
                      line_id, level, reasons)
            return result

        # HIGH confidence confirmed.
        fixture_id = (fixture.get("fixture") or {}).get("id")
        teams = fixture.get("teams") or {}
        home_id = (teams.get("home") or {}).get("id")
        away_id = (teams.get("away") or {}).get("id")
        league_obj = fixture.get("league") or {}
        league_id = league_obj.get("id")
        result.api_football_fixture_id = fixture_id
        result.api_football_home_team_id = home_id
        result.api_football_away_team_id = away_id
        result.api_football_league_id = league_id
        result.match_confidence = Confidence.HIGH.value
        result.match_confidence_reasons = list(conf.reasons) if conf else ["cached_fixture"]
        diag.info("fixture confirmed line_id=%s fixture_id=%s confidence=high",
                  line_id, fixture_id)

        home_name = (teams.get("home") or {}).get("name") or home
        away_name = (teams.get("away") or {}).get("name") or away

        # Fetch every block independently; a failure in one must not fail others.
        tasks: dict[str, Any] = {}
        if home_id:
            tasks["recent_home"] = _block_recent(client, home_id, home_name, now)
            if league_id:
                tasks["stats_home"] = _block_team_stats(client, home_id, home_name, league_id, now)
        if away_id:
            tasks["recent_away"] = _block_recent(client, away_id, away_name, now)
            if league_id:
                tasks["stats_away"] = _block_team_stats(client, away_id, away_name, league_id, now)
        if home_id and away_id:
            tasks["h2h"] = _block_h2h(client, home_id, away_id)
        if league_id:
            tasks["standings"] = _block_standings(client, league_id, now)
        if fixture_id:
            tasks["injuries"] = _block_injuries(client, fixture_id)
            tasks["lineups"] = _block_lineups(client, fixture_id)

        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        missing: list[str] = []
        for name, outcome in zip(tasks.keys(), gathered):
            if isinstance(outcome, Exception):
                diag.warning("block failed name=%s err=%s", name, type(outcome).__name__)
                result.blocks[name] = EnrichmentBlock(name, available=False,
                                                      missing=[name],
                                                      data=None)
                missing.append(name)
                continue
            result.blocks[name] = outcome
            diag.info("block name=%s available=%s missing=%s",
                      name, outcome.available, outcome.missing)
            if not outcome.available:
                missing.append(name)
        result.missing_fields = missing
        return result
    finally:
        if own_client:
            await client.aclose()
