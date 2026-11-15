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
from dataclasses import dataclass
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
    """https only, the exact host from the partner's list, no user info, no port, no control characters."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL or value != value.strip():
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


@dataclass(frozen=True)
class Policy:
    version: int
    generated_at: int
    campaigns: tuple[Campaign, ...]
    audiences: dict[str, frozenset[int]]
    sha256: str


def _campaign(raw: Any, audiences: dict[str, frozenset[int]]) -> Campaign:
    if not isinstance(raw, dict):
        raise PolicyError("invalid_campaign")
    hosts = tuple(check_hosts(raw.get("hosts")))
    variants = []
    for v in check_variants(raw.get("variants")):
        variants.append(Variant(
            key=v["key"], weight=v["weight"],
            headline=check_text(v.get("headline"), "headline", TEXT_LIMITS["headline"]),
            body=check_text(v.get("body"), "body", TEXT_LIMITS["body"], required=False, multiline=True),
            extra=check_text(v.get("extra"), "extra", TEXT_LIMITS["extra"], required=False),
            cta=check_text(v.get("cta"), "cta", TEXT_LIMITS["cta"]),
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
    return Campaign(
        id=_int(raw.get("id"), 1, 2**53, "invalid_campaign"), tier=tier,
        priority=_int(raw.get("priority"), 0, 1000, "invalid_priority"), starts_at=starts, ends_at=ends,
        lang_in=tuple(sorted(set(langs))), placements=tuple(sorted(set(placements))),
        countries=tuple(check_countries(raw.get("countries"))), allow_unknown_country=allow_unknown,
        age_min=_int(raw.get("ageMin"), 0, MAX_AGE, "invalid_age"), partner_id=_int(raw.get("partnerId"), 1, 2**53,
                                                                                 "invalid_partner"),
        hosts=hosts, disclosure=check_disclosure(raw.get("disclosure")), condition=condition,
        salt=experiment["salt"], control_pct=_int(experiment.get("controlPct"), 0, 50, "invalid_experiment"),
        variants=tuple(variants))


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
    parsed = tuple(_campaign(c, audiences) for c in campaigns)
    if len({c.id for c in parsed}) != len(parsed):
        raise PolicyError("bad_campaigns")
    return Policy(version=version, generated_at=generated, campaigns=parsed, audiences=audiences, sha256=sha)


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
