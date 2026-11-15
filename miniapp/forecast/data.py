"""Finished-match results the goal model learns from.

Only matches the provider marks FINISHED and that were played to the end count:
awarded results (a decision, not a game) and matches without a readable 90-minute
score are left out. Extra time and shoot-outs never count — every market here is
a 90-minute market.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from miniapp.sports.football_data import ninety_minute_score, team_number
from miniapp.sports.models import FootballEvent

# Backtest availability lag: a forecast made at time T learns only from matches that
# kicked off at least a full day earlier. The saved data no longer says when a result
# became known (extra time, shoot-outs, delays, a match suspended and finished later),
# so a generous lag is the only way to be sure no result from after T leaks in.
# Production needs no lag: it uses results the provider already marks FINISHED.
BACKTEST_LAG_S = 24 * 3600


@dataclass(frozen=True)
class MatchResult:
    match_id: int
    kickoff: float  # epoch seconds, UTC
    home: int  # provider team ids
    away: int
    home_goals: int
    away_goals: int


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _team(node: object) -> int | None:
    number = node.get("id") if isinstance(node, dict) else None
    return number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None


def results_from_payload(matches: Iterable[object]) -> list[MatchResult]:
    """Raw football-data.org match records → results (offline backtest input)."""
    out: list[MatchResult] = []
    for raw in matches:
        if not isinstance(raw, dict) or raw.get("status") != "FINISHED":
            continue
        match_id, kickoff = raw.get("id"), _epoch(raw.get("utcDate"))
        home, away = _team(raw.get("homeTeam")), _team(raw.get("awayTeam"))
        score = ninety_minute_score(raw.get("score"))
        if not isinstance(match_id, int) or kickoff is None or home is None or away is None or score is None:
            continue
        if home == away:
            continue
        out.append(MatchResult(match_id, kickoff, home, away, score[0], score[1]))
    return _ordered(out)


def results_from_events(events: Iterable[FootballEvent]) -> list[MatchResult]:
    """Normalized events (the app's season calendars) → results."""
    out: list[MatchResult] = []
    for event in events:
        if event.status != "finished" or event.awarded or event.regular_score is None:
            continue
        home, away = team_number(event.home.id), team_number(event.away.id)
        kickoff = _epoch(event.starts_at)
        match_id = event.id.partition(":")[2]
        if home is None or away is None or home == away or kickoff is None or not match_id.isdigit():
            continue
        out.append(MatchResult(int(match_id), kickoff, home, away, *event.regular_score))
    return _ordered(out)


def _ordered(results: list[MatchResult]) -> list[MatchResult]:
    unique = {r.match_id: r for r in results}  # one record per match even if two seasons overlap
    return sorted(unique.values(), key=lambda r: (r.kickoff, r.match_id))


def known_before(results: Iterable[MatchResult], moment: float, lag: float = BACKTEST_LAG_S) -> list[MatchResult]:
    """Results that were certainly final at `moment` (see BACKTEST_LAG_S)."""
    return [r for r in results if r.kickoff + lag <= moment]
