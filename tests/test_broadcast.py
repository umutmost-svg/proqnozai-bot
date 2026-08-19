"""Broadcasts: validation, scheduling, and the send loop. Offline — the bot is
a stub, no Telegram call leaves the process.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import broadcast as bc


# ─── HTML validation ──────────────────────────────────────────────────────────

def test_plain_text_is_valid():
    assert bc.validate_text("Привет!") is None


def test_supported_markup_is_valid():
    assert bc.validate_text('<b>Матч</b> дня — <a href="https://t.me/x">тут</a>') is None


def test_empty_text_is_rejected():
    assert bc.validate_text("   ") == "Пустой текст"


def test_unsupported_tag_is_rejected():
    """Telegram fails the whole send on an unknown tag, so it must be caught
    while the operator is still looking at the form."""
    err = bc.validate_text("<div>hi</div>")
    assert err and "div" in err


def test_unclosed_tag_is_rejected():
    assert bc.validate_text("<b>hi") is not None


def test_mismatched_closing_tag_is_rejected():
    assert bc.validate_text("<b><i>hi</b></i>") is not None


def test_link_must_use_a_real_scheme():
    assert bc.validate_text('<a href="javascript:alert(1)">x</a>') is not None
    assert bc.validate_text('<a href="tg://resolve?domain=x">x</a>') is None


def test_text_over_the_telegram_limit_is_rejected():
    assert bc.validate_text("x" * (bc.MAX_TEXT + 1)) is not None


# ─── Buttons ──────────────────────────────────────────────────────────────────

def test_buttons_parse_rows():
    rows, err = bc.parse_buttons([[{"text": "Открыть", "url": "https://a.example"}]])
    assert err is None
    assert rows == [[{"text": "Открыть", "url": "https://a.example"}]]


def test_a_flat_list_becomes_one_button_per_row():
    rows, err = bc.parse_buttons([{"text": "A", "url": "https://a.example"}])
    assert err is None and rows == [[{"text": "A", "url": "https://a.example"}]]


def test_buttons_accept_json_string():
    rows, err = bc.parse_buttons('[[{"text":"A","url":"https://a.example"}]]')
    assert err is None and len(rows) == 1


def test_half_filled_button_is_dropped_not_rejected():
    rows, err = bc.parse_buttons([{"text": "", "url": ""}])
    assert err is None and rows == []


def test_button_url_scheme_is_enforced():
    _, err = bc.parse_buttons([{"text": "A", "url": "ftp://a.example"}])
    assert err is not None


def test_too_many_button_rows_rejected():
    many = [{"text": f"b{i}", "url": "https://a.example"} for i in range(bc.MAX_BUTTON_ROWS + 1)]
    _, err = bc.parse_buttons(many)
    assert err is not None


# ─── Scheduling time ──────────────────────────────────────────────────────────

def test_empty_time_means_now():
    assert bc.parse_run_at("") == ("", None)


def test_moscow_input_is_stored_as_utc():
    """The operator types Moscow time; the DB and every comparison are UTC."""
    local = datetime.now(bc.ADMIN_TZ) + timedelta(hours=2)
    run_at, err = bc.parse_run_at(local.strftime("%Y-%m-%dT%H:%M"))
    assert err is None
    stored = datetime.strptime(run_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert abs((stored - local.astimezone(timezone.utc)).total_seconds()) < 60


def test_past_time_is_rejected():
    past = (datetime.now(bc.ADMIN_TZ) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    _, err = bc.parse_run_at(past)
    assert err is not None


def test_garbage_time_is_rejected():
    _, err = bc.parse_run_at("вчера")
    assert err is not None


def test_utc_to_admin_shifts_by_three_hours():
    assert bc.utc_to_admin("2026-08-19 07:30:00") == "19.08 10:30"


# ─── Queueing ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def two_users(temp_db):
    for uid in (910001, 910002):
        temp_db.db_ensure(uid, f"u{uid}", "ru")
        temp_db.db_set(uid, "is_registered", 1)
    return temp_db


def test_queue_persists_the_broadcast(two_users):
    info, err = bc.queue("Привет", "all")
    assert err is None
    row = two_users.db_get_broadcast(info["id"])
    assert row["status"] == "pending"
    assert row["run_at"] == ""          # immediate
    assert row["text"] == "Привет"


def test_queue_stores_buttons_as_json(two_users):
    info, err = bc.queue("Привет", "all",
                         buttons=[{"text": "Go", "url": "https://a.example"}])
    assert err is None
    row = two_users.db_get_broadcast(info["id"])
    assert json.loads(row["buttons"])[0][0]["url"] == "https://a.example"


def test_queue_rejects_broken_markup_before_storing(two_users):
    info, err = bc.queue("<b>oops", "all")
    assert info is None and err is not None


def test_queue_rejects_an_empty_segment(two_users):
    info, err = bc.queue("Привет", "lang:zz")
    assert info is None and "получател" in err


def test_a_scheduled_broadcast_waits(two_users):
    when = (datetime.now(bc.ADMIN_TZ) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    info, err = bc.queue("Позже", "all", run_at_local=when)
    assert err is None and info["scheduled"] is True
    assert two_users.db_get_broadcast(info["id"])["run_at"]
    # Not due yet, so the scheduler must not see it.
    assert info["id"] not in [r["id"] for r in two_users.db_due_broadcasts()]


# ─── Claiming and cancelling ──────────────────────────────────────────────────

def test_a_broadcast_can_only_be_claimed_once(two_users):
    """Two schedulers overlapping during a redeploy must not both send it."""
    info, _ = bc.queue("Привет", "all")
    assert two_users.db_claim_broadcast(info["id"]) is True
    assert two_users.db_claim_broadcast(info["id"]) is False


def test_pending_broadcast_can_be_cancelled(two_users):
    info, _ = bc.queue("Привет", "all")
    assert two_users.db_cancel_broadcast(info["id"]) is True
    assert two_users.db_get_broadcast(info["id"])["status"] == "canceled"


def test_a_started_broadcast_cannot_be_cancelled(two_users):
    """Its messages are already out; pretending otherwise would mislead."""
    info, _ = bc.queue("Привет", "all")
    two_users.db_claim_broadcast(info["id"])
    assert two_users.db_cancel_broadcast(info["id"]) is False


# ─── Sending ──────────────────────────────────────────────────────────────────

class _FakeBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None,
                           disable_web_page_preview=False):
        if chat_id in self.fail_for:
            raise RuntimeError("blocked by user")
        self.sent.append(dict(chat_id=chat_id, text=text, parse_mode=parse_mode,
                              markup=reply_markup, no_preview=disable_web_page_preview))


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_run_broadcast_sends_to_the_segment(two_users, monkeypatch):
    monkeypatch.setattr(bc, "SEND_DELAY", 0)
    info, _ = bc.queue("<b>Привет</b>", "all", no_preview=True)
    two_users.db_claim_broadcast(info["id"])
    bot = _FakeBot()
    _run(bc.run_broadcast(bot, info["id"]))

    assert {m["chat_id"] for m in bot.sent} == {910001, 910002}
    assert bot.sent[0]["parse_mode"] == "HTML"     # links and bold survive
    assert bot.sent[0]["no_preview"] is True
    row = two_users.db_get_broadcast(info["id"])
    assert (row["status"], row["ok"], row["fail"], row["total"]) == ("done", 2, 0, 2)


def test_a_blocked_recipient_only_costs_one_failure(two_users, monkeypatch):
    monkeypatch.setattr(bc, "SEND_DELAY", 0)
    info, _ = bc.queue("Привет", "all")
    two_users.db_claim_broadcast(info["id"])
    _run(bc.run_broadcast(_FakeBot(fail_for={910001}), info["id"]))
    row = two_users.db_get_broadcast(info["id"])
    assert (row["status"], row["ok"], row["fail"]) == ("done", 1, 1)


def test_buttons_reach_telegram_as_a_keyboard(two_users, monkeypatch):
    monkeypatch.setattr(bc, "SEND_DELAY", 0)
    info, _ = bc.queue("Привет", "all",
                       buttons=[{"text": "Открыть", "url": "https://a.example"}])
    two_users.db_claim_broadcast(info["id"])
    bot = _FakeBot()
    _run(bc.run_broadcast(bot, info["id"]))
    markup = bot.sent[0]["markup"]
    assert markup is not None
    assert markup.inline_keyboard[0][0].url == "https://a.example"


# ─── Segments ─────────────────────────────────────────────────────────────────

def test_unknown_segment_resolves_to_nobody(two_users):
    """A typo must never fall back to "everyone"."""
    assert two_users.db_segment_uids("act:whatever") == []


def test_blocked_users_are_never_recipients(two_users):
    two_users.db_set(910001, "is_blocked", 1)
    assert 910001 not in two_users.db_segment_uids("all")


# ─── Metrics ──────────────────────────────────────────────────────────────────

def test_broadcast_metrics_summarise_reach(two_users, monkeypatch):
    monkeypatch.setattr(bc, "SEND_DELAY", 0)
    info, _ = bc.queue("Привет", "all")
    two_users.db_claim_broadcast(info["id"])
    _run(bc.run_broadcast(_FakeBot(fail_for={910002}), info["id"]))

    m = two_users.db_broadcast_metrics()
    assert m["campaigns"] >= 1
    assert m["ok"] >= 1 and m["fail"] >= 1
    assert 0 < m["delivery_pct"] <= 100
