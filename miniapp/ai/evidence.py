"""Verified material for an AI analysis, summarised deterministically in Python.

Only provider data goes in: recent finished results of both teams and their rows
in the competition table. The model receives these numbers; it never computes or
supplies them.
"""

from __future__ import annotations

from dataclasses import dataclass

from miniapp.sports.football_data import FormMatch, StandingRow

RECENT = 5  # matches summarised per team
MIN_FORM = 3  # fewer finished matches per team → not enough to analyse


@dataclass(frozen=True)
class TeamSummary:
    name: str
    matches: int
    form: str  # newest first, e.g. "WDLWW"
    points: int
    goals_for: int
    goals_against: int
    home_away: str  # where each of those matches was played, e.g. "HAHHA"
    table_position: int | None
    table_points: int | None
    table_played: int | None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "matches": self.matches,
            "form": self.form,
            "wins": self.form.count("W"),
            "draws": self.form.count("D"),
            "losses": self.form.count("L"),
            "points": self.points,
            "goalsFor": self.goals_for,
            "goalsAgainst": self.goals_against,
            "goalsForPerMatch": round(self.goals_for / self.matches, 2) if self.matches else None,
            "goalsAgainstPerMatch": round(self.goals_against / self.matches, 2) if self.matches else None,
            "venues": self.home_away,
            "tablePosition": self.table_position,
            "tablePoints": self.table_points,
            "tablePlayed": self.table_played,
        }


def summarize(name: str, form: list[FormMatch], standing: StandingRow | None) -> TeamSummary:
    recent = form[:RECENT]
    points = sum(3 if m.result == "W" else 1 if m.result == "D" else 0 for m in recent)
    return TeamSummary(
        name=name,
        matches=len(recent),
        form="".join(m.result for m in recent),
        points=points,
        goals_for=sum(m.goals_for for m in recent),
        goals_against=sum(m.goals_against for m in recent),
        home_away="".join("H" if m.venue == "home" else "A" for m in recent),
        table_position=standing.position if standing else None,
        table_points=standing.points if standing else None,
        table_played=standing.played if standing else None,
    )


def has_verified_form(home: TeamSummary, away: TeamSummary) -> bool:
    return home.matches >= MIN_FORM and away.matches >= MIN_FORM
