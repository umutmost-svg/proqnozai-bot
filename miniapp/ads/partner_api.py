"""Optional partner API hook: conversions pulled from a partner's reporting API instead of (or besides) postbacks.

No partner integration ships here and nothing calls the network: a concrete source implements
`PartnerConversionSource.fetch` for one partner (its own HTTP client, credentials from env, timeouts) and a
scheduler calls `ingest`. Every conversion goes through the same idempotent path as a postback
(`Telemetry.record_conversion`, unique per partner and transaction id), so pulling the same report twice, or
receiving the same conversion by postback and by API, counts it once.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from miniapp.ads.telemetry import Telemetry

logger = logging.getLogger("miniapp.ads")

_TXN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_CLICK = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_EVENT = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


@dataclass(frozen=True)
class PartnerConversion:
    txn_id: str      # the partner's own id of the conversion (idempotency key)
    click_id: str    # our click id, as the partner received it through the {click_id} macro
    event: str = "conversion"


class PartnerConversionSource(Protocol):
    partner_id: int

    def fetch(self, since: int, until: int) -> Iterable[PartnerConversion]:
        """Conversions the partner recorded in [since, until) (Unix seconds). May raise on a failed request."""
        ...


def ingest(source: PartnerConversionSource, telemetry: Telemetry, since: int, until: int, now: int) -> dict[str, int]:
    """Pulls one window from the source and records it. Malformed rows are skipped (counted as "invalid");
    a failing source or database raises nothing past here — the counts say what happened."""
    counts: Counter[str] = Counter()
    try:
        rows = list(source.fetch(since, until))
    except Exception as exc:  # noqa: BLE001 — a partner's API failure must never reach the app
        logger.warning("event=ads_partner_api_failed partner=%s type=%s", source.partner_id, type(exc).__name__)
        return {"failed": 1}
    for row in rows:
        if (not isinstance(row, PartnerConversion) or not _TXN.match(row.txn_id) or not _CLICK.match(row.click_id)
                or not _EVENT.match(row.event)):
            counts["invalid"] += 1
            continue
        try:
            counts[telemetry.record_conversion(source.partner_id, row.txn_id, row.click_id, row.event, now,
                                               source="api")] += 1
        except sqlite3.Error as exc:
            logger.warning("event=ads_partner_api_write_failed type=%s", type(exc).__name__)
            counts["failed"] += 1
    return dict(counts)
