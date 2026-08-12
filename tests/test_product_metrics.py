"""Product metrics computed from the event log.

Two measurement traps these guard against: forecast_history is capped at 10
rows per user, so it can never count volume; and the menu forecast flow used to
log nothing at all, which made menu-only users invisible to last_active, the
dashboard, the broadcast segments and daily_push. Offline.
"""
import types




# ─── the FORECAST event exists and updates activity ───────────────────────────

def test_forecast_event_marks_the_user_active(temp_db):
    uid = 810001
    temp_db.db_ensure(uid, "u", "ru")
    assert (temp_db.db_get(uid)["last_active"] or "") == ""
    temp_db.db_log_req(uid, temp_db.REQ_FORECAST, ok=True, ms=1200)
    assert temp_db.db_get(uid)["last_active"]          # the regression


def test_forecast_event_records_outcome_and_duration(temp_db):
    """temp_db is shared across the session, so assert on this user's rows."""
    uid = 810002
    temp_db.db_ensure(uid, "u", "ru")
    temp_db.db_log_req(uid, temp_db.REQ_FORECAST, ok=True, ms=3000)
    temp_db.db_log_req(uid, temp_db.REQ_FORECAST, ok=False, ms=500)
    with temp_db.con() as c:
        rows = c.execute("SELECT ok, ms FROM requests WHERE user_id=? "
                         "AND msg_type='FORECAST' ORDER BY id", (uid,)).fetchall()
    assert rows == [(1, 3000), (0, 500)]
    health = temp_db.db_forecast_health()
    assert health["total"] >= 2
    assert health["ok"] + health["failed"] == health["total"]
    assert 0 <= health["ok_pct"] <= 100


def test_plain_messages_carry_no_outcome(temp_db):
    """ok/ms are meaningless for an inbound message and must stay NULL, or they
    would pollute the success rate."""
    uid = 810003
    temp_db.db_ensure(uid, "u", "ru")
    temp_db.db_log_req(uid, temp_db.REQ_TEXT)
    with temp_db.con() as c:
        row = c.execute("SELECT ok, ms FROM requests WHERE user_id=?", (uid,)).fetchone()
    assert row == (None, None)


# ─── volume is measured from the uncapped log ─────────────────────────────────

def test_forecast_count_is_not_capped_like_history(temp_db):
    """forecast_history keeps only 10 rows per user; the event log keeps all."""
    uid = 810004
    temp_db.db_ensure(uid, "u", "ru")
    for i in range(25):
        temp_db.db_save_history(uid, f"q{i}", "f")
        temp_db.db_log_req(uid, temp_db.REQ_FORECAST, ok=True, ms=100)
    with temp_db.con() as c:
        history_rows = c.execute(
            "SELECT COUNT(*) FROM forecast_history WHERE user_id=?", (uid,)).fetchone()[0]
        events = c.execute(
            "SELECT COUNT(*) FROM requests WHERE user_id=? AND msg_type='FORECAST'",
            (uid,)).fetchone()[0]
    assert history_rows == 10       # capped, useless as a volume metric
    assert events == 25             # what the dashboard now counts


# ─── funnel / engagement / churn ──────────────────────────────────────────────

def test_activation_funnel_steps_are_ordered(temp_db):
    for i, (reg, onb, forecast) in enumerate([(1, 1, True), (1, 1, False), (1, 0, False), (0, 0, False)]):
        uid = 810100 + i
        temp_db.db_ensure(uid, "u", "ru")
        temp_db.db_set(uid, "is_registered", reg)
        temp_db.db_set(uid, "onboarding_done", onb)
        if forecast:
            temp_db.db_log_req(uid, temp_db.REQ_FORECAST, ok=True, ms=10)
    f = temp_db.db_activation_funnel()
    # Strictly nested by construction — otherwise the percentages are nonsense.
    assert f["started"] >= f["registered"] >= f["onboarded"] >= f["forecasted"]
    # The raw count of anyone who ever got a forecast is kept separately and
    # may legitimately exceed the nested step (users from before onboarding).
    assert f["forecasted_any"] >= f["forecasted"]


def test_engagement_stickiness(temp_db):
    for i in range(4):
        uid = 810200 + i
        temp_db.db_ensure(uid, "u", "ru")
        temp_db.db_log_req(uid, temp_db.REQ_FORECAST, ok=True, ms=10)
    e = temp_db.db_engagement()
    assert e["dau"] >= 4 and e["mau"] >= 4
    assert 0 <= e["stickiness"] <= 100


def test_churn_buckets_are_disjoint(temp_db):
    e = temp_db.db_churn()
    total = e["active_7d"] + e["silent_7_30"] + e["silent_30"] + e["never"]
    registered = temp_db.db_activation_funnel()["registered"]
    assert total == registered      # every registered user lands in exactly one


def test_feedback_coverage_is_a_share_of_all_forecasts(temp_db):
    uid = 810300
    temp_db.db_ensure(uid, "u", "ru")
    for i in range(4):
        temp_db.db_save_history(uid, f"q{i}", "f")
    hid = temp_db.db_get_history(uid)[0]["id"]
    temp_db.db_set_feedback(uid, hid, 1)
    cov = temp_db.db_feedback_coverage()
    assert cov["rated"] >= 1
    assert 0 < cov["pct"] <= 100


def test_retention_percentages_are_bounded(temp_db):
    temp_db.db_ensure(810400, "u", "ru")
    for row in temp_db.db_retention():
        assert row["size"] > 0
        for k in ("d1", "d7", "d30"):
            # None = the window hasn't elapsed for this cohort yet.
            assert row[k] is None or 0 <= row[k] <= 100


def test_retention_hides_windows_a_cohort_cannot_have_reached(temp_db):
    """A cohort registered today has no D7 number; reporting 0% would make
    healthy retention look broken."""
    temp_db.db_ensure(810401, "u", "ru")
    today = [r for r in temp_db.db_retention() if r["age"] == 0]
    assert today, "today's cohort should be present"
    assert today[0]["d1"] is None and today[0]["d7"] is None and today[0]["d30"] is None


# ─── promo funnel ─────────────────────────────────────────────────────────────

def test_promo_funnel_reports_cap_and_conversion(temp_db):
    temp_db.db_ensure(810500, "u", "ru"); temp_db.db_set(810500, "is_registered", 1)
    temp_db.db_set_promo_code("Funnel", "FUNNEL-CODE", 10)
    temp_db.db_claim_promos(810500)
    p = temp_db.db_promo_funnel()
    names = [x["partner"] for x in p["partners"]]
    assert "Funnel" in names
    assert p["claimed"] >= 1 and p["remaining"] >= 0
    assert p["eligible"] >= 1 and 0 <= p["conversion"] <= 100


def test_promo_funnel_without_a_campaign(temp_db):
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_campaign")
    p = temp_db.db_promo_funnel()
    assert p["partners"] == [] and p["claimed"] == 0


# ─── partner clicks ───────────────────────────────────────────────────────────

def test_partner_clicks_group_by_partner(temp_db):
    temp_db.db_log_partner_click(810600, "Mostbet")
    temp_db.db_log_partner_click(810601, "Mostbet")
    temp_db.db_log_partner_click(810600, "1xBet")
    p = temp_db.db_partner_clicks()
    by = dict((name, (clicks, users)) for name, clicks, users in p["by_partner"])
    assert by["Mostbet"] == (2, 2)
    assert by["1xBet"] == (1, 1)
    assert p["unique_users"] >= 2


def test_click_through_never_exceeds_100(temp_db):
    """Clicks can come from a list opened before the window; the ratio is
    clamped so the dashboard never shows a nonsensical 200%."""
    temp_db.db_log_req(810700, "PARTNERS_OPEN")
    for uid in (810700, 810701, 810702):
        temp_db.db_log_partner_click(uid, "Mostbet")
    assert temp_db.db_partner_clicks()["click_through"] <= 100


def test_partner_click_logging_never_raises(temp_db, monkeypatch):
    """Recording must not be able to break the redirect."""
    import sqlite3

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(temp_db.sqlite3, "connect", _boom)
    temp_db.db_log_partner_click(810800, "Mostbet")     # must not raise


# ─── partner URL construction ─────────────────────────────────────────────────

def test_partner_url_is_direct_when_tracking_is_off(monkeypatch):
    import config
    from handlers.forecast import partner_url
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "")
    assert partner_url("Mostbet", "https://mostbet.com", 42) == "https://mostbet.com"


def test_partner_url_goes_through_the_redirect_when_configured(monkeypatch):
    import config
    from handlers.forecast import partner_url
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "https://dash.example")
    url = partner_url("Mostbet", "https://mostbet.com", 42)
    assert url == "https://dash.example/r/Mostbet?u=42"


def test_partner_url_escapes_the_name(monkeypatch):
    import config
    from handlers.forecast import partner_url
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "https://dash.example")
    url = partner_url("Bet & Win", "https://x.example", 7)
    assert " " not in url and "&" not in url.split("?")[0]


def test_partner_url_falls_back_to_the_host_when_unnamed(monkeypatch):
    import config
    from handlers.forecast import partner_url
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "https://dash.example")
    assert partner_url("", "https://mostbet.com/promo", 1) == \
        "https://dash.example/r/mostbet.com?u=1"


# ─── opening the partner list is recorded ─────────────────────────────────────

class _Msg:
    def __init__(self, text):
        self.text = text; self.caption = None; self.photo = None
        self.replies = []
        self.chat = types.SimpleNamespace(send_action=self._noop)

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))

    async def _noop(self, *a, **k):
        pass


def _update(uid, text):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid, username="u", full_name="U",
                                             first_name="U", language_code="ru"),
        message=_Msg(text))


async def test_opening_the_partner_list_is_logged(temp_db, monkeypatch):
    import config
    import handlers.forecast as fc
    from translations import T

    uid = 810900
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    monkeypatch.setattr(config, "PARTNERS", [("Mostbet", "https://mostbet.com")])
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "")

    upd = _update(uid, T["ru"]["menu_partners"])
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))

    with temp_db.con() as c:
        n = c.execute("SELECT COUNT(*) FROM requests WHERE user_id=? "
                      "AND msg_type='PARTNERS_OPEN'", (uid,)).fetchone()[0]
    assert n == 1
    _, kb = upd.message.replies[0]
    assert kb.inline_keyboard[0][0].url == "https://mostbet.com"
