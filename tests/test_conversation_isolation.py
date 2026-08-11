"""Conversation memory is scoped to one fixture.

claude_forecast prepends the user's last turns to every request. Unscoped, an
analysis of match A became context for an independent forecast of match B.
Memory is now keyed by fixture; an empty key (a request that names no fixture)
continues whatever is stored. Offline: the SDK call is stubbed.
"""
import pytest

import claude_client as cc
from handlers.forecast import _fixture_key


class _Block:
    def __init__(self, text):
        self.type = "text"; self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


@pytest.fixture
def sent(monkeypatch, temp_db):
    """Record the `messages` array of every call."""
    recorded = []

    def _fake_create(**kwargs):
        recorded.append(kwargs["messages"])
        return _Resp(f"reply {len(recorded)}")

    monkeypatch.setattr(cc.client.messages, "create", _fake_create)
    monkeypatch.setattr(cc, "_thinking_supported", True)
    return recorded


def _content(text):
    return [{"type": "text", "text": text}]


def _flat(messages):
    """All text in a request, for substring assertions."""
    return repr(messages)


# ─── fixture key ──────────────────────────────────────────────────────────────

def test_fixture_key_is_order_independent():
    assert _fixture_key(("Barcelona", "Real Madrid"), "") == \
           _fixture_key(("Real Madrid", "Barcelona"), "")


def test_fixture_key_differs_between_matches():
    assert _fixture_key(("Barcelona", "Real"), "") != _fixture_key(("Milan", "Inter"), "")


def test_fixture_key_falls_back_to_text():
    assert _fixture_key(None, "Barcelona Real") == _fixture_key(("Barcelona", "Real"), "")


def test_fixture_key_empty_when_nothing_identifiable():
    assert _fixture_key(None, "") == ""


# ─── isolation ────────────────────────────────────────────────────────────────

async def test_second_fixture_does_not_inherit_the_first(sent, temp_db):
    """The regression: forecast A → forecast B must not carry A into B."""
    uid = 890001
    temp_db.db_ensure(uid, "u", "ru")
    key_a = _fixture_key(("Barcelona", "Real"), "")
    key_b = _fixture_key(("Milan", "Inter"), "")

    await cc.claude_forecast(uid, _content("Barcelona Real"), "sys", 100, fixture_key=key_a)
    await cc.claude_forecast(uid, _content("Milan Inter"), "sys", 100, fixture_key=key_b)

    prompt_b = _flat(sent[1])
    assert "Barcelona" not in prompt_b
    assert "reply 1" not in prompt_b          # A's answer is gone too
    assert "Milan Inter" in prompt_b
    assert len(sent[1]) == 1                  # only the current turn


async def test_same_fixture_keeps_its_context(sent, temp_db):
    uid = 890002
    temp_db.db_ensure(uid, "u", "ru")
    key = _fixture_key(("Barcelona", "Real"), "")

    await cc.claude_forecast(uid, _content("Barcelona Real"), "sys", 100, fixture_key=key)
    await cc.claude_forecast(uid, _content("Barcelona Real again"), "sys", 100, fixture_key=key)

    prompt = _flat(sent[1])
    assert "reply 1" in prompt                # the earlier turn is still context
    assert len(sent[1]) == 3                  # prior user + assistant + current


async def test_unnamed_followup_continues_the_stored_context(sent, temp_db):
    """An empty key means "no fixture named" — a follow-up, not a new match."""
    uid = 890003
    temp_db.db_ensure(uid, "u", "ru")
    key = _fixture_key(("Barcelona", "Real"), "")

    await cc.claude_forecast(uid, _content("Barcelona Real"), "sys", 100, fixture_key=key)
    await cc.claude_forecast(uid, _content("а что по тоталу?"), "sys", 100, fixture_key="")

    assert "reply 1" in _flat(sent[1])


async def test_followup_does_not_relabel_the_stored_fixture(sent, temp_db):
    """After a follow-up, a genuinely new match must still reset the context."""
    uid = 890004
    temp_db.db_ensure(uid, "u", "ru")
    key_a = _fixture_key(("Barcelona", "Real"), "")
    key_b = _fixture_key(("Milan", "Inter"), "")

    await cc.claude_forecast(uid, _content("Barcelona Real"), "sys", 100, fixture_key=key_a)
    await cc.claude_forecast(uid, _content("подробнее"), "sys", 100, fixture_key="")
    await cc.claude_forecast(uid, _content("Milan Inter"), "sys", 100, fixture_key=key_b)

    prompt_c = _flat(sent[2])
    assert "Barcelona" not in prompt_c
    assert "подробнее" not in prompt_c


async def test_legacy_row_without_a_key_is_dropped_once(sent, temp_db):
    """Rows written before fixture_key existed carry '' and must not leak."""
    uid = 890005
    temp_db.db_ensure(uid, "u", "ru")
    with temp_db.con() as c:
        c.execute("INSERT OR REPLACE INTO conversation (user_id, messages, fixture_key) "
                  "VALUES (?,?,'')",
                  (uid, '[{"role": "user", "content": "OLD MATCH DATA"}]'))

    await cc.claude_forecast(uid, _content("Milan Inter"), "sys", 100,
                             fixture_key=_fixture_key(("Milan", "Inter"), ""))
    assert "OLD MATCH DATA" not in _flat(sent[0])
