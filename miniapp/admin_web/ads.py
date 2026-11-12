"""Ad campaigns in admin.db (ads-architecture.md §3): partners, campaigns, variants, experiments, groups,
and the publication of the `ads-policy.json` snapshot the Mini App reads.

Written only by the admin process. Every change and its journal event share ONE transaction; journal rows
carry whitelisted fields only: campaign texts and links (business content, audited on purpose) but never
Telegram ids or MB ids (personal data) — audiences appear as leaf types, counts and a digest.

Publication (§2, the runtime-policy pattern of admin-dashboard.md §3.3): the snapshot is built inside the
publishing transaction and stored in `ad_publications` together with its journal event; the file is
written after the commit. A crash in between is repaired by `ensure_published` (at startup and on every
publication status read): the latest stored payload is re-written whenever the file is missing, invalid
or older. Versions only grow: max(now in ms, last publication + 1, file + 1, Mini App applied mark + 1).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from miniapp.ads import policy, targeting
from miniapp.ads.policy import PolicyError
from miniapp.admin_web.accounts import ActionError, Admin, AdminStore
from miniapp.store import SUPPORTED_LANGS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ad_partners (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    url_hosts             TEXT NOT NULL,
    allowed_countries     TEXT NOT NULL,
    allow_unknown_country INTEGER NOT NULL DEFAULT 0 CHECK (allow_unknown_country IN (0, 1)),
    age_min               INTEGER NOT NULL DEFAULT 0,
    disclosure_template   TEXT NOT NULL DEFAULT '{}',
    active                INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    admin_id              INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ad_experiments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL UNIQUE,
    salt        TEXT NOT NULL,
    control_pct INTEGER NOT NULL DEFAULT 0 CHECK (control_pct BETWEEN 0 AND 50),
    started_at  INTEGER,
    stopped_at  INTEGER
);
CREATE TABLE IF NOT EXISTS ad_campaigns (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_name         TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('draft', 'scheduled', 'active', 'paused', 'ended', 'archived')),
    priority_tier         TEXT NOT NULL CHECK (priority_tier IN ('individual', 'segment', 'general')),
    priority              INTEGER NOT NULL DEFAULT 0,
    lang_in               TEXT NOT NULL DEFAULT '[]',
    placements            TEXT NOT NULL,
    countries             TEXT NOT NULL DEFAULT '[]',
    allow_unknown_country INTEGER NOT NULL DEFAULT 0 CHECK (allow_unknown_country IN (0, 1)),
    starts_at             INTEGER NOT NULL,
    ends_at               INTEGER NOT NULL,
    partner_id            INTEGER NOT NULL REFERENCES ad_partners(id),
    disclosure            TEXT NOT NULL DEFAULT '{}',
    targeting             TEXT,
    experiment_id         INTEGER REFERENCES ad_experiments(id),
    created_by            INTEGER NOT NULL,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    updated_by            INTEGER NOT NULL,
    published_version     INTEGER
);
CREATE INDEX IF NOT EXISTS ad_campaigns_status ON ad_campaigns(status);
CREATE TABLE IF NOT EXISTS ad_variants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES ad_campaigns(id),
    key         TEXT NOT NULL CHECK (key IN ('A', 'B', 'C')),
    weight_pct  INTEGER NOT NULL CHECK (weight_pct BETWEEN 1 AND 100),
    headline    TEXT NOT NULL,
    body        TEXT NOT NULL,
    extra       TEXT NOT NULL,
    cta_text    TEXT NOT NULL,
    url         TEXT NOT NULL,
    UNIQUE (campaign_id, key)
);
CREATE TABLE IF NOT EXISTS ad_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ad_group_members (
    group_id INTEGER NOT NULL REFERENCES ad_groups(id),
    user_id  INTEGER NOT NULL,
    added_at INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
-- Every publication with the exact payload written to the file: the file can always be rebuilt from here.
CREATE TABLE IF NOT EXISTS ad_publications (
    version      INTEGER PRIMARY KEY,
    sha256       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    campaigns    INTEGER NOT NULL,
    published_at INTEGER NOT NULL,
    admin_id     INTEGER,
    event_id     INTEGER,
    revision     INTEGER NOT NULL DEFAULT 0
);
"""

EDITABLE = ("draft", "paused")
MAX_GROUP_MEMBERS = 50_000
MAX_NAME = 120
# Which statuses an action may start from, and where it leads.
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "activate": (("draft", "paused"), "active"),
    "pause": (("active",), "paused"),
    "archive": (("draft", "paused"), "archived"),
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()[:16]


def _check(fn, *args, **kwargs):
    """Field validators raise PolicyError/ConditionError; the API speaks ActionError."""
    try:
        return fn(*args, **kwargs)
    except (PolicyError, targeting.ConditionError) as exc:
        raise ActionError(str(exc)) from None


def _int_field(value: Any, low: int, high: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ActionError(code)
    return value


def display_status(status: str, starts_at: int, ends_at: int, now: int) -> str:
    """Stored statuses are draft/active/paused/archived; the window makes an active one scheduled or ended."""
    if status == "active":
        if now < starts_at:
            return "scheduled"
        if now >= ends_at:
            return "ended"
    return status


class AdsStore:
    def __init__(self, accounts: AdminStore, policy_file: Path, applied_file: Path) -> None:
        self.accounts = accounts
        self.policy_file = policy_file
        self.applied_file = applied_file
        admin_dir = accounts.path.parent  # admin-only directory (never mounted into the Mini App)
        self.lock_file = admin_dir / "ads-publish.lock"
        self.signing_key = load_signing_key(admin_dir / "ads-signing.key")
        self.public_key = self.signing_key.public_key()
        self._thread_lock = threading.Lock()

    def init_schema(self) -> None:
        with self.accounts._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---- partners ----------------------------------------------------------------------------

    @staticmethod
    def _partner_row(r: tuple) -> dict:
        return {"id": r[0], "name": r[1], "hosts": json.loads(r[2]), "countries": json.loads(r[3]),
                "allowUnknownCountry": bool(r[4]), "ageMin": r[5], "disclosure": json.loads(r[6]),
                "active": bool(r[7]), "updatedAt": r[8]}

    _PARTNER_COLS = ("id, name, url_hosts, allowed_countries, allow_unknown_country, age_min, disclosure_template, "
                     "active, updated_at")

    def partners(self) -> list[dict]:
        with self.accounts._connect() as conn:
            rows = conn.execute(f"SELECT {self._PARTNER_COLS} FROM ad_partners ORDER BY name").fetchall()
        return [self._partner_row(r) for r in rows]

    @staticmethod
    def _partner_input(data: dict) -> dict:
        age = data.get("ageMin", 0)
        active = data.get("active", True)
        allow_unknown = data.get("allowUnknownCountry", False)
        if not isinstance(active, bool) or not isinstance(allow_unknown, bool):
            raise ActionError("invalid_partner")
        return {
            "name": _check(policy.check_text, data.get("name"), "name", MAX_NAME),
            "hosts": _check(policy.check_hosts, data.get("hosts")),
            "countries": _check(policy.check_countries, data.get("countries") or []),
            "allowUnknownCountry": allow_unknown,
            "ageMin": _int_field(age, 0, policy.MAX_AGE, "invalid_age"),
            "disclosure": _check(policy.check_disclosure, data.get("disclosure")),
            "active": active,
        }

    def save_partner(self, actor: Admin, data: dict, now: int, partner_id: int | None = None) -> dict:
        """Owner only: a partner bounds every campaign of it (link hosts, countries, age), §3."""
        item = self._partner_input(data)
        values = (item["name"], _json(item["hosts"]), _json(item["countries"]), int(item["allowUnknownCountry"]),
                  item["ageMin"], _json(item["disclosure"]), int(item["active"]), now, actor.id)
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_partners")
            before = None
            try:
                if partner_id is None:
                    partner_id = int(conn.execute(
                        "INSERT INTO ad_partners (name, url_hosts, allowed_countries, allow_unknown_country, age_min, "
                        "disclosure_template, active, updated_at, admin_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (*values, now)).lastrowid)
                else:
                    row = conn.execute(f"SELECT {self._PARTNER_COLS} FROM ad_partners WHERE id = ?",
                                       (partner_id,)).fetchone()
                    if row is None:
                        raise ActionError("not_found", 404)
                    before = {k: v for k, v in self._partner_row(row).items() if k not in ("id", "updatedAt")}
                    conn.execute(
                        "UPDATE ad_partners SET name = ?, url_hosts = ?, allowed_countries = ?, allow_unknown_country = ?, "
                        "age_min = ?, disclosure_template = ?, active = ?, updated_at = ?, admin_id = ? WHERE id = ?",
                        (*values, partner_id))
            except sqlite3.IntegrityError:
                raise ActionError("name_taken", 409) from None
            bump_revision(conn)
            event = self.accounts._event(conn, now, actor.id, "ads.partner_save", "ad_partner", str(partner_id),
                                         before, item, None, None, "done")
        return {"id": partner_id, "eventId": event}

    # ---- groups ------------------------------------------------------------------------------

    def groups(self) -> list[dict]:
        with self.accounts._connect() as conn:
            rows = conn.execute("SELECT g.id, g.name, g.created_at, (SELECT COUNT(*) FROM ad_group_members m "
                                "WHERE m.group_id = g.id) FROM ad_groups g ORDER BY g.name").fetchall()
        return [{"id": r[0], "name": r[1], "createdAt": r[2], "members": r[3]} for r in rows]

    def group_members(self, group_id: int, limit: int = 1000) -> list[int]:
        with self.accounts._connect() as conn:
            return [r[0] for r in conn.execute("SELECT user_id FROM ad_group_members WHERE group_id = ? "
                                               "ORDER BY user_id LIMIT ?", (group_id, limit))]

    def save_group(self, actor: Admin, name: Any, now: int, group_id: int | None = None) -> dict:
        name = _check(policy.check_text, name, "name", 80)
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_ads")
            try:
                if group_id is None:
                    group_id = int(conn.execute("INSERT INTO ad_groups (name, created_at, admin_id) VALUES (?, ?, ?)",
                                                (name, now, actor.id)).lastrowid)
                elif not conn.execute("UPDATE ad_groups SET name = ? WHERE id = ?", (name, group_id)).rowcount:
                    raise ActionError("not_found", 404)
            except sqlite3.IntegrityError:
                raise ActionError("name_taken", 409) from None
            bump_revision(conn)
            self.accounts._event(conn, now, actor.id, "ads.group_save", "ad_group", str(group_id), None,
                                 {"name": name}, None, None, "done")
        return {"id": group_id}

    def change_members(self, actor: Admin, group_id: int, add: Any, remove: Any, now: int) -> dict:
        """Adds/removes Telegram ids. The journal keeps counts and a digest, never the ids themselves."""
        add_ids = targeting._ids(add) if add else []
        remove_ids = targeting._ids(remove) if remove else []
        if not add_ids and not remove_ids:
            raise ActionError("invalid_users")
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_ads")
            if conn.execute("SELECT 1 FROM ad_groups WHERE id = ?", (group_id,)).fetchone() is None:
                raise ActionError("not_found", 404)
            added = sum(conn.execute("INSERT OR IGNORE INTO ad_group_members (group_id, user_id, added_at, admin_id) "
                                     "VALUES (?, ?, ?, ?)", (group_id, uid, now, actor.id)).rowcount for uid in add_ids)
            removed = sum(conn.execute("DELETE FROM ad_group_members WHERE group_id = ? AND user_id = ?",
                                       (group_id, uid)).rowcount for uid in remove_ids)
            total = conn.execute("SELECT COUNT(*) FROM ad_group_members WHERE group_id = ?", (group_id,)).fetchone()[0]
            if total > MAX_GROUP_MEMBERS:
                raise ActionError("group_too_large", 409)
            bump_revision(conn)
            self.accounts._event(conn, now, actor.id, "ads.group_members", "ad_group", str(group_id), None,
                                 {"added": added, "removed": removed, "total": total,
                                  "digest": _digest([add_ids, remove_ids])}, None, None, "done")
        return {"added": added, "removed": removed, "total": total}

    # ---- campaigns ---------------------------------------------------------------------------

    _CAMPAIGN_COLS = ("c.id, c.internal_name, c.status, c.priority_tier, c.priority, c.lang_in, c.placements, "
                      "c.countries, c.allow_unknown_country, c.starts_at, c.ends_at, c.partner_id, c.disclosure, "
                      "c.targeting, e.salt, e.control_pct, c.created_at, c.updated_at, c.published_version, p.name")

    def _campaign_rows(self, conn: sqlite3.Connection, where: str = "1 = 1", args: tuple = ()) -> list[dict]:
        rows = conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM ad_campaigns c LEFT JOIN ad_experiments e ON e.id = c.experiment_id "
            f"LEFT JOIN ad_partners p ON p.id = c.partner_id WHERE {where} ORDER BY c.id DESC", args).fetchall()
        out = []
        for r in rows:
            variants = [{"key": v[0], "weight": v[1], "headline": v[2], "body": v[3], "extra": v[4], "cta": v[5],
                         "url": v[6]} for v in conn.execute(
                "SELECT key, weight_pct, headline, body, extra, cta_text, url FROM ad_variants WHERE campaign_id = ? "
                "ORDER BY key", (r[0],))]
            out.append({"id": r[0], "name": r[1], "status": r[2], "tier": r[3], "priority": r[4],
                        "langIn": json.loads(r[5]), "placements": json.loads(r[6]), "countries": json.loads(r[7]),
                        "allowUnknownCountry": bool(r[8]), "startsAt": r[9], "endsAt": r[10], "partnerId": r[11],
                        "disclosure": json.loads(r[12]), "condition": json.loads(r[13]) if r[13] else None,
                        "controlPct": r[15] or 0, "createdAt": r[16], "updatedAt": r[17],
                        "publishedVersion": r[18], "partnerName": r[19], "variants": variants})
        return out

    def campaigns(self, now: int) -> list[dict]:
        with self.accounts._connect() as conn:
            items = self._campaign_rows(conn)
        for c in items:
            c["state"] = display_status(c["status"], c["startsAt"], c["endsAt"], now)
        return items

    def campaign(self, campaign_id: int, now: int) -> dict | None:
        with self.accounts._connect() as conn:
            items = self._campaign_rows(conn, "c.id = ?", (campaign_id,))
        if not items:
            return None
        item = items[0]
        item["state"] = display_status(item["status"], item["startsAt"], item["endsAt"], now)
        return item

    @staticmethod
    def _audit_view(c: dict) -> dict:
        """Journal whitelist of a campaign: everything but identifier lists (audience: types + digest)."""
        return {"name": c["name"], "tier": c["tier"], "priority": c["priority"], "langIn": c["langIn"],
                "placements": c["placements"], "countries": c["countries"],
                "allowUnknownCountry": c["allowUnknownCountry"], "startsAt": c["startsAt"], "endsAt": c["endsAt"],
                "partnerId": c["partnerId"], "disclosure": c["disclosure"], "controlPct": c["controlPct"],
                "audience": {"kinds": targeting.leaf_kinds(c["condition"]), "digest": _digest(c["condition"])},
                "variants": [{k: v[k] for k in ("key", "weight", "headline", "body", "extra", "cta", "url")}
                             for v in c["variants"]]}

    def _campaign_input(self, conn: sqlite3.Connection, data: dict) -> dict:
        partner_id = _int_field(data.get("partnerId"), 1, 2**53, "invalid_partner")
        partner = conn.execute("SELECT url_hosts FROM ad_partners WHERE id = ?", (partner_id,)).fetchone()
        if partner is None:
            raise ActionError("invalid_partner")
        hosts = json.loads(partner[0])
        raw_variants = _check(policy.check_variants, data.get("variants"))
        variants = []
        for v in raw_variants:
            variants.append({
                "key": v["key"], "weight": v["weight"],
                "headline": _check(policy.check_text, v.get("headline"), "headline", policy.TEXT_LIMITS["headline"]),
                "body": _check(policy.check_text, v.get("body"), "body", policy.TEXT_LIMITS["body"], False, True),
                "extra": _check(policy.check_text, v.get("extra"), "extra", policy.TEXT_LIMITS["extra"], False),
                "cta": _check(policy.check_text, v.get("cta"), "cta", policy.TEXT_LIMITS["cta"]),
                "url": _check(policy.check_url, v.get("url"), hosts),
            })
        langs = data.get("langIn") or []
        if not isinstance(langs, list) or not all(lang in SUPPORTED_LANGS for lang in langs):
            raise ActionError("invalid_langs")
        placements = data.get("placements")
        if not isinstance(placements, list) or not placements or not all(p in policy.PLACEMENTS for p in placements):
            raise ActionError("invalid_placements")
        tier = data.get("tier")
        if tier not in policy.TIERS:
            raise ActionError("invalid_tier")
        starts = _int_field(data.get("startsAt"), 0, 2**40, "invalid_window")
        ends = _int_field(data.get("endsAt"), 0, 2**40, "invalid_window")
        if ends <= starts:
            raise ActionError("invalid_window")
        allow_unknown = data.get("allowUnknownCountry", False)
        if not isinstance(allow_unknown, bool):
            raise ActionError("invalid_countries")
        return {
            "name": _check(policy.check_text, data.get("name"), "name", MAX_NAME),
            "tier": tier, "priority": _int_field(data.get("priority", 0), 0, 1000, "invalid_priority"),
            "langIn": sorted(set(langs)), "placements": sorted(set(placements)),
            "countries": _check(policy.check_countries, data.get("countries") or []),
            "allowUnknownCountry": allow_unknown, "startsAt": starts, "endsAt": ends, "partnerId": partner_id,
            "disclosure": _check(policy.check_disclosure, data.get("disclosure")),
            "condition": _check(targeting.validate, data.get("condition"), "editor"),
            "controlPct": _int_field(data.get("controlPct", 0), 0, 50, "invalid_experiment"),
            "variants": variants,
        }

    def save_campaign(self, actor: Admin, data: dict, now: int, campaign_id: int | None = None,
                      copied_from: int | None = None) -> dict:
        """Creates a draft or edits a draft/paused campaign. Active ones are paused first (what users see changes
        only through a publication anyway, but editing a running campaign in place hides who saw what)."""
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_ads")
            item = self._campaign_input(conn, data)
            before = None
            row_values = (item["name"], item["tier"], item["priority"], _json(item["langIn"]),
                          _json(item["placements"]), _json(item["countries"]), int(item["allowUnknownCountry"]),
                          item["startsAt"], item["endsAt"], item["partnerId"], _json(item["disclosure"]),
                          _json(item["condition"]) if item["condition"] is not None else None, now, actor.id)
            if campaign_id is None:
                campaign_id = int(conn.execute(
                    "INSERT INTO ad_campaigns (internal_name, priority_tier, priority, lang_in, placements, countries, "
                    "allow_unknown_country, starts_at, ends_at, partner_id, disclosure, targeting, updated_at, updated_by, "
                    "status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                    (*row_values, actor.id, now)).lastrowid)
                experiment = int(conn.execute("INSERT INTO ad_experiments (campaign_id, salt, control_pct) VALUES (?, ?, ?)",
                                              (campaign_id, secrets.token_hex(16), item["controlPct"])).lastrowid)
                conn.execute("UPDATE ad_campaigns SET experiment_id = ? WHERE id = ?", (experiment, campaign_id))
            else:
                prior = self._campaign_rows(conn, "c.id = ?", (campaign_id,))
                if not prior:
                    raise ActionError("not_found", 404)
                if prior[0]["status"] not in EDITABLE:
                    raise ActionError("not_editable", 409)
                before = self._audit_view(prior[0])
                conn.execute(
                    "UPDATE ad_campaigns SET internal_name = ?, priority_tier = ?, priority = ?, lang_in = ?, placements = ?, "
                    "countries = ?, allow_unknown_country = ?, starts_at = ?, ends_at = ?, partner_id = ?, disclosure = ?, "
                    "targeting = ?, updated_at = ?, updated_by = ? WHERE id = ?", (*row_values, campaign_id))
                # The salt stays: a user keeps their variant across edits (weights move only the interval borders).
                conn.execute("UPDATE ad_experiments SET control_pct = ? WHERE campaign_id = ?",
                             (item["controlPct"], campaign_id))
                conn.execute("DELETE FROM ad_variants WHERE campaign_id = ?", (campaign_id,))
            for v in item["variants"]:
                conn.execute("INSERT INTO ad_variants (campaign_id, key, weight_pct, headline, body, extra, cta_text, url) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (campaign_id, v["key"], v["weight"], v["headline"],
                                                                 v["body"], v["extra"], v["cta"], v["url"]))
            after = self._audit_view(item)
            if copied_from is not None:
                after["copiedFrom"] = copied_from
            bump_revision(conn)
            event = self.accounts._event(conn, now, actor.id, "ads.campaign_save", "ad_campaign", str(campaign_id),
                                         before, after, None, None, "done")
        return {"id": campaign_id, "eventId": event}

    def set_status(self, actor: Admin, campaign_id: int, action: str, now: int) -> dict:
        """activate / pause / archive. Takes effect for users with the next publication."""
        if action not in TRANSITIONS:
            raise ActionError("invalid_action")
        allowed_from, target = TRANSITIONS[action]
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_ads")
            rows = self._campaign_rows(conn, "c.id = ?", (campaign_id,))
            if not rows:
                raise ActionError("not_found", 404)
            c = rows[0]
            if c["status"] not in allowed_from:
                raise ActionError("invalid_transition", 409)
            if action == "activate":
                self._ready(conn, c, now)
            conn.execute("UPDATE ad_campaigns SET status = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                         (target, now, actor.id, campaign_id))
            if action == "activate":
                conn.execute("UPDATE ad_experiments SET started_at = COALESCE(started_at, ?), stopped_at = NULL "
                             "WHERE campaign_id = ?", (now, campaign_id))
            elif action == "archive":
                conn.execute("UPDATE ad_experiments SET stopped_at = ? WHERE campaign_id = ?", (now, campaign_id))
            bump_revision(conn)
            event = self.accounts._event(conn, now, actor.id, "ads.campaign_status", "ad_campaign", str(campaign_id),
                                         {"status": c["status"]}, {"status": target}, None, None, "done")
        return {"id": campaign_id, "status": target, "eventId": event}

    @staticmethod
    def _ready(conn: sqlite3.Connection, c: dict, now: int) -> None:
        """An activated campaign must be publishable now: active partner, links on its hosts, a future window."""
        partner = conn.execute("SELECT url_hosts, active FROM ad_partners WHERE id = ?", (c["partnerId"],)).fetchone()
        if partner is None or not partner[1]:
            raise ActionError("partner_inactive", 409)
        hosts = json.loads(partner[0])
        for v in c["variants"]:
            _check(policy.check_url, v["url"], hosts)
        if c["endsAt"] <= now:
            raise ActionError("window_over", 409)

    def copy_campaign(self, actor: Admin, campaign_id: int, now: int) -> dict:
        """A new draft with the same settings and texts (and a new A/B salt: a new experiment)."""
        source = self.campaign(campaign_id, now)
        if source is None:
            raise ActionError("not_found", 404)
        suffix = " (копия)"
        data = {**source, "name": source["name"][:MAX_NAME - len(suffix)] + suffix}
        return self.save_campaign(actor, data, now, copied_from=campaign_id)

    # ---- publication -------------------------------------------------------------------------

    def _build(self, conn: sqlite3.Connection, version: int, now: int) -> tuple[dict, list[int], list[dict]]:
        """The snapshot body from admin.db (inside the publishing transaction: one consistent read)."""
        partners = {r[0]: self._partner_row(r) for r in conn.execute(
            f"SELECT {self._PARTNER_COLS} FROM ad_partners")}
        rows = self._campaign_rows(conn, "c.status = 'active' AND c.ends_at > ?", (now,))
        audiences: dict[str, list[int]] = {}
        campaigns, included, skipped = [], [], []
        for c in sorted(rows, key=lambda r: r["id"]):
            partner = partners.get(c["partnerId"])
            if partner is None or not partner["active"]:
                skipped.append({"id": c["id"], "code": "partner_inactive"})
                continue
            try:
                condition = self._compile(conn, c["condition"], audiences)
                countries = (sorted(set(c["countries"]) & set(partner["countries"])) if c["countries"]
                             else partner["countries"])
                salt = conn.execute("SELECT salt FROM ad_experiments WHERE campaign_id = ?", (c["id"],)).fetchone()[0]
                entry = {
                    "id": c["id"], "tier": c["tier"], "priority": c["priority"], "startsAt": c["startsAt"],
                    "endsAt": c["endsAt"], "langIn": c["langIn"], "placements": c["placements"],
                    "countries": countries,
                    "allowUnknownCountry": partner["allowUnknownCountry"] and c["allowUnknownCountry"],
                    "ageMin": partner["ageMin"], "partnerId": partner["id"], "hosts": partner["hosts"],
                    # The partner's template, overridden by the campaign's non-empty fields.
                    "disclosure": {k: c["disclosure"].get(k) or partner["disclosure"].get(k, "")
                                   for k in policy.DISCLOSURE_LIMITS},
                    "condition": condition,
                    "experiment": {"salt": salt, "controlPct": c["controlPct"]},
                    "variants": c["variants"],
                }
                policy._campaign(entry, {k: frozenset(v) for k, v in audiences.items()})  # same check as the reader
            except (PolicyError, targeting.ConditionError) as exc:
                skipped.append({"id": c["id"], "code": str(exc)})
                continue
            campaigns.append(entry)
            included.append(c["id"])
        if sum(len(v) for v in audiences.values()) > policy.MAX_AUDIENCE_IDS:
            raise ActionError("snapshot_too_large", 409)
        body = {"schema": policy.SCHEMA, "version": version, "generatedAt": now, "campaigns": campaigns,
                "audiences": {k: sorted(v) for k, v in sorted(audiences.items())}}
        if len(campaigns) > policy.MAX_CAMPAIGNS:
            raise ActionError("too_many_campaigns", 409)
        return policy.sign(body, self.signing_key), included, skipped

    @staticmethod
    def _compile(conn: sqlite3.Connection, node: dict | None, audiences: dict[str, list[int]]) -> dict | None:
        """Editor leaves -> snapshot leaves: groups and MB holders become named id lists, MB ids become user ids."""
        if node is None:
            return None
        (key, value), = node.items()
        if key in ("all", "any"):
            return {key: [AdsStore._compile(conn, child, audiences) for child in value]}
        if key == "not":
            return {key: AdsStore._compile(conn, value, audiences)}
        if key == "group_in":
            # One union audience per leaf: the compiled tree keeps the editor tree's depth.
            name = "g:" + ",".join(str(g) for g in value)
            if name not in audiences:
                placeholders = ",".join("?" * len(value))
                audiences[name] = [r[0] for r in conn.execute(
                    f"SELECT DISTINCT user_id FROM ad_group_members WHERE group_id IN ({placeholders}) ORDER BY user_id",
                    value)]
            return {"aud": name}
        if key == "mb_verified":
            if "mb" not in audiences:
                audiences["mb"] = [r[0] for r in conn.execute("SELECT user_id FROM crm_external_ids ORDER BY user_id")]
            return {"aud": "mb"}
        if key == "mb_id_in":
            placeholders = ",".join("?" * len(value))
            ids = sorted(r[0] for r in conn.execute(
                f"SELECT user_id FROM crm_external_ids WHERE mb_id COLLATE NOCASE IN ({placeholders})", value))
            return {"user_in": ids}  # empty when nobody holds these MB ids: matches no user
        return {key: value}

    def _next_version(self, conn: sqlite3.Connection, now: int) -> int:
        last = conn.execute("SELECT MAX(version) FROM ad_publications").fetchone()[0] or 0
        on_file = policy.read_version(self.policy_file, self.public_key)
        applied = self.applied_version() or 0
        return max(now * 1000, last + 1, (on_file[0] if on_file else 0) + 1, applied + 1)

    def applied_version(self) -> int | None:
        """The version the Mini App last applied (its mark file), or None."""
        try:
            value = int(self.applied_file.read_text().strip())
        except (OSError, ValueError):
            return None
        return value if value > 0 else None

    def publish(self, actor: Admin, reason: str, key: str, code: str, now: int, pause_all: bool = False) -> dict:
        """Step-up + reason + idempotency. `pause_all` is the emergency stop: every active campaign is paused and
        an empty snapshot published, in one transaction."""
        requested = {"pauseAll": pause_all}
        with self.accounts._write() as conn:
            if (prior := self.accounts._replayed(conn, key, actor.id, "ads.publish", None, None, reason)) is not None:
                if (prior["after"] or {}).get("requested") != requested:
                    raise ActionError("idempotency_key_reused", 409)
                return {**prior, "fileWritten": self._try_write()}
            self.accounts.step_up(conn, actor.id, code, now, "publish_ads")
            if pause_all:
                for (cid,) in conn.execute("SELECT id FROM ad_campaigns WHERE status = 'active'").fetchall():
                    conn.execute("UPDATE ad_campaigns SET status = 'paused', updated_at = ?, updated_by = ? WHERE id = ?",
                                 (now, actor.id, cid))
                    self.accounts._event(conn, now, actor.id, "ads.campaign_status", "ad_campaign", str(cid),
                                         {"status": "active"}, {"status": "paused"}, reason, None, "done")
                bump_revision(conn)
            version = self._next_version(conn, now)
            payload, included, skipped = self._build(conn, version, now)
            content = policy.canonical(payload)
            if len(content) > policy.MAX_FILE_BYTES:
                raise ActionError("snapshot_too_large", 409)
            try:  # exactly the reader's check: a publication the Mini App would reject is never committed
                policy.parse(content, self.public_key)
            except PolicyError as exc:
                raise ActionError(f"snapshot_invalid_{exc}", 409) from None
            after = {"version": version, "sha256": payload["sha256"], "campaigns": included, "skipped": skipped,
                     "requested": requested}
            event = self.accounts._event(conn, now, actor.id, "ads.publish", "ads_policy", None, None, after,
                                         reason, key, "done")
            conn.execute("INSERT INTO ad_publications (version, sha256, payload, campaigns, published_at, admin_id, "
                         "event_id, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (version, payload["sha256"], content.decode(), len(included), now, actor.id, event,
                          current_revision(conn)))
            if included:
                conn.execute(f"UPDATE ad_campaigns SET published_version = ? WHERE id IN ({','.join('?' * len(included))})",
                             (version, *included))
        return {"eventId": event, "after": after, "replayed": False, "fileWritten": self._try_write()}

    def _try_write(self) -> bool:
        """After the commit: a failed file write is repaired by the next ensure_published (status read, startup)."""
        try:
            self.ensure_published()
        except OSError:
            return False
        latest = self.latest_publication()
        on_file = policy.read_version(self.policy_file, self.public_key)
        return latest is None or (on_file is not None and on_file[0] >= latest["version"])

    def latest_publication(self) -> dict | None:
        with self.accounts._connect() as conn:
            row = conn.execute("SELECT version, sha256, payload, campaigns, published_at, revision FROM ad_publications "
                               "ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"version": row[0], "sha256": row[1], "payload": row[2], "campaigns": row[3], "publishedAt": row[4],
                "revision": row[5]}

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Serializes check + write across threads and admin processes: an older publication read before a
        newer one was written can never be written after it."""
        with self._thread_lock:
            fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def ensure_published(self) -> bool:
        """Re-writes the latest publication when the file is missing, invalid, older, or of the same version with
        other content. A valid (signed) file of a HIGHER version is left alone: the next publication goes above it.
        True if it wrote."""
        with self._file_lock():
            latest = self.latest_publication()  # re-read under the lock
            if latest is None:
                return False
            on_file = policy.read_version(self.policy_file, self.public_key)
            if on_file is not None and (on_file[0] > latest["version"]
                                        or on_file == (latest["version"], latest["sha256"])):
                return False
            policy.write_atomic(self.policy_file, latest["payload"].encode())
            return True

    def publication_status(self) -> dict:
        self._try_write()
        latest = self.latest_publication()
        with self.accounts._connect() as conn:
            revision = current_revision(conn)
        on_file = policy.read_version(self.policy_file, self.public_key)
        if latest is None:
            unpublished = revision > 0
        else:
            unpublished = revision != latest["revision"]
        return {"latest": None if latest is None else {k: latest[k] for k in ("version", "sha256", "campaigns",
                                                                                "publishedAt")},
                "fileVersion": on_file[0] if on_file else None, "appliedVersion": self.applied_version(),
                # Any change to campaigns, partners, groups or MB ids since the latest publication.
                "unpublishedChanges": unpublished,
                "publicKey": policy.public_key_hex(self.signing_key)}


def current_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'ads_revision'").fetchone()
    return int(row[0]) if row else 0


def bump_revision(conn: sqlite3.Connection) -> None:
    """Every change to a snapshot input (campaigns, partners, groups, MB ids) moves the revision, in its own
    transaction; a publication records the revision it was built from."""
    conn.execute("INSERT INTO meta (key, value) VALUES ('ads_revision', '1') ON CONFLICT(key) DO UPDATE SET "
                 "value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)")


def load_signing_key(path: Path) -> Ed25519PrivateKey:
    """The admin's snapshot signing key: created once (0600, in the admin-only directory), never logged or
    returned by the API; only its public half is shown (to configure MINIAPP_ADS_POLICY_PUBLIC_KEY)."""
    if not path.exists():
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=0o700)
            _sync_dir(path.parent.parent)
        raw = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        # Written completely and synced under a temporary name, then installed with link(): the final name
        # never holds a partial key (a crash leaves only a stray temporary file), and of two processes
        # creating it at once exactly one key wins; the other reads it.
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(raw.hex())
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp)
        _sync_dir(path.parent)  # the new name survives a power loss (else a restart would mint another key)
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(path.read_text().strip()))


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
