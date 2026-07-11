"""Clean, deterministic football event list (Mostbet-backed, offline-testable).

This module turns the raw Mostbet feed into a normalized, filtered, sorted and
paginated event list for the Telegram menu. It is pure: no network, no clock
except the explicit ``now_utc`` passed in.

IDENTITY (important):
    Mostbet only supplies an authoritative fixture identity (``fixture_id``). It
    does NOT expose canonical team/league IDs. So:
      * ``fixture_id`` is authoritative (``fixture_id_source == "provider"``).
      * ``league_id`` / ``home_team_id`` / ``away_team_id`` are the provider's
        native IDs when present, otherwise ``None`` — we never fabricate them.
      * ``league_key`` / ``home_team_key`` / ``away_team_key`` are deterministic
        name-derived slugs used ONLY for local grouping/dedup. They are NOT
        provider IDs and do not solve cross-provider identity.
    ``*_identity_source`` records where each identity came from. The future
    API-Football migration will replace the nullable/derived fields with real
    provider team and league IDs.

The menu resolves a selected event by authoritative ``fixture_id`` (never by
fuzzy name matching).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import MOSTBET_SRC_TZ
from mostbet import _is_outright_market, _is_virtual_match

PROVIDER = "mostbet"

# Pagination caps (approved).
MAX_LEAGUES = 15
MAX_MATCHES_PER_LEAGUE = 10

# Grace fallback ONLY used when the provider gives no explicit status: a
# non-live fixture whose kickoff is more than this far in the past is treated as
# finished/stale and dropped. 3h30m comfortably exceeds a full match with
# stoppages, half-time and post-match settling, so we never hide a game that is
# merely running late without a live flag.
FINISHED_GRACE = timedelta(hours=3, minutes=30)

# Buckets.
LIVE, TODAY, TOMORROW, LATER = "LIVE", "TODAY", "TOMORROW", "LATER"

# ─── Status normalization ─────────────────────────────────────────────────────
# The Mostbet feed's status vocabulary is not documented; we read any of a few
# plausible status fields and map recognized tokens to canonical values. Unknown
# tokens become None so time/live rules apply.
_FINISHED_TOKENS = {"finished", "ft", "ended", "closed", "result", "aet", "pen", "full-time", "fulltime"}
_ABANDONED_TOKENS = {"abandoned", "aband", "interrupted", "suspended", "walkover"}
_POSTPONED_TOKENS = {"postponed", "pp", "delayed", "tbd"}
_CANCELLED_TOKENS = {"cancelled", "canceled", "canc"}
_LIVE_TOKENS = {"live", "inplay", "in_play", "1h", "2h", "ht", "1st half", "2nd half", "playing"}

_STATUS_FIELDS = ("status", "matchStatus", "state", "statusName", "lineStatus")


def parse_status(raw: dict) -> Optional[str]:
    """Canonical status: finished/abandoned/postponed/cancelled/live/None."""
    for f in _STATUS_FIELDS:
        v = raw.get(f)
        if not v:
            continue
        t = str(v).strip().lower()
        if t in _FINISHED_TOKENS:
            return "finished"
        if t in _ABANDONED_TOKENS:
            return "abandoned"
        if t in _POSTPONED_TOKENS:
            return "postponed"
        if t in _CANCELLED_TOKENS:
            return "cancelled"
        if t in _LIVE_TOKENS:
            return "live"
    return None


# ─── Kickoff parsing (→ tz-aware UTC) ─────────────────────────────────────────
_SRC_TZ = timezone(timedelta(hours=MOSTBET_SRC_TZ))


def parse_kickoff_utc(raw_dt: Optional[str]) -> Optional[datetime]:
    """Parse Mostbet kickoff into a tz-aware UTC datetime, or None.

    Accepts ISO (``2026-07-15T18:00:00[Z]``) and Mostbet's
    ``DD.MM.YYYY HH:MM:SS`` (in MOSTBET_SRC_TZ). Returns None on anything else.
    """
    if not raw_dt:
        return None
    ds = str(raw_dt).strip()
    try:
        if "T" in ds:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        if "." in ds:
            dt = datetime.strptime(ds[:19], "%d.%m.%Y %H:%M:%S")
            return dt.replace(tzinfo=_SRC_TZ).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None
    return None


# ─── Slugs (local grouping keys — NOT provider IDs) ───────────────────────────
def _slug(s: Optional[str]) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


# ─── Event model ──────────────────────────────────────────────────────────────
@dataclass
class EventItem:
    fixture_id: str
    provider: str
    home: str
    away: str
    league_name: str
    country: Optional[str]
    kickoff_utc: Optional[datetime]   # None only for live fixtures without a time
    is_live: bool
    status: Optional[str]
    sport: str
    # Provider-native identity (None when the feed doesn't supply it).
    league_id: Optional[str] = None
    home_team_id: Optional[str] = None
    away_team_id: Optional[str] = None
    # Local, name-derived grouping keys — NOT provider IDs.
    league_key: str = ""
    home_team_key: str = ""
    away_team_key: str = ""
    # Where each identity came from.
    fixture_id_source: str = "provider"
    team_identity_source: str = "derived_name_key"
    league_identity_source: str = "derived_name_key"
    # Set during build_event_list.
    bucket: Optional[str] = None

    @property
    def postponed(self) -> bool:
        return self.status == "postponed"


def normalize_fixture(raw: dict) -> Optional[EventItem]:
    """Map a raw Mostbet match to an EventItem, or None if it must not be shown.

    Rejects malformed, virtual/esports and outright fixtures. Requires an
    authoritative fixture_id, both team names and a league name always; requires
    a tz-aware kickoff too, EXCEPT live fixtures (provider live flag/status) which
    may legitimately arrive without a scheduled kickoff.
    """
    if _is_virtual_match(raw) or _is_outright_market(raw):
        return None

    fid = raw.get("id")
    if fid is None:
        return None
    fixture_id = str(fid)

    home = (raw.get("team1Title") or "").strip()
    away = (raw.get("team2Title") or "").strip()
    league_name = (raw.get("lineSubCategory") or "").strip()
    country = (raw.get("lineSuperCategory") or "").strip() or None
    sport = (raw.get("lineCategory") or "").strip() or "Other"
    is_live = bool(raw.get("isLive"))
    status = parse_status(raw)
    kickoff = parse_kickoff_utc(raw.get("matchBeginAt"))

    if not home or not away or home == "?" or away == "?":
        return None
    if not league_name:
        return None

    live_ok = is_live or status == "live"
    if not live_ok:
        # Non-live fixtures must have a valid tz-aware kickoff.
        if kickoff is None or kickoff.tzinfo is None:
            return None

    # Provider-native IDs if the feed ever supplies them; never fabricated.
    league_id = _opt_str(raw.get("tournamentId") or raw.get("subCategoryId"))
    home_team_id = _opt_str(raw.get("team1Id"))
    away_team_id = _opt_str(raw.get("team2Id"))

    return EventItem(
        fixture_id=fixture_id,
        provider=PROVIDER,
        home=home,
        away=away,
        league_name=league_name,
        country=country,
        kickoff_utc=kickoff,
        is_live=is_live,
        status=status,
        sport=sport,
        league_id=league_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        league_key=_slug(f"{country or ''}-{league_name}") or "unknown",
        home_team_key=_slug(home) or "unknown",
        away_team_key=_slug(away) or "unknown",
        team_identity_source="provider" if (home_team_id and away_team_id) else "derived_name_key",
        league_identity_source="provider" if league_id else "derived_name_key",
    )


def _opt_str(v) -> Optional[str]:
    return str(v) if v not in (None, "") else None


# ─── League priority ──────────────────────────────────────────────────────────
def _norm(s: Optional[str]) -> str:
    s = (s or "").lower()
    return s.replace("ü", "u").replace("ı", "i").replace("ə", "a")


# (name substring, optional country substring). Country disambiguates the
# domestic "Premier League"s so England's is not confused with Azerbaijan's.
_LEAGUE_PRIORITY: tuple[tuple[str, Optional[str]], ...] = (
    ("champions league", None),
    ("europa league", None),
    ("conference league", None),
    ("world cup", None),
    ("euro", None),                       # Euros / European Championship
    ("premier league", "england"),
    ("la liga", "spain"),
    ("serie a", "italy"),
    ("bundesliga", "germany"),
    ("ligue 1", "france"),
    ("super lig", "turkey"),              # Süper Lig
    ("premier league", "azerbaijan"),     # Azərbaycan Premyer Liqası
)


def league_rank(league_name: str, country: Optional[str]) -> int:
    """Lower = higher priority. Unlisted leagues share the lowest rank."""
    n, c = _norm(league_name), _norm(country)
    for i, (kw, country_hint) in enumerate(_LEAGUE_PRIORITY):
        if kw in n and (country_hint is None or country_hint in c):
            return i
    # Also honor an explicit Azerbaijani name regardless of country field.
    if "premyer liqa" in n:
        return len(_LEAGUE_PRIORITY) - 1
    return len(_LEAGUE_PRIORITY)


# ─── Filtering + bucketing ────────────────────────────────────────────────────
def _local_day_diff(kickoff_utc: datetime, now_utc: datetime, user_tz: timezone) -> int:
    return (kickoff_utc.astimezone(user_tz).date() - now_utc.astimezone(user_tz).date()).days


def visible_bucket(item: EventItem, now_utc: datetime, user_tz: timezone,
                   include_later: bool = False) -> Optional[str]:
    """Return the bucket (LIVE/TODAY/TOMORROW/LATER) or None if the item must be
    hidden. Status precedence: explicit provider status → live flag → kickoff
    grace fallback (only when status is absent)."""
    st = item.status
    if st in ("finished", "abandoned", "cancelled"):
        return None
    if st == "live" or item.is_live:
        return LIVE
    # Non-live, scheduled/postponed/unknown.
    if item.kickoff_utc is None:
        return None
    if st is None and item.kickoff_utc < now_utc - FINISHED_GRACE:
        return None  # grace fallback only when no explicit status
    d = _local_day_diff(item.kickoff_utc, now_utc, user_tz)
    if d <= 0:
        return TODAY
    if d == 1:
        return TOMORROW
    return LATER if include_later else None


def _dedup(items: list[EventItem]) -> list[EventItem]:
    """Drop duplicate provider fixture ids, then duplicate composite events
    (same league + teams + kickoff under different fixture ids). First occurrence
    wins. The composite key includes league_key AND kickoff so genuinely distinct
    fixtures are never collapsed: the same teams in two competitions (different
    league_key), a two-legged tie on different dates (different kickoff), and a
    senior vs reserve/women side (different team_key) all survive."""
    out, seen_fid, seen_comp = [], set(), set()
    for it in items:
        if it.fixture_id in seen_fid:
            continue
        seen_fid.add(it.fixture_id)
        ko = it.kickoff_utc.isoformat() if it.kickoff_utc else "live"
        comp = (it.league_key, it.home_team_key, it.away_team_key, ko)
        if comp in seen_comp:
            continue
        seen_comp.add(comp)
        out.append(it)
    return out


def select_visible(items: list[EventItem], now_utc: datetime, user_tz: timezone,
                   include_later: bool = False) -> list[EventItem]:
    """Filter to displayable items, set each item's bucket, and de-duplicate."""
    kept = []
    for it in items:
        b = visible_bucket(it, now_utc, user_tz, include_later)
        if b is None:
            continue
        it.bucket = b
        kept.append(it)
    return _dedup(kept)


# ─── Grouping / sorting / pagination ──────────────────────────────────────────
def _match_sort_key(it: EventItem):
    # Live first, then by kickoff ascending. Live/no-kickoff sort before timed.
    if it.is_live or it.kickoff_utc is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    return (1, it.kickoff_utc)


def sort_matches(items: list[EventItem]) -> list[EventItem]:
    return sorted(items, key=_match_sort_key)


@dataclass
class LeagueGroup:
    league_key: str
    league_name: str
    country: Optional[str]
    rank: int
    items: list[EventItem] = field(default_factory=list)
    truncated: bool = False   # were there more than MAX_MATCHES_PER_LEAGUE?


def group_by_sport(items: list[EventItem]) -> list[tuple[str, list[EventItem]]]:
    """Group visible items by sport, ordered by item count desc then name."""
    by: dict[str, list[EventItem]] = {}
    for it in items:
        by.setdefault(it.sport, []).append(it)
    return sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def group_by_league(items: list[EventItem], *, max_leagues: int = MAX_LEAGUES,
                    max_matches: int = MAX_MATCHES_PER_LEAGUE) -> tuple[list[LeagueGroup], bool]:
    """Group items into leagues sorted by priority then name; matches sorted by
    kickoff. Applies pagination caps and reports truncation.

    Returns (groups, leagues_truncated).
    """
    by: dict[str, LeagueGroup] = {}
    for it in items:
        g = by.get(it.league_key)
        if g is None:
            g = LeagueGroup(it.league_key, it.league_name, it.country,
                            league_rank(it.league_name, it.country))
            by[it.league_key] = g
        g.items.append(it)

    groups = sorted(by.values(), key=lambda g: (g.rank, _norm(g.league_name)))
    leagues_truncated = len(groups) > max_leagues
    groups = groups[:max_leagues]

    for g in groups:
        ordered = sort_matches(g.items)
        g.truncated = len(ordered) > max_matches
        g.items = ordered[:max_matches]
    return groups, leagues_truncated
