"""Offline tests for the Match Priority Engine (MVP). Pure, no network/DB."""
from datetime import datetime, timedelta, timezone

from priority_engine import PriorityInput, compute_priority

UTC = timezone.utc
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


def _inp(**kw):
    base = dict(
        league_name="Regional Cup", country="Nowhere",
        home="Team A", away="Team B", is_live=False,
        kickoff_utc=NOW + timedelta(hours=3), now_utc=NOW,
        stage_hint="", demand_count=0,
    )
    base.update(kw)
    return PriorityInput(**base)


# ─── Score bounds ──────────────────────────────────────────────────────────────

def test_total_always_between_0_and_100():
    # Maximal plausible input: tier-1 final, two tier-1 teams, derby, live, huge demand.
    maxed = _inp(league_name="Champions League", country="Europe",
                 stage_hint="Final", home="Real Madrid", away="Barcelona",
                 is_live=True, demand_count=100_000)
    b = compute_priority(maxed)
    assert 0 <= b.total <= 100

    minimal = _inp(league_name="Random Regional Cup", country="Nowhere",
                   home="Unknown FC", away="Obscure United",
                   kickoff_utc=NOW + timedelta(days=6), demand_count=0)
    b2 = compute_priority(minimal)
    assert 0 <= b2.total <= 100


def test_excluded_components_never_enter_total():
    b = compute_priority(_inp())
    assert b.team_strength is None
    assert b.match_competitiveness is None
    # Total must be fully explained by the remaining components.
    explained = (b.tournament_prestige + b.tournament_stage + b.team_popularity
                 + b.derby_or_rivalry + b.time_proximity + b.internal_user_demand)
    assert b.total == max(0, min(100, round(explained)))


# ─── Tournament prestige + stage ───────────────────────────────────────────────

def test_major_final_outranks_ordinary_top_league_match():
    final = compute_priority(_inp(
        league_name="World Cup", country="World", stage_hint="Final",
        home="Argentina", away="France"))
    ordinary_top_league = compute_priority(_inp(
        league_name="Premier League", country="England",
        home="Burnley", away="Luton Town"))
    assert final.total > ordinary_top_league.total


def test_popular_team_ordinary_match_not_always_above_lesser_final():
    """A popular team's routine match must not automatically outrank a final
    of a lesser (but still recognized) tournament — priority is not just
    team fandom."""
    popular_routine = compute_priority(_inp(
        league_name="Regional Cup", country="Nowhere", stage_hint="",
        home="Real Madrid", away="Obscure United"))
    lesser_final = compute_priority(_inp(
        league_name="Copa Libertadores", country=None, stage_hint="Final",
        home="Team X", away="Team Y"))
    assert lesser_final.total > popular_routine.total


# ─── Derby ─────────────────────────────────────────────────────────────────────

def test_known_derby_outranks_ordinary_match_same_league():
    derby = compute_priority(_inp(
        league_name="Premier League", country="England",
        home="Arsenal", away="Tottenham"))
    ordinary = compute_priority(_inp(
        league_name="Premier League", country="England",
        home="Burnley", away="Luton Town"))
    assert derby.total > ordinary.total
    assert derby.derby_or_rivalry > 0
    assert ordinary.derby_or_rivalry == 0


def test_derby_detection_is_order_independent():
    a = compute_priority(_inp(home="Arsenal", away="Tottenham"))
    b = compute_priority(_inp(home="Tottenham", away="Arsenal"))
    assert a.derby_or_rivalry == b.derby_or_rivalry > 0


# ─── Competitiveness proxy is intentionally absent from MVP ────────────────────

def test_even_matchup_not_scored_differently_without_competitiveness_signal():
    """MVP has no match_competitiveness signal (excluded per design), so two
    matches identical in every OTHER respect must score identically —
    regardless of which team is a "bigger" favorite. This guards against
    accidentally reintroducing an odds-based signal into priority_score."""
    a = compute_priority(_inp(home="Team A", away="Team B"))
    b = compute_priority(_inp(home="Team C", away="Team D"))
    assert a.total == b.total


# ─── Non-football sports: tournament + player/team tiers ──────────────────────

def test_tennis_grand_slam_recognized_as_tier_1():
    slam = compute_priority(_inp(
        league_name="Wimbledon", country=None,
        home="Unseeded Player", away="Qualifier"))
    ordinary = compute_priority(_inp(
        league_name="ATP Challenger Tour", country=None,
        home="Unseeded Player", away="Qualifier"))
    assert slam.tournament_prestige > ordinary.tournament_prestige


def test_tennis_star_player_popularity_recognized():
    b = compute_priority(_inp(home="Novak Djokovic", away="Qualifier"))
    ordinary = compute_priority(_inp(home="Unseeded Player", away="Qualifier"))
    assert b.team_popularity > ordinary.team_popularity


def test_nba_recognized_as_tier_1_above_euroleague():
    nba = compute_priority(_inp(
        league_name="NBA", country=None, home="Team A", away="Team B"))
    euroleague = compute_priority(_inp(
        league_name="EuroLeague", country=None, home="Team A", away="Team B"))
    assert nba.tournament_prestige > euroleague.tournament_prestige


def test_ufc_recognized_as_tier_1_above_bellator():
    ufc = compute_priority(_inp(
        league_name="UFC", country=None, home="Fighter A", away="Fighter B"))
    bellator = compute_priority(_inp(
        league_name="Bellator", country=None, home="Fighter A", away="Fighter B"))
    assert ufc.tournament_prestige > bellator.tournament_prestige


# ─── Live vs pre-match ──────────────────────────────────────────────────────────

def test_live_outranks_prematch_same_tournament():
    live = compute_priority(_inp(is_live=True, kickoff_utc=None))
    prematch = compute_priority(_inp(is_live=False, kickoff_utc=NOW + timedelta(hours=3)))
    assert live.total > prematch.total


def test_live_bounded_weight_does_not_beat_far_more_prestigious_prematch():
    """Live status must not blindly outrank a much more important upcoming
    match — its weight is capped, per the confirmed formula."""
    live_nobody = compute_priority(_inp(
        league_name="Regional Cup", country="Nowhere", is_live=True, kickoff_utc=None,
        home="Team A", away="Team B"))
    huge_final_soon = compute_priority(_inp(
        league_name="World Cup", country="World", stage_hint="Final",
        home="Argentina", away="France", kickoff_utc=NOW + timedelta(minutes=30)))
    assert huge_final_soon.total > live_nobody.total


# ─── Time proximity bands ───────────────────────────────────────────────────────

def test_closer_kickoff_scores_at_least_as_high():
    soon = compute_priority(_inp(kickoff_utc=NOW + timedelta(minutes=30)))
    later = compute_priority(_inp(kickoff_utc=NOW + timedelta(days=5)))
    assert soon.time_proximity >= later.time_proximity
    assert soon.total >= later.total


# ─── Internal demand: bounded, unique-user based, cannot dominate ──────────────

def test_demand_bonus_is_capped_and_cannot_flip_prestige_gap():
    no_demand = compute_priority(_inp(
        league_name="Champions League", country="Europe", demand_count=0))
    huge_demand_nobody_final = compute_priority(_inp(
        league_name="Random Regional Cup", country="Nowhere", demand_count=1_000_000))
    # Even an extreme demand spike on an obscure match cannot outrank a
    # Champions League match with no demand at all — demand is capped low.
    assert no_demand.total > huge_demand_nobody_final.total


def test_demand_bonus_saturates_within_0_to_5():
    low = compute_priority(_inp(demand_count=1))
    high = compute_priority(_inp(demand_count=1_000_000))
    assert 0 <= low.internal_user_demand <= 5
    assert 0 <= high.internal_user_demand <= 5
    assert high.internal_user_demand >= low.internal_user_demand


# ─── Stability / determinism ───────────────────────────────────────────────────

def test_same_input_always_same_output():
    inp = _inp(league_name="Serie A", country="Italy", home="Roma", away="Lazio")
    results = {compute_priority(inp).total for _ in range(10)}
    assert len(results) == 1


# ─── Competition / stage separation (post-validation-report fix) ──────────────
# stage_hint is the ONLY source of tournament_stage; league_name/country must
# never leak into stage detection (that was the pre-fix behavior that risked
# treating a competition's own name as a round).

def test_stage_points_come_only_from_stage_hint_not_league_name():
    with_stage = compute_priority(_inp(stage_hint="Final"))
    # A competition name that itself contains a stage-like word must NOT
    # score stage points — only stage_hint may.
    name_contains_stage_word = compute_priority(_inp(
        league_name="Final Regional Cup", country="Nowhere", stage_hint=""))
    assert with_stage.tournament_stage > 0
    assert name_contains_stage_word.tournament_stage == 0


def test_stage_hint_empty_gives_zero_stage_points_regardless_of_prestige():
    elite_no_stage = compute_priority(_inp(
        league_name="Champions League", country="Europe", stage_hint=""))
    assert elite_no_stage.tournament_stage == 0
    assert elite_no_stage.tournament_prestige > 0  # prestige unaffected
