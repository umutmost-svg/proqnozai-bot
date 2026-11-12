"""Audience conditions of ad campaigns (ads-architecture.md §4).

A condition is a tree `{"all": [...]} | {"any": [...]} | {"not": node}` of leaves. The admin editor
accepts leaves that refer to admin-only data (groups, MB ids); publication compiles them into
leaves the Mini App can evaluate on its own (`aud` = a named list of Telegram ids in the snapshot,
`user_in`). The same validator checks both forms: the editor on save, the Mini App on every
snapshot it reads, so a hand-edited or truncated file can never widen an audience.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from miniapp.store import SUPPORTED_LANGS

Stage = Literal["editor", "snapshot"]

MAX_DEPTH = 4
MAX_LEAVES = 30
MAX_IDS_PER_LEAF = 5000
MAX_TG_ID = 2**53
COUNTRY = re.compile(r"^[A-Z]{2}$")
# Cloudflare's "unknown" (XX) and Tor (T1): never a real country, never allowed in a list.
UNKNOWN_COUNTRIES = frozenset({"XX", "T1"})
MB_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# "mb" = every user with an MB id; "g:1,5" = the union of the members of groups 1 and 5.
AUDIENCE_NAME = re.compile(r"^(g:[1-9][0-9]{0,9}(,[1-9][0-9]{0,9}){0,49}|mb)$")

# Activity segments (§4): parameter -> inclusive bounds. Evaluated by the Mini App over its own data.
SEGMENTS: dict[str, dict[str, tuple[int, int]]] = {
    "new": {"days": (1, 365)},                                    # registered within N days
    "active": {"days": (1, 90), "min_visits": (1, 90)},           # >= N visit days within the window
    "inactive": {"days": (1, 365)},                               # no visit for N days
    "returned": {"gap_days": (1, 365), "within_days": (1, 90)},  # a gap >= N days, then a visit within M days
    "forecasts": {"days": (1, 90), "min": (1, 10_000)},           # forecast requests within the window
    "coupons": {"days": (1, 90), "min": (1, 10_000)},             # coupons created within the window
    "offer_clicks": {"days": (1, 90), "min": (1, 10_000)},        # clicks on offers within the window
}

_EDITOR_LEAVES = frozenset({"geo_in", "geo_unknown", "user_in", "mb_verified", "mb_id_in", "group_in",
                            "segment", "lang_in"})
_SNAPSHOT_LEAVES = frozenset({"geo_in", "geo_unknown", "user_in", "aud", "segment", "lang_in"})


class ConditionError(ValueError):
    """The condition is malformed; the message is a stable code for the API."""


class SegmentUnavailable(Exception):
    """The viewer's activity data cannot be evaluated: the campaign is not shown (default deny)."""


def _ids(value: Any, allow_empty: bool = False) -> list[int]:
    if (not isinstance(value, list) or not (0 if allow_empty else 1) <= len(value) <= MAX_IDS_PER_LEAF
            or not all(isinstance(v, int) and not isinstance(v, bool) and 0 < v < MAX_TG_ID for v in value)):
        raise ConditionError("invalid_condition")
    return sorted(set(value))


def _strings(value: Any, pattern: re.Pattern[str] | None, allowed: tuple[str, ...] | None = None,
             limit: int = 300) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= limit or not all(isinstance(v, str) for v in value):
        raise ConditionError("invalid_condition")
    if pattern is not None and not all(pattern.match(v) for v in value):
        raise ConditionError("invalid_condition")
    if allowed is not None and not all(v in allowed for v in value):
        raise ConditionError("invalid_condition")
    return sorted(set(value))


def _leaf(key: str, value: Any, stage: Stage) -> dict[str, Any]:
    allowed = _EDITOR_LEAVES if stage == "editor" else _SNAPSHOT_LEAVES
    if key not in allowed:
        raise ConditionError("invalid_condition")
    if key == "geo_in":
        countries = _strings(value, COUNTRY)
        if UNKNOWN_COUNTRIES & set(countries):
            raise ConditionError("invalid_condition")
        return {key: countries}
    if key in ("geo_unknown", "mb_verified"):
        if value is not True:
            raise ConditionError("invalid_condition")
        return {key: True}
    if key == "user_in":
        # In a snapshot an empty list is valid: MB ids nobody holds compile to "no user".
        return {key: _ids(value, allow_empty=stage == "snapshot")}
    if key == "group_in":
        return {key: _ids(value)}
    if key == "mb_id_in":
        return {key: _strings(value, MB_ID, limit=MAX_IDS_PER_LEAF)}
    if key == "aud":
        if not isinstance(value, str) or not AUDIENCE_NAME.match(value):
            raise ConditionError("invalid_condition")
        return {key: value}
    if key == "lang_in":
        return {key: _strings(value, None, SUPPORTED_LANGS)}
    # segment
    if not isinstance(value, dict) or set(value) - {"name", "params"} or "name" not in value:
        raise ConditionError("invalid_condition")
    name, params = value["name"], value.get("params", {})
    spec = SEGMENTS.get(name) if isinstance(name, str) else None
    if spec is None or not isinstance(params, dict) or set(params) != set(spec):
        raise ConditionError("invalid_condition")
    for param, (low, high) in spec.items():
        v = params[param]
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ConditionError("invalid_condition")
    return {"segment": {"name": name, "params": {p: params[p] for p in sorted(spec)}}}


def validate(node: Any, stage: Stage) -> dict[str, Any] | None:
    """Normalized copy of the condition (None = everyone), or ConditionError."""
    if node is None:
        return None
    leaves = [0]

    def walk(n: Any, depth: int) -> dict[str, Any]:
        if depth > MAX_DEPTH or not isinstance(n, dict) or len(n) != 1:
            raise ConditionError("invalid_condition")
        (key, value), = n.items()
        if key in ("all", "any"):
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_LEAVES:
                raise ConditionError("invalid_condition")
            return {key: [walk(child, depth + 1) for child in value]}
        if key == "not":
            return {key: walk(value, depth + 1)}
        leaves[0] += 1
        if leaves[0] > MAX_LEAVES:
            raise ConditionError("too_many_conditions")
        return _leaf(key, value, stage)

    return walk(node, 1)


def leaf_kinds(node: dict[str, Any] | None) -> list[str]:
    """Leaf types of a condition, for read-only views that must not show identifiers (analysts)."""
    if node is None:
        return []
    (key, value), = node.items()
    if key in ("all", "any"):
        return sorted({k for child in value for k in leaf_kinds(child)})
    if key == "not":
        return leaf_kinds(value)
    return [key]


def redact(node: dict[str, Any] | None) -> dict[str, Any] | None:
    """The same tree with identifier lists replaced by their sizes (analyst view, §4.8)."""
    if node is None:
        return None
    (key, value), = node.items()
    if key in ("all", "any"):
        return {key: [redact(child) for child in value]}
    if key == "not":
        return {key: redact(value)}
    if key in ("user_in", "mb_id_in"):
        return {key: {"count": len(value)}}
    return {key: value}


# ---- evaluation (Mini App side, over a validated snapshot condition) --------------------------------


def matches(node: dict[str, Any] | None, viewer: Any, audiences: dict[str, frozenset[int]]) -> bool:
    """True if the viewer is in the audience. `viewer` is engine.Viewer (duck-typed to keep this module
    free of the engine): user_id, lang, country (ISO2 or None = unknown) and segment(name, params)."""
    if node is None:
        return True
    (key, value), = node.items()
    if key == "all":
        return all(matches(child, viewer, audiences) for child in value)
    if key == "any":
        return any(matches(child, viewer, audiences) for child in value)
    if key == "not":
        return not matches(value, viewer, audiences)
    country = None if viewer.country in UNKNOWN_COUNTRIES else viewer.country
    if key == "geo_in":
        return country is not None and country in value
    if key == "geo_unknown":
        return country is None
    if key == "user_in":
        return viewer.user_id in value
    if key == "aud":
        return viewer.user_id in audiences.get(value, frozenset())
    if key == "lang_in":
        return viewer.lang in value
    if key == "segment":
        if viewer.segment is None:
            raise SegmentUnavailable()
        try:
            return bool(viewer.segment(value["name"], value["params"]))
        except Exception as exc:  # noqa: BLE001 — any failure of the data source means "unknown", never "match"
            raise SegmentUnavailable() from exc
    return False  # unreachable for a validated snapshot; an unknown leaf never widens the audience
