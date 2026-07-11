"""Normalized provenance/freshness metadata for factual blocks sent to Claude.

Every factual block (form, H2H, injuries, stats) is wrapped with a single
normalized header so the model — and our logs — always know where a fact came
from, when it was fetched, whether it is stale, and which fields are missing.
Raw provider JSON is never exposed; only this normalized text header plus the
already-formatted human-readable block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Provenance:
    """Where a factual block came from and how fresh it is.

    ``source``      — provider key, e.g. "api-football", "football-data".
    ``fetched_at``  — ISO-8601 UTC timestamp of retrieval.
    ``stale``       — True if served from cache past its intended freshness.
    ``missing``     — normalized field names the provider did NOT supply, so the
                      model states them as unavailable instead of inventing.
    """

    source: str
    fetched_at: str = field(default_factory=_utc_now_iso)
    stale: bool = False
    missing: list[str] = field(default_factory=list)

    def header(self) -> str:
        miss = ", ".join(self.missing) if self.missing else "none"
        return (
            f"[SOURCE: {self.source} | fetched_at: {self.fetched_at} | "
            f"stale: {'yes' if self.stale else 'no'} | missing: {miss}]"
        )

    def wrap(self, block: str) -> str:
        """Prepend the provenance header to an already-formatted text block."""
        return f"{self.header()}\n{block}" if block else self.header()
