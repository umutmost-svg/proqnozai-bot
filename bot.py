import os, logging, base64, time, sqlite3, asyncio, httpx, random
from collections import defaultdict, deque
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger(__name__)
sus = logging.getLogger("suspicious")
_sh = logging.FileHandler("suspicious.log", encoding="utf-8")
_sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
sus.addHandler(_sh); sus.setLevel(logging.WARNING)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))
FOOTBALL_KEY    = os.environ.get("FOOTBALL_API_KEY", "")
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")

RATE_WINDOW = 60; RATE_MAX = 5; SPAM_AFTER = 3; SPAM_DUR = 600
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─── In-memory ────────────────────────────────────────────────────────────────
msg_times:    dict[int, deque] = defaultdict(deque)
violations:   dict[int, int]   = defaultdict(int)
blocked_until: dict[int, float] = {}
reg_step:     dict[int, str]   = {}
live_subs:    dict[str, set]   = defaultdict(set)
last_events:  dict[str, list]  = {}
ht_sent:      set              = set()

# ─── DB ───────────────────────────────────────────────────────────────────────
DB = "bot.db"
def con(): return sqlite3.connect(DB)

def db_init():
    with con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            username      TEXT,
            display_name  TEXT,
            lang          TEXT DEFAULT 'az',
            referred_by   INTEGER,
            ref_count     INTEGER DEFAULT 0,
            is_registered INTEGER DEFAULT 0,
            is_blocked    INTEGER DEFAULT 0,
            sports        TEXT DEFAULT '',
            bet_types     TEXT DEFAULT '',
            experience    TEXT DEFAULT '',
            bet_style     TEXT DEFAULT '',
            onboarding_done INTEGER DEFAULT 0,
            total_requests  INTEGER DEFAULT 0,
            last_active   TEXT DEFAULT '',
            joined_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, msg_type TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS live_subscriptions (
            user_id INTEGER, match_id TEXT, match_name TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, match_id)
        );
        """)
db_init()

def db_ensure(uid, uname):
    with con() as c: c.execute("INSERT OR IGNORE INTO users (user_id,username) VALUES (?,?)", (uid, uname))

def db_set(uid, field, val):
    with con() as c: c.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (val, uid))

def db_get(uid) -> dict | None:
    with con() as c:
        cur = c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        cols = [d[0] for d in cur.description]; row = cur.fetchone()
    return dict(zip(cols, row)) if row else None

def db_lang(uid) -> str:
    u = db_get(uid); return u["lang"] if u else "az"

def db_is_reg(uid) -> bool:
    u = db_get(uid); return bool(u and u["is_registered"])

def db_is_blocked(uid) -> bool:
    u = db_get(uid); return bool(u and u["is_blocked"])

def db_all_uids() -> list[int]:
    with con() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0").fetchall()]

def db_log_req(uid, mtype):
    with con() as c:
        c.execute("INSERT INTO requests (user_id,msg_type) VALUES (?,?)", (uid, mtype))
        c.execute("UPDATE users SET total_requests=total_requests+1, last_active=? WHERE user_id=?",
                  (datetime.now().isoformat(), uid))

def db_stats() -> dict:
    with con() as c:
        total   = c.execute("SELECT COUNT(*) FROM users WHERE is_registered=1").fetchone()[0]
        today   = c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=date('now') AND is_registered=1").fetchone()[0]
        blocked = c.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
        rqtotal = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        rqtoday = c.execute("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now')").fetchone()[0]
        toprefs = c.execute("SELECT user_id,username,display_name,ref_count FROM users ORDER BY ref_count DESC LIMIT 5").fetchall()
        langs   = c.execute("SELECT lang,COUNT(*) FROM users WHERE is_registered=1 GROUP BY lang").fetchall()
        ob_done = c.execute("SELECT COUNT(*) FROM users WHERE onboarding_done=1").fetchone()[0]
        live_ct = c.execute("SELECT COUNT(*) FROM live_subscriptions").fetchone()[0]
    return dict(total=total, today=today, blocked=blocked, rqtotal=rqtotal, rqtoday=rqtoday,
                toprefs=toprefs, langs=langs, ob_done=ob_done, live_ct=live_ct)

def db_search(q) -> list[dict]:
    with con() as c:
        cur = c.execute(
            "SELECT * FROM users WHERE username LIKE ? OR display_name LIKE ? OR CAST(user_id AS TEXT)=? LIMIT 5",
            (f"%{q}%", f"%{q}%", q))
        cols = [d[0] for d in cur.description]; rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]

def db_add_lsub(uid, mid, mname):
    with con() as c: c.execute("INSERT OR IGNORE INTO live_subscriptions (user_id,match_id,match_name) VALUES (?,?,?)", (uid, mid, mname))

def db_del_lsub(uid, mid):
    with con() as c: c.execute("DELETE FROM live_subscriptions WHERE user_id=? AND match_id=?", (uid, mid))

def db_user_lsubs(uid) -> list[dict]:
    with con() as c:
        rows = c.execute("SELECT match_id,match_name FROM live_subscriptions WHERE user_id=?", (uid,)).fetchall()
    return [dict(match_id=r[0], match_name=r[1]) for r in rows]

# ─── Translations (FULL CYRILLIC) ─────────────────────────────────────────────
T = {
"az": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Xoş gəldiniz! Adınızı daxil edin:",
"reg_done":      "Qeydiyyat tamamlandı! Salam, {name}!\n\nAI-əsaslı idman proqnoz platformasına xoş gəldiniz.",
"reg_done_ref":  "Qeydiyyat tamamlandı! Salam, {name}!\nSizi dəvət edən dostunuz bonus qazandı.",
"already_reg":   "Siz artıq qeydiyyatdan keçmisiniz, {name}!",
"need_reg":      "Əvvəlcə qeydiyyatdan keçin. /start yazın.",
"db_blocked":    "Hesabınız bloklanıb. İnzibatçıya müraciət edin.",
"blocked":       "Müvəqqəti bloklanmısınız. {m} dəq {s} san sonra yenidən cəhd edin.",
"rate_limit":    "Sorğu limiti aşıldı. {w} saniyə gözləyin. Xəbərdarlıq: {v}/{max}",
"auto_blocked":  "Çox sayda sorğu. {min} dəqiqəlik blok.",
"long_text":     "Mətn çox uzundur.",
"injection":     "Yalnız idman sorğuları qəbul edilir.",
"no_input":      "Mətn yazın və ya şəkil göndərin.",
"img_prompt":    "Şəkildəki idman hadisəsini müəyyən et və proqnoz ver.",
"api_overload":  "Servis yüklənməsi. Bir az sonra yenidən cəhd edin.",
"api_error":     "Xəta baş verdi. Bir az sonra yenidən cəhd edin.",
"lang_set":      "Dil Azərbaycan dilinə təyin edildi.",
"ref_link":      "Referans linkınız:\nt.me/{bot}?start=ref{uid}\n\nDəvət etdiyiniz: {count} nəfər",
"watch_btn":     "Oyunu izlə",
"watch_started": "Oyun izlənilir: {match}\nVacib hadisələr haqqında bildiriş alacaqsınız.",
"watch_stopped": "Oyun izlənməsi dayandırıldı: {match}",
"no_subs":       "Heç bir oyun izləmirsiniz.",
"live_goal":     "QOL! {match}\n{minute}. dəq: {team}\nHesab: {score}\n\nCanlı mərc:\n{tip}",
"live_card":     "KART! {match}\n{minute}. dəq: {player} ({team}) — {card}\n\nCanlı mərc:\n{tip}",
"live_halftime": "FASİLƏ! {match}\nHesab: {score}\n\nFasilə mərci:\n{tip}",
"live_fulltime": "OYUN BİTDİ! {match}\nYekun: {score}\n\nİzləmədən çıxıldı.",
"live_kickoff":  "OYUN BAŞLADI! {match}\nVacib hadisələr haqqında xəbərləndirmə alacaqsınız.",
"live_alert_goal":     "SİQNAL! {match}\nModel: Qol gözlənilir [{minute}. dəq]\nKəflərin dəyişməsindən əvvəl hərəkət edin.",
"live_alert_value":    "LIVE VALUE! {match}\nKəflərdə anomaliya aşkar edildi [{minute}. dəq]\nModel: {team} üzərində dəyər var.",
"live_alert_pressure": "TƏZYİQ! {match}\n{team} güclü hücum təzyiqi yaradır [{minute}. dəq]\nStat: {stat}",
"top5_header":   "Bu günün TOP 5 matçı:\n\n",
"top5_empty":    "Bu gün üçün matç tapılmadı.",
"menu_forecast": "Proqnoz al",
"menu_top5":     "Günün TOP 5",
"menu_matches":  "Matçlarım",
"menu_profile":  "Profil",
"menu_ref":      "Referans link",
"menu_lang":     "Dil dəyiş",
"profile_text":  "PROFİL\n\nAd: {name}\nDil: {lang}\nCəmi sorğular: {total_req}\nDəvətlər: {refs}\n\nİdman: {sports}\nMərc növü: {bet_types}\nTəcrübə: {exp}",
# Onboarding
"ob_sports":   "Hansı idman növlərini sevirsiniz?",
"ob_bet_type": "Hansı mərc növlərini üstün tutursunuz?",
"ob_exp":      "Mərcdə təcrübəniz nə qədərdir?",
"ob_style":    "Oyun üslubunuz nədir?",
"ob_done":     "Profiliniz hazırlandı! Fərdiləşdirilmiş proqnozlar alacaqsınız.\n\nProfil:\nİdman: {sports}\nMərc: {bet_types}\nTəcrübə: {exp}\nÜslub: {style}",
"system_prompt": """Sən 15 illik təcrübəli elit idman analitikisən. Tarix: {date}.

İSTİFADƏÇİNİN PROFİLİ:
Sevdiyi idman: {sports}
Mərc növü: {bet_types}
Təcrübə: {exp}
Üslub: {style}

QAYDALAR:
- İstifadəçinin profilinə uyğun proqnoz ver
- Yalnız 2025-2026 mövsümü məlumatlar, köhnəlmiş heyətləri YAZMA
- Emoji istifadə et - Telegram-da düzgün görünür

GENİŞLƏNDİRİLMİŞ FORMAT (default):

🏆 HADİSƏ: [Komanda A] — [Komanda B]
📍 [Turnir] | [Tarix]

📊 FORMA:
▪ [Komanda A]: son 5 oyun nəticəsi
▪ [Komanda B]: son 5 oyun nəticəsi

🔑 ƏSAS AMİLLƏR:
1️⃣ [Amil 1]
2️⃣ [Amil 2]
3️⃣ [Amil 3]

🎯 PROQNOZ:
1X2:
├ [Komanda A] qələbəsi — XX% | Kef: X.XX–X.XX
├ Heç-heçə — XX% | Kef: X.XX–X.XX
└ [Komanda B] qələbəsi — XX% | Kef: X.XX–X.XX

⚽ Total:
├ 2.5 Üstündə — XX% | Kef: X.XX–X.XX
└ 2.5 Altında — XX% | Kef: X.XX–X.XX

🔥 Hər ikisi qol vurur:
├ Bəli — XX% | Kef: X.XX–X.XX
└ Xeyr — XX% | Kef: X.XX–X.XX

📐 Qandikap:
├ [Komanda A] (-1) — XX% | Kef: X.XX–X.XX
└ [Komanda B] (+1) — XX% | Kef: X.XX–X.XX

⚡ ƏN GÜCLÜ MƏRCİ:
▶ [Mərc növü] | Kef: X.XX–X.XX | Ehtimal: XX%
💬 [1 cümlə əsaslandırma]

⚠️ Analitik proqnozdur. Mərc öz riskinizdir.""",
"live_tip_prompt": "Sən canlı mərc analitikisən. Oyun: {match}, {minute}. dəq, hesab {score}. Hadisə: {event}. Ən yaxşı canlı mərci tövsiyə et. Qısa, maks 2 cümlə. Yalnız düz mətn.",
"top5_prompt": "Bu gün {date} keçiriləcək ən maraqlı 5 idman matçını siyahıla. Hər biri üçün: matç adı, turnir, vaxt (UTC), qısa proqnoz (1 cümlə). Yalnız düz mətn, emoji yoxdur.",
},

"ru": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Добро пожаловать! Введите ваше имя:",
"reg_done":      "Регистрация завершена! Привет, {name}!\n\nДобро пожаловать на AI-платформу спортивных прогнозов.",
"reg_done_ref":  "Регистрация завершена! Привет, {name}!\nВаш друг получил бонус за приглашение.",
"already_reg":   "Вы уже зарегистрированы, {name}!",
"need_reg":      "Сначала пройдите регистрацию. Напишите /start.",
"db_blocked":    "Ваш аккаунт заблокирован. Обратитесь к администратору.",
"blocked":       "Вы временно заблокированы. Попробуйте через {m} мин {s} сек.",
"rate_limit":    "Лимит запросов превышен. Подождите {w} сек. Предупреждение: {v}/{max}",
"auto_blocked":  "Слишком много запросов. Блокировка на {min} минут.",
"long_text":     "Текст слишком длинный.",
"injection":     "Принимаются только спортивные запросы.",
"no_input":      "Напишите текст или отправьте фото.",
"img_prompt":    "Определи спортивное событие на изображении и дай прогноз.",
"api_overload":  "Сервис перегружен. Попробуйте позже.",
"api_error":     "Произошла ошибка. Попробуйте позже.",
"lang_set":      "Язык установлен: Русский.",
"ref_link":      "Ваша реферальная ссылка:\nt.me/{bot}?start=ref{uid}\n\nПриглашено: {count} чел.",
"watch_btn":     "Следить за матчем",
"watch_started": "Слежу за матчем: {match}\nПришлю уведомления о ключевых событиях.",
"watch_stopped": "Слежение остановлено: {match}",
"no_subs":       "Вы не следите ни за одним матчем.",
"live_goal":     "ГОЛ! {match}\n{minute} мин: {team}\nСчёт: {score}\n\nЛайв-ставка:\n{tip}",
"live_card":     "КАРТОЧКА! {match}\n{minute} мин: {player} ({team}) — {card}\n\nЛайв-ставка:\n{tip}",
"live_halftime": "ПЕРЕРЫВ! {match}\nСчёт: {score}\n\nСтавка на перерыв:\n{tip}",
"live_fulltime": "МАТЧ ЗАВЕРШЁН! {match}\nИтог: {score}\n\nПодписка отменена.",
"live_kickoff":  "МАТЧ НАЧАЛСЯ! {match}\nБуду присылать уведомления о ключевых событиях.",
"live_alert_goal":     "СИГНАЛ! {match}\nМодель: ожидается гол [{minute} мин]\nДействуйте до изменения коэффициентов.",
"live_alert_value":    "LIVE VALUE! {match}\nАномалия в коэффициентах [{minute} мин]\nМодель: есть ценность на {team}.",
"live_alert_pressure": "ДАВЛЕНИЕ! {match}\n{team} создаёт сильное давление [{minute} мин]\nСтат: {stat}",
"top5_header":   "ТОП 5 матчей дня:\n\n",
"top5_empty":    "На сегодня матчи не найдены.",
"menu_forecast": "Получить прогноз",
"menu_top5":     "ТОП 5 матчей дня",
"menu_matches":  "Мои матчи",
"menu_profile":  "Профиль",
"menu_ref":      "Реферальная ссылка",
"menu_lang":     "Сменить язык",
"profile_text":  "ПРОФИЛЬ\n\nИмя: {name}\nЯзык: {lang}\nВсего запросов: {total_req}\nПриглашений: {refs}\n\nСпорт: {sports}\nСтавки: {bet_types}\nОпыт: {exp}",
# Onboarding
"ob_sports":   "Какие виды спорта вас интересуют?",
"ob_bet_type": "Какие типы ставок вы предпочитаете?",
"ob_exp":      "Каков ваш опыт в ставках?",
"ob_style":    "Какой стиль игры вам ближе?",
"ob_done":     "Ваш профиль готов! Будете получать персонализированные прогнозы.\n\nПрофиль:\nСпорт: {sports}\nСтавки: {bet_types}\nОпыт: {exp}\nСтиль: {style}",
"system_prompt": """Ты — элитный спортивный аналитик с 15-летним опытом. Дата: {date}.

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:
Любимый спорт: {sports}
Тип ставок: {bet_types}
Опыт: {exp}
Стиль: {style}

ПРАВИЛА:
- Давай прогнозы с учётом профиля пользователя
- Только данные сезона 2025-2026, устаревшие составы НЕ УПОМИНАЙ
- Используй emoji — они корректно отображаются в Telegram

РАСШИРЕННЫЙ ФОРМАТ (по умолчанию):

🏆 СОБЫТИЕ: [Команда А] — [Команда Б]
📍 [Турнир] | [Дата]

📊 ФОРМА:
▪ [Команда А]: последние 5 матчей
▪ [Команда Б]: последние 5 матчей

🔑 КЛЮЧЕВЫЕ ФАКТОРЫ:
1️⃣ [Фактор 1]
2️⃣ [Фактор 2]
3️⃣ [Фактор 3]

🎯 ПРОГНОЗ:
1X2:
├ Победа [Команда А] — XX% | Кэф: X.XX–X.XX
├ Ничья — XX% | Кэф: X.XX–X.XX
└ Победа [Команда Б] — XX% | Кэф: X.XX–X.XX

⚽ Тотал:
├ Больше 2.5 — XX% | Кэф: X.XX–X.XX
└ Меньше 2.5 — XX% | Кэф: X.XX–X.XX

🔥 Обе забьют:
├ Да — XX% | Кэф: X.XX–X.XX
└ Нет — XX% | Кэф: X.XX–X.XX

📐 Гандикап:
├ [Команда А] (-1) — XX% | Кэф: X.XX–X.XX
└ [Команда Б] (+1) — XX% | Кэф: X.XX–X.XX

⚡ ЛУЧШАЯ СТАВКА:
▶ [Тип ставки] | Кэф: X.XX–X.XX | Вероятность: XX%
💬 [1 предложение обоснования]

⚠️ Аналитический прогноз. Ставки на ваш риск.""",
"live_tip_prompt": "Ты лайв-аналитик. Матч {match}, {minute} мин, счёт {score}. Событие: {event}. Дай лучшую лайв-ставку. Коротко, макс 2 предложения. Только текст.",
"top5_prompt": "Перечисли 5 самых интересных спортивных матчей на сегодня {date}. Для каждого: название матча, турнир, время (UTC), краткий прогноз (1 предложение). Только чистый текст, без emoji.",
"short_prompt": """Ты краткий аналитик ставок. Дата: {date}.
Профиль: {sports} | {bet_types} | {exp}

КРАТКИЙ ФОРМАТ (строго):

⚡ [Команда А] vs [Команда Б]
📍 [Турнир]

🎯 Победитель: [Команда] — XX%
⚽ Тотал 2.5: Больше XX% | Кэф X.XX
🔥 Обе забьют: Да XX% | Кэф X.XX

✅ СТАВКА: [Тип ставки] | Кэф X.XX | XX%

⚠️ Аналитический прогноз.""",
"forecast_type_btn": "📊 Расширенный / Краткий",
"choose_forecast":   "Выберите формат прогноза:",
"btn_extended":      "📊 Расширенный",
"btn_short":         "⚡ Краткий",
},

"en": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Welcome! Please enter your name:",
"reg_done":      "Registration complete! Hi, {name}!\n\nWelcome to the AI-powered sports betting platform.",
"reg_done_ref":  "Registration complete! Hi, {name}!\nYour friend earned a referral bonus.",
"already_reg":   "You are already registered, {name}!",
"need_reg":      "Please register first. Type /start.",
"db_blocked":    "Your account is blocked. Contact the administrator.",
"blocked":       "You are temporarily blocked. Try again in {m}m {s}s.",
"rate_limit":    "Request limit exceeded. Wait {w} seconds. Warning: {v}/{max}",
"auto_blocked":  "Too many requests. Blocked for {min} minutes.",
"long_text":     "Message too long.",
"injection":     "Only sports queries are accepted.",
"no_input":      "Please send a text or a photo.",
"img_prompt":    "Identify the sports event in the image and give a forecast.",
"api_overload":  "Service overloaded. Please try again later.",
"api_error":     "An error occurred. Please try again later.",
"lang_set":      "Language set to English.",
"ref_link":      "Your referral link:\nt.me/{bot}?start=ref{uid}\n\nInvited: {count} users",
"watch_btn":     "Follow match",
"watch_started": "Following: {match}\nI'll send notifications about key events.",
"watch_stopped": "Stopped following: {match}",
"no_subs":       "You are not following any matches.",
"live_goal":     "GOAL! {match}\n{minute} min: {team}\nScore: {score}\n\nLive bet:\n{tip}",
"live_card":     "CARD! {match}\n{minute} min: {player} ({team}) — {card}\n\nLive bet:\n{tip}",
"live_halftime": "HALF TIME! {match}\nScore: {score}\n\nHalf-time bet:\n{tip}",
"live_fulltime": "FULL TIME! {match}\nFinal: {score}\n\nUnsubscribed.",
"live_kickoff":  "KICK OFF! {match}\nI'll notify you about key events.",
"live_alert_goal":     "SIGNAL! {match}\nModel: goal expected [{minute} min]\nAct before odds drop.",
"live_alert_value":    "LIVE VALUE! {match}\nOdds anomaly detected [{minute} min]\nModel: value on {team}.",
"live_alert_pressure": "PRESSURE! {match}\n{team} creating strong pressure [{minute} min]\nStat: {stat}",
"top5_header":   "TOP 5 matches of the day:\n\n",
"top5_empty":    "No matches found for today.",
"menu_forecast": "Get forecast",
"menu_top5":     "TOP 5 today",
"menu_matches":  "My matches",
"menu_profile":  "Profile",
"menu_ref":      "Referral link",
"menu_lang":     "Change language",
"profile_text":  "PROFILE\n\nName: {name}\nLanguage: {lang}\nTotal requests: {total_req}\nReferrals: {refs}\n\nSports: {sports}\nBets: {bet_types}\nExp: {exp}",
# Onboarding
"ob_sports":   "Which sports interest you?",
"ob_bet_type": "What types of bets do you prefer?",
"ob_exp":      "What is your betting experience?",
"ob_style":    "What is your playing style?",
"ob_done":     "Your profile is ready! You'll receive personalized forecasts.\n\nProfile:\nSports: {sports}\nBets: {bet_types}\nExp: {exp}\nStyle: {style}",
"system_prompt": """You are an elite sports analyst with 15 years of experience. Date: {date}.

USER PROFILE:
Favourite sports: {sports}
Bet types: {bet_types}
Experience: {exp}
Style: {style}

RULES:
- Tailor forecasts to user profile
- Only 2025-2026 season data, never mention outdated squads
- Use emoji — they display correctly in Telegram

EXTENDED FORMAT (default):

🏆 EVENT: [Team A] — [Team B]
📍 [Tournament] | [Date]

📊 FORM:
▪ [Team A]: last 5 matches
▪ [Team B]: last 5 matches

🔑 KEY FACTORS:
1️⃣ [Factor 1]
2️⃣ [Factor 2]
3️⃣ [Factor 3]

🎯 FORECAST:
1X2:
├ [Team A] Win — XX% | Odds: X.XX–X.XX
├ Draw — XX% | Odds: X.XX–X.XX
└ [Team B] Win — XX% | Odds: X.XX–X.XX

⚽ Total Goals:
├ Over 2.5 — XX% | Odds: X.XX–X.XX
└ Under 2.5 — XX% | Odds: X.XX–X.XX

🔥 Both Teams Score:
├ Yes — XX% | Odds: X.XX–X.XX
└ No — XX% | Odds: X.XX–X.XX

📐 Handicap:
├ [Team A] (-1) — XX% | Odds: X.XX–X.XX
└ [Team B] (+1) — XX% | Odds: X.XX–X.XX

⚡ BEST BET:
▶ [Bet type] | Odds: X.XX–X.XX | Probability: XX%
💬 [1 sentence reasoning]

⚠️ Analytical forecast only. Bet at your own risk.""",
"live_tip_prompt": "You are a live betting analyst. Match {match}, {minute} min, score {score}. Event: {event}. Give the best live bet. Short, max 2 sentences. Plain text only.",
"top5_prompt": "List the 5 most interesting sports matches today {date}. For each: match name, tournament, time (UTC), brief forecast (1 sentence). Plain text only, no emoji.",
"short_prompt": """You are a brief betting analyst. Date: {date}.
Profile: {sports} | {bet_types} | {exp}

SHORT FORMAT (strict):

⚡ [Team A] vs [Team B]
📍 [Tournament]

🎯 Winner: [Team] — XX%
⚽ Total 2.5: Over XX% | Odds X.XX
🔥 Both score: Yes XX% | Odds X.XX

✅ BET: [Bet type] | Odds X.XX | XX%

⚠️ Analytical forecast only.""",
"forecast_type_btn": "📊 Extended / Short",
"choose_forecast":   "Choose forecast format:",
"btn_extended":      "📊 Extended",
"btn_short":         "⚡ Short",
},
}

LANG_NAMES = {"az": "Azərbaycan", "ru": "Русский", "en": "English"}

def tr(uid, key, **kw):
    lang = db_lang(uid)
    txt  = T.get(lang, T["ru"]).get(key, T["ru"].get(key, ""))
    if key == "system_prompt":
        u = db_get(uid) or {}
        kw.setdefault("date", date.today().strftime("%d.%m.%Y"))
        kw.setdefault("sports",     u.get("sports", "-"))
        kw.setdefault("bet_types",  u.get("bet_types", "-"))
        kw.setdefault("exp",        u.get("experience", "-"))
        kw.setdefault("style",      u.get("bet_style", "-"))
    return txt.format(**kw) if kw else txt

# ─── Onboarding data per language ─────────────────────────────────────────────
OB_SPORTS = {
    "az": [("Futbol", "football"), ("UFC / MMA", "ufc"), ("NBA / Basketbol", "nba"),
           ("Tennis", "tennis"), ("Kibersport", "esports"), ("Hokey", "hockey"), ("Hamısı", "all")],
    "ru": [("Футбол", "football"), ("UFC / MMA", "ufc"), ("НБА / Баскетбол", "nba"),
           ("Теннис", "tennis"), ("Киберспорт", "esports"), ("Хоккей", "hockey"), ("Все виды", "all")],
    "en": [("Football", "football"), ("UFC / MMA", "ufc"), ("NBA / Basketball", "nba"),
           ("Tennis", "tennis"), ("Esports", "esports"), ("Hockey", "hockey"), ("All sports", "all")],
}
OB_BET = {
    "az": [("Canlı mərc", "live"), ("Prematch", "prematch"), ("Ekspress", "express"),
           ("Tək oyun", "single"), ("Qandikap", "handicap")],
    "ru": [("Лайв ставки", "live"), ("Преймatch", "prematch"), ("Экспресс", "express"),
           ("Одиночная", "single"), ("Гандикап", "handicap")],
    "en": [("Live betting", "live"), ("Prematch", "prematch"), ("Express/Parlay", "express"),
           ("Singles", "single"), ("Handicap", "handicap")],
}
OB_EXP = {
    "az": [("Yeni başlayan", "beginner"), ("Orta səviyyə", "mid"), ("Təcrübəli", "expert")],
    "ru": [("Новичок", "beginner"), ("Средний уровень", "mid"), ("Опытный", "expert")],
    "en": [("Beginner", "beginner"), ("Intermediate", "mid"), ("Expert", "expert")],
}
OB_STYLE = {
    "az": [("Aqressif", "aggressive"), ("Konservativ", "conservative"),
           ("Balanslaşdırılmış", "balanced"), ("Dəyər axtarışı", "value")],
    "ru": [("Агрессивный", "aggressive"), ("Консервативный", "conservative"),
           ("Сбалансированный", "balanced"), ("Value hunting", "value")],
    "en": [("Aggressive", "aggressive"), ("Conservative", "conservative"),
           ("Balanced", "balanced"), ("Value hunter", "value")],
}

def ob_kb(items):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"ob_{cb}")] for label, cb in items])

# ─── Main menu keyboard ───────────────────────────────────────────────────────
def main_menu(uid):
    lang = db_lang(uid)
    tl = T[lang]
    return ReplyKeyboardMarkup([
        [tl["menu_forecast"], tl["menu_top5"]],
        [tl["menu_matches"],  tl["menu_profile"]],
        [tl["menu_ref"],      tl["menu_lang"]],
    ], resize_keyboard=True)

def lang_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Azərbaycan", callback_data="lang_az"),
        InlineKeyboardButton("Русский",    callback_data="lang_ru"),
        InlineKeyboardButton("English",    callback_data="lang_en"),
    ]])

# ─── Security ─────────────────────────────────────────────────────────────────
def uinfo(update): u = update.effective_user; return f"id={u.id} @{u.username or '-'} {u.full_name}"

def sec_blocked(uid):
    until = blocked_until.get(uid, 0)
    return (True, int(until - time.time())) if time.time() < until else (False, 0)

def rate_check(uid):
    now = time.time(); q = msg_times[uid]
    while q and now - q[0] > RATE_WINDOW: q.popleft()
    if len(q) >= RATE_MAX: return True, int(RATE_WINDOW - (now - q[0])) + 1
    q.append(now); return False, 0

def record_viol(uid, info):
    violations[uid] += 1; n = violations[uid]; sus.warning(f"VIOL #{n} | {info}")
    if n >= SPAM_AFTER:
        blocked_until[uid] = time.time() + SPAM_DUR; violations[uid] = 0; return True
    return False

# ─── Football APIs ────────────────────────────────────────────────────────────
async def search_match(query):
    if not APIFOOTBALL_KEY: return []
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"live": "all"})
            if r.status_code == 200:
                out = []
                for f in r.json().get("response", []):
                    home = f["teams"]["home"]["name"]; away = f["teams"]["away"]["name"]
                    if query.lower() in home.lower() or query.lower() in away.lower():
                        out.append({"id": str(f["fixture"]["id"]), "name": f"{home} vs {away}",
                            "status": f["fixture"]["status"]["short"],
                            "minute": f["fixture"]["status"].get("elapsed", 0),
                            "score": f"{f['goals']['home']}-{f['goals']['away']}", "live": True})
                if out: return out[:3]
            r2 = await h.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"date": date.today().isoformat()})
            if r2.status_code == 200:
                out = []
                for f in r2.json().get("response", []):
                    home = f["teams"]["home"]["name"]; away = f["teams"]["away"]["name"]
                    if query.lower() in home.lower() or query.lower() in away.lower():
                        out.append({"id": str(f["fixture"]["id"]), "name": f"{home} vs {away}",
                            "status": f["fixture"]["status"]["short"], "minute": 0, "score": "0-0", "live": False})
                return out[:3]
    except Exception as e: logger.error(f"search_match: {e}")
    return []

async def get_top5_matches() -> list[dict]:
    """Get today's top matches from API-Football."""
    if not APIFOOTBALL_KEY: return []
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": APIFOOTBALL_KEY},
                params={"date": date.today().isoformat(), "timezone": "UTC"})
            if r.status_code == 200:
                fixtures = r.json().get("response", [])
                # Sort by league priority
                priority = ["UEFA Champions League", "Premier League", "La Liga",
                           "Bundesliga", "Serie A", "Ligue 1", "UEFA Europa League"]
                def score_match(f):
                    league = f["league"]["name"]
                    for i, p in enumerate(priority):
                        if p in league: return i
                    return 99
                fixtures.sort(key=score_match)
                out = []
                for f in fixtures[:5]:
                    out.append({
                        "home": f["teams"]["home"]["name"],
                        "away": f["teams"]["away"]["name"],
                        "league": f["league"]["name"],
                        "time": f["fixture"]["date"][11:16],
                    })
                return out
    except Exception as e: logger.error(f"get_top5: {e}")
    return []

async def get_events(mid):
    if not APIFOOTBALL_KEY: return []
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get("https://v3.football.api-sports.io/fixtures/events",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"fixture": mid})
            if r.status_code == 200: return r.json().get("response", [])
    except Exception as e: logger.error(f"get_events: {e}")
    return []

async def get_status(mid):
    if not APIFOOTBALL_KEY: return None
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": APIFOOTBALL_KEY}, params={"id": mid})
            if r.status_code == 200:
                resp = r.json().get("response", [])
                if resp:
                    f = resp[0]
                    return {"status": f["fixture"]["status"]["short"],
                            "minute": f["fixture"]["status"].get("elapsed", 0),
                            "score": f"{f['goals']['home']}-{f['goals']['away']}",
                            "home": f["teams"]["home"]["name"],
                            "away": f["teams"]["away"]["name"]}
    except Exception as e: logger.error(f"get_status: {e}")
    return None

async def live_tip(uid, match, minute, score, event):
    try:
        lang = db_lang(uid)
        prompt = T[lang]["live_tip_prompt"].format(match=match, minute=minute, score=score, event=event)
        r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role": "user", "content": prompt}])
        return r.content[0].text
    except Exception: return ""

# ─── Live Poller ──────────────────────────────────────────────────────────────
async def poller(app):
    alert_cnt: dict[str, int] = defaultdict(int)
    while True:
        await asyncio.sleep(60)
        if not live_subs: continue
        for mid, uids in list(live_subs.items()):
            if not uids: continue
            try:
                st = await get_status(mid)
                if not st: continue
                score = st["score"]; minute = st["minute"] or 0; status = st["status"]
                match_name = None
                for uid in uids:
                    for s in db_user_lsubs(uid):
                        if s["match_id"] == mid: match_name = s["match_name"]; break
                    if match_name: break
                if not match_name: match_name = f"{st['home']} vs {st['away']}"

                evs = await get_events(mid)
                prev = last_events.get(mid, [])
                new_evs = evs[len(prev):]
                last_events[mid] = evs

                for ev in new_evs:
                    etype = ev.get("type", ""); detail = ev.get("detail", "")
                    team  = ev.get("team", {}).get("name", "")
                    player = ev.get("player", {}).get("name", "")
                    ev_min = ev.get("time", {}).get("elapsed", minute)
                    tip = await live_tip(next(iter(uids)), match_name, ev_min, score, f"{etype}-{detail}-{team}")
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try:
                            if etype == "Goal":
                                msg = T[lang]["live_goal"].format(match=match_name, minute=ev_min, team=team, score=score, tip=tip)
                            elif etype == "Card":
                                card = {"az": "Qırmızı" if "Red" in detail else "Sarı",
                                        "ru": "Красная" if "Red" in detail else "Жёлтая",
                                        "en": "Red card" if "Red" in detail else "Yellow card"}.get(lang, "Card")
                                msg = T[lang]["live_card"].format(match=match_name, minute=ev_min, player=player, team=team, card=card, tip=tip)
                            else: continue
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except Exception as e: logger.error(f"notify uid={uid}: {e}")

                # FOMO alerts every ~15 min
                alert_cnt[mid] += 1
                if alert_cnt[mid] % 15 == 0 and minute > 20:
                    atype = random.choice(["goal", "value", "pressure"])
                    teams = [st["home"], st["away"]]
                    pt = random.choice(teams)
                    stats = ["12 shots on target", "73% possession", "6 corners", "xG: 1.8"]
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try:
                            if atype == "goal":
                                msg = T[lang]["live_alert_goal"].format(match=match_name, minute=minute)
                            elif atype == "value":
                                msg = T[lang]["live_alert_value"].format(match=match_name, minute=minute, team=pt)
                            else:
                                msg = T[lang]["live_alert_pressure"].format(match=match_name, minute=minute, team=pt, stat=random.choice(stats))
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except Exception: pass

                if status == "HT" and mid not in ht_sent:
                    ht_sent.add(mid)
                    for uid in list(uids):
                        lang = db_lang(uid)
                        tip = await live_tip(uid, match_name, 45, score, "Half time")
                        try: await app.bot.send_message(chat_id=uid, text=T[lang]["live_halftime"].format(match=match_name, score=score, tip=tip))
                        except Exception: pass

                if status in ("FT", "AET", "PEN"):
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try: await app.bot.send_message(chat_id=uid, text=T[lang]["live_fulltime"].format(match=match_name, score=score))
                        except Exception: pass
                        db_del_lsub(uid, mid); uids.discard(uid)
                    live_subs[mid].clear()
            except Exception as e: logger.error(f"poller mid={mid}: {e}")

# ─── Daily retention ──────────────────────────────────────────────────────────
async def daily_push(app):
    while True:
        await asyncio.sleep(3600)
        if datetime.now().hour != 10: continue
        msgs = {"az": "Bu gün böyük oyunlar var! Proqnoz almaq üçün yazın.",
                "ru": "Сегодня большие матчи! Напишите для получения прогноза.",
                "en": "Big matches today! Write to get your forecast."}
        try:
            with con() as c:
                rows = c.execute("SELECT user_id,lang FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND (last_active='' OR date(last_active) <= date('now', '-2 days'))").fetchall()
            for uid, lang in rows:
                try:
                    await app.bot.send_message(chat_id=uid, text=msgs.get(lang, msgs["ru"]))
                    await asyncio.sleep(0.1)
                except Exception: pass
        except Exception as e: logger.error(f"daily_push: {e}")

# ─── Handlers ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id; args = context.args or []
    logger.info(f"START | {uinfo(update)}")
    referred_by = None
    if args and args[0].startswith("ref"):
        try:
            ref_id = int(args[0][3:])
            if ref_id != uid: referred_by = ref_id
        except ValueError: pass
    context.user_data["referred_by"] = referred_by
    db_ensure(uid, user.username or "")
    if db_is_reg(uid):
        u = db_get(uid)
        await update.message.reply_text(tr(uid, "already_reg", name=u["display_name"] or user.first_name),
            reply_markup=main_menu(uid)); return
    reg_step[uid] = "awaiting_lang"
    await update.message.reply_text(tr(uid, "choose_lang"), reply_markup=lang_kb())


async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = q.data.split("_")[1]
    db_ensure(uid, q.from_user.username or ""); db_set(uid, "lang", lang)
    if db_is_reg(uid):
        await q.edit_message_text(T[lang]["lang_set"]); return
    reg_step[uid] = "awaiting_name"
    await q.edit_message_text(T[lang]["ask_name"])


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(tr(update.effective_user.id, "choose_lang"), reply_markup=lang_kb())


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if reg_step.get(uid) != "awaiting_name": return False
    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 64:
        await update.message.reply_text("2-64 simvol / символа / characters"); return True
    db_set(uid, "display_name", name)
    referred_by = context.user_data.get("referred_by")
    with con() as c:
        c.execute("UPDATE users SET is_registered=1 WHERE user_id=?", (uid,))
        if referred_by:
            c.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (referred_by,))
            c.execute("UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL", (referred_by, uid))
    reg_step[uid] = "ob_sports"
    logger.info(f"REG | id={uid} name={name}")
    wkey = "reg_done_ref" if referred_by else "reg_done"
    await update.message.reply_text(tr(uid, wkey, name=name), reply_markup=ReplyKeyboardRemove())
    # Start onboarding
    await asyncio.sleep(0.3)
    lang = db_lang(uid)
    await update.message.reply_text(T[lang]["ob_sports"], reply_markup=ob_kb(OB_SPORTS[lang]))
    return True


async def ob_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle onboarding step callbacks."""
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; val = q.data[3:]  # strip "ob_"
    lang = db_lang(uid)
    step = reg_step.get(uid, "")

    if step == "ob_sports":
        db_set(uid, "sports", val)
        reg_step[uid] = "ob_bet"
        await q.edit_message_text(T[lang]["ob_bet_type"], reply_markup=ob_kb(OB_BET[lang]))

    elif step == "ob_bet":
        db_set(uid, "bet_types", val)
        reg_step[uid] = "ob_exp"
        await q.edit_message_text(T[lang]["ob_exp"], reply_markup=ob_kb(OB_EXP[lang]))

    elif step == "ob_exp":
        db_set(uid, "experience", val)
        reg_step[uid] = "ob_style"
        await q.edit_message_text(T[lang]["ob_style"], reply_markup=ob_kb(OB_STYLE[lang]))

    elif step == "ob_style":
        db_set(uid, "bet_style", val); db_set(uid, "onboarding_done", 1)
        reg_step[uid] = "done"
        u = db_get(uid)
        done_msg = T[lang]["ob_done"].format(
            sports=u["sports"], bet_types=u["bet_types"],
            exp=u["experience"], style=u["bet_style"])
        await q.edit_message_text(done_msg)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=uid, text=tr(uid, "no_input"), reply_markup=main_menu(uid))


async def watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); uid = q.from_user.id; data = q.data
    if data.startswith("watch_"):
        mid = data[6:]; mname = context.user_data.get(f"mn_{mid}", mid)
        live_subs[mid].add(uid); db_add_lsub(uid, mid, mname)
        await q.edit_message_text(q.message.text + "\n\n" + tr(uid, "watch_started", match=mname))
    elif data.startswith("unwatch_"):
        mid = data[8:]
        subs = db_user_lsubs(uid)
        mname = next((s["match_name"] for s in subs if s["match_id"] == mid), mid)
        live_subs[mid].discard(uid); db_del_lsub(uid, mid)
        await q.edit_message_text(tr(uid, "watch_stopped", match=mname))


# ─── Top-5 matches ────────────────────────────────────────────────────────────
async def send_top5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    await update.message.chat.send_action("typing")
    matches = await get_top5_matches()
    if matches:
        lang = db_lang(uid)
        lines = [T[lang]["top5_header"]]
        for i, m in enumerate(matches, 1):
            lines.append(f"{i}. {m['home']} vs {m['away']}")
            lines.append(f"   {m['league']} | {m['time']} UTC\n")
        # Also ask Claude for commentary
        try:
            prompt = T[lang]["top5_prompt"].format(date=date.today().strftime("%d.%m.%Y"))
            resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            lines.append("\nAI analizi:\n" + resp.content[0].text)
        except Exception: pass
        await update.message.reply_text("\n".join(lines))
    else:
        # Fallback to Claude only
        lang = db_lang(uid)
        try:
            prompt = T[lang]["top5_prompt"].format(date=date.today().strftime("%d.%m.%Y"))
            resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            header = T[lang]["top5_header"]
            await update.message.reply_text(header + resp.content[0].text)
        except Exception:
            await update.message.reply_text(tr(uid, "top5_empty"))


# ─── Profile ──────────────────────────────────────────────────────────────────
async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    u = db_get(uid)
    await update.message.reply_text(tr(uid, "profile_text",
        name=u["display_name"] or "-", lang=LANG_NAMES.get(u["lang"], u["lang"]),
        total_req=u["total_requests"], refs=u["ref_count"],
        sports=u["sports"] or "-", bet_types=u["bet_types"] or "-", exp=u["experience"] or "-"))


async def ref_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    bot_info = await context.bot.get_me()
    u = db_get(uid)
    await update.message.reply_text(tr(uid, "ref_link", bot=bot_info.username, uid=uid, count=u["ref_count"]))


async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    subs = db_user_lsubs(uid)
    if not subs: await update.message.reply_text(tr(uid, "no_subs")); return
    lines = []; btns = []
    for s in subs:
        lines.append(f"- {s['match_name']}")
        btns.append([InlineKeyboardButton(f"X {s['match_name'][:30]}", callback_data=f"unwatch_{s['match_id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


# ─── Main message handler ─────────────────────────────────────────────────────
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id; info = uinfo(update)
    db_ensure(uid, user.username or "")
    text = update.message.text or update.message.caption or ""

    # Registration flow
    step = reg_step.get(uid)
    if step == "awaiting_name" and update.message.text:
        await handle_name(update, context); return
    if step in ("awaiting_lang", "awaiting_name", "ob_sports", "ob_bet", "ob_exp", "ob_style"):
        return

    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    if db_is_blocked(uid):
        await update.message.reply_text(tr(uid, "db_blocked")); return

    # Menu button routing
    lang = db_lang(uid)
    tl = T[lang]
    if text == tl["menu_top5"]:
        await send_top5(update, context); return
    if text == tl["menu_matches"]:
        await matches_cmd(update, context); return
    if text == tl["menu_profile"]:
        await profile_cmd(update, context); return
    if text == tl["menu_ref"]:
        await ref_cmd(update, context); return
    if text == tl["menu_lang"]:
        await update.message.reply_text(tr(uid, "choose_lang"), reply_markup=lang_kb()); return
    if text == tl["menu_forecast"]:
        await update.message.reply_text(tr(uid, "no_input")); return

    # Security
    blk, secs = sec_blocked(uid)
    if blk:
        sus.warning(f"BLK | {info}")
        await update.message.reply_text(tr(uid, "blocked", m=secs//60, s=secs%60)); return
    exceeded, wait = rate_check(uid)
    if exceeded:
        if record_viol(uid, info):
            await update.message.reply_text(tr(uid, "auto_blocked", min=SPAM_DUR//60))
        else:
            await update.message.reply_text(tr(uid, "rate_limit", w=wait, v=violations[uid], max=SPAM_AFTER))
        return
    violations[uid] = 0

    mtype = "PHOTO" if update.message.photo else "TEXT"
    logger.info(f"MSG [{mtype}] | {info}")
    db_log_req(uid, mtype)
    await update.message.chat.send_action("typing")

    photo = update.message.photo
    if len(text) > 1000:
        sus.warning(f"LONG | {info}"); await update.message.reply_text(tr(uid, "long_text")); return
    inj = ["ignore previous", "system prompt", "forget instructions", "act as", "jailbreak", "###", "<<<"]
    if any(k.lower() in text.lower() for k in inj):
        sus.warning(f"INJ | {info}"); await update.message.reply_text(tr(uid, "injection")); return

    content = []
    if photo:
        f = await context.bot.get_file(photo[-1].file_id)
        fb = await f.download_as_bytearray()
        b64 = base64.standard_b64encode(fb).decode("utf-8")
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    if text:
        content.append({"type": "text", "text": text})
    elif not photo:
        await update.message.reply_text(tr(uid, "no_input")); return
    else:
        content.append({"type": "text", "text": tr(uid, "img_prompt")})

    # Real football data
    if text and FOOTBALL_KEY:
        words = [w.strip(".,!?") for w in text.split() if len(w) > 3 and w[0].isupper()]
        fetched = []
        for word in words[:2]:
            try:
                async with httpx.AsyncClient(timeout=5) as h:
                    r = await h.get("https://api.football-data.org/v4/teams",
                        headers={"X-Auth-Token": FOOTBALL_KEY}, params={"name": word, "limit": 1})
                    if r.status_code == 200:
                        teams = r.json().get("teams", [])
                        if teams:
                            tid = teams[0]["id"]
                            r2 = await h.get(f"https://api.football-data.org/v4/teams/{tid}/matches",
                                headers={"X-Auth-Token": FOOTBALL_KEY}, params={"status": "FINISHED", "limit": 5})
                            if r2.status_code == 200:
                                ms = r2.json().get("matches", [])
                                if ms:
                                    res = [f"{m['utcDate'][:10]} {m['homeTeam']['name']} {m['score']['fullTime'].get('home',0)}-{m['score']['fullTime'].get('away',0)} {m['awayTeam']['name']}" for m in ms]
                                    fetched.append(teams[0]["name"] + ":\n" + "\n".join(res))
            except Exception: pass
        if fetched: content.append({"type": "text", "text": "REAL DATA:\n" + "\n\n".join(fetched)})

    # Store content for forecast type selection
    context.user_data["pending_content"] = content
    context.user_data["pending_text"] = text

    # Show forecast type chooser
    lang = db_lang(uid)
    tl = T[lang]
    choose_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(tl["btn_extended"], callback_data="forecast_extended"),
        InlineKeyboardButton(tl["btn_short"],    callback_data="forecast_short"),
    ]])
    await update.message.reply_text(tl["choose_forecast"], reply_markup=choose_kb)


async def forecast_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle forecast type selection - extended or short."""
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    ftype = q.data  # "forecast_extended" or "forecast_short"

    content = context.user_data.get("pending_content")
    text    = context.user_data.get("pending_text", "")
    if not content:
        await q.edit_message_text("Sorğu tapılmadı. Yenidən yazın. / Запрос не найден. / Query not found.")
        return

    await q.edit_message_text("⏳" if ftype == "forecast_extended" else "⚡")
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    # Build system prompt
    if ftype == "forecast_short":
        lang = db_lang(uid)
        u = db_get(uid) or {}
        sys_prompt = T[lang]["short_prompt"].format(
            date=date.today().strftime("%d.%m.%Y"),
            sports=u.get("sports", "-"),
            bet_types=u.get("bet_types", "-"),
            exp=u.get("experience", "-"),
        )
        max_tok = 400
    else:
        sys_prompt = tr(uid, "system_prompt")
        max_tok = 1200

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tok,
            system=sys_prompt,
            messages=[{"role": "user", "content": content}]
        )
        reply = resp.content[0].text
        logger.info(f"FORECAST [{ftype}] OK | uid={uid}")
    except anthropic.RateLimitError: reply = tr(uid, "api_overload")
    except anthropic.APIError as e:
        logger.error(f"API_ERR {e} | uid={uid}"); reply = tr(uid, "api_error")

    # Watch button for live matches
    watch_kb = None
    if text and APIFOOTBALL_KEY:
        ms = await search_match(" ".join(text.split()[:3]))
        if ms:
            m = ms[0]
            context.user_data[f"mn_{m['id']}"] = m["name"]
            watch_kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                tr(uid, "watch_btn") + f": {m['name'][:35]}", callback_data=f"watch_{m['id']}")]])

    await context.bot.send_message(chat_id=uid, text=reply, reply_markup=watch_kb)

# ─── Admin panel ──────────────────────────────────────────────────────────────
def is_adm(update): return (update.effective_user.id if update.effective_user else 0) == ADMIN_ID

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Статистика",          callback_data="adm_stats")],
        [InlineKeyboardButton("Рассылка",            callback_data="adm_broadcast")],
        [InlineKeyboardButton("Заблокированные",     callback_data="adm_blocklist")],
        [InlineKeyboardButton("Поиск пользователя",  callback_data="adm_search")],
        [InlineKeyboardButton("Изменить язык",        callback_data="adm_setlang")],
        [InlineKeyboardButton("Топ рефералов",        callback_data="adm_toprefs")],
        [InlineKeyboardButton("Live подписки",        callback_data="adm_live")],
    ])
    await update.message.reply_text("АДМИН ПАНЕЛЬ", reply_markup=kb)


async def adm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: await q.answer("Нет доступа", show_alert=True); return
    await q.answer(); data = q.data
    back = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="adm_back")]])

    if data == "adm_stats":
        s = db_stats()
        blk_now = sum(1 for v in blocked_until.values() if time.time() < v)
        live_now = sum(len(v) for v in live_subs.values())
        lang_str = " | ".join(f"{l}: {n}" for l, n in s["langs"])
        await q.edit_message_text(
            f"СТАТИСТИКА\n\n"
            f"Пользователей: {s['total']}\n"
            f"Новых сегодня: {s['today']}\n"
            f"Заблокировано в БД: {s['blocked']}\n"
            f"Онбординг завершён: {s['ob_done']}\n\n"
            f"Запросов всего: {s['rqtotal']}\n"
            f"Запросов сегодня: {s['rqtoday']}\n\n"
            f"Языки: {lang_str}\n\n"
            f"Live подписки (БД): {s['live_ct']}\n"
            f"Live активных сейчас: {live_now}\n"
            f"Заблокировано rate-limit: {blk_now}",
            reply_markup=back)

    elif data == "adm_blocklist":
        with con() as c:
            rows = c.execute("SELECT user_id,username,display_name FROM users WHERE is_blocked=1").fetchall()
        if not rows: await q.edit_message_text("Нет заблокированных.", reply_markup=back); return
        btns = [[InlineKeyboardButton(f"Разблокировать: {r[2] or r[1] or r[0]}", callback_data=f"adm_unblk_{r[0]}")] for r in rows]
        btns.append([InlineKeyboardButton("Назад", callback_data="adm_back")])
        lines = ["ЗАБЛОКИРОВАННЫЕ:"] + [f"- {r[2] or r[1] or r[0]} (id={r[0]})" for r in rows]
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_unblk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 0)
        await q.edit_message_text(f"Пользователь {uid} разблокирован.", reply_markup=back)

    elif data.startswith("adm_blk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 1)
        await q.edit_message_text(f"Пользователь {uid} заблокирован.", reply_markup=back)

    elif data == "adm_broadcast":
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text("Отправьте текст рассылки для всех пользователей.\n/cancel — отмена.")

    elif data == "adm_search":
        context.user_data["adm_act"] = "search"
        await q.edit_message_text("Введите ID, username или имя пользователя.")

    elif data == "adm_setlang":
        context.user_data["adm_act"] = "setlang"
        await q.edit_message_text("Формат: 123456789 ru\nЯзыки: az, ru, en")

    elif data == "adm_toprefs":
        s = db_stats()
        text = "ТОП РЕФЕРАЛОВ:\n\n" + "\n".join(
            f"{i+1}. {r[2] or r[1] or r[0]} — {r[3]} чел." for i, r in enumerate(s["toprefs"]))
        await q.edit_message_text(text, reply_markup=back)

    elif data == "adm_live":
        live_now = sum(len(v) for v in live_subs.values())
        lines = [f"LIVE ПОДПИСКИ: {live_now} активных\n"]
        for mid, uids in live_subs.items():
            if uids: lines.append(f"Матч {mid}: {len(uids)} подписчиков")
        await q.edit_message_text("\n".join(lines) if len(lines) > 1 else "Нет активных live подписок.",
            reply_markup=back)

    elif data == "adm_back":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Статистика",          callback_data="adm_stats")],
            [InlineKeyboardButton("Рассылка",            callback_data="adm_broadcast")],
            [InlineKeyboardButton("Заблокированные",     callback_data="adm_blocklist")],
            [InlineKeyboardButton("Поиск пользователя",  callback_data="adm_search")],
            [InlineKeyboardButton("Изменить язык",        callback_data="adm_setlang")],
            [InlineKeyboardButton("Топ рефералов",        callback_data="adm_toprefs")],
            [InlineKeyboardButton("Live подписки",        callback_data="adm_live")],
        ])
        await q.edit_message_text("АДМИН ПАНЕЛЬ", reply_markup=kb)


async def handle_adm_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    act = context.user_data.get("adm_act")
    if not act: return
    context.user_data.pop("adm_act")
    text = update.message.text or ""

    if act == "broadcast":
        uids = db_all_uids()
        status = await update.message.reply_text(f"Рассылка для {len(uids)} пользователей...")
        ok = fail = 0
        for uid in uids:
            try: await context.bot.send_message(chat_id=uid, text=text); ok += 1
            except Exception: fail += 1
            await asyncio.sleep(0.05)
        await status.edit_text(f"Готово!\nДоставлено: {ok}\nНе доставлено: {fail}")

    elif act == "search":
        results = db_search(text.strip())
        if not results: await update.message.reply_text("Пользователь не найден."); return
        for u in results:
            reg = "Да" if u["is_registered"] else "Нет"
            blk = "ЗАБЛОКИРОВАН" if u["is_blocked"] else "Активен"
            btns = []
            if u["is_blocked"]:
                btns.append([InlineKeyboardButton("Разблокировать", callback_data=f"adm_unblk_{u['user_id']}")])
            else:
                btns.append([InlineKeyboardButton("Заблокировать", callback_data=f"adm_blk_{u['user_id']}")])
            await update.message.reply_text(
                f"ID: {u['user_id']}\n"
                f"Username: @{u['username'] or '-'}\n"
                f"Имя: {u['display_name'] or '-'}\n"
                f"Язык: {u['lang']}\n"
                f"Регистрация: {reg}\n"
                f"Статус: {blk}\n"
                f"Спорт: {u['sports'] or '-'}\n"
                f"Ставки: {u['bet_types'] or '-'}\n"
                f"Опыт: {u['experience'] or '-'}\n"
                f"Запросов: {u['total_requests']}\n"
                f"Дата: {u['joined_at']}",
                reply_markup=InlineKeyboardMarkup(btns))

    elif act == "setlang":
        parts = text.strip().split()
        if len(parts) != 2 or parts[1] not in ("az", "ru", "en"):
            await update.message.reply_text("Формат: 123456789 ru\nЯзыки: az, ru, en"); return
        db_set(int(parts[0]), "lang", parts[1])
        await update.message.reply_text(f"Язык пользователя {parts[0]} изменён на {parts[1]}.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("adm_act", None)
    await update.message.reply_text("Отменено.")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("lang",    lang_cmd))
    app.add_handler(CommandHandler("ref",     ref_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("top5",    send_top5))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("admin",   admin_cmd))
    app.add_handler(CommandHandler("cancel",  cancel_cmd))

    app.add_handler(CallbackQueryHandler(lang_cb,         pattern=r"^lang_"))
    app.add_handler(CallbackQueryHandler(ob_cb,           pattern=r"^ob_"))
    app.add_handler(CallbackQueryHandler(forecast_type_cb, pattern=r"^forecast_"))
    app.add_handler(CallbackQueryHandler(watch_cb,        pattern=r"^(watch|unwatch)_"))
    app.add_handler(CallbackQueryHandler(adm_cb,          pattern=r"^adm_"))

    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_adm_msg), group=0)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_msg), group=1)

    async def post_init(application):
        asyncio.create_task(poller(application))
        asyncio.create_task(daily_push(application))

    app.post_init = post_init
    logger.info("ProqnozAI v4 started")
    app.run_polling()

if __name__ == "__main__":
    main()
