"""Deterministic team-form metrics computed in Python (never by the LLM).

Input is a normalized list of finished matches from one team's perspective:
each item is ``{"scored": int | None, "conceded": int | None}``. A match is
only counted when BOTH goal values are present integers; anything missing is
excluded from the sample rather than treated as 0. When the resulting sample
is empty, every metric is ``None`` (not 0) so the caller can tell "no data"
apart from a genuine zero.

Claude receives the finished numbers via :func:`format_metrics_block` and must
not recompute them.
"""
from __future__ import annotations

from typing import Optional, TypedDict


class TeamMetrics(TypedDict):
    matches: int
    wins: Optional[int]
    draws: Optional[int]
    losses: Optional[int]
    avg_scored: Optional[float]
    avg_conceded: Optional[float]
    avg_total: Optional[float]
    btts_pct: Optional[float]
    over25_pct: Optional[float]
    clean_sheet_pct: Optional[float]
    failed_to_score_pct: Optional[float]


_NULL_METRICS: TeamMetrics = {
    "matches": 0,
    "wins": None, "draws": None, "losses": None,
    "avg_scored": None, "avg_conceded": None, "avg_total": None,
    "btts_pct": None, "over25_pct": None,
    "clean_sheet_pct": None, "failed_to_score_pct": None,
}


def _valid(match: dict) -> bool:
    """A match counts only when both goal values are present integers.
    ``bool`` is rejected explicitly so True/False never masquerade as goals."""
    s, c = match.get("scored"), match.get("conceded")
    return (
        isinstance(s, int) and not isinstance(s, bool)
        and isinstance(c, int) and not isinstance(c, bool)
        and s >= 0 and c >= 0
    )


def compute_team_metrics(matches: list[dict]) -> TeamMetrics:
    """Compute deterministic form metrics over a team's recent finished matches.

    Missing inputs yield ``None`` (never 0). Percentages are fractions in
    ``[0, 1]`` rounded to 2 dp; averages rounded to 2 dp.
    """
    sample = [m for m in (matches or []) if _valid(m)]
    n = len(sample)
    if n == 0:
        return dict(_NULL_METRICS)  # type: ignore[return-value]

    scored = [m["scored"] for m in sample]
    conceded = [m["conceded"] for m in sample]

    wins = sum(1 for s, c in zip(scored, conceded) if s > c)
    draws = sum(1 for s, c in zip(scored, conceded) if s == c)
    losses = sum(1 for s, c in zip(scored, conceded) if s < c)

    btts = sum(1 for s, c in zip(scored, conceded) if s > 0 and c > 0)
    over25 = sum(1 for s, c in zip(scored, conceded) if s + c > 2.5)
    clean = sum(1 for c in conceded if c == 0)
    fts = sum(1 for s in scored if s == 0)

    return {
        "matches": n,
        "wins": wins, "draws": draws, "losses": losses,
        "avg_scored": round(sum(scored) / n, 2),
        "avg_conceded": round(sum(conceded) / n, 2),
        "avg_total": round((sum(scored) + sum(conceded)) / n, 2),
        "btts_pct": round(btts / n, 2),
        "over25_pct": round(over25 / n, 2),
        "clean_sheet_pct": round(clean / n, 2),
        "failed_to_score_pct": round(fts / n, 2),
    }


def format_metrics_block(team_name: str, metrics: TeamMetrics) -> str:
    """Render computed metrics as a compact, LLM-facing text block.

    Only non-null fields are emitted; an empty sample yields an explicit
    "metrics unavailable" line so Claude never infers a zero."""
    if not metrics or metrics.get("matches", 0) == 0:
        return f"{team_name} computed metrics: unavailable (no finished matches in sample)"

    def pct(v: Optional[float]) -> str:
        return f"{round(v * 100)}%" if v is not None else "n/a"

    return (
        f"{team_name} computed metrics (last {metrics['matches']}, Python-calculated — do not recompute):\n"
        f"  W/D/L: {metrics['wins']}/{metrics['draws']}/{metrics['losses']}\n"
        f"  Avg goals scored {metrics['avg_scored']} | conceded {metrics['avg_conceded']} | "
        f"total {metrics['avg_total']}\n"
        f"  BTTS {pct(metrics['btts_pct'])} | Over2.5 {pct(metrics['over25_pct'])} | "
        f"Clean sheet {pct(metrics['clean_sheet_pct'])} | Failed to score {pct(metrics['failed_to_score_pct'])}"
    )
