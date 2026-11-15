"""Fixed goal-model forecasts for offline AI tests (the model itself is tested in test_forecast_*)."""

from __future__ import annotations

from miniapp.forecast.engine import MatchForecast, Unavailable
from miniapp.forecast.markets import display_percentages, probabilities
from miniapp.forecast.scores import score_matrix
from miniapp.forecast.selection import NO_BET_NONE, Candidate, Selection


def fake_forecast(pick: str | None = None, *, alternatives: tuple[str, ...] = (), home_rate: float = 1.6,
                  away_rate: float = 1.0, limited: bool = False, home_rating=(1.3, 0.8), away_rating=(0.9, 1.1),
                  home_advantage: float = 1.25) -> MatchForecast:
    exact = probabilities(score_matrix(home_rate, away_rate))
    rates = {k: 0.5 for k in exact}
    selection = (Selection(Candidate(pick, exact[pick], 0.5), tuple(Candidate(a, exact[a], 0.5) for a in alternatives),
                           None) if pick else Selection(None, (), NO_BET_NONE))
    matrix = score_matrix(home_rate, away_rate)
    return MatchForecast(
        competition="PL", limited=limited, complete_history=True, match_day=True, computed_at="2027-01-14T08:00:00Z", data_until="2027-01-13T15:00:00Z",
        sample=240, home_matches=24, away_matches=24, expected_goals=(home_rate, away_rate), exact=exact,
        display=display_percentages(exact), top_scores=tuple(matrix.top_scores(3)), selection=selection,
        league_rates=rates, home_rating=home_rating, away_rating=away_rating, home_advantage=home_advantage,
        league_goals=(1.5, 1.2),
    )


class FakeForecaster:
    def __init__(self, result: MatchForecast | Unavailable | None = None) -> None:
        self.result = result if result is not None else fake_forecast()
        self.calls = 0

    async def forecast(self, event) -> MatchForecast | Unavailable:
        self.calls += 1
        return self.result
