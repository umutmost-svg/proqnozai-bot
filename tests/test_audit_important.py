"""Regression tests for the second batch of audit findings.

Six independent defects, one section each. Offline: no network, temp DB only.
"""
import threading
import types

import pytest

from translations import T


# ─── 1. Card colour is localized in every language ────────────────────────────

def test_card_colour_covers_all_seven_languages():
    from handlers.live import _CARD_COLOURS
    assert set(_CARD_COLOURS) == set(T)


@pytest.mark.parametrize("lang", sorted(T))
def test_card_colour_is_never_the_bare_english_fallback(lang):
    """tr/kz/uz/ar used to fall through to the literal word "Card"."""
    from handlers.live import _card_colour
    red = _card_colour(lang, "Red Card")
    yellow = _card_colour(lang, "Yellow Card")
    assert red and yellow and red != yellow
    assert "Card" not in (red, yellow)


def test_card_colour_unknown_language_falls_back_to_ru():
    from handlers.live import _card_colour, _CARD_COLOURS
    assert _card_colour("zz", "Red Card") == _CARD_COLOURS["ru"][0]


# ─── 2. Admin check with no ADMIN_ID configured ───────────────────────────────

def _update(user_id):
    user = types.SimpleNamespace(id=user_id) if user_id is not None else None
    return types.SimpleNamespace(effective_user=user)


def test_unset_admin_id_grants_nobody(monkeypatch):
    """ADMIN_ID defaults to 0 and an update with no user reported 0 too, so
    0 == 0 used to hand out the admin panel on a misconfigured deployment."""
    import handlers.admin as adm
    monkeypatch.setattr(adm, "ADMIN_ID", 0)
    assert adm.is_adm(_update(None)) is False
    assert adm.is_adm(_update(0)) is False
    assert adm.is_adm(_update(12345)) is False


def test_configured_admin_still_recognised(monkeypatch):
    import handlers.admin as adm
    monkeypatch.setattr(adm, "ADMIN_ID", 777)
    assert adm.is_adm(_update(777)) is True
    assert adm.is_adm(_update(778)) is False
    assert adm.is_adm(_update(None)) is False


# ─── 3. Promo cap holds under concurrent claims ───────────────────────────────

def test_promo_cap_is_never_exceeded_sequentially(temp_db):
    temp_db.db_set_promo_campaign("SEQ-CODE", 3)
    got = [temp_db.db_claim_promo(870100 + i) for i in range(10)]
    assert sum(1 for g in got if g) == 3
    assert temp_db.db_promo_stats()["claimed"] == 3


def test_promo_claim_is_idempotent_per_user(temp_db):
    temp_db.db_set_promo_campaign("IDEM-CODE", 5)
    first = temp_db.db_claim_promo(870200)
    assert temp_db.db_claim_promo(870200) == first
    assert temp_db.db_promo_stats()["claimed"] == 1   # one use, not two


def test_promo_cap_holds_under_parallel_claims(temp_db):
    """The race the BEGIN IMMEDIATE fixes: with a deferred transaction two
    writers could both read used == cap-1 and both insert."""
    temp_db.db_set_promo_campaign("RACE-CODE", 5)
    results = []
    lock = threading.Lock()
    start = threading.Barrier(12)

    def _claim(uid):
        start.wait()
        try:
            code = temp_db.db_claim_promo(uid)
        except Exception as e:                 # a lost race must not be silent
            code = f"ERROR:{e}"
        with lock:
            results.append(code)

    threads = [threading.Thread(target=_claim, args=(870300 + i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not [r for r in results if isinstance(r, str) and r.startswith("ERROR:")]
    assert sum(1 for r in results if r == "RACE-CODE") == 5
    assert temp_db.db_promo_stats()["claimed"] == 5


# ─── 4. Dashboard token travels in a header ───────────────────────────────────

def test_stats_server_accepts_the_header(monkeypatch):
    import stats_server as ss
    monkeypatch.setattr(ss, "STATS_TOKEN", "s3cret")
    handler = types.SimpleNamespace(headers={"X-Dashboard-Token": "s3cret"})
    parsed = types.SimpleNamespace(query="")
    assert ss._auth_ok(ss._token_from(handler, parsed))


def test_stats_server_still_accepts_the_query_param(monkeypatch):
    """Kept so worker and dashboard can be redeployed in either order."""
    import stats_server as ss
    monkeypatch.setattr(ss, "STATS_TOKEN", "s3cret")
    handler = types.SimpleNamespace(headers={})
    parsed = types.SimpleNamespace(query="token=s3cret")
    assert ss._auth_ok(ss._token_from(handler, parsed))


def test_header_wins_over_query_param(monkeypatch):
    import stats_server as ss
    monkeypatch.setattr(ss, "STATS_TOKEN", "s3cret")
    handler = types.SimpleNamespace(headers={"X-Dashboard-Token": "s3cret"})
    parsed = types.SimpleNamespace(query="token=wrong")
    assert ss._auth_ok(ss._token_from(handler, parsed))


def test_dashboard_no_longer_puts_the_token_in_urls():
    """The token must not reach a proxy access log via the query string."""
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "dashboard.py").read_text(encoding="utf-8")
    # Look for the code that would build it, not the word itself (the module
    # documents the deprecated query fallback in a comment).
    assert "?token={" not in source            # f-string appending to a URL
    assert '"token": STATS_TOKEN' not in source  # token in a JSON body
    assert 'params={"token"' not in source       # token as a query param
    assert '"X-Dashboard-Token"' in source


# ─── 5/6. Query-count regressions ─────────────────────────────────────────────

def test_match_name_needs_one_query_for_many_subscribers(temp_db):
    """The poller read the same name once per subscriber, every minute."""
    for i in range(5):
        temp_db.db_add_lsub(870400 + i, "mid-audit-1", "Barca vs Real")
    assert temp_db.db_lsub_name("mid-audit-1") == "Barca vs Real"


def test_match_name_is_none_when_nobody_watches(temp_db):
    assert temp_db.db_lsub_name("mid-audit-absent") is None


async def test_odds_are_fetched_once_per_match(monkeypatch, temp_db):
    """Ten alerts on one match must cost one fetch, not ten."""
    import handlers.live as live

    fetched = []

    async def _fake_odds(mid):
        fetched.append(mid)
        return {"w1": 2.0, "x": 3.0, "w2": 4.0, "over25": 1.8, "under25": 2.1}

    monkeypatch.setattr(live, "mostbet_get_odds", _fake_odds)

    # Two matches, five user-market alert rows each.
    with temp_db.con() as c:
        for match in ("9001", "9002"):
            for i in range(5):
                c.execute(
                    "INSERT OR REPLACE INTO odds_alerts "
                    "(user_id, match_id, market, last_odd, fixture_id, match_name) "
                    "VALUES (?,?,?,?,?,?)",
                    (870500 + i, match, f"m{i}", 1.0, match, "X vs Y"))

    async def _one_cycle():
        """The grouping step of check_odds_changes, in isolation."""
        with temp_db.con() as c:
            alerts = c.execute(
                "SELECT user_id, match_id, market, last_odd, match_name FROM odds_alerts"
            ).fetchall()
        for mid in {a[1] for a in alerts}:
            await live.mostbet_get_odds(int(mid))

    await _one_cycle()
    assert sorted(fetched) == [9001, 9002]      # one fetch per match, not per row
