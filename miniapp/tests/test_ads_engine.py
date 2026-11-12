"""Ads module, stage 2: audience evaluation, campaign order, A/B assignment (table-driven)."""
from __future__ import annotations

from collections import Counter

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from miniapp.ads import engine, policy
from miniapp.ads.engine import Viewer

NOW = 1_800_000_000
KEY = Ed25519PrivateKey.generate()
DAY = 86400


def campaign(cid: int, **over) -> dict:
    base = {"id": cid, "tier": "general", "priority": 0, "startsAt": NOW - DAY, "endsAt": NOW + DAY, "langIn": [],
            "placements": ["quick_pick"], "countries": ["AZ", "TR"], "allowUnknownCountry": False, "ageMin": 0,
            "partnerId": 1, "hosts": ["p.example.com"], "disclosure": {}, "condition": None,
            "experiment": {"salt": f"salt-{cid:08d}", "controlPct": 0},
            "variants": [{"key": "A", "weight": 100, "headline": f"c{cid}", "body": "", "extra": "", "cta": "Go",
                          "url": "https://p.example.com/"}]}
    base.update(over)
    return base


def snapshot(*campaigns: dict, audiences: dict | None = None) -> policy.Policy:
    body = {"schema": 1, "version": 1, "generatedAt": NOW, "campaigns": list(campaigns), "audiences": audiences or {}}
    return policy.parse(policy.canonical(policy.sign(body, KEY)), KEY.public_key())


def viewer(**over) -> Viewer:
    base = {"user_id": 101, "lang": "ru", "country": "AZ", "age_confirmed": False}
    base.update(over)
    return Viewer(**base)


def chosen(snap: policy.Policy, v: Viewer, placement: str = "quick_pick", now: int = NOW) -> int | None:
    choice = engine.choose(snap, v, placement, now)
    return None if choice is None else choice.campaign.id


@pytest.mark.parametrize("over,v,expected", [
    ({}, {}, 1),
    ({"startsAt": NOW + 1}, {}, None),                                  # not started
    ({"endsAt": NOW}, {}, None),                                        # window is half-open
    ({"placements": ["ai_pick"]}, {}, None),
    ({"langIn": ["az"]}, {}, None),
    ({"langIn": ["az", "ru"]}, {}, 1),
    ({}, {"country": "RU"}, None),                                      # outside the allowed countries
    ({}, {"country": None}, None),                                      # unknown country: denied by default
    ({"allowUnknownCountry": True}, {"country": None}, 1),
    ({"ageMin": 18}, {}, None),                                         # age confirmation required
    ({"ageMin": 18}, {"age_confirmed": True}, 1),
    ({"condition": {"geo_in": ["TR"]}}, {}, None),
    ({"condition": {"not": {"geo_in": ["TR"]}}}, {}, 1),
    ({"condition": {"user_in": [101]}}, {}, 1),
    ({"condition": {"user_in": []}}, {}, None),                         # MB ids nobody holds
    ({"condition": {"aud": "g:1"}}, {}, 1),
    ({"condition": {"aud": "g:1"}}, {"user_id": 7}, None),
    ({"condition": {"all": [{"aud": "mb"}, {"lang_in": ["ru"]}]}}, {}, 1),
    ({"condition": {"all": [{"aud": "mb"}, {"lang_in": ["en"]}]}}, {}, None),
    ({"condition": {"any": [{"geo_unknown": True}, {"user_in": [5]}]}}, {}, None),
    ({"condition": {"segment": {"name": "new", "params": {"days": 7}}}}, {}, None),
])
def test_eligibility(over, v, expected):
    snap = snapshot(campaign(1, **over), audiences={"g:1": [101], "mb": [101]})
    assert chosen(snap, viewer(**v)) == expected


def test_segments_are_evaluated_by_the_mini_app_callback_lazily():
    calls = []

    def seg(name, params):
        calls.append((name, params))
        return name == "active" and params["min_visits"] <= 3

    snap = snapshot(campaign(1, priority=5, condition={"segment": {"name": "active", "params": {"days": 7, "min_visits": 3}}}),
                    campaign(2, priority=1, condition={"segment": {"name": "new", "params": {"days": 7}}}))
    assert chosen(snap, viewer(segment=seg)) == 1
    assert calls == [("active", {"days": 7, "min_visits": 3})]  # the lower-priority campaign is never evaluated


@pytest.mark.parametrize("campaigns,expected", [
    ([campaign(1, tier="general", priority=99), campaign(2, tier="segment", priority=0)], 2),
    ([campaign(1, tier="segment"), campaign(2, tier="individual")], 2),
    ([campaign(1, priority=1), campaign(2, priority=2)], 2),
    ([campaign(1, startsAt=NOW - 2 * DAY), campaign(2, startsAt=NOW - DAY)], 2),       # the later start first
    ([campaign(2), campaign(1)], 1),                                                    # then the smaller id
    ([campaign(1, tier="individual", condition={"user_in": [5]}), campaign(2)], 2),     # ineligible is skipped
])
def test_order(campaigns, expected):
    assert chosen(snapshot(*campaigns), viewer()) == expected


def test_no_policy_or_no_campaign_means_no_offer():
    assert engine.choose(None, viewer(), "quick_pick", NOW) is None
    assert chosen(snapshot(), viewer()) is None


def ab_campaign(control: int, weights: list[int]) -> dict:
    keys = "ABC"
    return campaign(1, experiment={"salt": "experiment-salt-1", "controlPct": control}, variants=[
        {"key": keys[i], "weight": w, "headline": keys[i], "body": "", "extra": "", "cta": "Go",
         "url": "https://p.example.com/"} for i, w in enumerate(weights)])


def test_ab_split_follows_weights_and_control_group_sees_nothing():
    snap = snapshot(ab_campaign(20, [50, 30, 20]))
    counts: Counter = Counter()
    for uid in range(1, 20_001):
        choice = engine.choose(snap, viewer(user_id=uid), "quick_pick", NOW)
        assert choice is not None and choice.campaign.id == 1   # control does not fall through to another campaign
        counts[choice.variant.key if choice.variant else "control"] += 1
    shares = {k: v / 20_000 for k, v in counts.items()}
    assert abs(shares["control"] - 0.20) < 0.02
    assert abs(shares["A"] - 0.40) < 0.02 and abs(shares["B"] - 0.24) < 0.02 and abs(shares["C"] - 0.16) < 0.02


def test_assignment_is_stable_and_independent_of_texts():
    a = snapshot(ab_campaign(10, [50, 50]))
    edited = ab_campaign(10, [50, 50])
    edited["variants"][0]["headline"] = "new text"
    b = snapshot(edited)
    for uid in range(1, 500):
        va = engine.choose(a, viewer(user_id=uid), "quick_pick", NOW).variant
        vb = engine.choose(b, viewer(user_id=uid), "quick_pick", NOW).variant
        assert (va.key if va else None) == (vb.key if vb else None)
    assert engine.bucket("s", 1) == engine.bucket("s", 1)
    assert 0 <=engine.bucket("experiment-salt-1", 42) < engine.BUCKETS


def test_changing_weights_moves_only_users_near_the_border():
    before = snapshot(ab_campaign(0, [50, 50]))
    after = snapshot(ab_campaign(0, [60, 40]))
    moved = 0
    for uid in range(1, 10_001):
        ka = engine.choose(before, viewer(user_id=uid), "quick_pick", NOW).variant.key
        kb = engine.choose(after, viewer(user_id=uid), "quick_pick", NOW).variant.key
        assert not (ka == "A" and kb == "B")  # growing A never moves an A user to B
        moved += ka != kb
    assert abs(moved / 10_000 - 0.10) < 0.02
