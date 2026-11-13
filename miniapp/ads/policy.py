"""The published ads snapshot `ads-policy.json` (ads-architecture.md §2): format, validation, atomic write.

admin.db is the source of truth; the admin process publishes a full snapshot of the campaigns that may
be shown (active, inside their window) into the shared data directory, and the Mini App re-reads it.
The snapshot is not authoritative: it can be rebuilt from admin.db at any time. Every field the Mini
App relies on is validated again when it reads the file (links against the partner's hosts, text
limits, weights, the audience tree), so a corrupted or hand-edited file degrades to "no ads", never
to a wider audience or a foreign link.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from miniapp.ads import targeting
from miniapp.store import SUPPORTED_LANGS

SCHEMA = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CAMPAIGNS = 200
MAX_AUDIENCE_IDS = 200_000  # all audience lists of one snapshot together

Tier = Literal["individual", "segment", "general"]
TIERS: tuple[Tier, ...] = ("individual", "segment", "general")
PLACEMENTS = ("quick_pick", "ai_pick")
VARIANT_KEYS = ("A", "B", "C")
TEXT_LIMITS = {"headline": 80, "body": 300, "extra": 120, "cta": 30}
DISCLOSURE_LIMITS = {"advertiser": 120, "adId": 64, "ageLabel": 8}
MAX_URL = 500
MAX_HOSTS = 20
MAX_AGE = 25
# How an offer treats the price of the recommended market (always from the verified odds store, never
# from the campaign): "optional" — shown with the price when there is one, without it otherwise;
# "required" — not shown at all without a verified price.
ODDS_MODES = ("optional", "required")
MAX_CAP_PER_DAY = 100
MAX_CAP_TOTAL = 1000
# The only macro a partner link may carry: replaced on each click by that click's own id (postback key).
CLICK_MACRO = "{click_id}"
_CLICK_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# A price-like number in an advertiser's text ("1.85", "2,5"): prices come from the odds store only.
_ODDS_IN_TEXT = re.compile(r"(?<![\d.,·٫])\d{1,3}[.,·٫]\d{1,3}(?![\d.,·٫%])")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")


class PolicyError(ValueError):
    """Invalid campaign field or snapshot; the message is a stable code."""


# ---- field validators (the admin editor and the snapshot reader share them) ---------------------


def check_text(value: Any, field: str, limit: int, required: bool = True, multiline: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise PolicyError(f"invalid_{field}")
    text = value.strip()
    if (required and not text) or len(text) > limit or _CONTROL.search(text.replace("\n", "") if multiline else text):
        raise PolicyError(f"invalid_{field}")
    return text


def check_ad_text(value: Any, field: str, limit: int, required: bool = True, multiline: bool = False) -> str:
    """An advertiser's text: check_text plus no price-like numbers (an offer never states odds itself)."""
    text = check_text(value, field, limit, required, multiline)
    # NFKC folds look-alike digits and separators (fullwidth "１．８５", superscripts) into the plain ones first.
    if _ODDS_IN_TEXT.search(unicodedata.normalize("NFKC", text)):
        raise PolicyError("odds_in_text")
    return text


def check_host(value: Any) -> str:
    if not isinstance(value, str) or not _HOST.match(value):
        raise PolicyError("invalid_host")
    return value


def check_hosts(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_HOSTS:
        raise PolicyError("invalid_host")
    return sorted({check_host(h) for h in value})


def check_countries(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 300 or not all(
            isinstance(c, str) and targeting.COUNTRY.match(c) and c not in targeting.UNKNOWN_COUNTRIES for c in value):
        raise PolicyError("invalid_countries")
    return sorted(set(value))


def check_url(value: Any, hosts: list[str] | tuple[str, ...]) -> str:
    """https only, the exact host from the partner's list, no user info, no port, no control characters.
    The link may carry the {click_id} macro (outside the host); no other braces."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL or value != value.strip():
        raise PolicyError("invalid_url")
    if "{" in value or "}" in value:
        rest = value.replace(CLICK_MACRO, "")
        if "{" in rest or "}" in rest:
            raise PolicyError("invalid_url")
        _check_plain_url(value.replace(CLICK_MACRO, "c" * 32), hosts)
        if urlsplit(value).hostname != urlsplit(value.replace(CLICK_MACRO, "c" * 32)).hostname:
            raise PolicyError("invalid_url")  # the macro may not touch the host
        if CLICK_MACRO in urlsplit(value).fragment:
            raise PolicyError("invalid_url")  # a fragment never reaches the partner: its postback could not match
        return value
    return _check_plain_url(value, hosts)


def render_url(template: str, click_id: str, hosts: list[str] | tuple[str, ...]) -> str:
    """The partner link of one click: the macro replaced by the click id, checked once more as a plain link."""
    if not _CLICK_ID.match(click_id):
        raise PolicyError("invalid_click_id")
    return _check_plain_url(template.replace(CLICK_MACRO, click_id), hosts)


def _check_plain_url(value: str, hosts: list[str] | tuple[str, ...]) -> str:
    if len(value) > MAX_URL + 64:
        raise PolicyError("invalid_url")
    if _CONTROL.search(value) or "\\" in value or " " in value:
        raise PolicyError("invalid_url")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise PolicyError("invalid_url") from None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or port is not None:
        raise PolicyError("invalid_url")
    if "@" in parts.netloc or parts.hostname is None or parts.netloc != parts.hostname:
        raise PolicyError("invalid_url")  # the netloc is exactly a lower-case host name
    if parts.hostname not in hosts:
        raise PolicyError("url_host_not_allowed")
    return value


def check_variants(value: Any) -> list[dict[str, Any]]:
    """1..3 variants A/B/C; weights are whole percents summing to 100 (the control group is separate)."""
    if not isinstance(value, list) or not 1 <= len(value) <= len(VARIANT_KEYS):
        raise PolicyError("invalid_variants")
    keys = [v.get("key") if isinstance(v, dict) else None for v in value]
    if len(set(keys)) != len(keys) or not all(k in VARIANT_KEYS for k in keys):
        raise PolicyError("invalid_variants")
    weights = [v.get("weight") for v in value]
    if not all(isinstance(w, int) and not isinstance(w, bool) and 1 <= w <= 100 for w in weights) or sum(weights) != 100:
        raise PolicyError("invalid_weights")
    return sorted(value, key=lambda v: v["key"])


def check_disclosure(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DISCLOSURE_LIMITS):
        raise PolicyError("invalid_disclosure")
    return {k: check_text(value.get(k), "disclosure", limit, required=False) for k, limit in DISCLOSURE_LIMITS.items()}


def _int(value: Any, low: int, high: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PolicyError(code)
    return value


# ---- snapshot model -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    key: str
    weight: int
    headline: str
    body: str
    extra: str
    cta: str
    url: str


@dataclass(frozen=True)
class Campaign:
    id: int
    tier: Tier
    priority: int
    starts_at: int
    ends_at: int
    lang_in: tuple[str, ...]          # empty: every language
    placements: tuple[str, ...]
    countries: tuple[str, ...]        # effective: partner's list narrowed by the campaign
    allow_unknown_country: bool
    age_min: int                      # 0: no age confirmation required
    partner_id: int
    hosts: tuple[str, ...]
    disclosure: dict[str, str]
    condition: dict[str, Any] | None
    salt: str
    control_pct: int
    variants: tuple[Variant, ...]
    odds_mode: str = "optional"      # ODDS_MODES
    cap_per_day: int = 0              # impressions per user per UTC day (other matches); 0: no cap
    cap_total: int = 0                # impressions per user while events are kept (90 days); 0: no cap


@dataclass(frozen=True)
class DisplaySettings:
    """Global display rules (ads-architecture.md §9), above every partner and campaign."""
    blocked_countries: frozenset[str] = frozenset()   # never shown there, whatever a partner allows
    age_gate: bool = False                            # every offer requires the viewer's own age statement
    age_min: int = 18                                 # the age that statement must reach when age_gate is on


@dataclass(frozen=True)
class Policy:
    version: int
    generated_at: int
    campaigns: tuple[Campaign, ...]
    audiences: dict[str, frozenset[int]]
    sha256: str
    settings: DisplaySettings = DisplaySettings()
    # partner id -> SHA-256 (hex) of its postback token; partners without one cannot post conversions
    postback_hashes: dict[int, str] | None = None


def _campaign(raw: Any, audiences: dict[str, frozenset[int]]) -> Campaign:
    if not isinstance(raw, dict):
        raise PolicyError("invalid_campaign")
    hosts = tuple(check_hosts(raw.get("hosts")))
    variants = []
    for v in check_variants(raw.get("variants")):
        variants.append(Variant(
            key=v["key"], weight=v["weight"],
            headline=check_ad_text(v.get("headline"), "headline", TEXT_LIMITS["headline"]),
            body=check_ad_text(v.get("body"), "body", TEXT_LIMITS["body"], required=False, multiline=True),
            extra=check_ad_text(v.get("extra"), "extra", TEXT_LIMITS["extra"], required=False),
            cta=check_ad_text(v.get("cta"), "cta", TEXT_LIMITS["cta"]),
            url=check_url(v.get("url"), hosts)))
    langs = raw.get("langIn")
    if not isinstance(langs, list) or not all(lang in SUPPORTED_LANGS for lang in langs):
        raise PolicyError("invalid_langs")
    placements = raw.get("placements")
    if not isinstance(placements, list) or not placements or not all(p in PLACEMENTS for p in placements):
        raise PolicyError("invalid_placements")
    tier = raw.get("tier")
    if tier not in TIERS:
        raise PolicyError("invalid_tier")
    starts, ends = _int(raw.get("startsAt"), 0, 2**40, "invalid_window"), _int(raw.get("endsAt"), 0, 2**40, "invalid_window")
    if ends <= starts:
        raise PolicyError("invalid_window")
    experiment = raw.get("experiment")
    if not isinstance(experiment, dict) or not isinstance(experiment.get("salt"), str) or not 8 <= len(
            experiment["salt"]) <= 64:
        raise PolicyError("invalid_experiment")
    try:
        condition = targeting.validate(raw.get("condition"), "snapshot")
    except targeting.ConditionError as exc:
        raise PolicyError(str(exc)) from None
    for name in _audience_refs(condition):
        if name not in audiences:
            raise PolicyError("unknown_audience")
    allow_unknown = raw.get("allowUnknownCountry")
    if not isinstance(allow_unknown, bool):
        raise PolicyError("invalid_countries")
    # Fields added after schema 1 shipped: absent = the old behaviour (a newer file stays readable).
    odds_mode = raw.get("oddsMode", "optional")
    if odds_mode not in ODDS_MODES:
        raise PolicyError("invalid_odds_mode")
    caps = check_caps(raw.get("caps"))
    return Campaign(
        id=_int(raw.get("id"), 1, 2**53, "invalid_campaign"), tier=tier,
        priority=_int(raw.get("priority"), 0, 1000, "invalid_priority"), starts_at=starts, ends_at=ends,
        lang_in=tuple(sorted(set(langs))), placements=tuple(sorted(set(placements))),
        countries=tuple(check_countries(raw.get("countries"))), allow_unknown_country=allow_unknown,
        age_min=_int(raw.get("ageMin"), 0, MAX_AGE, "invalid_age"), partner_id=_int(raw.get("partnerId"), 1, 2**53,
                                                                                 "invalid_partner"),
        hosts=hosts, disclosure=check_disclosure(raw.get("disclosure")), condition=condition,
        salt=experiment["salt"], control_pct=_int(experiment.get("controlPct"), 0, 50, "invalid_experiment"),
        variants=tuple(variants), odds_mode=odds_mode, cap_per_day=caps["perDay"], cap_total=caps["total"])


def check_caps(value: Any) -> dict[str, int]:
    """Frequency caps {perDay, total}; absent or 0 = no cap."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"perDay", "total"}:
        raise PolicyError("invalid_caps")
    return {"perDay": _int(value.get("perDay", 0), 0, MAX_CAP_PER_DAY, "invalid_caps"),
            "total": _int(value.get("total", 0), 0, MAX_CAP_TOTAL, "invalid_caps")}


def check_settings(value: Any) -> DisplaySettings:
    if value is None:
        return DisplaySettings()
    if not isinstance(value, dict) or set(value) - {"blockedCountries", "ageGate", "ageMin"}:
        raise PolicyError("invalid_settings")
    gate = value.get("ageGate", False)
    if not isinstance(gate, bool):
        raise PolicyError("invalid_settings")
    return DisplaySettings(blocked_countries=frozenset(check_countries(value.get("blockedCountries", []))),
                           age_gate=gate, age_min=_int(value.get("ageMin", 18), 1, MAX_AGE, "invalid_settings"))


def _postback_hashes(value: Any) -> dict[int, str]:
    """{partner id: SHA-256 of its postback token}."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_CAMPAIGNS:
        raise PolicyError("bad_partners")
    out: dict[int, str] = {}
    for key, entry in value.items():
        if (not isinstance(key, str) or not key.isdigit() or len(key) > 15 or not isinstance(entry, dict)
                or set(entry) != {"postbackSha256"}):
            raise PolicyError("bad_partners")
        digest = entry["postbackSha256"]
        if not isinstance(digest, str) or not _SHA256_HEX.match(digest):
            raise PolicyError("bad_partners")
        out[_int(int(key), 1, 2**53, "bad_partners")] = digest
    return out


def apply_settings(c: Campaign, settings: DisplaySettings) -> Campaign:
    """The campaign as the global rules allow it: blocked countries removed, the age gate raised."""
    countries = tuple(x for x in c.countries if x not in settings.blocked_countries)
    age = max(c.age_min, settings.age_min) if settings.age_gate else c.age_min
    return replace(c, countries=countries, age_min=age)


def _audience_refs(node: dict[str, Any] | None) -> set[str]:
    if node is None:
        return set()
    (key, value), = node.items()
    if key in ("all", "any"):
        return set().union(*(_audience_refs(c) for c in value))
    if key == "not":
        return _audience_refs(value)
    return {value} if key == "aud" else set()


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


_SEAL_FIELDS = ("sha256", "signature")


def _body(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in _SEAL_FIELDS}


def sign(body: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    """The file content: the body, the SHA-256 of its canonical form and the admin's Ed25519 signature of it.

    The checksum alone would not do: whoever can write the shared volume could recompute it. Only the admin
    process holds the private key; the Mini App verifies with the public key from its own env."""
    body = _body(body)
    content = canonical(body)
    return {**body, "sha256": hashlib.sha256(content).hexdigest(),
            "signature": base64.urlsafe_b64encode(key.sign(content)).decode().rstrip("=")}


def public_key_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def load_public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = bytes.fromhex(value)
        if len(raw) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise PolicyError("bad_public_key") from None


def parse(raw: bytes, public_key: Ed25519PublicKey) -> Policy:
    """Validates a snapshot file completely; any defect raises PolicyError (the reader keeps its last good one)."""
    if len(raw) > MAX_FILE_BYTES:
        raise PolicyError("too_large")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PolicyError("bad_json") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise PolicyError("bad_schema")
    content = canonical(_body(data))
    sha, signature = data.get("sha256"), data.get("signature")
    if not isinstance(sha, str) or hashlib.sha256(content).hexdigest() != sha:
        raise PolicyError("bad_checksum")
    try:
        public_key.verify(base64.urlsafe_b64decode(str(signature) + "=" * (-len(str(signature)) % 4)), content)
    except (InvalidSignature, ValueError, TypeError):
        raise PolicyError("bad_signature") from None
    version = _int(data.get("version"), 1, 2**62, "bad_version")
    generated = _int(data.get("generatedAt"), 0, 2**40, "bad_version")
    raw_aud = data.get("audiences")
    if not isinstance(raw_aud, dict):
        raise PolicyError("bad_audiences")
    audiences: dict[str, frozenset[int]] = {}
    total = 0
    for name, ids in raw_aud.items():
        if not isinstance(name, str) or not targeting.AUDIENCE_NAME.match(name) or not isinstance(ids, list):
            raise PolicyError("bad_audiences")
        if not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i < targeting.MAX_TG_ID for i in ids):
            raise PolicyError("bad_audiences")
        total += len(ids)
        audiences[name] = frozenset(ids)
    if total > MAX_AUDIENCE_IDS:
        raise PolicyError("too_large")
    campaigns = data.get("campaigns")
    if not isinstance(campaigns, list) or len(campaigns) > MAX_CAMPAIGNS:
        raise PolicyError("bad_campaigns")
    display = check_settings(data.get("settings"))
    kept = []
    for raw_campaign in campaigns:
        try:
            kept.append(apply_settings(_campaign(raw_campaign, audiences), display))
        except PolicyError as exc:
            # A text rule added later (no price-like numbers) drops only that campaign from a signed file the
            # previous validator accepted; any other defect still rejects the whole file.
            if str(exc) != "odds_in_text":
                raise
    parsed = tuple(kept)
    if len({c.id for c in parsed}) != len(parsed):
        raise PolicyError("bad_campaigns")
    return Policy(version=version, generated_at=generated, campaigns=parsed, audiences=audiences, sha256=sha,
                  settings=display, postback_hashes=_postback_hashes(data.get("partners")))


def read_version(path: Path, public_key: Ed25519PublicKey) -> tuple[int, str] | None:
    """(version, sha256) of a valid snapshot file at `path`, or None (missing or invalid)."""
    try:
        parsed = parse(path.read_bytes(), public_key)
        return parsed.version, parsed.sha256
    except (OSError, PolicyError):
        return None


def write_atomic(path: Path, content: bytes, mode: int = 0o640) -> None:
    """Temporary file in the same directory + fsync + rename: readers see the old or the new file, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)  # before the sync: a crash never leaves a valid file with the wrong mode
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # The rename survives a power loss only once the directory entry is synced; a failure here is a failed
    # write (the caller reports it and the repair runs again), never a silent success.
    sync_dir(path.parent)


def sync_dir(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# Versions are wall-clock milliseconds (admin: max(now·1000, …)). A mark or version further ahead than this is
# not plausible: it is ignored, so a poisoned file on the shared volume cannot block every future publication.
MAX_VERSION_AHEAD_MS = 86_400_000


def plausible_version(value: int, now: float) -> bool:
    return 0 < value <= int(now * 1000) + MAX_VERSION_AHEAD_MS
