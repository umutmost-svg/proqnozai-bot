"""Model identity and the parameters frozen by the validation run.

MODEL_VERSION changes whenever the model's maths, data rules or frozen parameters
change; it is stored with every forecast in the history so models can be compared.
"""

from __future__ import annotations

MODEL_VERSION = "goals-1"
# Market families where the team model was not better than the league-average baseline on
# the validation period (totals, both teams to score; the 2026 diagnostic agreed): shown
# as estimates only, never recommended (docs/miniapp/m35-goal-model.md).
UNPROVEN_FAMILIES = frozenset({"total", "btts"})
# Fewer results than this for either team in the window → no model probabilities at all.
MIN_TEAM_SHOW = 3


def stability_half_lives(half_life: float | None) -> tuple[float, float]:
    """The two refits of the stability check: half and double the time-decay half-life
    (no decay ↔ one and two years)."""
    if half_life is None:
        return (365.0, 730.0)
    return (half_life / 2, half_life * 2)
