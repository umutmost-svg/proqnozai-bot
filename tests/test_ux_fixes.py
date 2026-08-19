"""Three user-visible defects, one section each.

1. A daily push that fired whether or not anything was worth pushing.
2. "I subscribed" answering with a generic error for a subscribed user.
3. Forecasts that were a page of "not available" lines.

Offline: no network, temp DB, the Mostbet cache is populated by hand.
"""
import types
from datetime import datetime, timedelta, timezone

import pytest

import db
import handlers.forecast as fc
import handlers.live as live
import handlers.promo as promo
from match_validation import INSUFFICIENT, PARTIAL, READY, forecast_readiness
from translations import T, tr


# ══ 1. Daily push only when there is something to say ═════════════════════════

def _raw_match(mid, home, away, league="UEFA Champions League",
               country="Europe", hours_ahead=3, sport="football"):
    """A Mostbet feed row, in the shape normalize_fixture expects."""
    kickoff = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return {
        "id": mid, "team1Title": home, "team2Title": away,
        "lineSuperCategory": league, "lineSubCategory": "",
        "categoryTitle": country, "lineCategory": sport,
        "matchBeginAt": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@pytest.fixture
def feed(monkeypatch):
    """Replace the Mostbet cache reader; asserts the digest never fetches."""
    state = {"rows": [], "reads": 0}

    def _cached():
        state["reads"] += 1
        return state["rows"]

    monkeypatch.setattr(live, "cached_matches", _cached)
    return state


def _now():
    return datetime.now(timezone.utc)


def test_no_worthy_match_means_no_digest(feed):
    feed["rows"] = [_raw_match(1, "FC Nowhere", "FC Obscure",
                               league="Third Division", country="Moldova")]
    assert live.digest_matches(_now()) == []


def test_empty_feed_produces_no_digest_and_no_crash(feed):
    feed["rows"] = []
    assert live.digest_matches(_now()) == []


def test_worthy_matches_are_returned_highest_first(feed):
    feed["rows"] = [
        _raw_match(1, "FC Nowhere", "FC Obscure", league="Third Division",
                   country="Moldova"),
        _raw_match(2, "Real Madrid", "Barcelona"),
        _raw_match(3, "Inter", "Milan"),
    ]
    top = live.digest_matches(_now())
    assert top, "a Champions League fixture should clear the bar"
    scores = [it.priority_score for it in top]
    assert scores == sorted(scores, reverse=True)
    assert all(s >= live.DAILY_PUSH_MIN_SCORE for s in scores)
    assert "Nowhere" not in " ".join(f"{it.home}{it.away}" for it in top)


def test_digest_is_capped(feed):
    feed["rows"] = [_raw_match(i, "Real Madrid", "Barcelona") for i in range(10)]
    assert len(live.digest_matches(_now())) <= live.DAILY_PUSH_MAX_MATCHES


def test_digest_never_triggers_a_fetch(feed, monkeypatch):
    """A timer that runs for every user must not cause a provider request."""
    async def _boom(*a, **k):
        raise AssertionError("digest must not load matches from the network")
    monkeypatch.setattr(live, "mostbet_get_odds", _boom)
    feed["rows"] = [_raw_match(1, "Real Madrid", "Barcelona")]
    live.digest_matches(_now())
    assert feed["reads"] == 1


def test_digest_names_the_matches(temp_db, feed):
    uid = 730001
    temp_db.db_ensure(uid, "u", "ru")
    feed["rows"] = [_raw_match(1, "Real Madrid", "Barcelona")]
    text = live.format_digest(uid, live.digest_matches(_now()))
    assert T["ru"]["push_digest_title"] in text
    assert "Real Madrid" in text and "Barcelona" in text
    assert T["ru"]["push_digest_cta"] in text


def test_digest_is_not_the_old_generic_line(temp_db, feed):
    """The line users learned to ignore."""
    uid = 730002
    temp_db.db_ensure(uid, "u", "ru")
    feed["rows"] = [_raw_match(1, "Real Madrid", "Barcelona")]
    text = live.format_digest(uid, live.digest_matches(_now()))
    assert "Напишите для прогноза" not in text


@pytest.mark.parametrize("lang", sorted(db.SUPPORTED_LANGS))
def test_digest_strings_exist_in_every_language(lang):
    assert T[lang]["push_digest_title"].strip()
    assert T[lang]["push_digest_cta"].strip()


# ══ 2. "I subscribed" must not answer with a generic error ════════════════════

class _FakeBot:
    def __init__(self, status):
        self._status = status

    async def get_chat_member(self, chat, uid):
        if self._status == "error":
            raise RuntimeError("bot is not an admin of the channel")
        return types.SimpleNamespace(status=self._status)


class _Query:
    def __init__(self, uid):
        self.from_user = types.SimpleNamespace(id=uid)
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True


def _cb_ctx(status):
    """A context whose send_message mirrors the real Bot signature — it accepts
    parse_mode. The bug was a reply() closure that did not, so handing it a
    formatted message raised TypeError and the user saw "error"."""
    sent = []

    class _Bot(_FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None,
                               parse_mode=None, **kw):
            sent.append(text)

    ctx = types.SimpleNamespace(bot=_Bot(status))
    return ctx, sent


async def _tap_i_subscribed(temp_db, uid, status, monkeypatch):
    monkeypatch.setattr(promo, "PROMO_CHANNEL", "@test")
    ctx, sent = _cb_ctx(status)
    q = _Query(uid)
    await promo.promo_check_cb(types.SimpleNamespace(callback_query=q), ctx)
    return q, sent


def _reset_promo(temp_db):
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_campaign")
        c.execute("DELETE FROM promo_claims")


async def test_subscribed_user_gets_codes_not_an_error(temp_db, monkeypatch):
    """The regression: the codes message is sent with parse_mode, which the
    callback's reply closure did not accept."""
    uid = 730101
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB-1", 10)

    q, sent = await _tap_i_subscribed(temp_db, uid, "member", monkeypatch)
    assert q.answered                      # the spinner always clears
    assert sent and "MB-1" in sent[0]
    assert tr(uid, "api_error") not in sent[0]


async def test_second_tap_returns_the_same_code(temp_db, monkeypatch):
    uid = 730102
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB-2", 10)

    _, first = await _tap_i_subscribed(temp_db, uid, "member", monkeypatch)
    _, again = await _tap_i_subscribed(temp_db, uid, "member", monkeypatch)
    assert first[0] == again[0]
    assert temp_db.db_list_promo_codes()[0]["claimed"] == 1   # one use, not two


@pytest.mark.parametrize("status", ["member", "administrator", "creator"])
async def test_every_subscribed_status_passes(temp_db, monkeypatch, status):
    uid = 730110 + hash(status) % 50
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB-3", 50)
    _, sent = await _tap_i_subscribed(temp_db, uid, status, monkeypatch)
    assert "MB-3" in sent[0]


@pytest.mark.parametrize("status", ["left", "kicked", "restricted"])
async def test_not_subscribed_is_asked_to_subscribe(temp_db, monkeypatch, status):
    uid = 730200 + hash(status) % 50
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB-4", 10)
    _, sent = await _tap_i_subscribed(temp_db, uid, status, monkeypatch)
    # The partner is named before the subscription is asked for.
    assert "Mostbet" in sent[0]
    assert tr(uid, "promo_subscribe") in sent[0]


async def test_api_failure_has_its_own_message(temp_db, monkeypatch):
    """A broken gate is not the same thing as "no campaign", and neither is a
    generic error."""
    uid = 730301
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB-5", 10)
    _, sent = await _tap_i_subscribed(temp_db, uid, "error", monkeypatch)
    assert sent[0] == tr(uid, "promo_check_failed")
    assert sent[0] not in (tr(uid, "promo_unavailable"), tr(uid, "api_error"))


async def test_cap_exhausted_has_its_own_message(temp_db, monkeypatch):
    uid = 730401
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB-6", 1)
    temp_db.db_claim_promos(999998)                 # someone took the only use
    _, sent = await _tap_i_subscribed(temp_db, uid, "member", monkeypatch)
    assert sent[0] == tr(uid, "promo_empty")


async def test_no_campaign_has_its_own_message(temp_db, monkeypatch):
    uid = 730501
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    _, sent = await _tap_i_subscribed(temp_db, uid, "member", monkeypatch)
    assert sent[0] == tr(uid, "promo_unavailable")


async def test_missing_channel_config_does_not_error(temp_db, monkeypatch):
    uid = 730601
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    _reset_promo(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB-7", 10)
    monkeypatch.setattr(promo, "PROMO_CHANNEL", "")
    ctx, sent = _cb_ctx("member")
    q = _Query(uid)
    await promo.promo_check_cb(types.SimpleNamespace(callback_query=q), ctx)
    assert q.answered
    assert sent[0] == tr(uid, "promo_check_failed")


@pytest.mark.parametrize("lang", sorted(db.SUPPORTED_LANGS))
def test_promo_messages_exist_in_every_language(lang):
    for key in ("promo_check_failed", "promo_unavailable", "promo_empty",
                "promo_subscribe"):
        assert T[lang][key].strip()


# ══ 3. No forecast worth reading → no forecast at all ═════════════════════════

def test_readiness_classification():
    assert forecast_readiness(True, True) == READY
    assert forecast_readiness(True, False) == PARTIAL
    assert forecast_readiness(False, True) == PARTIAL
    assert forecast_readiness(False, False) == INSUFFICIENT


class _StatusMsg:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, **kw):
        self.text = text
        self.markup = kw.get("reply_markup")


@pytest.fixture
def forecast_env(monkeypatch, partners):
    import config
    monkeypatch.setattr(config, "APIFOOTBALL_KEY", "")
    partners([])

    called = []

    async def _claude(*a, **k):
        called.append(1)
        return "Прогноз"

    async def _no_search(*a, **k):
        return []

    monkeypatch.setattr(fc, "claude_forecast", _claude)
    monkeypatch.setattr(fc, "search_match", _no_search)
    return called


def _ctx(has_odds, has_real_data):
    return types.SimpleNamespace(user_data={
        "pending_content": [{"type": "text", "text": "Barcelona Real"}],
        "pending_text": "Barcelona Real",
        "parsed_teams": ("Barcelona", "Real"),
        "odds_attached": True,
        "has_odds": has_odds,
        "has_real_data": has_real_data,
    }, bot=None)


async def test_no_data_at_all_skips_the_model(temp_db, forecast_env):
    uid = 730701
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(False, False), msg)

    assert forecast_env == [], "Claude must not be called with nothing to analyse"
    assert msg.text == T["ru"]["fc_insufficient"]
    labels = [b.text for row in msg.markup.inline_keyboard for b in row]
    assert labels == [T["ru"]["ev_more_matches"]]


async def test_insufficient_message_is_short_and_has_no_na_wall(temp_db, forecast_env):
    uid = 730702
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(False, False), msg)
    assert len(msg.text.splitlines()) <= 12
    for stale in ("N/A", "недоступн", "unavailable"):
        assert msg.text.lower().count(stale.lower()) <= 1


async def test_odds_only_still_produces_a_forecast(temp_db, forecast_env):
    """PARTIAL: a narrower forecast is honest, and the lean prompt already
    handles it."""
    uid = 730703
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(True, False), msg)
    assert forecast_env == [1]
    assert msg.text.startswith("Прогноз")


async def test_full_data_produces_a_forecast(temp_db, forecast_env):
    uid = 730704
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(True, True), msg)
    assert forecast_env == [1]


async def test_skipped_forecast_is_recorded_as_failed(temp_db, forecast_env):
    """It still happened from the user's side, so the metrics must see it."""
    uid = 730705
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    await fc._generate_forecast(uid, _ctx(False, False), _StatusMsg())
    with temp_db.con() as c:
        row = c.execute("SELECT ok FROM requests WHERE user_id=? AND msg_type='FORECAST'",
                        (uid,)).fetchone()
    assert row == (0,)


@pytest.mark.parametrize("lang", sorted(db.SUPPORTED_LANGS))
def test_insufficient_message_exists_in_every_language(lang):
    assert T[lang]["fc_insufficient"].strip()
