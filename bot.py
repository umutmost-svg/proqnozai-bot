import os
import logging
import base64
import time
import sqlite3
import asyncio
import httpx
from collections import defaultdict, deque

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import anthropic

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
suspicious_logger = logging.getLogger("suspicious")
_sh = logging.FileHandler("suspicious.log", encoding="utf-8")
_sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
suspicious_logger.addHandler(_sh)
suspicious_logger.setLevel(logging.WARNING)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID          = int(os.environ.get("ADMIN_ID", "0"))
FOOTBALL_API_KEY  = os.environ.get("FOOTBALL_API_KEY", "")

RATE_LIMIT_WINDOW   = 60
RATE_LIMIT_MAX      = 5
SPAM_BLOCK_AFTER    = 3
SPAM_BLOCK_DURATION = 10 * 60

# ─── Security (in-memory) ─────────────────────────────────────────────────────
user_message_times: dict[int, deque] = defaultdict(deque)
user_violations:    dict[int, int]   = defaultdict(int)
user_blocked_until: dict[int, float] = {}

# Шаги регистрации: None | "awaiting_name" | "awaiting_phone" | "done"
user_reg_step: dict[int, str] = {}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
# ─── Football Data API ────────────────────────────────────────────────────────

async def fetch_team_data(team_name: str) -> str:
    """Fetches real team form from football-data.org"""
    if not FOOTBALL_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(
                "https://api.football-data.org/v4/teams",
                headers={"X-Auth-Token": FOOTBALL_API_KEY},
                params={"name": team_name, "limit": 1}
            )
            if r.status_code != 200:
                return ""
            teams = r.json().get("teams", [])
            if not teams:
                return ""
            team_id   = teams[0]["id"]
            team_full = teams[0]["name"]
            r2 = await http.get(
                f"https://api.football-data.org/v4/teams/{team_id}/matches",
                headers={"X-Auth-Token": FOOTBALL_API_KEY},
                params={"status": "FINISHED", "limit": 5}
            )
            if r2.status_code != 200:
                return f"{team_full}: данные недоступны"
            matches = r2.json().get("matches", [])
            if not matches:
                return f"{team_full}: нет данных"
            results = []
            for m in matches:
                home  = m["homeTeam"]["name"]
                away  = m["awayTeam"]["name"]
                score = m["score"]["fullTime"]
                gh, ga = score.get("home") or 0, score.get("away") or 0
                date  = m["utcDate"][:10]
                if home == team_full:
                    res = "W" if gh > ga else ("D" if gh == ga else "L")
                else:
                    res = "W" if ga > gh else ("D" if gh == ga else "L")
                results.append(f"{date} {home} {gh}-{ga} {away} [{res}]")
            return team_full + ":\n" + "\n".join(results)
    except Exception as e:
        logger.error(f"Football API error: {e}")
        return ""



# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = "bot.db"

def db_connect():
    return sqlite3.connect(DB_PATH)

def db_init():
    with db_connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            display_name TEXT,
            phone        TEXT,
            lang         TEXT DEFAULT 'az',
            referred_by  INTEGER,
            ref_count    INTEGER DEFAULT 0,
            is_registered INTEGER DEFAULT 0,
            joined_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            msg_type   TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)

db_init()

# ─── DB helpers ───────────────────────────────────────────────────────────────

def db_ensure_user(user_id: int, username: str):
    with db_connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)",
            (user_id, username)
        )

def db_set_field(user_id: int, field: str, value):
    with db_connect() as con:
        con.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))

def db_get_user(user_id: int) -> dict | None:
    with db_connect() as con:
        row = con.execute(
            "SELECT user_id,username,display_name,phone,lang,ref_count,is_registered "
            "FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return dict(user_id=row[0], username=row[1], display_name=row[2],
                phone=row[3], lang=row[4], ref_count=row[5], is_registered=row[6])

def db_is_registered(user_id: int) -> bool:
    u = db_get_user(user_id)
    return bool(u and u["is_registered"])

def db_complete_registration(user_id: int, referred_by: int | None):
    with db_connect() as con:
        con.execute("UPDATE users SET is_registered=1 WHERE user_id=?", (user_id,))
        if referred_by:
            con.execute(
                "UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (referred_by,)
            )
            con.execute(
                "UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
                (referred_by, user_id)
            )

def db_log_request(user_id: int, msg_type: str):
    with db_connect() as con:
        con.execute("INSERT INTO requests (user_id,msg_type) VALUES (?,?)", (user_id, msg_type))

def db_all_users() -> list[int]:
    with db_connect() as con:
        return [r[0] for r in con.execute("SELECT user_id FROM users WHERE is_registered=1").fetchall()]

def db_get_lang(user_id: int) -> str | None:
    u = db_get_user(user_id)
    return u["lang"] if u else None

def db_get_ref_count(user_id: int) -> int:
    u = db_get_user(user_id)
    return u["ref_count"] if u else 0

def db_stats() -> dict:
    with db_connect() as con:
        total     = con.execute("SELECT COUNT(*) FROM users WHERE is_registered=1").fetchone()[0]
        today     = con.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=date('now') AND is_registered=1").fetchone()[0]
        req_total = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        req_today = con.execute("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now')").fetchone()[0]
        top_refs  = con.execute("SELECT user_id,username,ref_count FROM users ORDER BY ref_count DESC LIMIT 5").fetchall()
    return dict(total=total, today=today, req_total=req_total, req_today=req_today, top_refs=top_refs)

# ─── Translations ─────────────────────────────────────────────────────────────
T = {
    "az": {
        "choose_lang":    "🌐 Dil seçin / Выберите язык / Choose language:",
        "ask_name":       "👋 Xoş gəldiniz!\n\nZəhmət olmasa adınızı və soyadınızı daxil edin:",
        "ask_phone":      "📱 İndi telefon nömrənizi paylaşın.\nAşağıdakı düyməyə basın:",
        "phone_btn":      "📱 Nömrəmi paylaş",
        "reg_done":       "✅ *Qeydiyyat tamamlandı!*\n\n Salam, {name}! Artıq idman proqnozları əldə edə bilərsiniz.\n\nHadisə haqqında mətn yazın və ya şəkil göndərin. ⚽🏀🎾🥊",
        "reg_done_ref":   "✅ *Qeydiyyat tamamlandı!*\n\nSalam, {name}! Sizi dəvət edən dostunuz bonus qazandı 🎁\n\nHadisə haqqında mətn yazın və ya şəkil göndərin. ⚽🏀🎾🥊",
        "already_reg":    "✅ Siz artıq qeydiyyatdan keçmisiniz, {name}!",
        "need_reg":       "⚠️ Əvvəlcə qeydiyyatdan keçin. /start yazın.",
        "blocked":        "🚫 Müvəqqəti bloklanmısınız.\n⏳ {m} dəq {s} san sonra yenidən cəhd edin.",
        "rate_limit":     "⏳ Sorğu limiti aşıldı. {w} saniyə gözləyin.\n⚠️ Xəbərdarlıq: {v}/{max}",
        "auto_blocked":   "🚫 Çox sayda sorğu. {min} dəqiqəlik blok.",
        "long_text":      "⚠️ Mətn çox uzundur. Zəhmət olmasa qısaldın.",
        "injection":      "⚠️ Yalnız idman sorğuları qəbul edilir.",
        "no_input":       "Zəhmət olmasa mətn yazın və ya şəkil göndərin.",
        "img_prompt":     "Şəkildəki idman hadisəsini müəyyən et və proqnoz ver.",
        "api_overload":   "⚠️ Servis yükləməsi. Bir az sonra yenidən cəhd edin.",
        "api_error":      "⚠️ Xəta baş verdi. Bir az sonra yenidən cəhd edin.",
        "lang_set":       "✅ Dil Azərbaycan dilinə təyin edildi.",
        "ref_link":       "🔗 *Referans linkınız:*\n`https://t.me/{bot}?start=ref{uid}`\n\n👥 Dəvət etdiyiniz: *{count}* nəfər",
        "profile":        "👤 *Profiliniz*\n\n🏷 Ad: {name}\n📱 Telefon: {phone}\n🌐 Dil: {lang}\n👥 Dəvətlər: {refs}",
                "system_prompt":  """Sen 15 illik tecrubeli elit idman analitikisen. Tarix: May 2026.

SSLUB: Qeti, inamli, profesional. "Ola biler", "belkede" yoxdur.

QAYDALAR:
- Yalniz 2025-2026 mevsumu melumatlar
- Kohnelerin heyetlerini (Mbappe PSJ-de ve s.) YAZMA
- Real melumat varsa - onu istifade et
- Emoji istifade ETME - yalniz duz metn

CAVAB FORMATI:

HADISE: [Komanda A] - [Komanda B] | [Turnir] | [Tarix]

FORMA:
[Komanda A]: son 5 oyun
[Komanda B]: son 5 oyun

ESAS AMILLER:
1. [Amil 1]
2. [Amil 2]
3. [Amil 3]

PROQNOZ:
1X2:
- [Komanda A] qelebesi: XX% | Kes: 1.XX - 1.XX arasi
- Hec-hece: XX% | Kes: X.XX - X.XX arasi
- [Komanda B] qelebesi: XX% | Kes: X.XX - X.XX arasi

Total:
- 2.5 Ustunde: XX% | Kes: 1.XX - 1.XX arasi
- 2.5 Altinda: XX% | Kes: 1.XX - 1.XX arasi

Her ikisi qol vurur:
- Beli: XX% | Kes: 1.XX - 1.XX arasi
- Xeyr: XX% | Kes: 1.XX - 1.XX arasi

EN GUCLU MERC:
[Daqiq merc novu] | Kes: X.XX - X.XX | Ehtimal: XX%
Sebeb: [1 cumle]

XEBERDARLIQ: Analitik proqnozdur. Merc oz riskinizdir.""",
    },
    "ru": {
        "choose_lang":    "🌐 Dil seçin / Выберите язык / Choose language:",
        "ask_name":       "👋 Добро пожаловать!\n\nПожалуйста, введите ваше имя и фамилию:",
        "ask_phone":      "📱 Теперь поделитесь номером телефона.\nНажмите кнопку ниже:",
        "phone_btn":      "📱 Поделиться номером",
        "reg_done":       "✅ *Регистрация завершена!*\n\nПривет, {name}! Теперь вы можете получать спортивные прогнозы.\n\nОтправьте текст о матче или фото. ⚽🏀🎾🥊",
        "reg_done_ref":   "✅ *Регистрация завершена!*\n\nПривет, {name}! Ваш друг, который пригласил вас, получил бонус 🎁\n\nОтправьте текст о матче или фото. ⚽🏀🎾🥊",
        "already_reg":    "✅ Вы уже зарегистрированы, {name}!",
        "need_reg":       "⚠️ Сначала пройдите регистрацию. Напишите /start.",
        "blocked":        "🚫 Вы временно заблокированы.\n⏳ Попробуйте через {m} мин {s} сек.",
        "rate_limit":     "⏳ Лимит запросов превышен. Подождите {w} сек.\n⚠️ Предупреждение: {v}/{max}",
        "auto_blocked":   "🚫 Слишком много запросов. Блокировка на {min} минут.",
        "long_text":      "⚠️ Текст слишком длинный. Пожалуйста, сократите.",
        "injection":      "⚠️ Принимаются только спортивные запросы.",
        "no_input":       "Пожалуйста, напишите текст или отправьте фото.",
        "img_prompt":     "Определи спортивное событие на изображении и дай прогноз.",
        "api_overload":   "⚠️ Сервис перегружен. Попробуйте позже.",
        "api_error":      "⚠️ Произошла ошибка. Попробуйте позже.",
        "lang_set":       "✅ Язык установлен: Русский.",
        "ref_link":       "🔗 *Ваша реферальная ссылка:*\n`https://t.me/{bot}?start=ref{uid}`\n\n👥 Приглашено: *{count}* чел.",
        "profile":        "👤 *Ваш профиль*\n\n🏷 Имя: {name}\n📱 Телефон: {phone}\n🌐 Язык: {lang}\n👥 Приглашений: {refs}",
                "system_prompt":  """Ты — элитный спортивный аналитик с 15-летним опытом. Дата: май 2026.

СТИЛЬ: Уверенный, авторитетный, без воды. Никаких "возможно" и "наверное".

ПРАВИЛА:
- Только данные сезона 2025-2026
- Устаревшие составы (Мбаппе в ПСЖ и т.д.) НЕ УПОМИНАЙ
- Если есть реальные данные — используй их
- НЕ ИСПОЛЬЗУЙ emoji — только чистый текст

ФОРМАТ ОТВЕТА:

СОБЫТИЕ: [Команда А] - [Команда Б] | [Турнир] | [Дата]

ФОРМА:
[Команда А]: последние 5 матчей
[Команда Б]: последние 5 матчей

КЛЮЧЕВЫЕ ФАКТОРЫ:
1. [Фактор 1]
2. [Фактор 2]
3. [Фактор 3]

ПРОГНОЗ:
1X2:
- Победа [Команда А]: XX% | Кэф: 1.XX - 1.XX
- Ничья: XX% | Кэф: X.XX - X.XX
- Победа [Команда Б]: XX% | Кэф: X.XX - X.XX

Тотал:
- Больше 2.5: XX% | Кэф: 1.XX - 1.XX
- Меньше 2.5: XX% | Кэф: 1.XX - 1.XX

Обе забьют:
- Да: XX% | Кэф: 1.XX - 1.XX
- Нет: XX% | Кэф: 1.XX - 1.XX

Гандикап:
- [Команда А] (-1): XX% | Кэф: X.XX - X.XX
- [Команда Б] (+1): XX% | Кэф: X.XX - X.XX

ЛУЧШАЯ СТАВКА:
[Тип ставки] | Кэф: X.XX - X.XX | Вероятность: XX%
Обоснование: [1 предложение почему именно эта ставка]

ПРЕДУПРЕЖДЕНИЕ: Аналитический прогноз. Ставки на ваш риск.""",
    },
    "en": {
        "choose_lang":    "🌐 Dil seçin / Выберите язык / Choose language:",
        "ask_name":       "👋 Welcome!\n\nPlease enter your first and last name:",
        "ask_phone":      "📱 Now share your phone number.\nTap the button below:",
        "phone_btn":      "📱 Share my number",
        "reg_done":       "✅ *Registration complete!*\n\nHi, {name}! You can now get sports forecasts.\n\nSend a text about a match or a photo. ⚽🏀🎾🥊",
        "reg_done_ref":   "✅ *Registration complete!*\n\nHi, {name}! The friend who invited you earned a bonus 🎁\n\nSend a text about a match or a photo. ⚽🏀🎾🥊",
        "already_reg":    "✅ You are already registered, {name}!",
        "need_reg":       "⚠️ Please register first. Type /start.",
        "blocked":        "🚫 You are temporarily blocked.\n⏳ Try again in {m}m {s}s.",
        "rate_limit":     "⏳ Request limit exceeded. Wait {w} seconds.\n⚠️ Warning: {v}/{max}",
        "auto_blocked":   "🚫 Too many requests. Blocked for {min} minutes.",
        "long_text":      "⚠️ Message too long. Please shorten it.",
        "injection":      "⚠️ Only sports queries are accepted.",
        "no_input":       "Please send a text or a photo.",
        "img_prompt":     "Identify the sports event in the image and give a forecast.",
        "api_overload":   "⚠️ Service overloaded. Please try again later.",
        "api_error":      "⚠️ An error occurred. Please try again later.",
        "lang_set":       "✅ Language set to English.",
        "ref_link":       "🔗 *Your referral link:*\n`https://t.me/{bot}?start=ref{uid}`\n\n👥 Invited: *{count}* users",
        "profile":        "👤 *Your Profile*\n\n🏷 Name: {name}\n📱 Phone: {phone}\n🌐 Language: {lang}\n👥 Referrals: {refs}",
                "system_prompt":  """You are an elite sports analyst with 15 years of experience. Date: May 2026.

STYLE: Confident, authoritative, zero fluff. No "maybe" or "possibly".

RULES:
- Only 2025-2026 season data
- Never mention outdated squads (Mbappe at PSG, etc.)
- If real data is provided — use it
- DO NOT USE emoji — plain text only

RESPONSE FORMAT:

EVENT: [Team A] - [Team B] | [Tournament] | [Date]

FORM:
[Team A]: last 5 matches
[Team B]: last 5 matches

KEY FACTORS:
1. [Factor 1]
2. [Factor 2]
3. [Factor 3]

FORECAST:
1X2:
- [Team A] Win: XX% | Odds: 1.XX - 1.XX
- Draw: XX% | Odds: X.XX - X.XX
- [Team B] Win: XX% | Odds: X.XX - X.XX

Total Goals:
- Over 2.5: XX% | Odds: 1.XX - 1.XX
- Under 2.5: XX% | Odds: 1.XX - 1.XX

Both Teams Score:
- Yes: XX% | Odds: 1.XX - 1.XX
- No: XX% | Odds: 1.XX - 1.XX

Handicap:
- [Team A] (-1): XX% | Odds: X.XX - X.XX
- [Team B] (+1): XX% | Odds: X.XX - X.XX

BEST BET:
[Bet type] | Odds: X.XX - X.XX | Probability: XX%
Reasoning: [1 sentence why this specific bet]

WARNING: Analytical forecast only. Bet at your own risk.""",
    },
}

LANG_NAMES = {"az": "🇦🇿 Azərbaycan", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}

def t(user_id: int, key: str, **kwargs) -> str:
    lang = db_get_lang(user_id) or "az"
    text = T[lang].get(key, "")
    return text.format(**kwargs) if kwargs else text

def lang_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇦🇿 Azərbaycan", callback_data="lang_az"),
        InlineKeyboardButton("🇷🇺 Русский",    callback_data="lang_ru"),
        InlineKeyboardButton("🇬🇧 English",    callback_data="lang_en"),
    ]])

def phone_keyboard(user_id: int):
    lang = db_get_lang(user_id) or "az"
    btn_text = T[lang]["phone_btn"]
    return ReplyKeyboardMarkup(
        [[KeyboardButton(btn_text, request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

# ─── Security helpers ─────────────────────────────────────────────────────────

def get_user_info(update: Update) -> str:
    u = update.effective_user
    return f"id={u.id} username=@{u.username or '-'} name={u.full_name}"

def is_blocked(user_id: int) -> tuple[bool, int]:
    until = user_blocked_until.get(user_id, 0)
    if time.time() < until:
        return True, int(until - time.time())
    return False, 0

def check_rate_limit(user_id: int) -> tuple[bool, int]:
    now = time.time()
    times = user_message_times[user_id]
    while times and now - times[0] > RATE_LIMIT_WINDOW:
        times.popleft()
    if len(times) >= RATE_LIMIT_MAX:
        return True, int(RATE_LIMIT_WINDOW - (now - times[0])) + 1
    times.append(now)
    return False, 0

def record_violation(user_id: int, info: str) -> bool:
    user_violations[user_id] += 1
    count = user_violations[user_id]
    suspicious_logger.warning(f"VIOLATION #{count} | {info}")
    if count >= SPAM_BLOCK_AFTER:
        user_blocked_until[user_id] = time.time() + SPAM_BLOCK_DURATION
        user_violations[user_id] = 0
        suspicious_logger.warning(f"BLOCKED {SPAM_BLOCK_DURATION}s | {info}")
        return True
    return False

def reset_violations(user_id: int):
    user_violations[user_id] = 0

# ─── Registration flow ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id
    args    = context.args or []
    logger.info(f"START args={args} | {get_user_info(update)}")

    # Сохраняем реферера в context для использования после регистрации
    referred_by = None
    if args and args[0].startswith("ref"):
        try:
            ref_id = int(args[0][3:])
            if ref_id != user_id:
                referred_by = ref_id
        except ValueError:
            pass
    context.user_data["referred_by"] = referred_by

    db_ensure_user(user_id, user.username or "")

    if db_is_registered(user_id):
        u = db_get_user(user_id)
        await update.message.reply_text(
            t(user_id, "already_reg", name=u["display_name"] or user.first_name),
            parse_mode="Markdown"
        )
        return

    # Шаг 1: выбор языка
    user_reg_step[user_id] = "awaiting_lang"
    await update.message.reply_text(t(user_id, "choose_lang"), reply_markup=lang_keyboard())


async def lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user    = query.from_user
    user_id = user.id
    lang    = query.data.split("_")[1]

    db_ensure_user(user_id, user.username or "")
    db_set_field(user_id, "lang", lang)
    logger.info(f"LANG_SET lang={lang} | id={user_id}")

    # Если пользователь уже зарегистрирован — просто меняем язык
    if db_is_registered(user_id):
        await query.edit_message_text(T[lang]["lang_set"])
        return

    # Иначе — продолжаем регистрацию: шаг 2 — имя
    user_reg_step[user_id] = "awaiting_name"
    await query.edit_message_text(T[lang]["ask_name"])


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t(update.effective_user.id, "choose_lang"),
        reply_markup=lang_keyboard()
    )


async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обрабатывает ввод имени. Возвращает True если сообщение было обработано."""
    user_id = update.effective_user.id
    if user_reg_step.get(user_id) != "awaiting_name":
        return False

    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 64:
        await update.message.reply_text("⚠️ " + ("Ad 2-64 simvol olmalıdır." if db_get_lang(user_id) == "az"
                                                  else "Имя должно быть 2-64 символа." if db_get_lang(user_id) == "ru"
                                                  else "Name must be 2-64 characters."))
        return True

    db_set_field(user_id, "display_name", name)
    user_reg_step[user_id] = "awaiting_phone"

    # Шаг 3: запрос телефона
    await update.message.reply_text(
        t(user_id, "ask_phone"),
        reply_markup=phone_keyboard(user_id)
    )
    return True


async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обрабатывает отправку контакта. Возвращает True если обработано."""
    user_id = update.effective_user.id
    if user_reg_step.get(user_id) != "awaiting_phone":
        return False
    if not update.message.contact:
        return False

    contact = update.message.contact
    # Проверяем что пользователь поделился своим номером, а не чужим
    if contact.user_id and contact.user_id != user_id:
        await update.message.reply_text(
            "⚠️ Zəhmət olmasa öz nömrənizi paylaşın." if db_get_lang(user_id) == "az"
            else "⚠️ Пожалуйста, поделитесь своим номером." if db_get_lang(user_id) == "ru"
            else "⚠️ Please share your own number.",
            reply_markup=phone_keyboard(user_id)
        )
        return True

    phone = contact.phone_number
    db_set_field(user_id, "phone", phone)

    referred_by = context.user_data.get("referred_by")
    db_complete_registration(user_id, referred_by)
    user_reg_step[user_id] = "done"

    u = db_get_user(user_id)
    welcome_key = "reg_done_ref" if referred_by else "reg_done"
    logger.info(f"REG_COMPLETE | id={user_id} name={u['display_name']} phone={phone}")

    await update.message.reply_text(
        t(user_id, welcome_key, name=u["display_name"]),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return True


# ─── Profile ──────────────────────────────────────────────────────────────────

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db_is_registered(user_id):
        await update.message.reply_text(t(user_id, "need_reg"))
        return
    u    = db_get_user(user_id)
    lang = u["lang"] or "az"
    await update.message.reply_text(
        t(user_id, "profile",
          name=u["display_name"] or "-",
          phone=u["phone"] or "-",
          lang=LANG_NAMES.get(lang, lang),
          refs=u["ref_count"]),
        parse_mode="Markdown"
    )


async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db_is_registered(user_id):
        await update.message.reply_text(t(user_id, "need_reg"))
        return
    bot_info = await context.bot.get_me()
    await update.message.reply_text(
        t(user_id, "ref_link", bot=bot_info.username, uid=user_id, count=db_get_ref_count(user_id)),
        parse_mode="Markdown"
    )


# ─── Main message handler ─────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id
    info    = get_user_info(update)

    db_ensure_user(user_id, user.username or "")

    # Обработка шагов регистрации
    if update.message.contact:
        if await handle_phone_input(update, context):
            return

    step = user_reg_step.get(user_id)
    if step == "awaiting_name":
        if update.message.text:
            await handle_name_input(update, context)
        return
    if step == "awaiting_phone":
        await update.message.reply_text(
            t(user_id, "ask_phone"), reply_markup=phone_keyboard(user_id)
        )
        return

    # Если не зарегистрирован — направляем на /start
    if not db_is_registered(user_id):
        await update.message.reply_text(t(user_id, "need_reg"))
        return

    # ── Security ──────────────────────────────────────────────────────────────
    blocked, secs = is_blocked(user_id)
    if blocked:
        suspicious_logger.warning(f"BLOCKED_REQUEST | {info}")
        await update.message.reply_text(t(user_id, "blocked", m=secs // 60, s=secs % 60))
        return

    exceeded, wait = check_rate_limit(user_id)
    if exceeded:
        if record_violation(user_id, info):
            await update.message.reply_text(t(user_id, "auto_blocked", min=SPAM_BLOCK_DURATION // 60))
        else:
            await update.message.reply_text(
                t(user_id, "rate_limit", w=wait, v=user_violations[user_id], max=SPAM_BLOCK_AFTER)
            )
        return
    reset_violations(user_id)

    msg_type = "PHOTO" if update.message.photo else "TEXT"
    logger.info(f"MSG [{msg_type}] | {info}")
    db_log_request(user_id, msg_type)
    await update.message.chat.send_action("typing")

    text  = update.message.text or update.message.caption or ""
    photo = update.message.photo

    if len(text) > 1000:
        suspicious_logger.warning(f"LONG_TEXT | {info}")
        await update.message.reply_text(t(user_id, "long_text"))
        return

    injection_keywords = ["ignore previous", "system prompt", "forget instructions",
                          "act as", "jailbreak", "###", "<<<"]
    if any(kw.lower() in text.lower() for kw in injection_keywords):
        suspicious_logger.warning(f"INJECTION | {info}")
        await update.message.reply_text(t(user_id, "injection"))
        return

    content = []
    if photo:
        file       = await context.bot.get_file(photo[-1].file_id)
        file_bytes = await file.download_as_bytearray()
        b64        = base64.standard_b64encode(file_bytes).decode("utf-8")
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    if text:
        content.append({"type": "text", "text": text})
    elif not photo:
        await update.message.reply_text(t(user_id, "no_input"))
        return
    else:
        content.append({"type": "text", "text": t(user_id, "img_prompt")})

    # ── Fetch real football data ──────────────────────────────────────────────
    football_context = ""
    if text and FOOTBALL_API_KEY:
        # Extract potential team names (words with capital letters)
        words = [w.strip(".,!?") for w in text.split() if len(w) > 3 and w[0].isupper()]
        fetched = []
        for word in words[:2]:
            data = await fetch_team_data(word)
            if data:
                fetched.append(data)
        if fetched:
            football_context = "\n\n📡 РЕАЛЬНЫЕ ДАННЫЕ (football-data.org):\n" + "\n\n".join(fetched)
            content.append({"type": "text", "text": football_context})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=t(user_id, "system_prompt"),
            messages=[{"role": "user", "content": content}]
        )
        reply = response.content[0].text
        logger.info(f"REPLY_OK | {info}")
    except anthropic.RateLimitError:
        reply = t(user_id, "api_overload")
    except anthropic.APIError as e:
        logger.error(f"ANTHROPIC_ERROR {e} | {info}")
        reply = t(user_id, "api_error")

    await update.message.reply_text(reply)


# ─── Admin panel ──────────────────────────────────────────────────────────────

def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика",   callback_data="adm_stats")],
        [InlineKeyboardButton("📢 Рассылка",      callback_data="adm_broadcast_menu")],
        [InlineKeyboardButton("🚫 Список блоков", callback_data="adm_blocked")],
        [InlineKeyboardButton("👥 Топ рефералов", callback_data="adm_toprefs")],
    ])
    await update.message.reply_text("🔧 *Админ-панель*", parse_mode="Markdown", reply_markup=kb)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    data = query.data
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]])

    if data == "adm_stats":
        s = db_stats()
        blocked_now = sum(1 for v in user_blocked_until.values() if time.time() < v)
        await query.edit_message_text(
            f"📊 *Статистика*\n\n"
            f"👤 Зарегистрировано: `{s['total']}`\n"
            f"🆕 Новых сегодня: `{s['today']}`\n\n"
            f"📩 Запросов всего: `{s['req_total']}`\n"
            f"📩 Запросов сегодня: `{s['req_today']}`\n\n"
            f"🚫 Заблокировано: `{blocked_now}`",
            parse_mode="Markdown", reply_markup=back_btn
        )
    elif data == "adm_blocked":
        blocked = [(uid, int(v - time.time())) for uid, v in user_blocked_until.items() if time.time() < v]
        text = ("🚫 *Заблокированные:*\n\n" + "\n".join(f"• `{uid}` — {s//60}м {s%60}с" for uid, s in blocked)
                ) if blocked else "✅ Нет заблокированных."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_btn)
    elif data == "adm_toprefs":
        rows = db_stats()["top_refs"]
        text = ("👥 *Топ рефералов:*\n\n" + "\n".join(
            f"{i+1}. @{r[1] or r[0]} — `{r[2]}` чел." for i, r in enumerate(rows))
        ) if rows else "Рефералов пока нет."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_btn)
    elif data == "adm_broadcast_menu":
        context.user_data["awaiting_broadcast"] = True
        await query.edit_message_text(
            "📢 *Рассылка*\n\nОтправьте текст для рассылки всем зарегистрированным пользователям.\n"
            "Поддерживается Markdown. Для отмены — /cancel",
            parse_mode="Markdown"
        )
    elif data == "adm_back":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика",   callback_data="adm_stats")],
            [InlineKeyboardButton("📢 Рассылка",      callback_data="adm_broadcast_menu")],
            [InlineKeyboardButton("🚫 Список блоков", callback_data="adm_blocked")],
            [InlineKeyboardButton("👥 Топ рефералов", callback_data="adm_toprefs")],
        ])
        await query.edit_message_text("🔧 *Админ-панель*", parse_mode="Markdown", reply_markup=kb)

async def handle_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not context.user_data.get("awaiting_broadcast"): return
    context.user_data["awaiting_broadcast"] = False
    text     = update.message.text or ""
    all_uids = db_all_users()
    status   = await update.message.reply_text(f"📢 Рассылка для {len(all_uids)} пользователей...")
    ok = fail = 0
    for uid in all_uids:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(
        f"✅ Рассылка завершена!\n\n📨 Доставлено: `{ok}`\n❌ Не доставлено: `{fail}`",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_broadcast", None)
    await update.message.reply_text("❌ Отменено.")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("lang",    lang_command))
    app.add_handler(CommandHandler("ref",     referral))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("admin",   admin))
    app.add_handler(CommandHandler("cancel",  cancel))

    app.add_handler(CallbackQueryHandler(lang_callback,  pattern=r"^lang_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm_"))

    # Рассылка от админа — приоритет выше
    app.add_handler(MessageHandler(
        filters.TEXT & filters.User(ADMIN_ID), handle_admin_broadcast
    ), group=0)

    # Все остальные сообщения (текст + фото + контакт)
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.CONTACT, handle_message
    ), group=1)

    logger.info("Bot started / Bot işə düşdü / Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
