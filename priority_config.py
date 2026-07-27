"""Static, hand-curated configuration for the Match Priority Engine (MVP).

Leaf module: no internal imports (like config.py / translations.py), so any
layer may depend on it without violating the top-down dependency rule in
ARCHITECTURE.md. Extend these tables in dedicated PRs as coverage grows.

Nothing here talks to the network or the database — pure data plus small pure
helpers for tokenizing/normalizing names, shared by db.py, event_list.py and
priority_engine.py so there is exactly one normalization implementation.
"""
from __future__ import annotations

import re

# ─── Name normalization (shared) ───────────────────────────────────────────────
# Tokenization style similar to mostbet._norm_tokens / match_validation._tokens,
# but returns a token frozenset rather than a hyphen-slug/fuzzy-overlap score,
# since callers here need EXACT set equality (curated tier/derby lookups,
# demand-key grouping), not approximate matching. Deliberately a SHORTER noise
# list than mostbet._NOISE: words like "united"/"city"/"atletico"/"sporting"
# are dropped there for fuzzy matching, but stripping them here would collapse
# distinct real clubs onto the same key (e.g. "Manchester United" and
# "Manchester City" would both become {"manchester"}) — only strip cosmetic
# club-type words that never distinguish one real club from another.
_NOISE_TOKENS = {
    "fc", "cf", "ac", "sc", "afc", "fk", "sk", "bk", "rsc", "rc", "ud", "cd",
    "sd", "club", "the", "vs", "v",
}


def normalize_participant_tokens(text: str | None) -> frozenset[str]:
    """Token-set key for a name or an unordered pair of names written as free
    text ("Team A Team B" / "Team A vs Team B"). Two names/pairs normalize to
    the same key regardless of word order, case or minor punctuation."""
    if not text:
        return frozenset()
    low = re.sub(r"[^\w\s]", " ", text.lower())
    return frozenset(t for t in low.split() if len(t) > 1 and t not in _NOISE_TOKENS)


def _keys(names: frozenset[str]) -> frozenset[frozenset[str]]:
    return frozenset(normalize_participant_tokens(n) for n in names)


# ─── Tournament prestige tiers ──────────────────────────────────────────────────
# (name substring, optional country substring, tier). Country disambiguates
# domestic "Premier League"s (England vs Azerbaijan). First matching entry
# wins; unlisted tournaments fall back to DEFAULT_TOURNAMENT_TIER.
TOURNAMENT_TIERS: tuple[tuple[str, str | None, int], ...] = (
    # Tennis, basketball and MMA entries are checked before the generic
    # football "euro"/"league" keywords below, since e.g. "EuroLeague"
    # would otherwise match the broader "euro" substring first.
    # Tennis: the four majors are the sport's undisputed top tier; Masters
    # 1000 / WTA 1000 and the season-ending Finals are a clear step below.
    ("wimbledon", None, 1),
    ("us open", None, 1),
    ("roland garros", None, 1),
    ("french open", None, 1),
    ("australian open", None, 1),
    ("atp finals", None, 2),
    ("wta finals", None, 2),
    ("masters 1000", None, 2),
    # Basketball: NBA is the globally dominant league; EuroLeague is the
    # top continental club competition, a step below.
    ("nba", None, 1),
    ("euroleague", None, 2),
    # MMA: UFC is the sport's premier promotion by a wide margin.
    ("ufc", None, 1),
    ("bellator", None, 2),
    ("champions league", None, 1),
    ("world cup", None, 1),
    # Europa (Conference) League must be checked before the bare "euro"
    # keyword below: "euro" is a substring of "europa", so "UEFA Europa
    # League" / "UEFA Europa Conference League" would otherwise match the
    # generic Euro-Championship keyword first and be scored as tier 1.
    ("europa league", None, 2),
    ("conference league", None, 2),
    ("euro", None, 1),               # European Championship / Euros
    ("premier league", "england", 2),
    ("la liga", "spain", 2),
    ("serie a", "italy", 2),
    ("bundesliga", "germany", 2),
    ("ligue 1", "france", 2),
    ("super lig", "turkey", 2),       # Süper Lig (diacritics normalized upstream)
    ("copa libertadores", None, 2),
    ("premier league", "azerbaijan", 3),
    ("championship", "england", 3),
    ("primeira liga", None, 3),
    ("eredivisie", None, 3),
)

TOURNAMENT_TIER_POINTS: dict[int, float] = {1: 30.0, 2: 18.0, 3: 10.0, 4: 3.0}
DEFAULT_TOURNAMENT_TIER = 4


def _norm_league(s: str | None) -> str:
    s = (s or "").lower()
    return s.replace("ü", "u").replace("ı", "i").replace("ə", "a")


def tournament_tier(league_name: str | None, country: str | None) -> int:
    """Lookup tier 1 (highest prestige) .. 4 (default/unlisted)."""
    n, c = _norm_league(league_name), _norm_league(country)
    for kw, country_hint, tier in TOURNAMENT_TIERS:
        if kw in n and (country_hint is None or country_hint in c):
            return tier
    return DEFAULT_TOURNAMENT_TIER


# ─── Tournament stage detection (multilingual) ─────────────────────────────────
STAGE_POINTS: dict[str, float] = {
    "final": 15.0, "semifinal": 11.0, "quarterfinal": 7.0,
    "playoff": 5.0, "group": 1.0,
}
DEFAULT_STAGE_POINTS = 0.0  # stage unknown / not detected

# Checked in this order (most specific first) so "semifinal" never matches
# the looser "final" substring pattern, etc.
_STAGE_ORDER: tuple[str, ...] = ("final", "semifinal", "quarterfinal", "playoff", "group")

STAGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "final": (
        r"\bfinal\b",            # en/az/tr share the "final" spelling
        r"\bфинал\w*",           # ru
    ),
    "semifinal": (
        r"\bsemi[\s-]?final\b", r"\byar[ıi]\s?final\b",  # en/az/tr
        r"\bярымфинал", r"\bполуфинал\w*",               # kz / ru
    ),
    "quarterfinal": (
        r"\bquarter[\s-]?final\b", r"\bçeyrek\s?final\b",  # en/tr
        r"\bçərəkfinal\b",                                  # az
        r"\bчетвертьфинал\w*",                              # ru
    ),
    "playoff": (
        r"\bplay[\s-]?off\b", r"\bплей-?офф\w*",
        r"\bround of \d+\b", r"\b1/(2|4|8|16|32)\b",
    ),
    "group": (
        r"\bgroup\s?stage\b", r"\bgroup\s?[a-h]\b",
        r"\bgrupp\w*", r"\bгрупп\w*", r"\bqrup\w*",
    ),
}

_STAGE_RE: dict[str, re.Pattern] = {
    stage: re.compile("|".join(pats), re.IGNORECASE) for stage, pats in STAGE_PATTERNS.items()
}


def detect_stage(*texts: str | None) -> str | None:
    """First recognized canonical stage across the given free-text fields
    (most specific stage first), or None if nothing is recognized. Absence of
    a recognized stage is NOT a negative signal — see priority_engine."""
    joined = " ".join(t for t in texts if t)
    if not joined:
        return None
    for stage in _STAGE_ORDER:
        if _STAGE_RE[stage].search(joined):
            return stage
    return None


def is_pure_stage_label(text: str | None) -> str | None:
    """Return the canonical stage if `text`, taken as a WHOLE field, is
    nothing but a stage/round label ("Play-off", "Semi-final") — as opposed to
    a stage word embedded inside a longer competition name ("Champions League
    - Semi-final"). A match only counts as "pure" when removing the matched
    stage substring leaves no other word characters behind.

    Used by event_list.normalize_fixture to decide whether Mostbet's
    subcategory field is standing in for the round (and the real competition
    name lives in the supercategory field instead) versus is itself the
    competition name. This is the ONLY heuristic swap performed — a stage
    embedded inside a longer subcategory string is deliberately NOT parsed
    apart (unverified against a live Mostbet feed; see the field-mapping
    diagnostic script)."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    for stage in _STAGE_ORDER:
        for pat in STAGE_PATTERNS[stage]:
            m = re.search(pat, stripped, re.IGNORECASE)
            if not m:
                continue
            remainder = stripped[:m.start()] + stripped[m.end():]
            remainder = re.sub(r"[^\w]", "", remainder)
            if not remainder:
                return stage
    return None


# ─── Team popularity tiers ──────────────────────────────────────────────────────
# Curated, editorial list — deliberately NOT derived from odds (a short-priced
# favorite is not necessarily a popular team; see priority_engine module doc).
# Extend in dedicated PRs. Tier 3 is an open extension point, empty for MVP.
TEAM_POPULARITY_TIER1: frozenset[str] = frozenset({
    "real madrid", "barcelona", "manchester united", "manchester city",
    "liverpool", "chelsea", "arsenal", "bayern munich", "psg",
    "juventus", "inter", "ac milan",
    # Tennis (players stand in for "teams" — same two-participant scoring)
    "novak djokovic", "rafael nadal", "roger federer", "carlos alcaraz",
    "iga swiatek", "serena williams",
    # Basketball (NBA franchises with the widest global following)
    "los angeles lakers", "golden state warriors", "boston celtics", "chicago bulls",
    # MMA (UFC's most globally recognized names)
    "conor mcgregor", "khabib nurmagomedov", "jon jones", "israel adesanya",
})
TEAM_POPULARITY_TIER2: frozenset[str] = frozenset({
    "tottenham", "atletico madrid", "borussia dortmund", "napoli",
    "roma", "sevilla", "ajax", "porto", "benfica", "galatasaray",
    "fenerbahce", "besiktas", "marseille", "lyon", "leipzig",
    # Tennis
    "daniil medvedev", "jannik sinner", "andy murray", "naomi osaka", "coco gauff",
    # Basketball
    "brooklyn nets", "miami heat", "milwaukee bucks", "denver nuggets",
    # MMA
    "charles oliveira", "alexander volkanovski", "francis ngannou",
})
TEAM_POPULARITY_TIER3: frozenset[str] = frozenset()

TEAM_POPULARITY_POINTS: dict[int, float] = {1: 10.0, 2: 6.0, 3: 3.0}
TEAM_POPULARITY_MAX_TOTAL = 20.0  # cap for the summed two-participant popularity

TEAM_POPULARITY_TIER1_KEYS = _keys(TEAM_POPULARITY_TIER1)
TEAM_POPULARITY_TIER2_KEYS = _keys(TEAM_POPULARITY_TIER2)
TEAM_POPULARITY_TIER3_KEYS = _keys(TEAM_POPULARITY_TIER3)


# ─── Derby / rivalry pairs ───────────────────────────────────────────────────────
DERBY_POINTS = 15.0

DERBY_PAIRS: frozenset[frozenset[str]] = frozenset({
    frozenset({"real madrid", "barcelona"}),
    frozenset({"real madrid", "atletico madrid"}),
    frozenset({"manchester united", "manchester city"}),
    frozenset({"manchester united", "liverpool"}),
    frozenset({"arsenal", "tottenham"}),
    frozenset({"chelsea", "arsenal"}),
    frozenset({"liverpool", "everton"}),
    frozenset({"ac milan", "inter"}),
    frozenset({"roma", "lazio"}),
    frozenset({"galatasaray", "fenerbahce"}),
    frozenset({"galatasaray", "besiktas"}),
    frozenset({"fenerbahce", "besiktas"}),
    frozenset({"boca juniors", "river plate"}),
    frozenset({"celtic", "rangers"}),
})

DERBY_PAIR_KEYS: frozenset[frozenset[frozenset[str]]] = frozenset(
    frozenset({normalize_participant_tokens(a), normalize_participant_tokens(b)})
    for a, b in (tuple(pair) for pair in DERBY_PAIRS)
)


# ─── Time proximity / live ───────────────────────────────────────────────────────
TIME_PROXIMITY_LIVE = 15.0
# (max_hours_until_kickoff, points) — first matching band wins, ascending order.
TIME_PROXIMITY_BANDS: tuple[tuple[float, float], ...] = (
    (1.0, 12.0),
    (6.0, 9.0),
    (24.0, 6.0),
    (72.0, 3.0),
    (168.0, 1.0),   # up to 7 days ahead
)
TIME_PROXIMITY_DEFAULT = 0.0


# ─── Internal user demand ────────────────────────────────────────────────────────
DEMAND_MAX_POINTS = 5.0
DEMAND_CAP_REFERENCE = 20   # unique users (14-day window) that earn the full bonus
