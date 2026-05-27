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
mostbet_cache: dict              = {}   # cache: key -> (timestamp, data)
MOSTBET_CACHE_TTL = 900           # 15 minutes cache
last_events:   dict[str, list]  = {}
ht_sent:       set              = set()

UNIVERSAL_WELCOME = """ProqnozAI

Azərbaycan: Dil seçin aşağıda
Русский: Выберите язык ниже
English: Choose language below
Türkçe: Aşağıdan dil seçin
Қазақша: Төменде тілді таңдаңыз
O'zbek: Quyida tilni tanlang
العربية: اختر اللغة أدناه
"""

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
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER, team TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, team)
        );
        CREATE TABLE IF NOT EXISTS forecast_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, query TEXT, forecast TEXT,
            match_name TEXT DEFAULT '',
            feedback INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS conversation (
            user_id INTEGER PRIMARY KEY,
            messages TEXT DEFAULT '[]',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds_alerts (
            user_id INTEGER, match_id TEXT, market TEXT,
            last_odd REAL, created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, match_id, market)
        );
        CREATE TABLE IF NOT EXISTS request_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, created_at TEXT DEFAULT (datetime('now'))
        );
        """)
db_init()

def detect_lang(tg_lang: str | None) -> str:
    """Map Telegram language_code to bot language."""
    if not tg_lang: return "ru"
    code = tg_lang.lower()[:2]
    mapping = {
        "az": "az", "ru": "ru", "uk": "ru", "be": "ru",
        "tr": "tr", "kk": "kz", "uz": "uz",
        "ar": "ar", "fa": "ar",
        "en": "en",
    }
    return mapping.get(code, "ru")

def db_ensure(uid, uname, tg_lang=None):
    lang = detect_lang(tg_lang)
    with con() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id,username,lang) VALUES (?,?,?)", (uid, uname, lang))

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

def db_restore_live_subs():
    with con() as c:
        rows = c.execute("SELECT user_id, match_id FROM live_subscriptions").fetchall()
    for uid, mid in rows:
        live_subs[mid].add(uid)
    if rows:
        logger.info(f"Restored {len(rows)} live subscriptions from DB")

# ─── Favorites ────────────────────────────────────────────────────────────────
def db_add_fav(uid, team):
    with con() as c: c.execute("INSERT OR IGNORE INTO favorites (user_id,team) VALUES (?,?)", (uid, team))

def db_del_fav(uid, team):
    with con() as c: c.execute("DELETE FROM favorites WHERE user_id=? AND team=?", (uid, team))

def db_get_favs(uid) -> list[str]:
    with con() as c:
        return [r[0] for r in c.execute("SELECT team FROM favorites WHERE user_id=?", (uid,)).fetchall()]

def db_is_fav(uid, team) -> bool:
    with con() as c:
        return bool(c.execute("SELECT 1 FROM favorites WHERE user_id=? AND team=?", (uid, team)).fetchone())

# ─── History ──────────────────────────────────────────────────────────────────
def db_save_history(uid, query, forecast, match_name=""):
    with con() as c:
        c.execute("INSERT INTO forecast_history (user_id,query,forecast,match_name) VALUES (?,?,?,?)",
                  (uid, query[:200], forecast[:2000], match_name))
        # Keep only last 10 per user
        c.execute("DELETE FROM forecast_history WHERE user_id=? AND id NOT IN "
                  "(SELECT id FROM forecast_history WHERE user_id=? ORDER BY id DESC LIMIT 10)",
                  (uid, uid))

def db_get_history(uid) -> list[dict]:
    with con() as c:
        rows = c.execute(
            "SELECT id,query,forecast,match_name,feedback,created_at FROM forecast_history "
            "WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    return [dict(id=r[0], query=r[1], forecast=r[2], match_name=r[3],
                 feedback=r[4], created_at=r[5]) for r in rows]

def db_set_feedback(history_id, feedback):
    with con() as c:
        c.execute("UPDATE forecast_history SET feedback=? WHERE id=?", (feedback, history_id))

def db_feedback_stats(uid) -> dict:
    with con() as c:
        total = c.execute("SELECT COUNT(*) FROM forecast_history WHERE user_id=? AND feedback IS NOT NULL", (uid,)).fetchone()[0]
        wins  = c.execute("SELECT COUNT(*) FROM forecast_history WHERE user_id=? AND feedback=1", (uid,)).fetchone()[0]
    return dict(total=total, wins=wins, pct=round(wins/total*100) if total > 0 else 0)

# ─── Conversation memory ─────────────────────────────────────────────────────

import json

def db_get_conv(uid) -> list:
    """Get last 3 conversation turns for context."""
    with con() as c:
        row = c.execute("SELECT messages FROM conversation WHERE user_id=?", (uid,)).fetchone()
    if not row: return []
    try: return json.loads(row[0])[-6:]  # last 3 turns (6 messages)
    except: return []

def db_save_conv(uid, messages: list):
    """Save conversation history (keep last 6 messages)."""
    trimmed = messages[-6:]
    with con() as c:
        c.execute("INSERT OR REPLACE INTO conversation (user_id, messages, updated_at) VALUES (?,?,datetime('now'))",
                  (uid, json.dumps(trimmed, ensure_ascii=False)))

def db_clear_conv(uid):
    with con() as c: c.execute("DELETE FROM conversation WHERE user_id=?", (uid,))

# ─── Request queue (concurrency limiter) ─────────────────────────────────────

request_semaphore = asyncio.Semaphore(5)  # max 5 concurrent Claude requests

# ─── Human-readable label maps ────────────────────────────────────────────────
SPORTS_LABELS = {
    "az": {"football": "Futbol", "ufc": "UFC/MMA", "nba": "Basketbol",
           "tennis": "Tennis", "hockey": "Hokey", "all": "Hamısı"},
    "ru": {"football": "Футбол", "ufc": "UFC/MMA", "nba": "Баскетбол",
           "tennis": "Теннис", "hockey": "Хоккей", "all": "Все виды"},
    "en": {"football": "Football", "ufc": "UFC/MMA", "nba": "Basketball",
           "tennis": "Tennis", "hockey": "Hockey", "all": "All sports"},
    "tr": {"football": "Futbol", "ufc": "UFC/MMA", "nba": "Basketbol",
           "tennis": "Tenis", "hockey": "Hokey", "all": "Tümü"},
    "kz": {"football": "Футбол", "ufc": "UFC/MMA", "nba": "Баскетбол",
           "tennis": "Теннис", "hockey": "Хоккей", "all": "Барлығы"},
    "uz": {"football": "Futbol", "ufc": "UFC/MMA", "nba": "Basketbol",
           "tennis": "Tennis", "hockey": "Xokkey", "all": "Barchasi"},
    "ar": {"football": "كرة القدم", "ufc": "UFC/MMA", "nba": "كرة السلة",
           "tennis": "تنس", "hockey": "هوكي", "all": "جميع الرياضات"},
}
EXP_LABELS = {
    "az": {"beginner": "Yeni başlayanam", "mid": "Orta səviyyə", "expert": "Təcrübəliyəm"},
    "ru": {"beginner": "Новичок", "mid": "Средний уровень", "expert": "Опытный"},
    "en": {"beginner": "Beginner", "mid": "Intermediate", "expert": "Expert"},
    "tr": {"beginner": "Yeni başlayan", "mid": "Orta seviye", "expert": "Deneyimli"},
    "kz": {"beginner": "Жаңадан бастаған", "mid": "Орта деңгей", "expert": "Тәжірибелі"},
    "uz": {"beginner": "Yangi boshlagan", "mid": "O'rta daraja", "expert": "Tajribali"},
    "ar": {"beginner": "مبتدئ", "mid": "متوسط", "expert": "خبير"},
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
"match_too_far": "Bu matç 1 həftədən uzaqdadır. Yalnız növbəti 7 gün ərzindəki matçlar üçün proqnoz verirəm.",
"choose_forecast": "Proqnoz növünü seçin:",
"btn_extended":    "Geniş proqnoz",
"btn_short":       "Qısa proqnoz",
"system_prompt": """Sən peşəkar idman analitikisən. Dürüst və real proqnozlar ver.

PROFİL: İdman: {sports} | Təcrübə: {exp}

VACIB QAYDALAR:
0. Əgər sorğuda "MOSTBET REAL KEFLƏRİ" varsa — YALNIZ bu kefləri istifadə et, öz keflerini UYDURMA
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
"fav_added":    "Sevimlilərə əlavə edildi: {team}",
"fav_removed":  "Sevimlilərdən silindi: {team}",
"fav_list":     "Sevimli komandalarınız:",
"fav_empty":    "Sevimli komandanız yoxdur. /fav yazın.",
"fav_btn_add":  "Sevimlilərə əlavə et",
"fav_btn_del":  "Sevimlilərdən sil",
"history_title":"Son proqnozlarınız:",
"history_empty":"Hələ proqnoz almamısınız.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Bu proqnoz oynadımı?",
"feedback_yes": "Bəli, oynadı!",
"feedback_no":  "Xeyr, oynamadı",
"feedback_done":"Təşəkkürlər! Statistika yeniləndi.",
"winrate":      "Proqnoz dəqiqliyi: {pct}% ({wins}/{total})",
"express_ask":  "Neçə matç? (2-5)",
"express_title":"Günün ekspresi:",
"compare_ask":  "İki komanda adını yazın. Məsələn: Barcelona Real Madrid",
"menu_history": "Tarix",
"menu_favs":    "Sevimlilər",
"menu_express": "Ekspress",

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
"match_too_far": "Этот матч слишком далеко. Я даю прогнозы только на матчи в ближайшие 7 дней.",
"choose_forecast": "Выберите формат прогноза:",
"btn_extended":    "Расширенный",
"btn_short":       "Краткий",
"system_prompt": """Ты — профессиональный спортивный аналитик. Твоя задача — давать честные, реалистичные прогнозы.

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ: Спорт: {sports} | Опыт: {exp}

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:
0. Если в запросе есть "РЕАЛЬНЫЕ КОЭФФИЦИЕНТЫ MOSTBET" — используй ТОЛЬКО эти коэффициенты, не придумывай свои
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
"fav_added": "Добавлено в избранное: {team}",
"fav_removed": "Удалено из избранного: {team}",
"fav_list": "Ваши избранные команды:",
"fav_empty": "Нет избранных команд. Напишите /fav.",
"fav_btn_add": "В избранное",
"fav_btn_del": "Убрать из избранного",
"history_title": "Ваши последние прогнозы:",
"history_empty": "У вас ещё нет прогнозов.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Этот прогноз сыграл?",
"feedback_yes": "Да, сыграло!",
"feedback_no": "Нет, не сыграло",
"feedback_done": "Спасибо! Статистика обновлена.",
"winrate": "Точность прогнозов: {pct}% ({wins}/{total})",
"express_ask": "Сколько матчей? (2-5)",
"express_title": "Экспресс дня:",
"compare_ask": "Напишите две команды. Например: Barcelona Real Madrid",
"menu_history": "История",
"menu_favs": "Избранное",
"menu_express": "Экспресс",

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
"match_too_far": "This match is too far ahead. I only give forecasts for matches within the next 7 days.",
"choose_forecast": "Choose forecast format:",
"btn_extended":    "Extended",
"btn_short":       "Short",
"system_prompt": """You are a professional sports analyst. Your job is to give honest, realistic forecasts.

USER PROFILE: Sports: {sports} | Experience: {exp}

CRITICAL RULES:
0. If the request contains "REAL MOSTBET ODDS" — use ONLY those odds, never invent your own
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
"fav_added": "Added to favourites: {team}",
"fav_removed": "Removed from favourites: {team}",
"fav_list": "Your favourite teams:",
"fav_empty": "No favourite teams yet. Type /fav.",
"fav_btn_add": "Add to favourites",
"fav_btn_del": "Remove from favourites",
"history_title": "Your recent forecasts:",
"history_empty": "No forecasts yet.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Did this forecast hit?",
"feedback_yes": "Yes, it hit!",
"feedback_no": "No, it missed",
"feedback_done": "Thanks! Stats updated.",
"winrate": "Forecast accuracy: {pct}% ({wins}/{total})",
"express_ask": "How many matches? (2-5)",
"express_title": "Express of the day:",
"compare_ask": "Type two teams. Example: Barcelona Real Madrid",
"menu_history": "History",
"menu_favs": "Favourites",
"menu_express": "Express",

},
"tr": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Hoş geldiniz! Adınızı girin:",
"reg_done":      "Kayıt tamamlandı! Merhaba, {name}!",
"already_reg":   "Zaten kayıtlısınız, {name}!",
"need_reg":      "Önce kayıt olun. /start yazın.",
"db_blocked":    "Hesabınız engellendi. Yöneticiye başvurun.",
"blocked":       "Geçici olarak engellendi. {m} dk {s} sn sonra tekrar deneyin.",
"rate_limit":    "İstek limiti aşıldı. {w} saniye bekleyin. Uyarı: {v}/{max}",
"auto_blocked":  "Çok fazla istek. {min} dakika engel.",
"long_text":     "Metin çok uzun.",
"injection":     "Yalnızca spor sorguları kabul edilir.",
"no_input":      "Metin yazın veya fotoğraf gönderin.",
"img_prompt":    "Görseldeki spor etkinliğini belirle ve tahmin ver.",
"api_overload":  "Servis aşırı yüklendi. Lütfen daha sonra tekrar deneyin.",
"api_error":     "Bir hata oluştu. Lütfen daha sonra tekrar deneyin.",
"lang_set":      "Dil Türkçe olarak ayarlandı.",
"watch_btn":     "Maçı takip et",
"watch_started": "Maç takip ediliyor: {match}",
"watch_stopped": "Takip durduruldu: {match}",
"no_subs":       "Hiçbir maçı takip etmiyorsunuz.",
"live_goal":     "GOL! {match}\n{minute}. dk: {team}\nSkor: {score}\n\nCanlı bahis:\n{tip}",
"live_card":     "KART! {match}\n{minute}. dk: {player} ({team}) - {card}\n\nCanlı bahis:\n{tip}",
"live_halftime": "DEVRE ARASI! {match}\nSkor: {score}\n\nDevre arası bahsi:\n{tip}",
"live_fulltime": "MAÇ BİTTİ! {match}\nSonuç: {score}",
"live_alert_goal":     "SİNYAL! {match} - Gol bekleniyor [{minute}. dk]",
"live_alert_value":    "CANLI DEĞER! {match} - {team} üzerinde değer var [{minute}. dk]",
"live_alert_pressure": "BASKI! {match} - {team} güçlü baskı yapıyor [{minute}. dk] {stat}",
"menu_forecast": "Tahmin al",
"menu_matches":  "Maçlarım",
"menu_profile":  "Profil",
"menu_lang":     "Dil değiştir",
"profile_text":  "PROFİL\n\nAd: {name}\nDil: {lang}\nToplam istek: {total_req}\n\nSpor: {sports}\nDeneyim: {exp}",
"ob_sports":     "En çok hangi sporu seviyorsunuz?",
"ob_exp":        "Bahis deneyiminiz nedir?",
"ob_done":       "Profil hazır! Kişiselleştirilmiş tahminler alacaksınız.\n\nSpor: {sports}\nDeneyim: {exp}",
"welcome_intro": """ProqnozAI'ye hoş geldiniz!

Ben bir AI spor bahis analistiyim. Yapabileceklerim:

• Herhangi bir maç için tahmin — takım adlarını yazın veya program fotoğrafı gönderin
• Genişletilmiş analiz — form, faktörler, tüm bahis türleri oranlarla
• Kısa tahmin — 5 saniyede sadece ana bahis
• Canlı bildirimler — maçı takip eder, olayları gerçek zamanlı gönderirim

2 hızlı soruyu yanıtlayın — tahminleri kişiselleştireyim.""",
"post_onboarding": "Hazır! Şimdi bir maç adı yazın — örneğin:\n\nBarcelona Alavés\nReal Madrid Arsenal\nPSG Manchester City\n\nYa da maç programının fotoğrafını gönderin.",
"match_too_far": "Bu maç çok uzakta. Yalnızca önümüzdeki 7 gün içindeki maçlar için tahmin yapıyorum.",
"choose_forecast": "Tahmin formatını seçin:",
"btn_extended":    "Genişletilmiş",
"btn_short":       "Kısa",
"system_prompt": """Sen profesyonel bir spor analistisin. Dürüst ve gerçekçi tahminler ver.

PROFİL: Spor: {sports} | Deneyim: {exp}

KRİTİK KURALLAR:
0. Eğer istekte "GERÇEK MOSTBET ORANLAR" varsa — YALNIZCA bu oranları kullan
1. Kulübü terk eden oyuncuları HİÇBİR ZAMAN belirtme
2. Güncel kadroyu bilmiyorsan "kadro kontrol ediliyor" yaz — UYDURMA
3. Oranlar GERÇEKÇI olmalı: favori 1.20-1.60, eşit 2.20-3.00, toplam 2.5 1.70-2.10
4. Markdown ** KULLANMA — yalnızca metin ve emoji

FORMAT:

🏆 [Takım A] — [Takım B]
📍 [Turnuva] | [Tarih]

📊 FORM (son 5 maç):
[Takım A]: [sonuçlar]
[Takım B]: [sonuçlar]

🔍 ANALİZ:
[Maça gerçekten etkileyen 3-4 faktör]

🎯 TAHMİN:

1X2:
[Takım A] — XX% | Oran: X.XX-X.XX
Beraberlik — XX% | Oran: X.XX-X.XX
[Takım B] — XX% | Oran: X.XX-X.XX

⚽ Toplam Gol:
2.5 Üstü — XX% | Oran: X.XX-X.XX
2.5 Altı — XX% | Oran: X.XX-X.XX

🔥 İki Takım da Gol Atar:
Evet — XX% | Oran: X.XX-X.XX
Hayır — XX% | Oran: X.XX-X.XX

📐 Handikap:
[Takım A] (-1) — XX% | Oran: X.XX-X.XX
[Takım B] (+1) — XX% | Oran: X.XX-X.XX

⚡ EN İYİ BAHİS:
[Bahis türü] | Oran: X.XX-X.XX
[Neden bu bahis — 1-2 cümle]

⚠️ Analitik tahmin, sonuç garantisi değildir.""",
"short_prompt": """Kısa tahmin. Profil: {sports} | {exp}
KURALLAR: gerçekçi oranlar, ayrılan oyuncuları belirtme, yalnızca metin ve emoji.
FORMAT:
🏆 [Takım A] — [Takım B] | [Turnuva]
🎯 Favori: [Takım] XX% | Oran X.XX-X.XX
⚽ 2.5 Üstü: XX% | Oran X.XX-X.XX
🔥 İTGO Evet: XX% | Oran X.XX-X.XX
⚡ BAHİS: [tür] | Oran X.XX-X.XX
[1 cümle]
⚠️ Analitik tahmin.""",
"live_tip_prompt": "Canlı bahis analistisin. Maç {match}, {minute}. dk, skor {score}. Olay: {event}. En iyi canlı bahsi öner. Kısa, max 2 cümle.",
"fav_added": "Favorilere eklendi: {team}",
"fav_removed": "Favorilerden kaldırıldı: {team}",
"fav_list": "Favori takımlarınız:",
"fav_empty": "Favori takımınız yok. /fav yazın.",
"fav_btn_add": "Favorilere ekle",
"fav_btn_del": "Favorilerden kaldır",
"history_title": "Son tahminleriniz:",
"history_empty": "Henüz tahmin yok.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Bu tahmin tuttu mu?",
"feedback_yes": "Evet, tuttu!",
"feedback_no": "Hayır, tutmadı",
"feedback_done": "Teşekkürler! İstatistik güncellendi.",
"winrate": "Tahmin doğruluğu: {pct}% ({wins}/{total})",
"express_ask": "Kaç maç? (2-5)",
"express_title": "Günün ekspresi:",
"compare_ask": "İki takım adı yazın. Örnek: Barcelona Real Madrid",
"menu_history": "Geçmiş",
"menu_favs": "Favoriler",
"menu_express": "Ekspres",

},

"kz": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Қош келдіңіз! Атыңызды енгізіңіз:",
"reg_done":      "Тіркеу аяқталды! Сәлем, {name}!",
"already_reg":   "Сіз бұрыннан тіркелгенсіз, {name}!",
"need_reg":      "Алдымен тіркеліңіз. /start жазыңыз.",
"db_blocked":    "Сіздің аккаунтыңыз бұғатталған. Әкімшіге хабарласыңыз.",
"blocked":       "Уақытша бұғатталдыңыз. {m} мин {s} сек кейін қайталаңыз.",
"rate_limit":    "Сұраныс шегі асылды. {w} секунд күтіңіз. Ескерту: {v}/{max}",
"auto_blocked":  "Тым көп сұраныс. {min} минуттық бұғат.",
"long_text":     "Мәтін тым ұзын.",
"injection":     "Тек спорт сұраныстары қабылданады.",
"no_input":      "Мәтін жазыңыз немесе фото жіберіңіз.",
"img_prompt":    "Суреттегі спорт оқиғасын анықта және болжам бер.",
"api_overload":  "Қызмет шамадан тыс жүктелді. Кейінірек қайталаңыз.",
"api_error":     "Қате орын алды. Кейінірек қайталаңыз.",
"lang_set":      "Тіл қазақша деп орнатылды.",
"watch_btn":     "Матчты бақылау",
"watch_started": "Матч бақылануда: {match}",
"watch_stopped": "Бақылау тоқтатылды: {match}",
"no_subs":       "Сіз ешбір матчты бақыламайсыз.",
"live_goal":     "ГОЛ! {match}\n{minute} мин: {team}\nЕсеп: {score}\n\nТікелей эфир ставкасы:\n{tip}",
"live_card":     "КАРТОЧКА! {match}\n{minute} мин: {player} ({team}) - {card}\n\nТікелей ставка:\n{tip}",
"live_halftime": "ҮЗІЛІС! {match}\nЕсеп: {score}\n\nҮзіліс ставкасы:\n{tip}",
"live_fulltime": "МАЧ АЯҚТАЛДЫ! {match}\nҚорытынды: {score}",
"live_alert_goal":     "СИГНАЛ! {match} - гол күтілуде [{minute} мин]",
"live_alert_value":    "ТІКЕЛЕЙ ЭФИР! {match} - {team} бойынша мән бар [{minute} мин]",
"live_alert_pressure": "ҚЫСЫМ! {match} - {team} күшті шабуыл [{minute} мин] {stat}",
"menu_forecast": "Болжам алу",
"menu_matches":  "Матчтарым",
"menu_profile":  "Профиль",
"menu_lang":     "Тілді өзгерту",
"profile_text":  "ПРОФИЛЬ\n\nАты: {name}\nТіл: {lang}\nЖалпы сұраныстар: {total_req}\n\nСпорт: {sports}\nТәжірибе: {exp}",
"ob_sports":     "Қандай спортты ұнатасыз?",
"ob_exp":        "Ставкалардағы тәжірибеңіз қандай?",
"ob_done":       "Профиль дайын! Жекелендірілген болжамдар аласыз.\n\nСпорт: {sports}\nТәжірибе: {exp}",
"welcome_intro": """ProqnozAI-ге қош келдіңіз!

Мен AI спорт ставкалар аналитигімін. Не істей аламын:

• Кез келген матч болжамы — командалар атын жазыңыз немесе кесте фотосын жіберіңіз
• Толық талдау — форма, факторлар, барлық ставка түрлері коэффициенттермен
• Қысқа болжам — 5 секундта негізгі ставка
• Тікелей хабарламалар — матчты бақылаймын, оқиғаларды нақты уақытта жіберемін

2 сұраққа жауап беріңіз — болжамдарды жекелендіремін.""",
"post_onboarding": "Дайын! Матч атын жазыңыз — мысалы:\n\nБарселона Алавес\nРеал Мадрид Арсенал\nПСЖ Манчестер Сити\n\nНемесе матч кестесінің фотосын жіберіңіз.",
"match_too_far": "Бұл матч тым алыс. Мен тек келесі 7 күн ішіндегі матчтарға болжам беремін.",
"choose_forecast": "Болжам форматын таңдаңыз:",
"btn_extended":    "Толық",
"btn_short":       "Қысқаша",
"system_prompt": """Сен кәсіби спорт аналитикісің. Адал және нақты болжамдар бер.

ПРОФИЛЬ: Спорт: {sports} | Тәжірибе: {exp}

МАҢЫЗДЫ ЕРЕЖЕЛЕР:
0. Егер сұранымда "НАҚТЫ MOSTBET КОЭФФИЦИЕНТТЕРІ" болса — ТЕК осыларды қолдан
1. Клубты тастаған ойыншыларды ЕШҚАшан атама
2. Қолданыстағы құраманы білмесең "құрам тексерілуде" деп жаз — ОЙДАН ШЫҒАРМА
3. Коэффициенттер НАҚТЫ болуы керек: фаворит 1.20-1.60, тең 2.20-3.00, тотал 2.5 1.70-2.10
4. Markdown ** ҚОЛДАНБА — тек мәтін және emoji

ФОРМАТ:

🏆 [Команда А] — [Команда Б]
📍 [Турнир] | [Күні]

📊 ФОРМА (соңғы 5 матч):
[Команда А]: [нәтижелер]
[Команда Б]: [нәтижелер]

🔍 ТАЛДАУ:
[Матчқа нақты әсер ететін 3-4 фактор]

🎯 БОЛЖАМ:

1X2:
[Команда А] — XX% | Коэф: X.XX-X.XX
Тең — XX% | Коэф: X.XX-X.XX
[Команда Б] — XX% | Коэф: X.XX-X.XX

⚽ Жалпы гол:
2.5 Жоғары — XX% | Коэф: X.XX-X.XX
2.5 Төмен — XX% | Коэф: X.XX-X.XX

🔥 Екі команда да гол соғады:
Иә — XX% | Коэф: X.XX-X.XX
Жоқ — XX% | Коэф: X.XX-X.XX

⚡ ЕҢ ЖАҚСЫ СТАВКА:
[Ставка түрі] | Коэф: X.XX-X.XX
[Неге дәл осы ставка — 1-2 сөйлем]

⚠️ Аналитикалық болжам, нәтиже кепілі емес.""",
"short_prompt": """Қысқа болжам. Профиль: {sports} | {exp}
ЕРЕЖЕЛЕР: нақты коэффициенттер, кеткен ойыншыларды атама, тек мәтін және emoji.
FORMAT:
🏆 [А] — [Б] | [Турнир]
🎯 Фаворит: [Команда] XX% | Коэф X.XX-X.XX
⚽ 2.5 жоғары: XX% | Коэф X.XX-X.XX
🔥 Екеуі де: Иә XX% | Коэф X.XX-X.XX
⚡ СТАВКА: [түрі] | Коэф X.XX-X.XX
[1 сөйлем]
⚠️ Аналитикалық болжам.""",
"live_tip_prompt": "Тікелей ставка аналитигісің. Матч {match}, {minute} мин, есеп {score}. Оқиға: {event}. Үздік тікелей ставканы ұсын. Қысқа, максимум 2 сөйлем.",
"fav_added": "Таңдаулыларға қосылды: {team}",
"fav_removed": "Таңдаулылардан жойылды: {team}",
"fav_list": "Таңдаулы командаларыңыз:",
"fav_empty": "Таңдаулы командалар жоқ. /fav жазыңыз.",
"fav_btn_add": "Таңдаулыларға қос",
"fav_btn_del": "Таңдаулылардан алып тастау",
"history_title": "Соңғы болжамдарыңыз:",
"history_empty": "Болжамдар жоқ.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Бұл болжам ойнады ма?",
"feedback_yes": "Иә, ойнады!",
"feedback_no": "Жоқ, ойнамады",
"feedback_done": "Рахмет! Статистика жаңартылды.",
"winrate": "Болжам дәлдігі: {pct}% ({wins}/{total})",
"express_ask": "Неше матч? (2-5)",
"express_title": "Күннің экспресі:",
"compare_ask": "Екі команда атын жазыңыз. Мысалы: Barcelona Real Madrid",
"menu_history": "Тарих",
"menu_favs": "Таңдаулылар",
"menu_express": "Экспресс",

},

"uz": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Xush kelibsiz! Ismingizni kiriting:",
"reg_done":      "Ro'yxatdan o'tish yakunlandi! Salom, {name}!",
"already_reg":   "Siz allaqachon ro'yxatdan o'tgansiz, {name}!",
"need_reg":      "Avval ro'yxatdan o'ting. /start yozing.",
"db_blocked":    "Hisobingiz bloklangan. Administrator bilan bog'laning.",
"blocked":       "Vaqtincha bloklandi. {m} daq {s} soniyadan keyin qayta urinib ko'ring.",
"rate_limit":    "So'rovlar limiti oshib ketdi. {w} soniya kuting. Ogohlantirish: {v}/{max}",
"auto_blocked":  "Juda ko'p so'rovlar. {min} daqiqalik blok.",
"long_text":     "Matn juda uzun.",
"injection":     "Faqat sport so'rovlari qabul qilinadi.",
"no_input":      "Matn yozing yoki rasm yuboring.",
"img_prompt":    "Rasmdagi sport tadbirini aniqlang va bashorat bering.",
"api_overload":  "Xizmat haddan tashqari yuklangan. Keyinroq urinib ko'ring.",
"api_error":     "Xatolik yuz berdi. Keyinroq urinib ko'ring.",
"lang_set":      "Til o'zbek tiliga o'rnatildi.",
"watch_btn":     "O'yinni kuzatish",
"watch_started": "O'yin kuzatilmoqda: {match}",
"watch_stopped": "Kuzatish to'xtatildi: {match}",
"no_subs":       "Siz hech qanday o'yinni kuzatmayapsiz.",
"live_goal":     "GOL! {match}\n{minute} daq: {team}\nHisob: {score}\n\nLive stavka:\n{tip}",
"live_card":     "KARTOCHKA! {match}\n{minute} daq: {player} ({team}) - {card}\n\nLive stavka:\n{tip}",
"live_halftime": "TANAFFUS! {match}\nHisob: {score}\n\nTanaffus stavkasi:\n{tip}",
"live_fulltime": "O'YIN TUGADI! {match}\nYakuniy: {score}",
"live_alert_goal":     "SIGNAL! {match} - gol kutilmoqda [{minute} daq]",
"live_alert_value":    "LIVE VALUE! {match} - {team} bo'yicha qiymat bor [{minute} daq]",
"live_alert_pressure": "BOSIM! {match} - {team} kuchli hujum [{minute} daq] {stat}",
"menu_forecast": "Bashorat olish",
"menu_matches":  "Mening o'yinlarim",
"menu_profile":  "Profil",
"menu_lang":     "Tilni o'zgartirish",
"profile_text":  "PROFIL\n\nIsm: {name}\nTil: {lang}\nJami so'rovlar: {total_req}\n\nSport: {sports}\nTajriba: {exp}",
"ob_sports":     "Qaysi sportni yaxshi ko'rasiz?",
"ob_exp":        "Stavkalardagi tajribangiz qanday?",
"ob_done":       "Profil tayyor! Shaxsiylashtirilgan bashoratlar olasiz.\n\nSport: {sports}\nTajriba: {exp}",
"welcome_intro": """ProqnozAI-ga xush kelibsiz!

Men AI sport stavkalari analitikiman. Nima qila olaman:

• Istalgan o'yin uchun bashorat — jamoa nomini yozing yoki jadval rasmini yuboring
• Kengaytirilgan tahlil — shakl, omillar, barcha stavka turlari koeffitsientlar bilan
• Qisqa bashorat — 5 soniyada asosiy stavka
• Jonli bildirishnomalar — o'yinni kuzataman, voqealarni real vaqtda yuboraman

2 ta tezkor savolga javob bering — bashoratlarni shaxsiylashtiraman.""",
"post_onboarding": "Tayyor! O'yin nomini yozing — masalan:\n\nBarcelona Alavés\nReal Madrid Arsenal\nPSG Manchester City\n\nYoki o'yin jadvalining rasmini yuboring.",
"match_too_far": "Bu o'yin juda uzoqda. Men faqat keyingi 7 kun ichidagi o'yinlar uchun bashorat beraman.",
"choose_forecast": "Bashorat formatini tanlang:",
"btn_extended":    "Kengaytirilgan",
"btn_short":       "Qisqa",
"system_prompt": """Sen professional sport analitikisisan. Halol va real bashoratlar ber.

PROFIL: Sport: {sports} | Tajriba: {exp}

MUHIM QOIDALAR:
0. Agar so'rovda "HAQIQIY MOSTBET KOEFFITSIENTLARI" bo'lsa — FAQAT shularni ishlatish
1. Klubni tark etgan o'yinchilarni HECH QACHON eslatma
2. Joriy tarkibni bilmasang "tarkib tekshirilmoqda" deb yoz — O'YLAB TOPMA
3. Koeffitsientlar REAL bo'lishi kerak: favorit 1.20-1.60, teng 2.20-3.00, total 2.5 1.70-2.10
4. Markdown ** ISHLATMA — faqat matn va emoji

FORMAT:

🏆 [Jamoa A] — [Jamoa B]
📍 [Turnir] | [Sana]

📊 SHAKL (oxirgi 5 o'yin):
[Jamoa A]: [natijalar]
[Jamoa B]: [natijalar]

🔍 TAHLIL:
[O'yinga haqiqatan ta'sir qiluvchi 3-4 omil]

🎯 BASHORAT:

1X2:
[Jamoa A] — XX% | Koef: X.XX-X.XX
Durrang — XX% | Koef: X.XX-X.XX
[Jamoa B] — XX% | Koef: X.XX-X.XX

⚽ Jami gol:
2.5 dan yuqori — XX% | Koef: X.XX-X.XX
2.5 dan past — XX% | Koef: X.XX-X.XX

🔥 Ikkala jamoa ham gol uradi:
Ha — XX% | Koef: X.XX-X.XX
Yo'q — XX% | Koef: X.XX-X.XX

⚡ ENG YAXSHI STAVKA:
[Stavka turi] | Koef: X.XX-X.XX
[Nima uchun — 1-2 jumla]

⚠️ Tahliliy bashorat, natija kafolati emas.""",
"short_prompt": """Qisqa bashorat. Profil: {sports} | {exp}
QOIDALAR: real koeffitsientlar, ketgan o'yinchilarni eslatma, faqat matn va emoji.
FORMAT:
🏆 [A] — [B] | [Turnir]
🎯 Favorit: [Jamoa] XX% | Koef X.XX-X.XX
⚽ 2.5 yuqori: XX% | Koef X.XX-X.XX
🔥 Ikkala ham: Ha XX% | Koef X.XX-X.XX
⚡ STAVKA: [turi] | Koef X.XX-X.XX
[1 jumla]
⚠️ Tahliliy bashorat.""",
"live_tip_prompt": "Sen jonli stavkalar analitikisisan. O'yin {match}, {minute} daq, hisob {score}. Voqea: {event}. Eng yaxshi jonli stavkani tavsiya et. Qisqa, max 2 jumla.",
"fav_added": "Sevimlilariga qo'shildi: {team}",
"fav_removed": "Sevimlilardan o'chirildi: {team}",
"fav_list": "Sevimli jamoalaringiz:",
"fav_empty": "Sevimli jamoalar yo'q. /fav yozing.",
"fav_btn_add": "Sevimlilarga qo'shish",
"fav_btn_del": "Sevimlilardan o'chirish",
"history_title": "Oxirgi bashoratlaringiz:",
"history_empty": "Bashoratlar yo'q.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Bu bashorat o'yndimi?",
"feedback_yes": "Ha, o'ynadi!",
"feedback_no": "Yo'q, o'ynamadi",
"feedback_done": "Rahmat! Statistika yangilandi.",
"winrate": "Bashorat aniqligi: {pct}% ({wins}/{total})",
"express_ask": "Nechta o'yin? (2-5)",
"express_title": "Kunning ekspressi:",
"compare_ask": "Ikki jamoa nomini yozing. Masalan: Barcelona Real Madrid",
"menu_history": "Tarix",
"menu_favs": "Sevimlilar",
"menu_express": "Ekspress",

},

"ar": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "مرحباً! أدخل اسمك:",
"reg_done":      "اكتمل التسجيل! مرحباً، {name}!",
"already_reg":   "أنت مسجل بالفعل، {name}!",
"need_reg":      "سجّل أولاً. اكتب /start.",
"db_blocked":    "حسابك محظور. تواصل مع المسؤول.",
"blocked":       "محظور مؤقتاً. حاول بعد {m} دقيقة {s} ثانية.",
"rate_limit":    "تجاوزت حد الطلبات. انتظر {w} ثانية. تحذير: {v}/{max}",
"auto_blocked":  "طلبات كثيرة جداً. حظر لمدة {min} دقيقة.",
"long_text":     "النص طويل جداً.",
"injection":     "يُقبل فقط استفسارات رياضية.",
"no_input":      "اكتب نصاً أو أرسل صورة.",
"img_prompt":    "حدد الحدث الرياضي في الصورة وقدّم توقعاً.",
"api_overload":  "الخدمة مثقلة. حاول لاحقاً.",
"api_error":     "حدث خطأ. حاول لاحقاً.",
"lang_set":      "تم ضبط اللغة على العربية.",
"watch_btn":     "تابع المباراة",
"watch_started": "جارٍ متابعة: {match}",
"watch_stopped": "توقفت المتابعة: {match}",
"no_subs":       "لا تتابع أي مباراة.",
"live_goal":     "هدف! {match}\n{minute} د: {team}\nالنتيجة: {score}\n\nرهان مباشر:\n{tip}",
"live_card":     "بطاقة! {match}\n{minute} د: {player} ({team}) - {card}\n\nرهان مباشر:\n{tip}",
"live_halftime": "نهاية الشوط! {match}\nالنتيجة: {score}\n\nرهان الاستراحة:\n{tip}",
"live_fulltime": "انتهت المباراة! {match}\nالنتيجة النهائية: {score}",
"live_alert_goal":     "إشارة! {match} - هدف متوقع [{minute} د]",
"live_alert_value":    "قيمة مباشرة! {match} - قيمة على {team} [{minute} د]",
"live_alert_pressure": "ضغط! {match} - {team} يضغط بقوة [{minute} د] {stat}",
"menu_forecast": "احصل على توقع",
"menu_matches":  "مبارياتي",
"menu_profile":  "الملف الشخصي",
"menu_lang":     "تغيير اللغة",
"profile_text":  "الملف الشخصي\n\nالاسم: {name}\nاللغة: {lang}\nإجمالي الطلبات: {total_req}\n\nالرياضة: {sports}\nالخبرة: {exp}",
"ob_sports":     "ما هي الرياضة المفضلة لديك؟",
"ob_exp":        "ما هي خبرتك في الرهانات؟",
"ob_done":       "الملف جاهز! ستحصل على توقعات مخصصة.\n\nالرياضة: {sports}\nالخبرة: {exp}",
"welcome_intro": """مرحباً بك في ProqnozAI!

أنا محلل رهانات رياضية بالذكاء الاصطناعي. ما أستطيع فعله:

• توقع لأي مباراة — اكتب أسماء الفرق أو أرسل صورة الجدول
• تحليل موسع — الشكل، العوامل، جميع أنواع الرهانات مع الأرباح
• توقع قصير — الرهان الرئيسي في 5 ثوانٍ
• تنبيهات مباشرة — أتابع المباراة وأرسل الأحداث فورياً

أجب على سؤالين سريعين لأخصص التوقعات لك.""",
"post_onboarding": "جاهز! اكتب الآن اسم المباراة — مثلاً:\n\nبرشلونة ألافيس\nريال مدريد آرسنال\nPSG مانشستر سيتي\n\nأو أرسل صورة جدول المباريات.",
"match_too_far": "هذه المباراة بعيدة جداً. أقدم التوقعات فقط للمباريات خلال الأيام السبعة القادمة.",
"choose_forecast": "اختر تنسيق التوقع:",
"btn_extended":    "موسع",
"btn_short":       "مختصر",
"system_prompt": """أنت محلل رياضي محترف. قدم توقعات صادقة وواقعية.

الملف: الرياضة: {sports} | الخبرة: {exp}

قواعد حرجة:
0. إذا كان الطلب يحتوي على "أرباح موستبت الحقيقية" — استخدم هذه الأرباح فقط
1. لا تذكر أبداً لاعبين غادروا الناديكن
2. إذا كنت لا تعرف التشكيلة الحالية اكتب "قيد المراجعة" — لا تخترع
3. الأرباح يجب أن تكون واقعية: المفضل 1.20-1.60، متكافئ 2.20-3.00، الأهداف 2.5 بين 1.70-2.10
4. لا تستخدم Markdown ** — نص عادي وإيموجي فقط

الصيغة:

🏆 [الفريق أ] — [الفريق ب]
📍 [البطولة] | [التاريخ]

📊 الشكل (آخر 5 مباريات):
[الفريق أ]: [النتائج]
[الفريق ب]: [النتائج]

🔍 التحليل:
[3-4 عوامل تؤثر فعلاً على المباراة]

🎯 التوقع:

1X2:
[الفريق أ] — XX% | ربح: X.XX-X.XX
تعادل — XX% | ربح: X.XX-X.XX
[الفريق ب] — XX% | ربح: X.XX-X.XX

⚽ إجمالي الأهداف:
أكثر من 2.5 — XX% | ربح: X.XX-X.XX
أقل من 2.5 — XX% | ربح: X.XX-X.XX

🔥 كلا الفريقين يسجل:
نعم — XX% | ربح: X.XX-X.XX
لا — XX% | ربح: X.XX-X.XX

⚡ أفضل رهان:
[نوع الرهان] | ربح: X.XX-X.XX
[السبب — جملة أو جملتان]

⚠️ توقع تحليلي، ليس ضماناً للنتيجة.""",
"short_prompt": """توقع قصير. الملف: {sports} | {exp}
القواعد: أرباح واقعية، لا تذكر لاعبين مغادرين، نص وإيموجي فقط.
الصيغة:
🏆 [أ] — [ب] | [البطولة]
🎯 المفضل: [الفريق] XX% | ربح X.XX-X.XX
⚽ أكثر 2.5: XX% | ربح X.XX-X.XX
🔥 كلاهما يسجل: نعم XX% | ربح X.XX-X.XX
⚡ الرهان: [النوع] | ربح X.XX-X.XX
[جملة واحدة]
⚠️ توقع تحليلي.""",
"live_tip_prompt": "أنت محلل رهانات مباشرة. المباراة {match}، الدقيقة {minute}، النتيجة {score}. الحدث: {event}. اقترح أفضل رهان مباشر. مختصر، جملتان كحد أقصى.",
"fav_added": "أضيف إلى المفضلة: {team}",
"fav_removed": "حُذف من المفضلة: {team}",
"fav_list": "فرقك المفضلة:",
"fav_empty": "لا توجد فرق مفضلة. اكتب /fav.",
"fav_btn_add": "إضافة للمفضلة",
"fav_btn_del": "حذف من المفضلة",
"history_title": "توقعاتك الأخيرة:",
"history_empty": "لا توجد توقعات بعد.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "هل نجح هذا التوقع؟",
"feedback_yes": "نعم، نجح!",
"feedback_no": "لا، لم ينجح",
"feedback_done": "شكراً! تم تحديث الإحصاء.",
"winrate": "دقة التوقعات: {pct}% ({wins}/{total})",
"express_ask": "كم مباراة؟ (2-5)",
"express_title": "إكسبريس اليوم:",
"compare_ask": "اكتب اسمي الفريقين. مثال: Barcelona Real Madrid",
"menu_history": "السجل",
"menu_favs": "المفضلة",
"menu_express": "إكسبريس",

},

}

LANG_NAMES = {"az": "Azerbaycan", "ru": "Русский", "en": "English"}

def tr(uid, key, **kw):
    lang = db_lang(uid)
    # Fallback chain: current lang -> ru -> en -> empty string
    txt = T.get(lang, {}).get(key) or T.get("ru", {}).get(key) or T.get("en", {}).get(key, "")
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
    "tr": [("Futbol", "football"), ("UFC/MMA", "ufc"), ("Basketbol", "nba"),
           ("Tenis", "tennis"), ("Hokey", "hockey"), ("Tümü", "all")],
    "kz": [("Футбол", "football"), ("UFC/MMA", "ufc"), ("Баскетбол", "nba"),
           ("Теннис", "tennis"), ("Хоккей", "hockey"), ("Барлығы", "all")],
    "uz": [("Futbol", "football"), ("UFC/MMA", "ufc"), ("Basketbol", "nba"),
           ("Tennis", "tennis"), ("Xokkey", "hockey"), ("Barchasi", "all")],
    "ar": [("كرة القدم", "football"), ("UFC/MMA", "ufc"), ("كرة السلة", "nba"),
           ("تنس", "tennis"), ("هوكي", "hockey"), ("جميع الرياضات", "all")],
}
OB_EXP = {
    "az": [("Yeni başlayanam", "beginner"), ("Orta səviyyə", "mid"), ("Təcrübəliyəm", "expert")],
    "ru": [("Новичок", "beginner"), ("Средний уровень", "mid"), ("Опытный", "expert")],
    "en": [("Beginner", "beginner"), ("Intermediate", "mid"), ("Expert", "expert")],
    "tr": [("Yeni başlayan", "beginner"), ("Orta seviye", "mid"), ("Deneyimli", "expert")],
    "kz": [("Жаңадан бастаған", "beginner"), ("Орта деңгей", "mid"), ("Тәжірибелі", "expert")],
    "uz": [("Yangi boshlagan", "beginner"), ("O'rta daraja", "mid"), ("Tajribali", "expert")],
    "ar": [("مبتدئ", "beginner"), ("متوسط", "mid"), ("خبير", "expert")],
}

def ob_kb(items):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"ob_{val}")] for label, val in items])

# ─── Main menu ────────────────────────────────────────────────────────────────
def main_menu(uid):
    lang = db_lang(uid)
    tl = T[lang]
    return ReplyKeyboardMarkup([
        [tl["menu_forecast"],  tl["menu_express"]],
        [tl["menu_history"],   tl["menu_favs"]],
        [tl["menu_matches"],   tl["menu_profile"]],
        [tl["menu_lang"]],
    ], resize_keyboard=True)

def lang_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Azərbaycan", callback_data="lang_az"),
            InlineKeyboardButton("Русский",    callback_data="lang_ru"),
            InlineKeyboardButton("English",    callback_data="lang_en"),
        ],
        [
            InlineKeyboardButton("Türkçe",     callback_data="lang_tr"),
            InlineKeyboardButton("Қазақша",    callback_data="lang_kz"),
            InlineKeyboardButton("O'zbek",     callback_data="lang_uz"),
        ],
        [
            InlineKeyboardButton("العربية",    callback_data="lang_ar"),
        ],
    ])

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
        r = await asyncio.to_thread(
            client.messages.create, model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role": "user", "content": p}])
        return r.content[0].text
    except Exception: return ""


# ─── Mostbet Odds Checker API ─────────────────────────────────────────────────

async def _mostbet_load_matches() -> list:
    """Load all matches from Mostbet with caching (10 min TTL).
    Returns cached data if available, even if stale, on 429."""
    cache_key = "all_matches"
    now = time.time()

    # Return fresh cache
    if cache_key in mostbet_cache:
        ts, data = mostbet_cache[cache_key]
        if now - ts < MOSTBET_CACHE_TTL:
            return data

    all_matches = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
            last_id = 0
            page = 0
            while True:
                # Rate limit: wait between pages
                if page > 0:
                    await asyncio.sleep(1.0)
                page += 1
                r = await h.get(
                    f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                    headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                    params={"lastId": last_id, "locale": "en", "limit": 100}
                )
                if r.status_code == 429:
                    logger.warning(f"Mostbet 429 on page {page}")
                    # Return stale cache if exists
                    if cache_key in mostbet_cache:
                        _, stale = mostbet_cache[cache_key]
                        logger.info(f"Returning stale cache: {len(stale)} matches")
                        return stale
                    # Wait and retry once
                    await asyncio.sleep(3)
                    r2 = await h.get(
                        f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                        headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                        params={"lastId": 0, "locale": "en", "limit": 100}
                    )
                    if r2.status_code == 200:
                        matches = r2.json().get("lineMatches", [])
                        all_matches.extend(matches)
                    break
                if r.status_code != 200:
                    logger.error(f"Mostbet list error: {r.status_code} | {r.text[:100]}")
                    break
                matches = r.json().get("lineMatches", [])
                if not matches:
                    break
                all_matches.extend(matches)
                logger.info(f"Mostbet loaded page {page}: {len(matches)} matches (total: {len(all_matches)})")
                if len(matches) < 100:
                    break
                last_id = matches[-1]["id"]
    except Exception as e:
        logger.error(f"_mostbet_load_matches: {e}")
        if cache_key in mostbet_cache:
            _, stale = mostbet_cache[cache_key]
            return stale

    if all_matches:
        mostbet_cache[cache_key] = (now, all_matches)
        logger.info(f"Mostbet cache updated: {len(all_matches)} total matches")
    return all_matches


def _is_within_week(match_date_str: str) -> bool:
    """Check if match is within next 7 days or live."""
    if not match_date_str:
        return True  # unknown date - include
    try:
        from datetime import timezone
        # Format: "01.06.2025 19:00:00" or "2025-06-01T19:00:00"
        ds = match_date_str.strip()
        if "T" in ds:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        elif "." in ds:
            dt = datetime.strptime(ds[:16], "%d.%m.%Y %H:%M")
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            return True
        now_utc = datetime.now(timezone.utc)
        delta = (dt - now_utc).total_seconds()
        return -3600 <= delta <= 7 * 24 * 3600  # from 1hr ago to 7 days ahead
    except Exception:
        return True  # parse error - include


async def mostbet_find_match(team1: str, team2: str) -> dict | None:
    """Search match in Mostbet by team names, only within next 7 days."""
    try:
        all_matches = await _mostbet_load_matches()
        # Filter to next 7 days + live matches
        matches = [m for m in all_matches
                   if m.get("isLive") or _is_within_week(m.get("matchBeginAt", ""))]
        logger.info(f"Mostbet filtered: {len(matches)}/{len(all_matches)} within 7 days")

        t1 = team1.lower().strip(); t2 = team2.lower().strip()
        if not t1 or not t2 or t1 == t2:
            return None
        for m in matches:
            t1m = m.get("team1Title", "").lower()
            t2m = m.get("team2Title", "").lower()
            mt  = m.get("matchTitle", "").lower()
            if (t1 in t1m or t1 in mt) and (t2 in t2m or t2 in mt):
                return m
            if (t2 in t1m or t2 in mt) and (t1 in t2m or t1 in mt):
                return m
            if t1 in mt and t2 in mt:
                return m
    except Exception as e:
        logger.error(f"mostbet_find_match: {e}")
    return None


async def mostbet_get_odds(line_id: int) -> dict:
    """Get odds for a match from Mostbet with caching."""
    cache_key = f"odds_{line_id}"
    now = time.time()
    if cache_key in mostbet_cache:
        ts, data = mostbet_cache[cache_key]
        if now - ts < MOSTBET_CACHE_TTL:
            return data
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
    mostbet_cache[f"odds_{line_id}"] = (time.time(), result)
    return result


def format_mostbet_odds(odds: dict, lang: str) -> str:
    """Format Mostbet odds as a clean string to inject into Claude prompt."""
    if not any([odds["w1"], odds["over25"], odds["btts_yes"]]):
        return ""
    lines = []
    # Map new langs to existing formats
    if lang in ("kz", "uz", "tr"):
        lang = "ru"
    elif lang == "ar":
        lang = "en"
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

# ─── Smart query parser ───────────────────────────────────────────────────────

async def parse_match_query(text: str, lang: str) -> dict:
    """Use Claude to extract team names and date from user query."""
    try:
        prompt = f"""Extract match info from this text: "{text}"
Return JSON only, no explanation:
{{"team1": "...", "team2": "...", "date": "DD.MM.YYYY or null", "sport": "football/basketball/ufc/tennis/other"}}
If you cannot find two teams, return {{"team1": null, "team2": null, "date": null, "sport": "football"}}"""
        r = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001", max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = r.content[0].text.strip()
        # Clean JSON
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        logger.error(f"parse_match_query: {e}")
    return {"team1": None, "team2": None, "date": None, "sport": "football"}


# ─── Odds change alerts ────────────────────────────────────────────────────────

async def check_odds_changes(app):
    """Background: check if odds changed significantly for subscribed matches."""
    while True:
        await asyncio.sleep(300)  # every 5 min
        try:
            with con() as c:
                alerts = c.execute("SELECT user_id, match_id, market, last_odd FROM odds_alerts").fetchall()
            for uid, mid, market, last_odd in alerts:
                try:
                    odds = await mostbet_get_odds(int(mid))
                    market_map = {"w1": odds["w1"], "x": odds["x"], "w2": odds["w2"],
                                  "over25": odds["over25"], "under25": odds["under25"]}
                    new_odd = market_map.get(market)
                    if new_odd and last_odd and abs(new_odd - last_odd) >= 0.3:
                        lang = db_lang(uid)
                        direction = "↑" if new_odd > last_odd else "↓"
                        msgs = {
                            "ru": f"ИЗМЕНЕНИЕ КОЭФФИЦИЕНТА {direction}\nМатч: {mid}\nРынок: {market}\nБыло: {last_odd} → Стало: {new_odd}\nРазница: {abs(new_odd-last_odd):.2f}",
                            "en": f"ODDS CHANGE {direction}\nMatch: {mid}\n{market}: {last_odd} → {new_odd}",
                            "az": f"KEF DƏYİŞDİ {direction}\nMatç: {mid}\n{market}: {last_odd} → {new_odd}",
                        }
                        msg = msgs.get(lang, msgs["ru"])
                        await app.bot.send_message(chat_id=uid, text=msg)
                        # Update stored odd
                        with con() as c:
                            c.execute("UPDATE odds_alerts SET last_odd=? WHERE user_id=? AND match_id=? AND market=?",
                                      (new_odd, uid, mid, market))
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"check_odds_changes: {e}")


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
    db_ensure(uid, user.username or "", user.language_code)
    if db_is_reg(uid):
        u = db_get(uid)
        await update.message.reply_text(tr(uid, "already_reg", name=u["display_name"] or user.first_name),
            reply_markup=main_menu(uid)); return
    reg_step[uid] = "awaiting_lang"
    await update.message.reply_text(UNIVERSAL_WELCOME, reply_markup=lang_kb())


async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = q.data.split("_")[1]
    db_ensure(uid, q.from_user.username or "", q.from_user.language_code); db_set(uid, "lang", lang)

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
    uid   = q.from_user.id; ftype = q.data
    msg_content = list(context.user_data.get("pending_content") or [])
    text        = context.user_data.get("pending_text", "")
    if not msg_content:
        await q.edit_message_text(tr(uid, "no_input")); return

    lang = db_lang(uid)
    thinking = {"ru": "⏳ Анализирую...", "az": "⏳ Analiz edilir...",
                "en": "⏳ Analysing...", "tr": "⏳ Analiz ediliyor...",
                "kz": "⏳ Талдау жасалуда...", "uz": "⏳ Tahlil qilinmoqda...", "ar": "⏳ جارٍ التحليل..."}
    await q.edit_message_text(thinking.get(lang, "⏳"))
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    # Build personalized system prompt
    u = db_get(uid) or {}
    exp = u.get("experience", "beginner")

    # Extra personalization based on experience
    extra_hints = {
        "ru": {
            "expert":   " Profil: ekspert — dobavlyay xG, aziatskie linii.",
            "mid":      " Profil: sredniy — kratko.",
            "beginner": " Profil: novichok — ob'yasnyay prosto.",
        },
        "en": {
            "expert":   " Profile: expert — include xG, Asian lines.",
            "mid":      " Profile: intermediate — brief.",
            "beginner": " Profile: beginner — explain simply.",
        },
        "az": {
            "expert":   " Profil: tecrubell — xG, Asiya xetleri.",
            "mid":      " Profil: orta — qisa.",
            "beginner": " Profil: yeni — sade izah et.",
        },
    }
    hint = extra_hints.get(lang, extra_hints["ru"]).get(exp, "")

    if ftype == "forecast_short":
        sys_prompt = tr(uid, "short_prompt") + hint; max_tok = 500
    else:
        sys_prompt = tr(uid, "system_prompt") + hint; max_tok = 1500

    # ── Fetch real Mostbet odds ───────────────────────────────────────────────
    if text:
        words = [w.strip(".,!?-") for w in text.split() if len(w) > 2]
        # Try ALL combinations of words as team1 and team2
        pairs_to_try = []
        # Single words
        for i, w1 in enumerate(words):
            for j, w2 in enumerate(words):
                if i != j:
                    pairs_to_try.append((w1, w2))
        # Two-word combos
        for i in range(len(words)-1):
            tw = " ".join(words[i:i+2])
            for j, w in enumerate(words):
                if j != i and j != i+1:
                    pairs_to_try.append((tw, w))
                    pairs_to_try.append((w, tw))

        mb_match = None
        seen = set()
        for t1, t2 in pairs_to_try:
            key = (t1.lower(), t2.lower())
            if key in seen: continue
            seen.add(key)
            mb_match = await mostbet_find_match(t1, t2)
            if mb_match:
                break

        if mb_match:
            mb_odds = await mostbet_get_odds(mb_match["id"])
            odds_str = format_mostbet_odds(mb_odds, lang)
            if odds_str:
                msg_content.append({"type": "text", "text": odds_str})
                logger.info(f"Mostbet odds OK | uid={uid} match={mb_match.get('matchTitle','?')}")
            else:
                logger.info(f"Mostbet match found but no odds | uid={uid}")
        else:
            try:
                all_m = await _mostbet_load_matches()
                week_m = [m for m in all_m if m.get("isLive") or _is_within_week(m.get("matchBeginAt",""))]
                all_m_count = len(all_m); week_m_count = len(week_m)
                sample = [f"{m.get('team1Title','?')} vs {m.get('team2Title','?')}" for m in week_m[:5]]
                logger.info(f"Mostbet no match for '{text[:40]}' | Week: {week_m_count}/{all_m_count} | Sample: {sample}")

                # Check if match exists but is outside 7 days
                if all_m_count > 0 and week_m_count < all_m_count:
                    # Search in ALL matches (no date filter)
                    words_all = [w.strip(".,!?") for w in text.split() if len(w) > 2]
                    found_far = None
                    for i, w1 in enumerate(words_all):
                        for w2 in words_all:
                            if w1 != w2:
                                for m in all_m:
                                    t1m = m.get("team1Title","").lower()
                                    t2m = m.get("team2Title","").lower()
                                    mt  = m.get("matchTitle","").lower()
                                    if (w1.lower() in t1m or w1.lower() in mt) and (w2.lower() in t2m or w2.lower() in mt):
                                        found_far = m
                                        break
                                if found_far: break
                            if found_far: break
                        if found_far: break

                    if found_far and not _is_within_week(found_far.get("matchBeginAt","")):
                        lang = db_lang(uid)
                        msg = T.get(lang, T["ru"]).get("match_too_far", T["ru"]["match_too_far"])
                        await context.bot.edit_message_text(
                            chat_id=uid, message_id=q.message.message_id, text=msg)
                        return
            except Exception:
                logger.info(f"Mostbet match not found: {text[:50]} | uid={uid}")

    # ── Claude request ────────────────────────────────────────────────────────
    try:
        async with request_semaphore:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-sonnet-4-6",
                max_tokens=max_tok,
                system=sys_prompt,
                messages=[{"role": "user", "content": msg_content}]
            )
        reply = resp.content[0].text
        logger.info(f"FORECAST [{ftype}] OK | uid={uid}")
    except anthropic.RateLimitError:
        reply = tr(uid, "api_overload")
    except anthropic.APIError as e:
        logger.error(f"API_ERR {e} | uid={uid}")
        reply = tr(uid, "api_error")

    # ── Watch button ──────────────────────────────────────────────────────────
    watch_kb = None
    if text and APIFOOTBALL_KEY:
        ms = await search_match(" ".join(text.split()[:3]))
        if ms:
            m = ms[0]; context.user_data[f"mn_{m['id']}"] = m["name"]
            watch_kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                tr(uid, "watch_btn") + f": {m['name'][:35]}", callback_data=f"watch_{m['id']}")]])

    # Save to history
    db_save_history(uid, text, reply)

    # Build buttons: watch + fav
    final_kb_rows = []
    if watch_kb:
        final_kb_rows.extend(watch_kb.inline_keyboard)

    # Extract team names for fav button
    words = text.split()
    if len(words) >= 2 and not db_is_fav(uid, words[0]):
        final_kb_rows.append([
            InlineKeyboardButton(tr(uid, "fav_btn_add") + f" {words[0]}", callback_data=f"addfav_{words[0][:30]}"),
        ])
        if len(words) >= 2 and words[-1] != words[0] and not db_is_fav(uid, words[-1]):
            final_kb_rows[-1].append(
                InlineKeyboardButton(tr(uid, "fav_btn_add") + f" {words[-1]}", callback_data=f"addfav_{words[-1][:30]}")
            )

    final_kb = InlineKeyboardMarkup(final_kb_rows) if final_kb_rows else None
    await context.bot.send_message(chat_id=uid, text=reply, reply_markup=final_kb)


async def watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); uid = q.from_user.id
    if q.data.startswith("watch_"):
        mid = q.data[6:]; mname = context.user_data.get(f"mn_{mid}", mid)
        live_subs[mid].add(uid); db_add_lsub(uid, mid, mname)
        # Register odds alerts for key markets
        try:
            odds = await mostbet_get_odds(int(mid))
            with con() as c:
                for market, odd in [("w1", odds["w1"]), ("over25", odds["over25"])]:
                    if odd:
                        c.execute("INSERT OR REPLACE INTO odds_alerts VALUES (?,?,?,?,datetime('now'))",
                                  (uid, mid, market, odd))
        except Exception:
            pass
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
    db_ensure(uid, user.username or "", user.language_code)
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
    if text == tl["menu_matches"]:  await matches_cmd(update, context); return
    if text == tl["menu_profile"]:  await profile_cmd(update, context); return
    if text == tl["menu_history"]:  await history_cmd(update, context); return
    if text == tl["menu_favs"]:     await favs_cmd(update, context); return
    if text == tl["menu_express"]:  await express_cmd(update, context); return
    if text == tl["menu_lang"]:
        await update.message.reply_text(tr(uid, "choose_lang"), reply_markup=lang_kb()); return
    if text == tl["menu_forecast"]:
        await update.message.reply_text(tr(uid, "no_input")); return
    # Compare handler
    if context.user_data.get("awaiting_compare"):
        if await handle_compare(uid, text, context): return

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

    # Smart date check
    import re as _re
    date_patterns = [r'\b(\d{1,2})[./](\d{1,2})\b', r'\b(\d{4})-(\d{2})-(\d{2})\b']
    for pat in date_patterns:
        dm = _re.search(pat, text)
        if dm:
            try:
                g = dm.groups()
                if len(g) == 2:
                    fd = date(date.today().year, int(g[1]), int(g[0]))
                elif len(g) == 3:
                    fd = date(int(g[0]), int(g[1]), int(g[2]))
                else:
                    fd = None
                if fd and (fd - date.today()).days > 7:
                    await update.message.reply_text(
                        T.get(lang, T["ru"]).get("match_too_far", T["ru"]["match_too_far"]))
                    return
            except Exception:
                pass
            break

    # Smart query parsing - extract teams from natural language
    if text and not photo:
        parsed = await parse_match_query(text, lang)
        if parsed.get("team1") and parsed.get("team2"):
            t1, t2 = parsed["team1"], parsed["team2"]
            # Enrich content with structured team info
            structured = f"Команды: {t1} vs {t2}"
            if parsed.get("date"):
                structured += f" | Дата: {parsed['date']}"
            if parsed.get("sport"):
                structured += f" | Спорт: {parsed['sport']}"
            content.append({"type": "text", "text": structured})
            logger.info(f"Parsed query: {t1} vs {t2} | uid={uid}")

    # Clear old conversation if new match topic
    # (detect if asking about same match or new one)

    # Store and show format chooser
    context.user_data["pending_content"] = content
    context.user_data["pending_text"] = text
    choose_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(tl["btn_extended"], callback_data="forecast_extended"),
        InlineKeyboardButton(tl["btn_short"],    callback_data="forecast_short"),
    ]])
    await update.message.reply_text(tl["choose_forecast"], reply_markup=choose_kb)


# ─── Favourites ────────────────────────────────────────────────────────────────

async def favs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    favs = db_get_favs(uid)
    if not favs:
        await update.message.reply_text(tr(uid, "fav_empty")); return
    lines = [tr(uid, "fav_list")]
    btns = []
    for team in favs:
        lines.append(f"- {team}")
        btns.append([InlineKeyboardButton(f"X {team}", callback_data=f"delfav_{team[:30]}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def fav_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if q.data.startswith("addfav_"):
        team = q.data[7:]
        db_add_fav(uid, team)
        await q.edit_message_text(q.message.text + "\n\n" + tr(uid, "fav_added", team=team))
    elif q.data.startswith("delfav_"):
        team = q.data[7:]
        db_del_fav(uid, team)
        await q.edit_message_text(tr(uid, "fav_removed", team=team))


# ─── History ──────────────────────────────────────────────────────────────────

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    history = db_get_history(uid)
    if not history:
        await update.message.reply_text(tr(uid, "history_empty")); return

    stats = db_feedback_stats(uid)
    lines = [tr(uid, "history_title")]
    if stats["total"] > 0:
        lines.append(tr(uid, "winrate", pct=stats["pct"], wins=stats["wins"], total=stats["total"]))
    lines.append("")
    btns = []
    for i, h in enumerate(history, 1):
        d = h["created_at"][:10]
        q_short = h["query"][:40]
        fb = " ✅" if h["feedback"] == 1 else (" ❌" if h["feedback"] == 0 else "")
        lines.append(f"{i}. {q_short} ({d}){fb}")
        if h["feedback"] is None:
            btns.append([
                InlineKeyboardButton(f"✅ #{i}", callback_data=f"fb_1_{h['id']}"),
                InlineKeyboardButton(f"❌ #{i}", callback_data=f"fb_0_{h['id']}"),
                InlineKeyboardButton(f"🔄 #{i}", callback_data=f"repeat_{h['id']}"),
            ])
        else:
            btns.append([InlineKeyboardButton(f"🔄 Повторить #{i}", callback_data=f"repeat_{h['id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def history_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; data = q.data

    if data.startswith("fb_"):
        parts = data.split("_")
        feedback = int(parts[1]); hist_id = int(parts[2])
        db_set_feedback(hist_id, feedback)
        await q.edit_message_text(tr(uid, "feedback_done"))

    elif data.startswith("repeat_"):
        hist_id = int(data.split("_")[1])
        history = db_get_history(uid)
        item = next((h for h in history if h["id"] == hist_id), None)
        if not item:
            await q.edit_message_text(tr(uid, "api_error")); return
        await q.edit_message_text(item["forecast"][:4000])


# ─── Express ──────────────────────────────────────────────────────────────────

async def express_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    lang = db_lang(uid)
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("2", callback_data="expr_2"),
         InlineKeyboardButton("3", callback_data="expr_3"),
         InlineKeyboardButton("4", callback_data="expr_4"),
         InlineKeyboardButton("5", callback_data="expr_5")],
    ])
    await update.message.reply_text(T[lang]["express_ask"], reply_markup=btns)


async def express_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; n = int(q.data.split("_")[1])
    lang = db_lang(uid)
    await q.edit_message_text("⏳")
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    u = db_get(uid) or {}
    sports = SPORTS_LABELS.get(lang, SPORTS_LABELS["ru"]).get(u.get("sports", "football"), "Football")
    exp = EXP_LABELS.get(lang, EXP_LABELS["ru"]).get(u.get("experience", "beginner"), "Beginner")

    # Try to get real matches from Mostbet cache
    mb_matches = await _mostbet_load_matches()
    real_matches_str = ""
    if mb_matches:
        # Filter to next 7 days only
        week_matches = [m for m in mb_matches
                        if m.get("isLive") or _is_within_week(m.get("matchBeginAt", ""))]
        # Pick football first, fallback to any sport
        football = [m for m in week_matches if "football" in m.get("lineCategory","").lower() or
                    "soccer" in m.get("lineCategory","").lower() or
                    "футбол" in m.get("lineCategory","").lower()][:n]
        if not football:
            football = week_matches[:n]
        if football:
            lines_mb = ["Реальные матчи из Mostbet:"]
            for m in football:
                t1 = m.get("team1Title","?"); t2 = m.get("team2Title","?")
                league = m.get("lineSubCategory","")
                dt = m.get("matchBeginAt","")[:16]
                lines_mb.append(f"- {t1} vs {t2} | {league} | {dt}")
            real_matches_str = "\n".join(lines_mb) + "\n\nИспользуй ИМЕННО эти матчи для экспресса.\n"

    express_prompts = {
        "ru": f"""{real_matches_str}Составь экспресс на {n} матчей. Правила:\n- Используй только матчи из списка выше (если есть)\n- Для каждого: команды, лучший тип ставки, реалистичный коэффициент\n- Коэффициенты: фаворит 1.20-1.60, равные 2.00-2.80, тотал 1.70-2.10\n- НЕ используй markdown ## ** — только чистый текст и emoji
- В конце посчитай итоговый коэффициент

Формат:
⚽ Матч 1: [Команда А] — [Команда Б]
Ставка: [тип] | Кэф: X.XX
Обоснование: [1 предложение]

⚽ Матч 2: ...

💰 Итог: X.XX × X.XX × X.XX = X.XX

⚠️ Аналитический прогноз.""",
        "az": f"""{real_matches_str}Bu matçlar üçün {n} oyunluq ekspress yarat. Qaydalar:\n- Yuxarıdakı matçları istifadə et (əgər varsa)\n- Hər matç üçün: komandalar, ən yaxşı mərc növü, real kef\n- Keflər: favorit 1.20-1.60, bərabər 2.00-2.80\n- markdown ## ** işlətmə — yalnız mətn və emoji
- Sonunda ümumi kef hesabla

Format:
⚽ Matç 1: [Komanda A] — [Komanda B]
Mərc: [növ] | Kef: X.XX
Səbəb: [1 cümlə]

💰 Nəticə: X.XX × X.XX = X.XX

⚠️ Analitik proqnozdur.""",
        "en": f"""{real_matches_str}Build an express bet with {n} matches. Rules:\n- Use only matches from the list above (if available)\n- For each: teams, best bet type, realistic odds\n- Odds: favorite 1.20-1.60, even 2.00-2.80, total 1.70-2.10\n- NO markdown ## ** — plain text and emoji only
- Calculate total express odds at the end

Format:
⚽ Match 1: [Team A] — [Team B]
Bet: [type] | Odds: X.XX
Reason: [1 sentence]

💰 Total: X.XX × X.XX = X.XX

⚠️ Analytical forecast.""",
        "tr": f"""{real_matches_str}{n} maçlık ekspres oluştur. Kurallar:\n- Yukarıdaki maçları kullan (varsa)\n- Her biri: takımlar, en iyi bahis türü, gerçekçi oran\n- markdown ## ** kullanma — sadece metin ve emoji\n- Sonunda toplam oranı hesapla

Format:
⚽ Maç 1: [Takım A] — [Takım B]
Bahis: [tür] | Oran: X.XX

💰 Toplam: X.XX × X.XX = X.XX

⚠️ Analitik tahmin.""",
        "kz": f"""{real_matches_str}{n} матчтық экспресс жаса. Ережелер:\n- Жоғарыдағы матчтарды қолдан (болса)\n- markdown ## ** жоқ — тек мәтін және emoji\n\nFormat:
⚽ Матч 1: [А] — [Б]
Ставка: [түрі] | Коэф: X.XX

💰 Жалпы: X.XX × X.XX = X.XX""",
        "uz": f"""{real_matches_str}{n} ta o'yin uchun ekspress tuzing. Qoidalar:\n- Yuqoridagi o'yinlarni ishlating (agar bor bo'lsa)\n- markdown ## ** yo'q — faqat matn va emoji\n\nFormat:
⚽ O'yin 1: [A] — [B]
Stavka: [turi] | Koef: X.XX

💰 Jami: X.XX × X.XX = X.XX""",
        "ar": f"""{real_matches_str}أنشئ رهاناً مركباً من {n} مباريات. القواعد:\n- استخدم المباريات من القائمة أعلاه (إن وُجدت)\n- بدون markdown ## ** — نص وإيموجي فقط\n\nالصيغة:
⚽ مباراة 1: [أ] — [ب]
الرهان: [النوع] | الربح: X.XX

💰 الإجمالي: X.XX × X.XX = X.XX""",
    }
    prompt = express_prompts.get(lang, express_prompts["ru"])

    try:
        async with request_semaphore:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-haiku-4-5-20251001", max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
        reply = resp.content[0].text
    except Exception:
        reply = tr(uid, "api_error")

    header = T[lang]["express_title"]
    await context.bot.send_message(chat_id=uid, text=header + "\n\n" + reply)


# ─── Compare ──────────────────────────────────────────────────────────────────

async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    lang = db_lang(uid)
    await update.message.reply_text(T[lang]["compare_ask"])
    context.user_data["awaiting_compare"] = True


async def handle_compare(uid: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("awaiting_compare"): return False
    context.user_data.pop("awaiting_compare")
    words = text.strip().split()
    if len(words) < 2:
        await context.bot.send_message(chat_id=uid, text=tr(uid, "compare_ask"))
        return True

    lang = db_lang(uid)
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    compare_prompts = {
        "az": f"İki komandanı müqayisə et: {text}. Forma (son 5 matç), baş-başa görüşlər (son 5), güclü/zəif tərəflər, xG statistikası, hücum/müdafiə. Emoji istifadə et, markdown ** yox. Qısa və konkret.",
        "ru": f"Сравни две команды: {text}. Форма (последние 5 матчей), очные встречи (последние 5), сильные/слабые стороны, xG статистика, атака/защита. Используй emoji, markdown ** не используй. Кратко и по делу.",
        "en": f"Compare two teams: {text}. Form (last 5 matches), head-to-head (last 5), strengths/weaknesses, xG stats, attack/defense. Use emoji, no markdown **. Brief and factual.",
        "tr": f"İki takımı karşılaştır: {text}. Form (son 5 maç), karşılıklı maçlar (son 5), güçlü/zayıf yönler, xG istatistikleri. Emoji kullan, markdown ** kullanma. Kısa ve öz.",
        "kz": f"Екі команданы салыстыр: {text}. Форма (соңғы 5 матч), бетпе-бет кездесулер (соңғы 5), күшті/әлсіз жақтар, xG статистикасы. Emoji қолдан, markdown ** жоқ. Қысқа.",
        "uz": f"Ikkita jamoani solishtirish: {text}. Shakl (oxirgi 5 o'yin), to'g'ridan-to'g'ri uchrashuvlar (oxirgi 5), kuchli/zaif tomonlar, xG statistikasi. Emoji ishlatish, markdown ** yo'q. Qisqa.",
        "ar": f"قارن بين فريقين: {text}. الشكل (آخر 5 مباريات)، المواجهات المباشرة (آخر 5)، نقاط القوة والضعف، إحصاءات xG. استخدم emoji، بدون markdown **. موجز.",
    }
    prompt = compare_prompts.get(lang, compare_prompts["ru"])

    try:
        async with request_semaphore:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-haiku-4-5-20251001", max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
        reply = resp.content[0].text
    except Exception:
        reply = tr(uid, "api_error")

    await context.bot.send_message(chat_id=uid, text=reply)
    return True

# ─── Admin ────────────────────────────────────────────────────────────────────
def is_adm(update): return (update.effective_user.id if update.effective_user else 0) == ADMIN_ID

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Статистика",             callback_data="adm_stats")],
        [InlineKeyboardButton("Рассылка — Все",         callback_data="adm_broadcast_all")],
        [InlineKeyboardButton("Рассылка по языку/гео",  callback_data="adm_broadcast_geo")],
        [InlineKeyboardButton("Заблокированные",        callback_data="adm_blocklist")],
        [InlineKeyboardButton("Поиск пользователя",     callback_data="adm_search")],
        [InlineKeyboardButton("Изменить язык",           callback_data="adm_setlang")],
        [InlineKeyboardButton("Live подписки",           callback_data="adm_live")],
        [InlineKeyboardButton("Тест Mostbet API",        callback_data="adm_test_mostbet")],
    ])

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    await update.message.reply_text("АДМИН ПАНЕЛЬ", reply_markup=admin_kb())

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

    elif data == "adm_broadcast_all":
        context.user_data["adm_act"] = "broadcast_all"
        with con() as c:
            cnt = c.execute("SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0").fetchone()[0]
        await q.edit_message_text(
            f"РАССЫЛКА ВСЕМ\n\nПолучателей: {cnt}\n\nОтправьте текст. /cancel — отмена.")

    elif data == "adm_broadcast_geo":
        # Show geo selection
        geo_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Azərbaycan (az)", callback_data="adm_bcast_az")],
            [InlineKeyboardButton("Русский (ru)",    callback_data="adm_bcast_ru")],
            [InlineKeyboardButton("English (en)",    callback_data="adm_bcast_en")],
            [InlineKeyboardButton("Türkçe (tr)",     callback_data="adm_bcast_tr")],
            [InlineKeyboardButton("Қазақша (kz)",    callback_data="adm_bcast_kz")],
            [InlineKeyboardButton("O'zbek (uz)",     callback_data="adm_bcast_uz")],
            [InlineKeyboardButton("العربية (ar)",    callback_data="adm_bcast_ar")],
            [InlineKeyboardButton("Назад",           callback_data="adm_back")],
        ])
        # Show counts per lang
        with con() as c:
            langs = c.execute("SELECT lang, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 GROUP BY lang").fetchall()
        lang_counts = {l: n for l, n in langs}
        lines = ["РАССЫЛКА ПО ГЕО\n\nВыберите аудиторию:\n"]
        for code, name in [("az","Azərbaycan"),("ru","Русский"),("en","English"),
                           ("tr","Türkçe"),("kz","Қазақша"),("uz","O'zbek"),("ar","العربية")]:
            lines.append(f"{name}: {lang_counts.get(code, 0)} чел.")
        await q.edit_message_text("\n".join(lines), reply_markup=geo_kb)

    elif data.startswith("adm_bcast_"):
        lang_code = data.split("_")[2]
        with con() as c:
            cnt = c.execute("SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 AND lang=?", (lang_code,)).fetchone()[0]
        lang_names = {"az":"Azərbaycan","ru":"Русский","en":"English","tr":"Türkçe","kz":"Қазақша","uz":"O'zbek","ar":"العربية"}
        context.user_data["adm_act"] = f"broadcast_geo_{lang_code}"
        await q.edit_message_text(
            f"РАССЫЛКА: {lang_names.get(lang_code, lang_code)}\n\nПолучателей: {cnt}\n\nОтправьте текст. /cancel — отмена.")

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

    elif data == "adm_test_mostbet":
        await q.edit_message_text("Тестирую Mostbet API...")
        try:
            matches = await _mostbet_load_matches()
            if not matches:
                await q.edit_message_text(
                    "MOSTBET API\n\nСтатус: НЕТ ДАННЫХ\n\nВозможные причины:\n- 429 Rate limit\n- IP не в whitelist\n- Проблемы с сетью",
                    reply_markup=back)
            else:
                # Show first 5 matches
                sample = matches[:5]
                lines = [f"MOSTBET API\n\nСтатус: РАБОТАЕТ\nВсего матчей: {len(matches)}\n\nПримеры:"]
                for m in sample:
                    t1 = m.get("team1Title", "?")
                    t2 = m.get("team2Title", "?")
                    league = m.get("lineSubCategory", "")
                    live = "LIVE" if m.get("isLive") else "Pre"
                    lines.append(f"[{live}] {t1} vs {t2} ({league})")
                cache_ts = mostbet_cache.get("all_matches", (0, []))[0]
                if cache_ts:
                    age = int(time.time() - cache_ts)
                    lines.append(f"\nКэш: {age} сек назад")
                await q.edit_message_text("\n".join(lines), reply_markup=back)
        except Exception as e:
            await q.edit_message_text(f"MOSTBET API\n\nОшибка: {e}", reply_markup=back)

    elif data == "adm_back":
        await q.edit_message_text("АДМИН ПАНЕЛЬ", reply_markup=admin_kb())

async def handle_adm_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    act = context.user_data.get("adm_act")
    if not act: return
    context.user_data.pop("adm_act")
    text = update.message.text or ""

    if act == "broadcast_all":
        uids = db_all_uids()
        status = await update.message.reply_text(f"Рассылка для {len(uids)} пользователей...")
        ok = fail = 0
        for uid in uids:
            try: await context.bot.send_message(chat_id=uid, text=text); ok += 1
            except Exception: fail += 1
            await asyncio.sleep(0.05)
        await status.edit_text(f"Готово! Доставлено: {ok} | Не доставлено: {fail}")

    elif act.startswith("broadcast_geo_"):
        lang_code = act.split("_")[2]
        with con() as c:
            uids = [r[0] for r in c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0 AND lang=?",
                (lang_code,)).fetchall()]
        lang_names = {"az":"Azərbaycan","ru":"Русский","en":"English","tr":"Türkçe","kz":"Қазақша","uz":"O'zbek","ar":"العربية"}
        status = await update.message.reply_text(
            f"Рассылка [{lang_names.get(lang_code, lang_code)}]: {len(uids)} пользователей...")
        ok = fail = 0
        for uid in uids:
            try: await context.bot.send_message(chat_id=uid, text=text); ok += 1
            except Exception: fail += 1
            await asyncio.sleep(0.05)
        await status.edit_text(f"Готово! [{lang_names.get(lang_code, lang_code)}]\nДоставлено: {ok} | Не доставлено: {fail}")

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


async def testapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test Mostbet API directly - admin only."""
    if not is_adm(update): return
    await update.message.reply_text("Тестирую Mostbet API напрямую...")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
            r = await h.get(
                f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                params={"lastId": 0, "locale": "ru", "limit": 3}
            )
            status = r.status_code
            body = r.text[:800]

            if status == 200:
                data = r.json()
                matches = data.get("lineMatches", [])
                lines = [f"Mostbet API: OK ({status})\nМатчей в ответе: {len(matches)}\n"]
                for m in matches[:3]:
                    t1 = m.get("team1Title","?")
                    t2 = m.get("team2Title","?")
                    league = m.get("lineSubCategory","")
                    live = "LIVE" if m.get("isLive") else "Pre-match"
                    lines.append(f"[{live}] {t1} vs {t2} ({league})")
                await update.message.reply_text("\n".join(lines))
            else:
                deny = r.headers.get("x-deny-reason", "")
                await update.message.reply_text(
                    f"Mostbet API: ОШИБКА\nСтатус: {status}\nПричина: {deny}\nОтвет: {body[:200]}"
                )
    except Exception as e:
        await update.message.reply_text(f"Mostbet API: ИСКЛЮЧЕНИЕ\n{e}")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("lang",    lang_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("admin",   admin_cmd))
    app.add_handler(CommandHandler("cancel",  cancel_cmd))
    app.add_handler(CommandHandler("testapi", testapi_cmd))

    app.add_handler(CallbackQueryHandler(lang_cb,       pattern=r"^lang_"))
    app.add_handler(CallbackQueryHandler(ob_cb,         pattern=r"^ob_"))
    app.add_handler(CallbackQueryHandler(forecast_cb,   pattern=r"^forecast_"))
    app.add_handler(CallbackQueryHandler(watch_cb,      pattern=r"^(watch|unwatch)_"))
    app.add_handler(CallbackQueryHandler(fav_toggle_cb, pattern=r"^(addfav|delfav)_"))
    app.add_handler(CallbackQueryHandler(history_cb,    pattern=r"^(fb_|repeat_)"))
    app.add_handler(CallbackQueryHandler(express_cb,    pattern=r"^expr_"))
    app.add_handler(CallbackQueryHandler(adm_cb,        pattern=r"^adm_"))

    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_adm_msg), group=0)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_msg), group=1)

    async def post_init(application):
        db_restore_live_subs()
        asyncio.create_task(poller(application))
        asyncio.create_task(daily_push(application))
        asyncio.create_task(_preload_mostbet())
        asyncio.create_task(check_odds_changes(application))

    async def _preload_mostbet():
        """Preload Mostbet matches at startup, then refresh every 15 min."""
        await asyncio.sleep(10)
        while True:
            logger.info("Loading Mostbet matches...")
            matches = await _mostbet_load_matches()
            logger.info(f"Mostbet loaded: {len(matches)} matches")
            await asyncio.sleep(MOSTBET_CACHE_TTL)

    app.post_init = post_init
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
    PORT = int(os.environ.get("PORT", "8080"))

    if WEBHOOK_URL:
        # Webhook mode - faster, no polling overhead
        logger.info(f"ProqnozAI v5 started (webhook: {WEBHOOK_URL})")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
        )
    else:
        # Polling mode - fallback
        logger.info("ProqnozAI v5 started (polling)")
        app.run_polling()

if __name__ == "__main__":
    main()
