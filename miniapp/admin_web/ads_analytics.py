"""Ads analytics (ads-architecture.md §5, §8): impressions, reach, clicks, CTR with a Wilson interval per
variant, control exposures, series by day and country, confirmed conversions, CSV export.

Sources: `miniapp.telemetry.db` opened READ-ONLY (written by the Mini App only, non-authoritative) and the
confirmed conversions in admin.db (entered by an admin from the partner's report: MB ids -> users, attributed
to the variant the user last clicked). Every figure is an aggregate: no user ids leave this module, so the
analyst role sees it too.
"""
from __future__ import annotations

import csv
import io
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miniapp.ads.telemetry import DAY, Telemetry, days_back
from miniapp.admin_web.accounts import ActionError, Admin, AdminStore

_SCHEMA = """
-- Confirmed conversions from the partner's report (by MB id). One per campaign and user; the variant is the
-- one the user last clicked within the attribution window ('' = no click found: unattributed).
CREATE TABLE IF NOT EXISTS ad_conversions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    variant      TEXT NOT NULL,
    confirmed_at INTEGER NOT NULL,
    admin_id     INTEGER NOT NULL,
    UNIQUE (campaign_id, user_id)
);
"""

ATTRIBUTION_DAYS = 30
# Fits the admin's 16 KiB request body limit (app.MAX_BODY) with the longest ids; bigger reports go in batches.
MAX_REPORT = 400
Z95 = 1.959964


def wilson(successes: int, trials: int) -> tuple[float, float] | None:
    """95 % Wilson score interval of a proportion; None without trials."""
    if trials <= 0:
        return None
    p = successes / trials
    denom = 1 + Z95 ** 2 / trials
    centre = (p + Z95 ** 2 / (2 * trials)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / trials + Z95 ** 2 / (4 * trials ** 2)) / denom
    return round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)


class AdsAnalytics:
    def __init__(self, accounts: AdminStore, telemetry_file: Path) -> None:
        self.accounts = accounts
        self.telemetry = Telemetry(telemetry_file, read_only=True)

    def init_schema(self) -> None:
        with self.accounts._connect() as conn:
            conn.executescript(_SCHEMA)

    def _read(self, state: dict[str, str], read: Any, empty: Any) -> Any:
        """Telemetry is not authoritative: a missing file is "not collected", a busy/corrupt one "unavailable" —
        never an error page, and never presented as zero activity."""
        if not self.telemetry.exists():
            state["source"] = state["source"].replace("telemetry", "not_collected")  # "unavailable" stays
            return empty
        try:
            return read()
        except sqlite3.Error:
            state["source"] = "unavailable"
            return empty

    def _conversions(self, since: int, campaign_id: int | None) -> dict[tuple[int, str], int]:
        where, args = "confirmed_at >= ?", [since]
        if campaign_id is not None:
            where += " AND campaign_id = ?"
            args.append(campaign_id)
        with self.accounts._connect() as conn:
            rows = conn.execute(f"SELECT campaign_id, variant, COUNT(*) FROM ad_conversions WHERE {where} "
                                "GROUP BY campaign_id, variant", args).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def report(self, now: int, days: int, campaign_id: int | None = None, names: dict[int, str] | None = None) -> dict:
        since_day = days_back(now, days - 1)
        state = {"source": "telemetry"}

        def totals(group: str) -> list[dict]:
            return self._read(state, lambda: self.telemetry.ad_totals(since_day, group, campaign_id), [])

        engaged = self._read(state, lambda: self.telemetry.engaged_clickers(since_day, campaign_id), None)
        variants: dict[tuple[int, str], dict[str, Any]] = {}
        for row in totals("variant"):
            item = variants.setdefault((row["campaignId"], row["key"]), {
                "campaignId": row["campaignId"], "variant": row["key"], "impressions": 0, "reach": 0, "clicks": 0,
                "clickers": 0, "control": 0, "conversions": 0})
            if row["kind"] == "impression":
                item["impressions"], item["reach"] = row["events"], row["users"]
            elif row["kind"] == "click":
                item["clicks"], item["clickers"] = row["events"], row["users"]
            else:
                item["control"] = row["users"]
        # Same period as the events: from 00:00 UTC of the first day of the window (not now - days x 24 h).
        since_ts = int(datetime.strptime(since_day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
        for (cid, variant), count in self._conversions(since_ts, campaign_id).items():
            key = (cid, variant or "unattributed")
            variants.setdefault(key, {"campaignId": cid, "variant": key[1], "impressions": 0, "reach": 0,
                                      "clicks": 0, "clickers": 0, "control": 0, "conversions": 0})
            variants[key]["conversions"] += count
        rows = []
        for item in sorted(variants.values(), key=lambda v: (v["campaignId"], v["variant"])):
            item["ctr"] = round(item["clicks"] / item["impressions"], 6) if item["impressions"] else None
            # Unique CTR with its 95 % interval: what the A/B comparison is read on. Only clickers who were shown
            # the variant in the period count, so the proportion never exceeds 1 (raw clickers stay in "clickers").
            if engaged is None:  # that read failed: unknown, not zero
                item["uniqueCtr"] = item["uniqueCtrCi"] = None
            else:
                shown = min(engaged.get((item["campaignId"], item["variant"]), 0), item["reach"])
                item["uniqueCtr"] = round(shown / item["reach"], 6) if item["reach"] else None
                item["uniqueCtrCi"] = wilson(shown, item["reach"])
            item["campaign"] = (names or {}).get(item["campaignId"])
            rows.append(item)
        by_day: dict[str, dict[str, int]] = {}
        for row in totals("day"):
            day = by_day.setdefault(row["key"], {"impressions": 0, "clicks": 0, "control": 0})
            day[{"impression": "impressions", "click": "clicks", "control": "control"}[row["kind"]]] += row["events"]
        countries: dict[str, dict[str, int]] = {}
        for row in totals("country"):
            c = countries.setdefault(row["key"] or "unknown", {"impressions": 0, "clicks": 0})
            if row["kind"] in ("impression", "click"):
                c[row["kind"] + "s"] += row["events"]
        return {
            "period": days, "source": state["source"],
            "variants": rows,
            "days": [{"day": d, **v} for d, v in sorted(by_day.items())],
            "countries": [{"country": k, **v} for k, v in sorted(countries.items(), key=lambda kv: -kv[1]["impressions"])],
        }

    def csv(self, report: dict) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["campaign_id", "campaign", "variant", "impressions", "reach", "clicks", "clickers", "ctr",
                         "unique_ctr", "unique_ctr_low", "unique_ctr_high", "control", "conversions", "source"])
        for v in report["variants"]:
            ci = v["uniqueCtrCi"] or (None, None)
            writer.writerow([v["campaignId"], _cell(v["campaign"] or ""), v["variant"], v["impressions"], v["reach"],
                             v["clicks"], v["clickers"], v["ctr"], v["uniqueCtr"], ci[0], ci[1], v["control"],
                             v["conversions"], report["source"]])
        if not report["variants"]:  # an empty export still says whether telemetry was readable
            writer.writerow([""] * 13 + [report["source"]])
        return out.getvalue()

    def record_conversions(self, actor: Admin, campaign_id: int, mb_ids: Any, now: int) -> dict:
        """Confirmed conversions from the partner's report: MB ids -> users (CRM), attributed to the variant the
        user last clicked within ATTRIBUTION_DAYS. The journal keeps counts only (no MB ids, no user ids)."""
        if (not isinstance(mb_ids, list) or not 1 <= len(mb_ids) <= MAX_REPORT
                or not all(isinstance(m, str) and 1 <= len(m) <= 32 for m in mb_ids)):
            raise ActionError("invalid_mb_ids")
        wanted = sorted({m.strip().lower() for m in mb_ids})
        with self.accounts._connect() as conn:
            if conn.execute("SELECT 1 FROM ad_campaigns WHERE id = ?", (campaign_id,)).fetchone() is None:
                raise ActionError("not_found", 404)
            users: list[int] = []
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                users += [r[0] for r in conn.execute(
                    f"SELECT user_id FROM crm_external_ids WHERE lower(mb_id) IN ({','.join('?' * len(chunk))})", chunk)]
        # Without readable telemetry the attribution is unknown, not "no click": refuse before inserting, since a
        # stored conversion is never re-attributed (one per campaign and user). The admin retries later.
        if not self.telemetry.exists():
            raise ActionError("telemetry_unavailable", 503)
        try:
            variants = self.telemetry.last_click_variants(campaign_id, sorted(users), now - ATTRIBUTION_DAYS * DAY)
        except sqlite3.Error:
            raise ActionError("telemetry_unavailable", 503) from None
        with self.accounts._write() as conn:
            self.accounts._require_actor(conn, actor.id, "manage_ads")
            added = sum(conn.execute(
                "INSERT OR IGNORE INTO ad_conversions (campaign_id, user_id, variant, confirmed_at, admin_id) "
                "VALUES (?, ?, ?, ?, ?)", (campaign_id, uid, variants.get(uid, ""), now, actor.id)).rowcount
                for uid in users)
            attributed = sum(1 for uid in users if uid in variants)
            self.accounts._event(conn, now, actor.id, "ads.conversions", "ad_campaign", str(campaign_id), None,
                                 {"reported": len(wanted), "matched": len(users), "added": added,
                                  "attributed": attributed}, None, None, "done")
        return {"reported": len(wanted), "matched": len(users), "added": added, "attributed": attributed}


def _cell(value: str) -> str:
    """CSV injection guard: a cell that a spreadsheet would run as a formula starts with a quote."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value
