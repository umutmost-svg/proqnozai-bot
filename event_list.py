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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from config import MOSTBET_SRC_TZ
from mostbet import _is_outright_market, _is_virtual_match
from priority_config import is_pure_stage_label, normalize_participant_tokens
from priority_engine import PriorityInput, compute_priority

PROVIDER = "mostbet"

# Grace fallback ONLY used when the provider gives no explicit status: a
# non-live fixture whose kickoff is more than this far in the past is treated as
# finished/stale and dropped. 3h30m comfortably exceeds a full match with
# stoppages, half-time and post-match settling, so we never hide a game that is
# merely running late without a live flag.
FINISHED_GRACE = timedelta(hours=3, minutes=30)

# Buckets.
LIVE, TODAY, TOMORROW, LATER = "LIVE", "TODAY", "TOMORROW", "LATER"

# LATER window: matches further out are hidden even when include_later=True.
# Keep in sync with the forecast policy (match_too_far: forecasts cover the
# next 7 days) — the menu must not hide what the bot is willing to analyse
# (a World Cup final 5 days ahead was invisible), nor offer what it refuses.
MAX_DAYS_AHEAD = 7

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
    # Stage/round text (e.g. "Play-off", "Semi-final"), separated from
    # league_name so grouping/prestige never mixes with stage detection — see
    # _resolve_competition. Empty when Mostbet gave no round information
    # (the common case for domestic leagues).
    stage_raw: str = ""
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
    # Match Priority Engine output (0-100), assigned by assign_priority_scores.
    # None until assigned; every function that sorts/groups by priority
    # assigns it first, so a caller can also compute it directly if needed.
    priority_score: Optional[int] = None

    @property
    def postponed(self) -> bool:
        return self.status == "postponed"


def _resolve_competition(sub: str, sup: str) -> tuple[str, str, Optional[str]]:
    """Split Mostbet's two free-text category fields into
    (competition_name, stage_raw, display_country).

    Mostbet has no dedicated round/stage field. Normally
    (subcategory=competition, supercategory=country/region) — e.g.
    sub="Premier League", sup="England". But some international fixtures
    instead put ONLY the round/stage label in the subcategory ("Play-off")
    while the supercategory holds the real competition name ("World Cup
    2026"); is_pure_stage_label distinguishes that shape from a subcategory
    that is genuinely the competition name, so grouping never fragments one
    tournament by round. A stage word embedded INSIDE a longer subcategory
    string (e.g. a hypothetical "Champions League - Semi-final") is
    deliberately not parsed apart here — unverified heuristic, out of scope.
    """
    sub = (sub or "").strip()
    sup = (sup or "").strip()
    if not sub and sup:
        # International feeds sometimes carry the tournament only in the
        # super category with an empty subcategory; dropping such rows
        # silently hid entire tournaments.
        return sup, "", None
    if sub:
        stage = is_pure_stage_label(sub)
        if stage and sup:
            return sup, sub, None
    return sub, "", (sup or None)


def normalize_fixture(raw: dict) -> Optional[EventItem]:
    """Map a raw Mostbet match to an EventItem, or None if it must not be shown.

    Rejects malformed, virtual/esports and outright fixtures. Requires an
    authoritative fixture_id, both team names and a competition name always;
    requires a tz-aware kickoff too, EXCEPT live fixtures (provider live
    flag/status) which may legitimately arrive without a scheduled kickoff.
    """
    if _is_virtual_match(raw) or _is_outright_market(raw):
        return None

    fid = raw.get("id")
    if fid is None:
        return None
    fixture_id = str(fid)

    home = (raw.get("team1Title") or "").strip()
    away = (raw.get("team2Title") or "").strip()
    league_name, stage_raw, country = _resolve_competition(
        raw.get("lineSubCategory") or "", raw.get("lineSuperCategory") or "")
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

    # Grouping identity: a stable provider tournamentId is authoritative and
    # never drifts across rounds, so it takes priority over the free-text
    # competition name (which two different tournaments could coincidentally
    # share). stage_raw NEVER participates in this key — that is the whole
    # point of separating it from league_name.
    if league_id:
        league_key = _slug(f"{sport}-{league_id}") or "unknown"
    else:
        league_key = _slug(f"{sport}-{country or ''}-{league_name}") or "unknown"

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
        stage_raw=stage_raw,
        league_id=league_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        league_key=league_key,
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
    return LATER if (include_later and d <= MAX_DAYS_AHEAD) else None


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


# ─── Match Priority Engine integration ────────────────────────────────────────
def _priority_input(it: EventItem, now_utc: datetime, demand: Optional[dict]) -> PriorityInput:
    demand_count = 0
    if demand:
        key = normalize_participant_tokens(f"{it.home} {it.away}")
        demand_count = demand.get(key, 0)
    return PriorityInput(
        league_name=it.league_name,
        country=it.country,
        home=it.home,
        away=it.away,
        is_live=it.is_live,
        kickoff_utc=it.kickoff_utc,
        now_utc=now_utc,
        stage_hint=it.stage_raw,
        demand_count=demand_count,
    )


def assign_priority_scores(items: list[EventItem], now_utc: Optional[datetime] = None,
                           demand: Optional[dict] = None) -> None:
    """Compute and attach `priority_score` to each item IN PLACE (same style as
    the `bucket` assignment in select_visible). Idempotent — safe to call more
    than once on the same items.

    `now_utc` defaults to the real current time so callers that only care about
    tournament/team-derived priority (not time-sensitivity) don't have to pass
    it — this is the ONLY place in this otherwise clock-free module that may
    read the real clock, and only on that default path. Production call sites
    (handlers/forecast.py) always pass the already-computed now_utc explicitly.
    """
    now = now_utc or datetime.now(timezone.utc)
    for it in items:
        it.priority_score = compute_priority(_priority_input(it, now, demand)).total


# ─── Grouping / sorting / pagination ──────────────────────────────────────────
def _match_sort_key(it: EventItem):
    """Deterministic order: priority_score DESC, is_live DESC, kickoff_utc ASC,
    normalized competition_name/home/away ASC, and finally the provider
    fixture_id ASC as an absolute last resort — so items with equal priority
    (including two fully identical fixtures) never depend on input/iteration
    order, regardless of the order Mostbet's feed happened to return them in."""
    score = it.priority_score if it.priority_score is not None else 0
    live_rank = 0 if it.is_live else 1
    kickoff = it.kickoff_utc if it.kickoff_utc is not None else datetime.max.replace(tzinfo=timezone.utc)
    return (-score, live_rank, kickoff, _norm(it.league_name), it.home_team_key,
            it.away_team_key, _fixture_id_sort_key(it.fixture_id))


def _fixture_id_sort_key(fixture_id: str) -> tuple[int, object]:
    """Numeric fixture ids sort numerically (so "9" < "10"); any non-numeric
    canonical event key still sorts deterministically as a string."""
    try:
        return (0, int(fixture_id))
    except (TypeError, ValueError):
        return (1, fixture_id)


def sort_matches(items: list[EventItem]) -> list[EventItem]:
    return sorted(items, key=_match_sort_key)


@dataclass
class LeagueGroup:
    league_key: str
    league_name: str
    country: Optional[str]
    items: list[EventItem] = field(default_factory=list)


def group_by_sport(items: list[EventItem]) -> list[tuple[str, list[EventItem]]]:
    """Group visible items by sport, ordered by item count desc then name."""
    by: dict[str, list[EventItem]] = {}
    for it in items:
        by.setdefault(it.sport, []).append(it)
    return sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def _group_sort_key(g: "LeagueGroup"):
    # A league ranks by its single most important match; ties break on the
    # normalized league name, then on the absolute league_key, so order never
    # depends on dict iteration order (e.g. Mostbet feed order) even when two
    # distinct provider tournaments share an identical display name.
    best_score = max((it.priority_score or 0) for it in g.items)
    return (-best_score, _norm(g.league_name), g.league_key)


def group_by_league(items: list[EventItem], *, now_utc: Optional[datetime] = None,
                    demand: Optional[dict] = None) -> list[LeagueGroup]:
    """Group items into leagues ranked by the Match Priority Engine (each
    league ranked by its most important contained match); matches within a
    league sorted by the same priority order.

    Returns ALL leagues and ALL matches within each — no cap. The Telegram UI
    paginates ("show more") in the handler layer instead of a hard cutoff; see
    `paginate` below.

    `now_utc`/`demand` are forwarded to assign_priority_scores; see its
    docstring for the now_utc default-to-real-clock convenience.
    """
    assign_priority_scores(items, now_utc, demand)

    by: dict[str, LeagueGroup] = {}
    for it in items:
        g = by.get(it.league_key)
        if g is None:
            g = LeagueGroup(it.league_key, it.league_name, it.country)
            by[it.league_key] = g
        g.items.append(it)

    groups = sorted(by.values(), key=_group_sort_key)
    for g in groups:
        g.items = sort_matches(g.items)
    return groups


# ─── Pagination ("show more" instead of a hard cutoff) ─────────────────────────
PAGE_SIZE = 10


def paginate(seq: list, page: int, page_size: int = PAGE_SIZE) -> tuple[list, int, bool, bool]:
    """Slice `seq` at `page` (0-indexed). Returns
    (page_items, clamped_page, has_prev, has_next).

    An out-of-range page clamps to the last valid page rather than raising or
    returning an empty screen — a stale/late "next page" tap can never land on
    nothing. Callers MUST use the returned `clamped_page` (not the `page` they
    passed in) for any absolute-index math (offsets) or further pagination
    callbacks — otherwise a stale/out-of-range page number renders a page's
    worth of items with the WRONG absolute indices/callback_data, since only
    the slice itself was clamped, not the caller's own arithmetic."""
    if not seq:
        return [], 0, False, False
    total_pages = max(1, (len(seq) + page_size - 1) // page_size)
    clamped = max(0, min(page, total_pages - 1))
    start = clamped * page_size
    return seq[start:start + page_size], clamped, clamped > 0, (clamped + 1) < total_pages


# ─── Day filter ─────────────────────────────────────────────────────────────
DAY_ALL = "ALL"
DAY_LIVE = "LIVE"
DAY_TODAY = "TODAY"
DAY_TOMORROW = "TOMORROW"


def _item_local_date(it: EventItem, user_tz: timezone) -> Optional[date]:
    if it.kickoff_utc is None:
        return None
    return it.kickoff_utc.astimezone(user_tz).date()


def available_day_options(items: list[EventItem], user_tz: timezone) -> list[tuple[str, int]]:
    """Ordered (day_key, count) options actually present in `items`: LIVE,
    TODAY, TOMORROW, then specific ISO dates for anything further out (the
    LATER bucket) — each included only when at least one item falls in it.
    Does NOT include DAY_ALL; the caller adds that as a fixed "show everything"
    option."""
    bucket_counts: dict[str, int] = {}
    dated_counts: dict[str, int] = {}
    for it in items:
        if it.bucket == LIVE:
            bucket_counts[DAY_LIVE] = bucket_counts.get(DAY_LIVE, 0) + 1
        elif it.bucket == TODAY:
            bucket_counts[DAY_TODAY] = bucket_counts.get(DAY_TODAY, 0) + 1
        elif it.bucket == TOMORROW:
            bucket_counts[DAY_TOMORROW] = bucket_counts.get(DAY_TOMORROW, 0) + 1
        else:
            d = _item_local_date(it, user_tz)
            if d is not None:
                key = d.isoformat()
                dated_counts[key] = dated_counts.get(key, 0) + 1

    options = [(key, bucket_counts[key]) for key in (DAY_LIVE, DAY_TODAY, DAY_TOMORROW)
               if bucket_counts.get(key)]
    options.extend((key, dated_counts[key]) for key in sorted(dated_counts))
    return options


def filter_by_day(items: list[EventItem], day_key: str, user_tz: timezone) -> list[EventItem]:
    """Restrict `items` to one day option from available_day_options, or
    return all items unchanged for DAY_ALL / an unrecognized key."""
    if day_key == DAY_LIVE:
        return [it for it in items if it.bucket == LIVE]
    if day_key == DAY_TODAY:
        return [it for it in items if it.bucket == TODAY]
    if day_key == DAY_TOMORROW:
        return [it for it in items if it.bucket == TOMORROW]
    try:
        target = datetime.fromisoformat(day_key).date()
    except ValueError:
        return list(items)
    return [it for it in items if it.bucket not in (LIVE, TODAY, TOMORROW)
            and _item_local_date(it, user_tz) == target]


# ─── Country/region filter ──────────────────────────────────────────────────
COUNTRY_ALL = "ALL"
COUNTRY_INTERNATIONAL = "International"


def available_countries(items: list[EventItem]) -> list[tuple[str, int]]:
    """(country, count) — country is COUNTRY_INTERNATIONAL when the item has
    none — sorted by count desc then name asc. Does NOT include COUNTRY_ALL;
    the caller adds that as a fixed "show everything" option."""
    counts: dict[str, int] = {}
    for it in items:
        key = it.country or COUNTRY_INTERNATIONAL
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def filter_by_country(items: list[EventItem], country_key: str) -> list[EventItem]:
    if country_key == COUNTRY_ALL:
        return list(items)
    if country_key == COUNTRY_INTERNATIONAL:
        return [it for it in items if not it.country]
    return [it for it in items if it.country == country_key]
