import os, logging, base64, time, sqlite3, asyncio, httpx, random
from collections import defaultdict, deque
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
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
MOSTBET_BASE    = "https://mostbet2.com"   # Odds Checker API (IP whitelisted)

RATE_WINDOW = 60; RATE_MAX = 5; SPAM_AFTER = 3; SPAM_DUR = 600
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─── In-memory ────────────────────────────────────────────────────────────────
msg_times:     dict[int, deque] = defaultdict(deque)
violations:    dict[int, int]   = defaultdict(int)
blocked_until: dict[int, float] = {}
reg_step:      dict[int, str]   = {}
live_subs:     dict[str, set]   = defaultdict(set)
last_events:   dict[str, list]  = {}
ht_sent:       set              = set()

# ─── DB ───────────────────────────────────────────────────────────────────────
DB = "bot.db"
def con(): return sqlite3.connect(DB)

def db_init():
    with con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            username        TEXT,
            display_name    TEXT,
            lang            TEXT DEFAULT 'az',
            is_registered   INTEGER DEFAULT 0,
            is_blocked      INTEGER DEFAULT 0,
            sports          TEXT DEFAULT '',
            experience      TEXT DEFAULT '',
            onboarding_done INTEGER DEFAULT 0,
            total_requests  INTEGER DEFAULT 0,
            last_active     TEXT DEFAULT '',
            joined_at       TEXT DEFAULT (datetime('now'))
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
        langs   = c.execute("SELECT lang,COUNT(*) FROM users WHERE is_registered=1 GROUP BY lang").fetchall()
        ob_done = c.execute("SELECT COUNT(*) FROM users WHERE onboarding_done=1").fetchone()[0]
        live_ct = c.execute("SELECT COUNT(*) FROM live_subscriptions").fetchone()[0]
        top_req = c.execute("SELECT user_id,display_name,total_requests FROM users WHERE is_registered=1 ORDER BY total_requests DESC LIMIT 5").fetchall()
    return dict(total=total, today=today, blocked=blocked, rqtotal=rqtotal, rqtoday=rqtoday,
                langs=langs, ob_done=ob_done, live_ct=live_ct, top_req=top_req)

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

# ─── Human-readable label maps ────────────────────────────────────────────────
SPORTS_LABELS = {
    "az": {"football": "Futbol", "ufc": "UFC/MMA", "nba": "Basketbol",
           "tennis": "Tennis", "hockey": "Hokey", "all": "Hamısı"},
    "ru": {"football": "Футбол", "ufc": "UFC/MMA", "nba": "Баскетбол",
           "tennis": "Теннис", "hockey": "Хоккей", "all": "Все виды"},
    "en": {"football": "Football", "ufc": "UFC/MMA", "nba": "Basketball",
           "tennis": "Tennis", "hockey": "Hockey", "all": "All sports"},
}
EXP_LABELS = {
    "az": {"beginner": "Yeni başlayanam", "mid": "Orta səviyyə", "expert": "Təcrübəliyəm"},
    "ru": {"beginner": "Новичок", "mid": "Средний уровень", "expert": "Опытный"},
    "en": {"beginner": "Beginner", "mid": "Intermediate", "expert": "Expert"},
}

def sport_label(uid, val):
    lang = db_lang(uid)
    return SPORTS_LABELS.get(lang, SPORTS_LABELS["ru"]).get(val, val)

def exp_label(uid, val):
    lang = db_lang(uid)
    return EXP_LABELS.get(lang, EXP_LABELS["ru"]).get(val, val)

# ─── Translations ─────────────────────────────────────────────────────────────
T = {
"az": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Xoş gəldiniz! Adınızı daxil edin:",
"reg_done":      "Qeydiyyat tamamlandı! Salam, {name}!",
"welcome_intro": """ProqnozAI-yə xoş gəldiniz!

Mən AI-əsaslı idman mərc analitikiyəm. Nə edə bilərəm:

• İstənilən matç üzrə proqnoz — komanda adını yazın və ya cədvəl şəkli göndərin
• Geniş analiz — forma, amillər, bütün mərc növləri keflər ilə
• Qısa proqnoz — 5 saniyədə əsas mərc
• Canlı bildirişlər — matçı izləyirəm, hadisələri real vaxtda göndərirəm

2 sual cavablayın — sizə uyğun proqnozlar seçim.""",
"post_onboarding": """Hazırdır! İndi matç adı yazın — məsələn:

Barselona Alavés
Real Madrid Arsenal
PSJ Manchester City

Və ya oyun cədvəlinin şəklini göndərin.""",
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
"watch_btn":     "Oyunu izlə",
"watch_started": "Oyun izlənilir: {match}",
"watch_stopped": "Dayandırıldı: {match}",
"no_subs":       "Heç bir oyun izləmirsiniz.",
"live_goal":     "QOL! {match}\n{minute}. dəq: {team}\nHesab: {score}\n\nCanlı mərc:\n{tip}",
"live_card":     "KART! {match}\n{minute}. dəq: {player} ({team}) - {card}\n\nCanlı mərc:\n{tip}",
"live_halftime": "FASİLƏ! {match}\nHesab: {score}\n\nFasilə mərci:\n{tip}",
"live_fulltime": "OYUN BİTDİ! {match}\nYekun: {score}",
"live_alert_goal":     "SİQNAL! {match} - Qol gözlənilir [{minute}. dəq]",
"live_alert_value":    "LIVE VALUE! {match} - {team} üzərində dəyər var [{minute}. dəq]",
"live_alert_pressure": "TƏZYİQ! {match} - {team} hücum təzyiqi [{minute}. dəq] {stat}",
"menu_forecast": "Proqnoz al",
"menu_matches":  "Matçlarım",
"menu_profile":  "Profil",
"menu_lang":     "Dil dəyiş",
"profile_text":  "PROFİL\n\nAd: {name}\nDil: {lang}\nCəmi sorğular: {total_req}\n\nİdman: {sports}\nTəcrübə: {exp}",
"ob_sports":     "Hansı idman növünü sevirsiniz?",
"ob_exp":        "Mərcdə təcrübəniz nə qədərdir?",
"ob_done":       "Profil hazırdır! Fərdiləşdirilmiş proqnozlar alacaqsınız.\n\nİdman: {sports}\nTəcrübə: {exp}",
"choose_forecast": "Proqnoz növünü seçin:",
"btn_extended":    "Geniş proqnoz",
"btn_short":       "Qısa proqnoz",
"system_prompt": """Sən peşəkar idman analitikisən. Dürüst və real proqnozlar ver.

PROFİL: İdman: {sports} | Təcrübə: {exp}

VACIB QAYDALAR:
1. Komandanı tərk etmiş oyunçuları HEÇ VAXT qeyd etmə
2. Cari heyəti bilmirsənsə "heyət məlumatı yoxlanılır" yaz — UYDURMА
3. Keflər REAL olmalıdır: fаvorit 1.20-1.60, bərabər 2.20-3.00, tоtal 2.5 1.70-2.10
4. Markdown ** istifadə etmə — yalnız mətn və emoji
5. Real faktorları analiz et: forma, ev/dəhliz, motivasiya

FORMAT:

🏆 [Komanda A] — [Komanda B]
📍 [Turnir] | [Tarix]

📊 FORMA (son 5 oyun):
[Komanda A]: [nəticələr və ya "məlumat yoxlanılır"]
[Komanda B]: [nəticələr və ya "məlumat yoxlanılır"]

🔍 ANALİZ:
[Matça real təsir edən 3-4 əsas amil. Konkret və aydın.]

🎯 PROQNOZ:

1X2:
[Komanda A] — XX% | Kef: X.XX-X.XX
Heç-heçə — XX% | Kef: X.XX-X.XX
[Komanda B] — XX% | Kef: X.XX-X.XX

⚽ Total qol:
2.5 Üstündə — XX% | Kef: X.XX-X.XX
2.5 Altında — XX% | Kef: X.XX-X.XX

🔥 Hər ikisi qol vurur:
Bəli — XX% | Kef: X.XX-X.XX
Xeyr — XX% | Kef: X.XX-X.XX

📐 Asiya handicapı:
[Komanda A] (-1) — XX% | Kef: X.XX-X.XX
[Komanda B] (+1) — XX% | Kef: X.XX-X.XX

⚡ ƏN YAXŞI MƏRCİ:
[Konkret növ] | Kef: X.XX-X.XX
[Niyə məhz bu mərc — 1-2 cümlə real əsaslandırma]

⚠️ Analitik proqnozdur, nəticə zəmanəti deyil.""",
"short_prompt": """Qısa proqnoz. Profil: {sports} | {exp}

QАYDALAR: real keflər, getmiş oyunçuları qeyd etmə, yalnız mətn və emoji.

FORMAT:
🏆 [Komanda A] — [Komanda B] | [Turnir]
🎯 Favorit: [Komanda] XX% | Kef X.XX-X.XX
⚽ Total 2.5 üstündə: XX% | Kef X.XX-X.XX
🔥 Hər ikisi qol Bəli: XX% | Kef X.XX-X.XX
⚡ MƏRCİ: [növü] | Kef X.XX-X.XX
[1 cümlə — niyə]
⚠️ Analitik proqnozdur.""",
"live_tip_prompt": "Canlı mərc analitikisən. Oyun: {match}, {minute}. dəq, hesab {score}. Hadisə: {event}. Ən yaxşı canlı mərci tövsiyə et. Qısa, maks 2 cümlə.",
},

"ru": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Добро пожаловать! Введите ваше имя:",
"reg_done":      "Регистрация завершена! Привет, {name}!",
"welcome_intro": """Добро пожаловать в ProqnozAI!

Я — AI-аналитик спортивных ставок. Вот что я умею:

• Прогноз на любой матч — напишите команды или отправьте фото расписания
• Расширенный анализ — форма, факторы, все виды ставок с коэффициентами
• Краткий прогноз — только главная ставка за 5 секунд
• Live уведомления — слежу за матчем и присылаю события в реальном времени

Ответьте на 2 быстрых вопроса — подберу прогнозы под вас.""",
"post_onboarding": """Готово! Теперь напишите название матча — например:

Барселона Алавес
Реал Мадрид Арсенал
ПСЖ Манчестер Сити

Или отправьте фото расписания матчей.""",
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
"watch_btn":     "Следить за матчем",
"watch_started": "Слежу за матчем: {match}",
"watch_stopped": "Слежение остановлено: {match}",
"no_subs":       "Вы не следите ни за одним матчем.",
"live_goal":     "ГОЛ! {match}\n{minute} мин: {team}\nСчёт: {score}\n\nЛайв-ставка:\n{tip}",
"live_card":     "КАРТОЧКА! {match}\n{minute} мин: {player} ({team}) - {card}\n\nЛайв-ставка:\n{tip}",
"live_halftime": "ПЕРЕРЫВ! {match}\nСчёт: {score}\n\nСтавка на перерыв:\n{tip}",
"live_fulltime": "МАТЧ ЗАВЕРШЁН! {match}\nИтог: {score}",
"live_alert_goal":     "СИГНАЛ! {match} - ожидается гол [{minute} мин]",
"live_alert_value":    "LIVE VALUE! {match} - есть ценность на {team} [{minute} мин]",
"live_alert_pressure": "ДАВЛЕНИЕ! {match} - {team} создаёт давление [{minute} мин] {stat}",
"menu_forecast": "Получить прогноз",
"menu_matches":  "Мои матчи",
"menu_profile":  "Профиль",
"menu_lang":     "Сменить язык",
"profile_text":  "ПРОФИЛЬ\n\nИмя: {name}\nЯзык: {lang}\nВсего запросов: {total_req}\n\nСпорт: {sports}\nОпыт: {exp}",
"ob_sports":     "Какой вид спорта вас интересует больше всего?",
"ob_exp":        "Каков ваш опыт в ставках?",
"ob_done":       "Профиль готов! Будете получать персонализированные прогнозы.\n\nСпорт: {sports}\nОпыт: {exp}",
"choose_forecast": "Выберите формат прогноза:",
"btn_extended":    "Расширенный",
"btn_short":       "Краткий",
"system_prompt": """Ты — профессиональный спортивный аналитик. Твоя задача — давать честные, реалистичные прогнозы.

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ: Спорт: {sports} | Опыт: {exp}

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:
1. НИКОГДА не упоминай игроков которые покинули клуб (Мбаппе ушёл из ПСЖ в 2024, Неймар давно не в ПСЖ и т.д.)
2. Если не знаешь актуальный состав — пиши "состав уточняется" — НЕ ПРИДУМЫВАЙ
3. Коэффициенты должны быть РЕАЛИСТИЧНЫМИ для букмекеров:
   - Явный фаворит: 1.20-1.60
   - Небольшое преимущество: 1.60-2.20
   - Равные команды: 2.20-3.00
   - Андердог: 3.00-8.00
   - Тотал 2.5 больше/меньше обычно: 1.70-2.10
   - Обе забьют да/нет: 1.60-2.20
4. НЕ используй markdown ** — только чистый текст и emoji
5. Анализируй реальные факторы: форму, домашний/гостевой фактор, мотивацию, травмы если известны

ФОРМАТ ОТВЕТА:

🏆 [Команда А] — [Команда Б]
📍 [Турнир] | [Дата если известна]

📊 ФОРМА (последние 5 матчей):
[Команда А]: [результаты или "данные уточняются"]
[Команда Б]: [результаты или "данные уточняются"]

🔍 АНАЛИЗ:
[3-4 ключевых фактора которые реально влияют на матч. Конкретно и по делу.]

🎯 ПРОГНОЗ:

1X2:
[Команда А] — XX% | Кэф: X.XX-X.XX
Ничья — XX% | Кэф: X.XX-X.XX
[Команда Б] — XX% | Кэф: X.XX-X.XX

⚽ Тотал голов:
Больше 2.5 — XX% | Кэф: X.XX-X.XX
Меньше 2.5 — XX% | Кэф: X.XX-X.XX

🔥 Обе забьют:
Да — XX% | Кэф: X.XX-X.XX
Нет — XX% | Кэф: X.XX-X.XX

📐 Азиатский гандикап:
[Команда А] (-1) — XX% | Кэф: X.XX-X.XX
[Команда Б] (+1) — XX% | Кэф: X.XX-X.XX

⚡ ЛУЧШАЯ СТАВКА:
[Конкретный тип] | Кэф: X.XX-X.XX
[Почему именно эта ставка — 1-2 предложения с реальным обоснованием]

⚠️ Это аналитический прогноз, не гарантия результата.""",
"short_prompt": """Краткий прогноз. Профиль: {sports} | {exp}

ПРАВИЛА: реалистичные коэффициенты, не упоминай ушедших игроков, только чистый текст и emoji.

ФОРМАТ:
🏆 [Команда А] — [Команда Б] | [Турнир]
🎯 Фаворит: [Команда] XX% | Кэф X.XX-X.XX
⚽ Тотал 2.5 больше: XX% | Кэф X.XX-X.XX
🔥 Обе забьют Да: XX% | Кэф X.XX-X.XX
⚡ СТАВКА: [тип ставки] | Кэф X.XX-X.XX
[1 предложение — почему]
⚠️ Аналитический прогноз.""",
"live_tip_prompt": "Ты лайв-аналитик. Матч {match}, {minute} мин, счёт {score}. Событие: {event}. Дай лучшую лайв-ставку. Коротко, макс 2 предложения.",
},

"en": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Welcome! Please enter your name:",
"reg_done":      "Registration complete! Hi, {name}!",
"welcome_intro": """Welcome to ProqnozAI!

I'm an AI sports betting analyst. Here's what I do:

• Forecast for any match — type the teams or send a schedule photo
• Extended analysis — form, key factors, all bet types with odds
• Short forecast — just the main bet in 5 seconds
• Live alerts — I follow the match and send events in real time

Answer 2 quick questions — I'll personalize your forecasts.""",
"post_onboarding": """All set! Now type a match name — for example:

Barcelona Alavés
Real Madrid Arsenal
PSG Manchester City

Or send a photo of the match schedule.""",
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
"watch_btn":     "Follow match",
"watch_started": "Following: {match}",
"watch_stopped": "Stopped: {match}",
"no_subs":       "You are not following any matches.",
"live_goal":     "GOAL! {match}\n{minute} min: {team}\nScore: {score}\n\nLive bet:\n{tip}",
"live_card":     "CARD! {match}\n{minute} min: {player} ({team}) - {card}\n\nLive bet:\n{tip}",
"live_halftime": "HALF TIME! {match}\nScore: {score}\n\nHalf-time bet:\n{tip}",
"live_fulltime": "FULL TIME! {match}\nFinal: {score}",
"live_alert_goal":     "SIGNAL! {match} - goal expected [{minute} min]",
"live_alert_value":    "LIVE VALUE! {match} - value on {team} [{minute} min]",
"live_alert_pressure": "PRESSURE! {match} - {team} attacking hard [{minute} min] {stat}",
"menu_forecast": "Get forecast",
"menu_matches":  "My matches",
"menu_profile":  "Profile",
"menu_lang":     "Change language",
"profile_text":  "PROFILE\n\nName: {name}\nLanguage: {lang}\nTotal requests: {total_req}\n\nSports: {sports}\nExp: {exp}",
"ob_sports":     "Which sport interests you the most?",
"ob_exp":        "What is your betting experience?",
"ob_done":       "Profile ready! You'll get personalized forecasts.\n\nSports: {sports}\nExp: {exp}",
"choose_forecast": "Choose forecast format:",
"btn_extended":    "Extended",
"btn_short":       "Short",
"system_prompt": """You are a professional sports analyst. Your job is to give honest, realistic forecasts.

USER PROFILE: Sports: {sports} | Experience: {exp}

CRITICAL RULES:
1. NEVER mention players who left the club (Mbappe left PSG in 2024, etc.)
2. If you don't know the current squad — write "squad data pending" — DO NOT invent
3. Odds must be REALISTIC for bookmakers:
   - Clear favorite: 1.20-1.60 | Slight edge: 1.60-2.20
   - Even match: 2.20-3.00 | Underdog: 3.00-8.00
   - Over/Under 2.5: 1.70-2.10 | BTTS Yes/No: 1.60-2.20
4. Do NOT use markdown ** — plain text and emoji only
5. Analyze real factors: form, home/away, motivation, injuries if known

RESPONSE FORMAT:

🏆 [Team A] — [Team B]
📍 [Tournament] | [Date if known]

📊 FORM (last 5 matches):
[Team A]: [results or "data pending"]
[Team B]: [results or "data pending"]

🔍 ANALYSIS:
[3-4 key factors that genuinely affect this match. Specific and factual.]

🎯 FORECAST:

1X2:
[Team A] — XX% | Odds: X.XX-X.XX
Draw — XX% | Odds: X.XX-X.XX
[Team B] — XX% | Odds: X.XX-X.XX

⚽ Total Goals:
Over 2.5 — XX% | Odds: X.XX-X.XX
Under 2.5 — XX% | Odds: X.XX-X.XX

🔥 Both Teams Score:
Yes — XX% | Odds: X.XX-X.XX
No — XX% | Odds: X.XX-X.XX

📐 Asian Handicap:
[Team A] (-1) — XX% | Odds: X.XX-X.XX
[Team B] (+1) — XX% | Odds: X.XX-X.XX

⚡ BEST BET:
[Specific type] | Odds: X.XX-X.XX
[Why this bet — 1-2 sentences with real reasoning]

⚠️ Analytical forecast, not a guaranteed result.""",
"short_prompt": """Short forecast. Profile: {sports} | {exp}

RULES: realistic odds, never mention departed players, plain text and emoji only.

FORMAT:
🏆 [Team A] — [Team B] | [Tournament]
🎯 Favourite: [Team] XX% | Odds X.XX-X.XX
⚽ Over 2.5: XX% | Odds X.XX-X.XX
🔥 BTTS Yes: XX% | Odds X.XX-X.XX
⚡ BET: [type] | Odds X.XX-X.XX
[1 sentence — why]
⚠️ Analytical forecast.""",
"live_tip_prompt": "You are a live betting analyst. Match {match}, {minute} min, score {score}. Event: {event}. Best live bet now. Max 2 sentences.",
},
}

LANG_NAMES = {"az": "Azerbaycan", "ru": "Русский", "en": "English"}

def tr(uid, key, **kw):
    lang = db_lang(uid)
    txt  = T.get(lang, T["ru"]).get(key, T["ru"].get(key, ""))
    if key in ("system_prompt", "short_prompt"):
        u = db_get(uid) or {}
        kw.setdefault("sports", sport_label(uid, u.get("sports", "-")))
        kw.setdefault("exp",    exp_label(uid, u.get("experience", "-")))
    return txt.format(**kw) if kw else txt

# ─── Onboarding data ──────────────────────────────────────────────────────────
OB_SPORTS = {
    "az": [("Futbol", "football"), ("UFC/MMA", "ufc"), ("Basketbol", "nba"),
           ("Tennis", "tennis"), ("Hokey", "hockey"), ("Hamısı", "all")],
    "ru": [("Футбол", "football"), ("UFC/MMA", "ufc"), ("Баскетбол", "nba"),
           ("Теннис", "tennis"), ("Хоккей", "hockey"), ("Все виды", "all")],
    "en": [("Football", "football"), ("UFC/MMA", "ufc"), ("Basketball", "nba"),
           ("Tennis", "tennis"), ("Hockey", "hockey"), ("All sports", "all")],
}
OB_EXP = {
    "az": [("Yeni başlayanam", "beginner"), ("Orta səviyyə", "mid"), ("Təcrübəliyəm", "expert")],
    "ru": [("Новичок", "beginner"), ("Средний уровень", "mid"), ("Опытный", "expert")],
    "en": [("Beginner", "beginner"), ("Intermediate", "mid"), ("Expert", "expert")],
}

def ob_kb(items):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"ob_{val}")] for label, val in items])

# ─── Main menu ────────────────────────────────────────────────────────────────
def main_menu(uid):
    lang = db_lang(uid)
    tl = T[lang]
    return ReplyKeyboardMarkup([
        [tl["menu_forecast"], tl["menu_matches"]],
        [tl["menu_profile"],  tl["menu_lang"]],
    ], resize_keyboard=True)

def lang_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Azerbaycan", callback_data="lang_az"),
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

# ─── Football API ─────────────────────────────────────────────────────────────
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
                            "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"]}
    except Exception as e: logger.error(f"get_status: {e}")
    return None

async def live_tip(uid, match, minute, score, event):
    try:
        lang = db_lang(uid)
        p = T[lang]["live_tip_prompt"].format(match=match, minute=minute, score=score, event=event)
        r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role": "user", "content": p}])
        return r.content[0].text
    except Exception: return ""


# ─── Mostbet Odds Checker API ─────────────────────────────────────────────────

async def mostbet_find_match(team1: str, team2: str) -> dict | None:
    """Search match in Mostbet by team names, return match with lineId."""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as h:
            last_id = 0
            while True:
                r = await h.get(
                    f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                    headers={"Accept": "application/json"},
                    params={"lastId": last_id, "locale": "en", "limit": 100}
                )
                if r.status_code != 200:
                    logger.error(f"Mostbet list error: {r.status_code}")
                    return None
                data = r.json()
                matches = data.get("lineMatches", [])
                if not matches:
                    break
                t1 = team1.lower(); t2 = team2.lower()
                for m in matches:
                    t1m = m.get("team1Title", "").lower()
                    t2m = m.get("team2Title", "").lower()
                    mt  = m.get("matchTitle", "").lower()
                    if (t1 in t1m or t1 in mt) and (t2 in t2m or t2 in mt):
                        return m
                    if (t2 in t1m or t2 in mt) and (t1 in t2m or t1 in mt):
                        return m
                # Pagination
                last_id = matches[-1]["id"]
                if len(matches) < 100:
                    break
    except Exception as e:
        logger.error(f"mostbet_find_match: {e}")
    return None


async def mostbet_get_odds(line_id: int) -> dict:
    """Get odds for a match from Mostbet. Returns dict with key bet types."""
    result = {
        "w1": None, "x": None, "w2": None,
        "over25": None, "under25": None,
        "btts_yes": None, "btts_no": None,
        "url": f"{MOSTBET_BASE}/line/{line_id}"
    }
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as h:
            r = await h.get(
                f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/{line_id}/outcomes/list",
                headers={"Accept": "application/json"},
                params={"locale": "en", "limit": 100}
            )
            if r.status_code != 200:
                logger.error(f"Mostbet odds error: {r.status_code}")
                return result
            outcomes = r.json().get("lineMatchOutcomes", [])
            for o in outcomes:
                title = o.get("outcomeTitle", "").lower()
                group = o.get("groupTitle", "").lower()
                odd   = o.get("odd", "")
                try:
                    odd_f = float(odd)
                except Exception:
                    continue
                # 1X2
                if group in ("winner", "match result", "1x2", "result"):
                    if "1" == title or "w1" in title or "(1)" in title:
                        result["w1"] = odd_f
                    elif "x" == title or "draw" in title or "x" in title:
                        result["x"] = odd_f
                    elif "2" == title or "w2" in title or "(2)" in title:
                        result["w2"] = odd_f
                # Total over/under 2.5
                if "2.5" in title or "2.5" in group:
                    if "over" in title or "more" in title or "больше" in title or "(+)" in title:
                        result["over25"] = odd_f
                    elif "under" in title or "less" in title or "меньше" in title or "(-)" in title:
                        result["under25"] = odd_f
                # BTTS
                if "both" in group or "btts" in group or "gg" in group or "обе" in group:
                    if "yes" in title or "да" in title:
                        result["btts_yes"] = odd_f
                    elif "no" in title or "нет" in title:
                        result["btts_no"] = odd_f
    except Exception as e:
        logger.error(f"mostbet_get_odds: {e}")
    return result


def format_mostbet_odds(odds: dict, lang: str) -> str:
    """Format Mostbet odds as a clean string to inject into Claude prompt."""
    if not any([odds["w1"], odds["over25"], odds["btts_yes"]]):
        return ""
    lines = []
    if lang == "ru":
        lines.append("РЕАЛЬНЫЕ КОЭФФИЦИЕНТЫ MOSTBET:")
        if odds["w1"] and odds["x"] and odds["w2"]:
            lines.append(f"1X2: П1={odds['w1']} | X={odds['x']} | П2={odds['w2']}")
        if odds["over25"] and odds["under25"]:
            lines.append(f"Тотал 2.5: Больше={odds['over25']} | Меньше={odds['under25']}")
        if odds["btts_yes"] and odds["btts_no"]:
            lines.append(f"Обе забьют: Да={odds['btts_yes']} | Нет={odds['btts_no']}")
        lines.append(f"Ссылка: {odds['url']}")
        lines.append("ВАЖНО: используй ИМЕННО эти коэффициенты в прогнозе, не выдумывай свои.")
    elif lang == "az":
        lines.append("MOSTBET REAL KEFLƏRİ:")
        if odds["w1"] and odds["x"] and odds["w2"]:
            lines.append(f"1X2: Q1={odds['w1']} | X={odds['x']} | Q2={odds['w2']}")
        if odds["over25"] and odds["under25"]:
            lines.append(f"Total 2.5: Üstündə={odds['over25']} | Altında={odds['under25']}")
        if odds["btts_yes"] and odds["btts_no"]:
            lines.append(f"Hər ikisi qol: Bəli={odds['btts_yes']} | Xeyr={odds['btts_no']}")
        lines.append(f"Link: {odds['url']}")
        lines.append("VACIB: proqnozda MƏHZbu kefləri istifadə et.")
    else:
        lines.append("REAL MOSTBET ODDS:")
        if odds["w1"] and odds["x"] and odds["w2"]:
            lines.append(f"1X2: W1={odds['w1']} | X={odds['x']} | W2={odds['w2']}")
        if odds["over25"] and odds["under25"]:
            lines.append(f"Total 2.5: Over={odds['over25']} | Under={odds['under25']}")
        if odds["btts_yes"] and odds["btts_no"]:
            lines.append(f"BTTS: Yes={odds['btts_yes']} | No={odds['btts_no']}")
        lines.append(f"Link: {odds['url']}")
        lines.append("IMPORTANT: use THESE exact odds in the forecast, do not invent your own.")
    return "\n".join(lines)

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
                                card = {"az": "Qirmizi" if "Red" in detail else "Sari",
                                        "ru": "Красная" if "Red" in detail else "Жёлтая",
                                        "en": "Red" if "Red" in detail else "Yellow"}.get(lang, "Card")
                                msg = T[lang]["live_card"].format(match=match_name, minute=ev_min, player=player, team=team, card=card, tip=tip)
                            else: continue
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except Exception as e: logger.error(f"notify uid={uid}: {e}")

                alert_cnt[mid] += 1
                if alert_cnt[mid] % 15 == 0 and minute > 20:
                    atype = random.choice(["goal", "value", "pressure"])
                    pt = random.choice([st["home"], st["away"]])
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

# ─── Daily push ───────────────────────────────────────────────────────────────
async def daily_push(app):
    while True:
        await asyncio.sleep(3600)
        if datetime.now().hour != 10: continue
        msgs = {"az": "Bugun maraqli oyunlar var! Proqnoz ucun yazin.",
                "ru": "Сегодня интересные матчи! Напишите для прогноза.",
                "en": "Interesting matches today! Write for a forecast."}
        try:
            with con() as c:
                rows = c.execute("SELECT user_id,lang FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND (last_active='' OR date(last_active) <= date('now', '-2 days'))").fetchall()
            for uid, lang in rows:
                try: await app.bot.send_message(chat_id=uid, text=msgs.get(lang, msgs["ru"])); await asyncio.sleep(0.1)
                except Exception: pass
        except Exception as e: logger.error(f"daily_push: {e}")

# ─── Handlers ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
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
        # Update menu with new language
        await q.edit_message_text(T[lang]["lang_set"])
        await context.bot.send_message(chat_id=uid, text=T[lang]["lang_set"],
            reply_markup=main_menu(uid))
        return

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
    with con() as c: c.execute("UPDATE users SET is_registered=1 WHERE user_id=?", (uid,))
    reg_step[uid] = "ob_sports"
    await update.message.reply_text(tr(uid, "reg_done", name=name), reply_markup=ReplyKeyboardRemove())
    await asyncio.sleep(0.3)
    lang = db_lang(uid)
    await update.message.reply_text(T[lang]["welcome_intro"])
    await asyncio.sleep(0.5)
    await update.message.reply_text(T[lang]["ob_sports"], reply_markup=ob_kb(OB_SPORTS[lang]))
    return True


async def ob_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; val = q.data[3:]  # strip "ob_"
    lang = db_lang(uid); step = reg_step.get(uid, "")

    if step == "ob_sports":
        db_set(uid, "sports", val)
        reg_step[uid] = "ob_exp"
        await q.edit_message_text(T[lang]["ob_exp"], reply_markup=ob_kb(OB_EXP[lang]))

    elif step == "ob_exp":
        db_set(uid, "experience", val); db_set(uid, "onboarding_done", 1)
        reg_step[uid] = "done"
        u = db_get(uid)
        done_msg = T[lang]["ob_done"].format(
            sports=sport_label(uid, u["sports"]),
            exp=exp_label(uid, u["experience"]))
        await q.edit_message_text(done_msg)
        await asyncio.sleep(0.3)
        lang2 = db_lang(uid)
        await context.bot.send_message(chat_id=uid, text=T[lang2]["post_onboarding"], reply_markup=main_menu(uid))


async def forecast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; ftype = q.data
    content = context.user_data.get("pending_content")
    text    = context.user_data.get("pending_text", "")
    if not content:
        await q.edit_message_text(tr(uid, "no_input")); return

    await q.edit_message_text("...")
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    if ftype == "forecast_short":
        sys_prompt = tr(uid, "short_prompt"); max_tok = 350
    else:
        sys_prompt = tr(uid, "system_prompt"); max_tok = 1200

    try:
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=max_tok,
            system=sys_prompt, messages=[{"role": "user", "content": content}])
        reply = resp.content[0].text
    except anthropic.RateLimitError: reply = tr(uid, "api_overload")
    except anthropic.APIError as e: logger.error(f"API_ERR {e}"); reply = tr(uid, "api_error")

    watch_kb = None
    if text and APIFOOTBALL_KEY:
        ms = await search_match(" ".join(text.split()[:3]))
        if ms:
            m = ms[0]; context.user_data[f"mn_{m['id']}"] = m["name"]
            watch_kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                tr(uid, "watch_btn") + f": {m['name'][:35]}", callback_data=f"watch_{m['id']}")]])

    await context.bot.send_message(chat_id=uid, text=reply, reply_markup=watch_kb)


async def watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); uid = q.from_user.id
    if q.data.startswith("watch_"):
        mid = q.data[6:]; mname = context.user_data.get(f"mn_{mid}", mid)
        live_subs[mid].add(uid); db_add_lsub(uid, mid, mname)
        await q.edit_message_text(q.message.text + "\n\n" + tr(uid, "watch_started", match=mname))
    elif q.data.startswith("unwatch_"):
        mid = q.data[8:]
        mname = next((s["match_name"] for s in db_user_lsubs(uid) if s["match_id"] == mid), mid)
        live_subs[mid].discard(uid); db_del_lsub(uid, mid)
        await q.edit_message_text(tr(uid, "watch_stopped", match=mname))



async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    u = db_get(uid)
    await update.message.reply_text(tr(uid, "profile_text",
        name=u["display_name"] or "-", lang=LANG_NAMES.get(u["lang"], u["lang"]),
        total_req=u["total_requests"],
        sports=sport_label(uid, u["sports"]) if u["sports"] else "-",
        exp=exp_label(uid, u["experience"]) if u["experience"] else "-"))


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

    step = reg_step.get(uid)
    if step == "awaiting_name" and update.message.text:
        await handle_name(update, context); return
    if step in ("awaiting_lang", "awaiting_name", "ob_sports", "ob_exp"):
        return

    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    if db_is_blocked(uid): await update.message.reply_text(tr(uid, "db_blocked")); return

    # Menu routing
    lang = db_lang(uid); tl = T[lang]
    if text == tl["menu_matches"]: await matches_cmd(update, context); return
    if text == tl["menu_profile"]: await profile_cmd(update, context); return
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
        if record_viol(uid, info): await update.message.reply_text(tr(uid, "auto_blocked", min=SPAM_DUR//60))
        else: await update.message.reply_text(tr(uid, "rate_limit", w=wait, v=violations[uid], max=SPAM_AFTER))
        return
    violations[uid] = 0

    mtype = "PHOTO" if update.message.photo else "TEXT"
    logger.info(f"MSG [{mtype}] | {info}")
    db_log_req(uid, mtype)
    await update.message.chat.send_action("typing")

    photo = update.message.photo
    if len(text) > 1000: sus.warning(f"LONG | {info}"); await update.message.reply_text(tr(uid, "long_text")); return
    inj = ["ignore previous", "system prompt", "forget instructions", "act as", "jailbreak"]
    if any(k.lower() in text.lower() for k in inj):
        sus.warning(f"INJ | {info}"); await update.message.reply_text(tr(uid, "injection")); return

    content = []
    if photo:
        f = await context.bot.get_file(photo[-1].file_id)
        fb = await f.download_as_bytearray()
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                         "data": base64.standard_b64encode(fb).decode("utf-8")}})
    if text: content.append({"type": "text", "text": text})
    elif not photo: await update.message.reply_text(tr(uid, "no_input")); return
    else: content.append({"type": "text", "text": tr(uid, "img_prompt")})

    # Real data
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
                            r2 = await h.get(f"https://api.football-data.org/v4/teams/{teams[0]['id']}/matches",
                                headers={"X-Auth-Token": FOOTBALL_KEY}, params={"status": "FINISHED", "limit": 5})
                            if r2.status_code == 200:
                                ms = r2.json().get("matches", [])
                                if ms:
                                    res = [f"{m['utcDate'][:10]} {m['homeTeam']['name']} {m['score']['fullTime'].get('home',0)}-{m['score']['fullTime'].get('away',0)} {m['awayTeam']['name']}" for m in ms]
                                    fetched.append(teams[0]["name"] + ":\n" + "\n".join(res))
            except Exception: pass
        if fetched: content.append({"type": "text", "text": "REAL DATA:\n" + "\n\n".join(fetched)})

    # Store and show format chooser
    context.user_data["pending_content"] = content
    context.user_data["pending_text"] = text
    choose_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(tl["btn_extended"], callback_data="forecast_extended"),
        InlineKeyboardButton(tl["btn_short"],    callback_data="forecast_short"),
    ]])
    await update.message.reply_text(tl["choose_forecast"], reply_markup=choose_kb)

# ─── Admin ────────────────────────────────────────────────────────────────────
def is_adm(update): return (update.effective_user.id if update.effective_user else 0) == ADMIN_ID

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Статистика",         callback_data="adm_stats")],
        [InlineKeyboardButton("Рассылка",           callback_data="adm_broadcast")],
        [InlineKeyboardButton("Заблокированные",    callback_data="adm_blocklist")],
        [InlineKeyboardButton("Поиск пользователя", callback_data="adm_search")],
        [InlineKeyboardButton("Изменить язык",       callback_data="adm_setlang")],
        [InlineKeyboardButton("Live подписки",       callback_data="adm_live")],
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
        top = "\n".join(f"{i+1}. {r[1] or r[0]}: {r[2]} запросов" for i, r in enumerate(s["top_req"]))
        await q.edit_message_text(
            f"СТАТИСТИКА\n\n"
            f"Пользователей: {s['total']}\n"
            f"Новых сегодня: {s['today']}\n"
            f"Заблокировано: {s['blocked']}\n"
            f"Онбординг: {s['ob_done']}\n\n"
            f"Запросов всего: {s['rqtotal']}\n"
            f"Сегодня: {s['rqtoday']}\n\n"
            f"Языки: {lang_str}\n\n"
            f"Live подписки: {s['live_ct']} (активных: {live_now})\n"
            f"Rate-limit блок: {blk_now}\n\n"
            f"Топ активных:\n{top}",
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
        await q.edit_message_text("Отправьте текст рассылки.\n/cancel — отмена.")

    elif data == "adm_search":
        context.user_data["adm_act"] = "search"
        await q.edit_message_text("Введите ID, username или имя.")

    elif data == "adm_setlang":
        context.user_data["adm_act"] = "setlang"
        await q.edit_message_text("Формат: 123456789 ru\nЯзыки: az, ru, en")

    elif data == "adm_live":
        live_now = sum(len(v) for v in live_subs.values())
        lines = [f"LIVE ПОДПИСКИ: {live_now} активных\n"]
        for mid, uids in live_subs.items():
            if uids: lines.append(f"Матч {mid}: {len(uids)} подписчиков")
        await q.edit_message_text("\n".join(lines) if len(lines) > 1 else "Нет активных.", reply_markup=back)

    elif data == "adm_back":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Статистика",         callback_data="adm_stats")],
            [InlineKeyboardButton("Рассылка",           callback_data="adm_broadcast")],
            [InlineKeyboardButton("Заблокированные",    callback_data="adm_blocklist")],
            [InlineKeyboardButton("Поиск пользователя", callback_data="adm_search")],
            [InlineKeyboardButton("Изменить язык",       callback_data="adm_setlang")],
            [InlineKeyboardButton("Live подписки",       callback_data="adm_live")],
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
        await status.edit_text(f"Готово! Доставлено: {ok} | Не доставлено: {fail}")

    elif act == "search":
        results = db_search(text.strip())
        if not results: await update.message.reply_text("Не найдено."); return
        for u in results:
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
                f"Статус: {'ЗАБЛОКИРОВАН' if u['is_blocked'] else 'Активен'}\n"
                f"Спорт: {sport_label(u['user_id'], u['sports']) if u['sports'] else '-'}\n"
                f"Опыт: {exp_label(u['user_id'], u['experience']) if u['experience'] else '-'}\n"
                f"Запросов: {u['total_requests']}\n"
                f"Зарегистрирован: {u['joined_at']}",
                reply_markup=InlineKeyboardMarkup(btns))

    elif act == "setlang":
        parts = text.strip().split()
        if len(parts) != 2 or parts[1] not in ("az", "ru", "en"):
            await update.message.reply_text("Формат: 123456789 ru"); return
        db_set(int(parts[0]), "lang", parts[1])
        await update.message.reply_text(f"Язык {parts[0]} изменён на {parts[1]}.")

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("adm_act", None)
    await update.message.reply_text("Отменено.")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("lang",    lang_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("admin",   admin_cmd))
    app.add_handler(CommandHandler("cancel",  cancel_cmd))

    app.add_handler(CallbackQueryHandler(lang_cb,     pattern=r"^lang_"))
    app.add_handler(CallbackQueryHandler(ob_cb,       pattern=r"^ob_"))
    app.add_handler(CallbackQueryHandler(forecast_cb, pattern=r"^forecast_"))
    app.add_handler(CallbackQueryHandler(watch_cb,    pattern=r"^(watch|unwatch)_"))
    app.add_handler(CallbackQueryHandler(adm_cb,      pattern=r"^adm_"))

    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_adm_msg), group=0)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_msg), group=1)

    async def post_init(application):
        asyncio.create_task(poller(application))
        asyncio.create_task(daily_push(application))

    app.post_init = post_init
    logger.info("ProqnozAI v5 started")
    app.run_polling()

if __name__ == "__main__":
    main()
