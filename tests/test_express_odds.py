"""Offline tests for express odds honesty: the express is built ONLY from
matches with real Mostbet odds, the real values are passed to the model as
data, fabrication guidance is gone, and with fewer than two priced matches the
user gets an honest localized message instead of a model call. No network."""
import types

import handlers.express as ex
from translations import T

import pytest


def _match(mid, t1, t2, league="Premier League"):
    return {"id": mid, "team1Title": t1, "team2Title": t2,
            "lineCategory": "Football", "lineSubCategory": league,
            "lineSuperCategory": "England", "matchBeginAt": "", "isLive": True}


def _odds(w1=None, x=None, w2=None, over25=None, under25=None):
    base = {k: None for k in (
        "w1", "x", "w2", "dc_1x", "dc_12", "dc_x2", "hcp_w1", "hcp_w2",
        "hcp_val", "over15", "under15", "over25", "under25", "over35",
        "under35", "btts_yes", "btts_no", "h1_w1", "h1_x", "h1_w2",
        "h1_over05", "h1_under05", "h1_over15", "h1_under15",
        "dnb_w1", "dnb_w2")}
    base.update(w1=w1, x=x, w2=w2, over25=over25, under25=under25)
    return base


class _Q:
    def __init__(self, n=3):
        self.data = f"expr_{n}"
        self.edited = None

    async def edit_message_text(self, text, **kw):
        self.edited = text


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def send_chat_action(self, chat_id, action):
        pass


def _ctx():
    return types.SimpleNamespace(user_data={}, bot=_Bot())


@pytest.fixture()
def capture(monkeypatch):
    """Stub the feed, per-match odds and the model; capture the prompt."""
    state = {"prompts": [], "odds": {}, "matches": []}

    async def _load():
        return state["matches"]

    async def _get_odds(mid):
        return state["odds"].get(mid, _odds())

    async def _create(**kw):
        state["prompts"].append(kw["messages"][0]["content"])
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="EXPRESS")])

    monkeypatch.setattr(ex, "_mostbet_load_matches", _load)
    monkeypatch.setattr(ex, "mostbet_get_odds", _get_odds)
    monkeypatch.setattr(ex, "_create_with_retry", _create)
    return state


async def _run(uid, n=3):
    q = _Q(n)
    ctx = _ctx()
    await ex._express_run(ctx, q, uid)
    return q, ctx


async def test_prompt_contains_real_odds_no_fabrication(temp_db, capture):
    uid = 830101
    temp_db.db_ensure(uid, "u", "ru")
    capture["matches"] = [_match(1, "Arsenal", "Chelsea"),
                          _match(2, "Barca", "Madrid"),
                          _match(3, "Milan", "Inter")]
    capture["odds"] = {1: _odds(w1=1.85, x=3.4, w2=4.2, over25=1.9, under25=1.95),
                       2: _odds(w1=2.1, x=3.3, w2=3.1),
                       3: _odds(w1=1.55)}

    q, ctx = await _run(uid, 3)

    assert len(capture["prompts"]) == 1
    p = capture["prompts"][0]
    # Real values passed as data.
    assert "1X2: 1.85/3.4/4.2" in p
    assert "O/U 2.5: 1.9/1.95" in p
    # Fabrication guidance is gone.
    assert "1.20-1.60" not in p
    assert "реалистичный коэффициент" not in p
    # Reply delivered.
    assert ctx.bot.sent


async def test_unpriced_matches_excluded_spares_used(temp_db, capture):
    uid = 830102
    temp_db.db_ensure(uid, "u", "ru")
    capture["matches"] = [_match(1, "NoOdds", "Yet"),
                          _match(2, "Barca", "Madrid"),
                          _match(3, "Milan", "Inter"),
                          _match(4, "PSG", "Lyon")]
    capture["odds"] = {2: _odds(w1=2.1), 3: _odds(w1=1.7), 4: _odds(w1=1.9)}

    await _run(uid, 3)

    p = capture["prompts"][0]
    assert "NoOdds" not in p          # priceless match never reaches the model
    for team in ("Barca", "Milan", "PSG"):
        assert team in p              # spare candidate filled the slot


async def test_zero_priced_matches_honest_message_no_model(temp_db, capture):
    uid = 830103
    temp_db.db_ensure(uid, "u", "ru")
    capture["matches"] = [_match(1, "A", "B"), _match(2, "C", "D")]
    capture["odds"] = {}              # nothing priced

    q, ctx = await _run(uid, 3)

    assert capture["prompts"] == []   # model never called
    assert q.edited == T["ru"]["express_no_odds"]
    assert ctx.bot.sent == []


async def test_single_priced_match_is_not_an_express(temp_db, capture):
    uid = 830104
    temp_db.db_ensure(uid, "u", "ru")
    capture["matches"] = [_match(1, "A", "B"), _match(2, "C", "D")]
    capture["odds"] = {1: _odds(w1=1.8)}

    q, _ = await _run(uid, 3)

    assert capture["prompts"] == []
    assert q.edited == T["ru"]["express_no_odds"]


async def test_prompt_count_matches_actual_priced(temp_db, capture):
    uid = 830105
    temp_db.db_ensure(uid, "u", "ru")
    capture["matches"] = [_match(i, f"H{i}", f"A{i}") for i in range(1, 6)]
    capture["odds"] = {1: _odds(w1=1.8), 2: _odds(w1=2.0)}  # only 2 priced

    await _run(uid, 5)                # user asked for 5

    p = capture["prompts"][0]
    assert "экспресс на 2 матчей" in p   # k reflects reality, not the request


async def test_az_prompt_uses_emsal_not_kef(temp_db, capture):
    uid = 830106
    temp_db.db_ensure(uid, "u", "az")
    capture["matches"] = [_match(1, "Qarabağ", "Neftçi"),
                          _match(2, "Sabah", "Zirə")]
    capture["odds"] = {1: _odds(w1=1.9), 2: _odds(w1=2.2)}

    await _run(uid, 2)

    p = capture["prompts"][0]
    assert "Əmsal" in p or "əmsal" in p
    assert "kef" not in p.lower()     # canonical terminology in the AZ prompt


def test_new_translation_key_in_all_seven():
    for lang in ("az", "ru", "en", "tr", "kz", "uz", "ar"):
        assert T[lang]["express_no_odds"].strip()
