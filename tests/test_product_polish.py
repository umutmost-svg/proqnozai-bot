"""Product-polish batch: things the bot already did but never showed.

Each test pins one visible behaviour — a button that exists, a dead end that
now has a way out, a stored field that is actually asked for. Offline.
"""
import types

import pytest

import config
import handlers.registration as reg
import handlers.utils as hutils
from config import reg_step
from translations import T, BOT_COMMANDS, OB_EXP, tr


# ─── Menu surfaces what is built ──────────────────────────────────────────────

def test_express_is_gone_from_the_menu(temp_db, monkeypatch):
    """The express feature was removed: no button, no command, no keys."""
    monkeypatch.setattr(hutils, "PROMO_CHANNEL", "")
    labels = [b.text for row in hutils.main_menu(1).keyboard for b in row]
    assert not any("кспресс" in b or "ombine" in b for b in labels), labels
    for lang in T:
        for dead in ("menu_express", "express_ask", "express_title",
                     "express_no_odds"):
            assert dead not in T[lang], f"{lang}: {dead}"
    assert "express" not in [c for c, _ in BOT_COMMANDS["ru"]]


def test_bonus_button_hidden_without_codes(temp_db, monkeypatch):
    """Promising a bonus and then answering "none yet" is the worst order."""
    monkeypatch.setattr(hutils, "PROMO_CHANNEL", "@ch")
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_campaign")
    labels = [b.text for row in hutils.main_menu(1).keyboard for b in row]
    assert T["ru"]["menu_get_promo"] not in labels

    temp_db.db_set_promo_code("Mostbet", "MB-POLISH", 5)
    labels = [b.text for row in hutils.main_menu(1).keyboard for b in row]
    assert T["ru"]["menu_get_promo"] in labels


# ─── Telegram's own command menu ──────────────────────────────────────────────

def test_every_language_lists_the_same_commands():
    ref = [c for c, _ in BOT_COMMANDS["ru"]]
    for lang, cmds in BOT_COMMANDS.items():
        assert [c for c, _ in cmds] == ref, lang
        for cmd, desc in cmds:
            assert cmd.islower() and 0 < len(desc) <= 256


def test_published_commands_are_all_registered():
    import inspect
    src = inspect.getsource(__import__("handlers", fromlist=["register_handlers"]))
    for cmd, _ in BOT_COMMANDS["ru"]:
        assert f'CommandHandler("{cmd}"' in src, cmd


# ─── Dead ends have a way out ─────────────────────────────────────────────────

def test_error_screens_offer_the_match_menu(temp_db):
    from handlers.forecast import _way_out_kb
    kb = _way_out_kb(1)
    assert kb.inline_keyboard[0][0].callback_data == "fm_restart"


# ─── Sport names are localized ────────────────────────────────────────────────

@pytest.mark.parametrize("uid,lang,expected", [(880101, "ru", "Футбол"),
                                               (880102, "en", "Football"),
                                               (880103, "tr", "Futbol")])
def test_sport_names_are_translated(temp_db, uid, lang, expected):
    from handlers.forecast_kb import sport_name
    temp_db.db_ensure(uid, "u", lang)
    assert sport_name(uid, "football") == expected


def test_unknown_sport_falls_back_readably(temp_db):
    from handlers.forecast_kb import sport_name
    uid = 880200
    temp_db.db_ensure(uid, "u", "ru")
    assert sport_name(uid, "cricket") == "Cricket"


# ─── Onboarding asks for the field the prompt uses ────────────────────────────

class _Q:
    def __init__(self, uid, data):
        self.from_user = types.SimpleNamespace(id=uid, username="u",
                                               language_code="ru")
        self.data = data
        self.edited = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


async def test_sport_choice_leads_to_the_experience_question(temp_db):
    uid = 880300
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    reg_step[uid] = "ob_sports"
    bot = _Bot()
    await reg.ob_cb(types.SimpleNamespace(callback_query=_Q(uid, "ob_football")),
                    types.SimpleNamespace(bot=bot, user_data={}))
    assert reg_step[uid] == "ob_exp"
    assert bot.sent[-1][0] == T["ru"]["ob_exp"]
    # Not finished yet — onboarding_done must wait for the second answer.
    assert temp_db.db_get(uid)["onboarding_done"] == 0


async def test_experience_answer_is_stored_and_finishes_onboarding(temp_db):
    uid = 880301
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    temp_db.db_set(uid, "sports", "football")
    reg_step[uid] = "ob_exp"
    bot = _Bot()
    await reg.ob_cb(types.SimpleNamespace(callback_query=_Q(uid, "ob_expert")),
                    types.SimpleNamespace(bot=bot, user_data={}))
    u = temp_db.db_get(uid)
    assert u["experience"] == "expert"          # the value the prompt reads
    assert u["onboarding_done"] == 1
    assert reg_step[uid] == "done"


def test_experience_options_cover_every_language():
    for lang, opts in OB_EXP.items():
        assert [v for _, v in opts] == ["beginner", "mid", "expert"], lang


# ─── Profile is not a dead end ────────────────────────────────────────────────

async def test_profile_offers_language_and_timezone(temp_db):
    uid = 880400
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    sent = []

    class _M:
        async def reply_text(self, text, reply_markup=None, **kw):
            sent.append((text, reply_markup))

    await reg.profile_cmd(
        types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid), message=_M()),
        types.SimpleNamespace(user_data={}))
    data = [b.callback_data for row in sent[0][1].inline_keyboard for b in row]
    assert data == ["prof_lang", "prof_tz"]


async def test_timezone_button_starts_the_same_flow_as_the_command(temp_db):
    uid = 880401
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    bot = _Bot()
    ctx = types.SimpleNamespace(bot=bot, user_data={})
    await reg.profile_settings_cb(
        types.SimpleNamespace(callback_query=_Q(uid, "prof_tz")), ctx)
    assert ctx.user_data["awaiting_tz"] is True
    assert "UTC" in bot.sent[-1][0]


# ─── Welcome copy matches the product ─────────────────────────────────────────

def test_welcome_language_list_matches_the_buttons():
    """The welcome screen used to greet in 7 languages while offering 5."""
    offered = {b.callback_data.split("_", 1)[1]
               for row in hutils.lang_kb().inline_keyboard for b in row}
    for gone in ("uz", "ar"):
        assert gone not in offered
    assert "O'zbek" not in config.UNIVERSAL_WELCOME
    assert "العربية" not in config.UNIVERSAL_WELCOME


def test_welcome_is_not_football_only():
    for lang in T:
        intro = T[lang]["welcome_intro"]
        assert "📸" in intro, lang          # more than one way in is offered
        assert "⚡" not in intro, lang      # …but not the removed express


def test_no_service_footer_keys_remain():
    """The forecast ends on the recommendation: every footer string that used
    to be appended under it is gone from the translations."""
    for lang in T:
        for dead in ("fc_basis", "bot_winrate", "bot_winrate_building",
                     "enr_standings_unavailable", "enr_lineups_unavailable",
                     "enr_injuries_unavailable"):
            assert dead not in T[lang], f"{lang}: {dead}"


@pytest.mark.parametrize("tail", [
    "⚠️ (оценочно — реальные данные по форме и составам недоступны)",
    "📐 Основано на доступных данных и рыночных коэффициентах",
    "18+ • Не является финансовой рекомендацией",
    "ℹ️ Составы пока неизвестны.",
])
def test_a_model_written_footer_is_trimmed(tail):
    """The prompt no longer asks for a closing caveat, but the model still
    volunteers one; the reply must end on the bet line regardless."""
    from handlers.forecast import _strip_footer
    body = "🏆 A — B\n⚡ Ставка: П1 @1.85 — причина"
    assert _strip_footer(f"{body}\n\n{tail}") == body


def test_trimming_never_empties_a_reply():
    from handlers.forecast import _strip_footer
    only_footer = "⚠️ всё, что вернула модель"
    assert _strip_footer(only_footer) == only_footer


def test_error_texts_survive_trimming(temp_db):
    """_strip_footer runs before the produced/failed check, so it must not
    rewrite the two error strings that check compares against."""
    from handlers.forecast import _strip_footer
    uid = 880600
    temp_db.db_ensure(uid, "u", "ru")
    for key in ("api_error", "api_overload"):
        assert _strip_footer(tr(uid, key)) == tr(uid, key)


def test_promo_preview_names_the_partners(temp_db):
    uid = 880500
    temp_db.db_ensure(uid, "u", "ru")
    assert "Mostbet" in tr(uid, "promo_preview", partners="Mostbet")


async def test_command_scopes_use_real_language_codes_and_survive_failures():
    """Telegram scopes by ISO 639-1: our "kz" must go out as "kk". And one
    rejected scope must not abandon the ones after it in the loop."""
    from telegram.error import BadRequest
    import main

    seen = []

    class _Bot:
        async def set_my_commands(self, cmds, language_code=None, **kw):
            seen.append(language_code)
            if language_code == "tr":
                raise BadRequest("rejected on purpose")

    await main._publish_commands(types.SimpleNamespace(bot=_Bot()))
    assert seen[0] is None                       # default scope published first
    assert "kk" in seen and "kz" not in seen
    assert {"az", "ru", "en", "tr", "uz", "ar"} <= set(seen)   # tr failure not fatal


def test_legacy_users_without_an_experience_value_still_get_a_prompt(temp_db):
    """The column defaults to '', so pre-existing users never answered. That
    must mean "no extra hint", not a crash or a broken prompt."""
    from handlers.forecast import _build_system_prompt
    from translations import EXP_LABELS
    base = _build_system_prompt("ru", "beginner", False)
    for legacy in ("", None):
        p = _build_system_prompt("ru", legacy, False)
        assert p and len(p) < len(base)          # valid prompt, just no hint
    assert EXP_LABELS["ru"].get("", "Beginner") == "Beginner"


# ─── the handler marks PARTIAL, and nothing trails the recommendation ─────────

class _StatusMsg2:
    text = ""
    markup = None

    async def edit_text(self, text, **kw):
        self.text = text
        self.markup = kw.get("reply_markup")


@pytest.fixture
def captured_prompt(monkeypatch, partners):
    """Capture the system prompt the handler actually sends."""
    import config
    import handlers.forecast as fc
    monkeypatch.setattr(config, "APIFOOTBALL_KEY", "")
    partners([])
    seen = {}

    async def _claude(uid, content, sys_prompt, *a, **k):
        seen["prompt"] = sys_prompt
        return "🏆 A — B\n⚡ Ставка: П1 @1.85 — причина"

    async def _no_search(*a, **k):
        return []

    monkeypatch.setattr(fc, "claude_forecast", _claude)
    monkeypatch.setattr(fc, "search_match", _no_search)
    return seen


def _fc_ctx(has_odds, has_real_data):
    return types.SimpleNamespace(user_data={
        "pending_content": [{"type": "text", "text": "Barcelona Real"}],
        "pending_text": "Barcelona Real",
        "parsed_teams": ("Barcelona", "Real"),
        "odds_attached": True,
        "has_odds": has_odds,
        "has_real_data": has_real_data,
    }, bot=None)


@pytest.mark.parametrize("has_odds,has_real_data", [(True, False), (False, True)])
async def test_partial_forecast_is_marked_in_the_prompt(temp_db, captured_prompt,
                                                        has_odds, has_real_data):
    import handlers.forecast as fc
    uid = 880700
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    await fc._generate_forecast(uid, _fc_ctx(has_odds, has_real_data), _StatusMsg2())
    assert "PARTIAL DATA" in captured_prompt["prompt"]


async def test_ready_forecast_is_not_marked(temp_db, captured_prompt):
    import handlers.forecast as fc
    uid = 880701
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    await fc._generate_forecast(uid, _fc_ctx(True, True), _StatusMsg2())
    assert "PARTIAL DATA" not in captured_prompt["prompt"]


async def test_reply_ends_on_the_recommendation(temp_db, captured_prompt):
    """Whatever the mode, the last line of the message is the bet."""
    import handlers.forecast as fc
    uid = 880702
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    msg = _StatusMsg2()
    await fc._generate_forecast(uid, _fc_ctx(True, False), msg)
    assert msg.text.splitlines()[-1].startswith("⚡ Ставка:")
