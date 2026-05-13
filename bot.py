import os, logging, base64, time, sqlite3, asyncio, httpx, random
from collections import defaultdict, deque
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID         = int(os.environ.get("ADMIN_ID", "0"))
FOOTBALL_KEY     = os.environ.get("FOOTBALL_API_KEY", "")
APIFOOTBALL_KEY  = os.environ.get("APIFOOTBALL_KEY", "")

RATE_WINDOW = 60; RATE_MAX = 5; SPAM_AFTER = 3; SPAM_DUR = 600
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─── In-memory ────────────────────────────────────────────────────────────────
msg_times:  dict[int, deque] = defaultdict(deque)
violations: dict[int, int]   = defaultdict(int)
blocked_until: dict[int, float] = {}
reg_step:   dict[int, str]   = {}
live_subs:  dict[str, set]   = defaultdict(set)
user_subs:  dict[int, set]   = defaultdict(set)
last_events: dict[str, list] = {}
ht_sent:    set              = set()

# ─── DB ───────────────────────────────────────────────────────────────────────
DB = "bot.db"
def con(): return sqlite3.connect(DB)

def db_init():
    with con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT, display_name TEXT,
            lang         TEXT DEFAULT 'az',
            referred_by  INTEGER, ref_count INTEGER DEFAULT 0,
            is_registered INTEGER DEFAULT 0, is_blocked INTEGER DEFAULT 0,
            -- onboarding
            sports       TEXT DEFAULT '',
            bet_types    TEXT DEFAULT '',
            experience   TEXT DEFAULT '',
            bet_style    TEXT DEFAULT '',
            onboarding_done INTEGER DEFAULT 0,
            -- gamification
            points       INTEGER DEFAULT 0,
            level        INTEGER DEFAULT 1,
            streak_days  INTEGER DEFAULT 0,
            last_active  TEXT DEFAULT '',
            total_requests INTEGER DEFAULT 0,
            -- stats
            joined_at    TEXT DEFAULT (datetime('now'))
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
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, achievement TEXT, earned_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, type TEXT, sent_at TEXT DEFAULT (datetime('now'))
        );
        """)
db_init()

def db_ensure(uid, uname):
    with con() as c: c.execute("INSERT OR IGNORE INTO users (user_id,username) VALUES (?,?)", (uid, uname))

def db_set(uid, field, val):
    with con() as c: c.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (val, uid))

def db_get(uid) -> dict | None:
    with con() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not r: return None
        cols = [d[0] for d in c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).description or
                c.execute("PRAGMA table_info(users)").fetchall()]
    if not r: return None
    with con() as c:
        cur = c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        cols = [d[0] for d in cur.description]
        r = cur.fetchone()
    return dict(zip(cols, r)) if r else None

def db_lang(uid) -> str:
    u = db_get(uid); return u["lang"] if u else "az"

def db_is_reg(uid) -> bool:
    u = db_get(uid); return bool(u and u["is_registered"])

def db_is_blocked(uid) -> bool:
    u = db_get(uid); return bool(u and u["is_blocked"])

def db_all_uids() -> list[int]:
    with con() as c: return [r[0] for r in c.execute("SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0").fetchall()]

def db_log_req(uid, mtype):
    with con() as c:
        c.execute("INSERT INTO requests (user_id,msg_type) VALUES (?,?)", (uid, mtype))
        c.execute("UPDATE users SET total_requests=total_requests+1, last_active=? WHERE user_id=?",
                  (datetime.now().isoformat(), uid))

def db_add_points(uid, pts, reason=""):
    with con() as c:
        c.execute("UPDATE users SET points=points+? WHERE user_id=?", (pts, uid))
    update_level(uid)
    if reason: logger.info(f"POINTS +{pts} ({reason}) | uid={uid}")

def update_level(uid):
    u = db_get(uid)
    if not u: return
    pts = u["points"]
    level = 1
    if pts >= 5000: level = 10
    elif pts >= 2000: level = 7
    elif pts >= 1000: level = 5
    elif pts >= 500: level = 4
    elif pts >= 200: level = 3
    elif pts >= 100: level = 2
    if level != u["level"]: db_set(uid, "level", level)

def update_streak(uid):
    u = db_get(uid)
    if not u: return
    last = u.get("last_active", "")
    today = date.today().isoformat()
    if last[:10] == today: return
    yesterday = (date.today().replace(day=date.today().day-1)).isoformat() if date.today().day > 1 else ""
    if last[:10] == yesterday:
        db_set(uid, "streak_days", u["streak_days"] + 1)
        if u["streak_days"] + 1 in (3, 7, 14, 30):
            db_add_points(uid, 50 * (u["streak_days"] + 1 // 7 + 1), "streak_bonus")
    else:
        db_set(uid, "streak_days", 1)

def db_add_achievement(uid, ach):
    with con() as c:
        existing = c.execute("SELECT id FROM achievements WHERE user_id=? AND achievement=?", (uid, ach)).fetchone()
        if not existing:
            c.execute("INSERT INTO achievements (user_id,achievement) VALUES (?,?)", (uid, ach))
            return True
    return False

def db_stats() -> dict:
    with con() as c:
        total   = c.execute("SELECT COUNT(*) FROM users WHERE is_registered=1").fetchone()[0]
        today   = c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=date('now') AND is_registered=1").fetchone()[0]
        blocked = c.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
        rqtotal = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        rqtoday = c.execute("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now')").fetchone()[0]
        toprefs = c.execute("SELECT user_id,username,ref_count FROM users ORDER BY ref_count DESC LIMIT 5").fetchall()
        langs   = c.execute("SELECT lang,COUNT(*) FROM users WHERE is_registered=1 GROUP BY lang").fetchall()
        toppts  = c.execute("SELECT user_id,display_name,points,level FROM users WHERE is_registered=1 ORDER BY points DESC LIMIT 5").fetchall()
        ob_done = c.execute("SELECT COUNT(*) FROM users WHERE onboarding_done=1").fetchone()[0]
        live_ct = c.execute("SELECT COUNT(*) FROM live_subscriptions").fetchone()[0]
    return dict(total=total, today=today, blocked=blocked, rqtotal=rqtotal, rqtoday=rqtoday,
                toprefs=toprefs, langs=langs, toppts=toppts, ob_done=ob_done, live_ct=live_ct)

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

# ─── Translations ─────────────────────────────────────────────────────────────
T = {
"az": {
"choose_lang": "Dil secin / Выберите язык / Choose language:",
"ask_name": "Xos geldiniz! Adinizi daxil edin:",
"reg_done": "Qeydiyyat tamamlandi! Salam, {name}!\n\nAi-driven idman proqnoz platformasina xos geldiniz.",
"reg_done_ref": "Qeydiyyat tamamlandi! Salam, {name}!\nDostu devet etdiyin ucun bonus qazandi.",
"already_reg": "Siz artiq qeydiyyatdan kecmisiniz, {name}!",
"need_reg": "Evvelce qeydiyyatdan kesin. /start yazin.",
"db_blocked": "Hesabiniz bloklanib. Inzibatciya muraciet edin.",
"blocked": "Muveqqeti bloklanmissiniz. {m} deq {s} san sonra yeniden ced edin.",
"rate_limit": "Sorgu limiti asildi. {w} saniye gozleyin. Xeberdarliq: {v}/{max}",
"auto_blocked": "Cox sayda sorgu. {min} deqiqelik blok.",
"long_text": "Metn cox uzundur.",
"injection": "Yalniz idman sorqulari qebul edilir.",
"no_input": "Metn yazin ve ya sekil gonderin.",
"img_prompt": "Sekildeki idman hadisesini mueyyen et ve proqnoz ver.",
"api_overload": "Servis yuklemesi. Bir az sonra yeniden ced edin.",
"api_error": "Xeta bas verdi. Bir az sonra yeniden ced edin.",
"lang_set": "Dil Azerbaycan diline teyin edildi.",
"ref_link": "Referans linkIniz:\nt.me/{bot}?start=ref{uid}\n\nDevet etdiyiniz: {count} nefer",
"watch_btn": "Oyunu izle",
"unwatch_btn": "Izlemekden cix",
"watch_started": "Oyun izlenilir: {match}\nVacib hadiseler haqda bildirish alacaqsiniz.",
"watch_stopped": "Oyun izlenmesi dayandirIldi: {match}",
"no_subs": "Hec bir oyun izlemirsiniz.",
"live_goal": "QOL! {match}\n{minute}. deq: {team}\nHesab: {score}\n\nCanli merc:\n{tip}",
"live_card": "KART! {match}\n{minute}. deq: {player} ({team}) — {card}\n\nCanli merc:\n{tip}",
"live_halftime": "FASILE! {match}\nHesab: {score}\n\nFasile merci:\n{tip}",
"live_fulltime": "OYUN BITTI! {match}\nYekun: {score}\n\nIzlemeden cixildi.",
"live_kickoff": "OYUN BASLADI! {match}\nVacib hadiseler haqda xeberlendirme alacaqsiniz.",
"live_alert_goal_coming": "SIGNAL! {match}\nModel: Gol gozlenilir [{minute}. deq]\nKeflerin deyismesinden evvel hareket edin.",
"live_alert_value": "LIVE VALUE! {match}\nKeflerde anomaliya askar edildi [{minute}. deq]\nModel: {team} uzerinde deger var.",
"live_alert_pressure": "TEZYIQ! {match}\n{team} guclu hucum tezyiqi yaradir [{minute}. deq]\nStat: {stat}",
# Onboarding
"ob_sports": "Hansı idman növlerini seversiniz? (birdən çox seçe bilərsiniz)",
"ob_bet_types": "Hansı mərc növlərini üstün tutursunuz?",
"ob_experience": "Mərcdə təcrübəniz nə qədərdir?",
"ob_style": "Oyun üslubunuz nədir?",
"ob_done": "Profiliniz hazırlandı! Kişiselleşdirilmiş proqnozlar alacaqsınız.\n\nProfiliniz:\nIdman: {sports}\nMerc novu: {bet_types}\nTecurbe: {experience}\nUslub: {style}",
# Gamification
"points_earned": "+{pts} xal qazandiniz! Umumi: {total} xal | Seviye {level}",
"streak": "{days} gun ard-arda aktiv! Bonus: +{bonus} xal",
"achievement": "NEZARET EDILDI: {name}",
"level_up": "YENİ SƏVİYYƏ! Seviye {level} catdınız! +{bonus} xal bonus",
"leaderboard": "LİDERLƏR CƏDVƏLI:\n\n{entries}\n\nSizin yeriniz: {rank}. sıra | {pts} xal | Sev. {level}",
"profile_full": "PROFİL\n\nAd: {name}\nDil: {lang}\nSeviye: {level}\nXal: {pts}\nStrik: {streak} gun\nCemi sorqular: {total_req}\nDavetler: {refs}\n\nİdman: {sports}\nMerc: {bet_types}\nTecurbe: {exp}",
"system_prompt": """Sen 15 illik tecrubeli elit idman analitikisen. Tarix: {date}.

ISTIFADECININ PROFILI:
Sevdigi idman: {sports}
Merc novu: {bet_types}
Tecurbe: {experience}
Uslub: {style}

QAYDALAR:
- Istifadecinin profiline uygun proqnoz ver
- Yalniz 2025-2026 mevsumu melumatlar, kohneleri YAZMA
- Emoji ISTIFADE ETME - yalniz duz metn

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
- [Komanda A] qelebesi: XX% | Kes: X.XX - X.XX
- Hec-hece: XX% | Kes: X.XX - X.XX
- [Komanda B] qelebesi: XX% | Kes: X.XX - X.XX

Total:
- 2.5 Ustunde: XX% | Kes: X.XX - X.XX
- 2.5 Altinda: XX% | Kes: X.XX - X.XX

Her ikisi qol vurur:
- Beli: XX% | Kes: X.XX - X.XX
- Xeyr: XX% | Kes: X.XX - X.XX

Gandikap:
- [Komanda A] (-1): XX% | Kes: X.XX - X.XX
- [Komanda B] (+1): XX% | Kes: X.XX - X.XX

EN GUCLU MERC:
[Merc novu] | Kes: X.XX - X.XX | Ehtimal: XX%
Sebeb: [1 cumle]

XEBERDARLIQ: Analitik proqnozdur. Merc oz riskinizdir.""",
"live_tip_prompt": "Sen canli merc analitikisen. Oyun: {match}, {minute}. deq, hesab {score}. Hadise: {event}. En yaxsi canli merci tovsiye et. Qisa, maks 2 cumle. Yalniz duz metn.",
},

"ru": {
"choose_lang": "Dil secin / Выберите язык / Choose language:",
"ask_name": "Добро пожаловать! Введите ваше имя:",
"reg_done": "Регистрация завершена! Привет, {name}!\n\nДобро пожаловать на AI-платформу спортивных прогнозов.",
"reg_done_ref": "Регистрация завершена! Привет, {name}!\nВаш друг получил бонус за приглашение.",
"already_reg": "Вы уже зарегистрированы, {name}!",
"need_reg": "Сначала пройдите регистрацию. Напишите /start.",
"db_blocked": "Ваш аккаунт заблокирован. Обратитесь к администратору.",
"blocked": "Вы временно заблокированы. Попробуйте через {m} мин {s} сек.",
"rate_limit": "Лимит запросов превышен. Подождите {w} сек. Предупреждение: {v}/{max}",
"auto_blocked": "Слишком много запросов. Блокировка на {min} минут.",
"long_text": "Текст слишком длинный.",
"injection": "Принимаются только спортивные запросы.",
"no_input": "Напишите текст или отправьте фото.",
"img_prompt": "Определи спортивное событие на изображении и дай прогноз.",
"api_overload": "Сервис перегружен. Попробуйте позже.",
"api_error": "Произошла ошибка. Попробуйте позже.",
"lang_set": "Язык установлен: Русский.",
"ref_link": "Ваша реферальная ссылка:\nt.me/{bot}?start=ref{uid}\n\nПриглашено: {count} чел.",
"watch_btn": "Следить за матчем",
"unwatch_btn": "Отписаться",
"watch_started": "Слежу за матчем: {match}\nПришлю уведомления о важных событиях.",
"watch_stopped": "Слежение остановлено: {match}",
"no_subs": "Вы не следите ни за одним матчем.",
"live_goal": "ГОЛ! {match}\n{minute} мин: {team}\nСчёт: {score}\n\nЛайв-ставка:\n{tip}",
"live_card": "КАРТОЧКА! {match}\n{minute} мин: {player} ({team}) — {card}\n\nЛайв-ставка:\n{tip}",
"live_halftime": "ПЕРЕРЫВ! {match}\nСчёт: {score}\n\nСтавка на перерыв:\n{tip}",
"live_fulltime": "МАТЧ ЗАВЕРШЁН! {match}\nИтог: {score}\n\nПодписка отменена.",
"live_kickoff": "МАТЧ НАЧАЛСЯ! {match}\nБуду присылать уведомления о ключевых событиях.",
"live_alert_goal_coming": "СИГНАЛ! {match}\nМодель: ожидается гол [{minute} мин]\nДействуйте до изменения коэффициентов.",
"live_alert_value": "LIVE VALUE! {match}\nОбнаружена аномалия в коэффициентах [{minute} мин]\nМодель: есть ценность на {team}.",
"live_alert_pressure": "ДАВЛЕНИЕ! {match}\n{team} создаёт сильное давление [{minute} мин]\nСтат: {stat}",
# Onboarding
"ob_sports": "Какие виды спорта вас интересуют? (можно несколько)",
"ob_bet_types": "Какие типы ставок вы предпочитаете?",
"ob_experience": "Каков ваш опыт в ставках?",
"ob_style": "Какой стиль игры вам ближе?",
"ob_done": "Ваш профиль готов! Вы будете получать персонализированные прогнозы.\n\nПрофиль:\nСпорт: {sports}\nТип ставок: {bet_types}\nОпыт: {experience}\nСтиль: {style}",
# Gamification
"points_earned": "+{pts} очков! Всего: {total} | Уровень {level}",
"streak": "{days} дней подряд активны! Бонус: +{bonus} очков",
"achievement": "ДОСТИЖЕНИЕ: {name}",
"level_up": "НОВЫЙ УРОВЕНЬ! Достигнут уровень {level}! +{bonus} очков бонус",
"leaderboard": "ТАБЛИЦА ЛИДЕРОВ:\n\n{entries}\n\nВаша позиция: {rank} место | {pts} очков | Ур. {level}",
"profile_full": "ПРОФИЛЬ\n\nИмя: {name}\nЯзык: {lang}\nУровень: {level}\nОчки: {pts}\nСтрик: {streak} дней\nВсего запросов: {total_req}\nПриглашений: {refs}\n\nСпорт: {sports}\nСтавки: {bet_types}\nОпыт: {exp}",
"system_prompt": """Ты — элитный спортивный аналитик с 15-летним опытом. Дата: {date}.

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:
Любимый спорт: {sports}
Тип ставок: {bet_types}
Опыт: {experience}
Стиль: {style}

ПРАВИЛА:
- Давай прогнозы с учётом профиля пользователя
- Только данные сезона 2025-2026, устаревшие составы НЕ УПОМИНАЙ
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
- Победа [Команда А]: XX% | Кэф: X.XX - X.XX
- Ничья: XX% | Кэф: X.XX - X.XX
- Победа [Команда Б]: XX% | Кэф: X.XX - X.XX

Тотал:
- Больше 2.5: XX% | Кэф: X.XX - X.XX
- Меньше 2.5: XX% | Кэф: X.XX - X.XX

Обе забьют:
- Да: XX% | Кэф: X.XX - X.XX
- Нет: XX% | Кэф: X.XX - X.XX

Гандикап:
- [Команда А] (-1): XX% | Кэф: X.XX - X.XX
- [Команда Б] (+1): XX% | Кэф: X.XX - X.XX

ЛУЧШАЯ СТАВКА:
[Тип ставки] | Кэф: X.XX - X.XX | Вероятность: XX%
Обоснование: [1 предложение]

ПРЕДУПРЕЖДЕНИЕ: Аналитический прогноз. Ставки на ваш риск.""",
"live_tip_prompt": "Ты лайв-аналитик. Матч {match}, {minute} мин, счёт {score}. Событие: {event}. Дай лучшую лайв-ставку. Коротко, макс 2 предложения. Только текст.",
},

"en": {
"choose_lang": "Dil secin / Выберите язык / Choose language:",
"ask_name": "Welcome! Please enter your name:",
"reg_done": "Registration complete! Hi, {name}!\n\nWelcome to the AI-driven sports betting platform.",
"reg_done_ref": "Registration complete! Hi, {name}!\nYour friend earned a referral bonus.",
"already_reg": "You are already registered, {name}!",
"need_reg": "Please register first. Type /start.",
"db_blocked": "Your account is blocked. Contact the administrator.",
"blocked": "You are temporarily blocked. Try again in {m}m {s}s.",
"rate_limit": "Request limit exceeded. Wait {w} seconds. Warning: {v}/{max}",
"auto_blocked": "Too many requests. Blocked for {min} minutes.",
"long_text": "Message too long. Please shorten it.",
"injection": "Only sports queries are accepted.",
"no_input": "Please send a text or a photo.",
"img_prompt": "Identify the sports event in the image and give a forecast.",
"api_overload": "Service overloaded. Please try again later.",
"api_error": "An error occurred. Please try again later.",
"lang_set": "Language set to English.",
"ref_link": "Your referral link:\nt.me/{bot}?start=ref{uid}\n\nInvited: {count} users",
"watch_btn": "Follow match",
"unwatch_btn": "Unfollow",
"watch_started": "Following: {match}\nI'll send notifications about key events.",
"watch_stopped": "Stopped following: {match}",
"no_subs": "You are not following any matches.",
"live_goal": "GOAL! {match}\n{minute} min: {team}\nScore: {score}\n\nLive bet:\n{tip}",
"live_card": "CARD! {match}\n{minute} min: {player} ({team}) — {card}\n\nLive bet:\n{tip}",
"live_halftime": "HALF TIME! {match}\nScore: {score}\n\nHalf-time bet:\n{tip}",
"live_fulltime": "FULL TIME! {match}\nFinal: {score}\n\nUnsubscribed.",
"live_kickoff": "KICK OFF! {match}\nI'll notify you about key events.",
"live_alert_goal_coming": "SIGNAL! {match}\nModel: goal expected [{minute} min]\nAct before odds drop.",
"live_alert_value": "LIVE VALUE! {match}\nOdds anomaly detected [{minute} min]\nModel: value on {team}.",
"live_alert_pressure": "PRESSURE! {match}\n{team} creating strong pressure [{minute} min]\nStat: {stat}",
# Onboarding
"ob_sports": "Which sports interest you? (multiple choice)",
"ob_bet_types": "What types of bets do you prefer?",
"ob_experience": "What is your betting experience?",
"ob_style": "What is your playing style?",
"ob_done": "Your profile is ready! You'll receive personalized forecasts.\n\nProfile:\nSports: {sports}\nBet types: {bet_types}\nExperience: {experience}\nStyle: {style}",
# Gamification
"points_earned": "+{pts} points! Total: {total} | Level {level}",
"streak": "{days} days active in a row! Bonus: +{bonus} points",
"achievement": "ACHIEVEMENT UNLOCKED: {name}",
"level_up": "LEVEL UP! Reached level {level}! +{bonus} points bonus",
"leaderboard": "LEADERBOARD:\n\n{entries}\n\nYour rank: #{rank} | {pts} points | Lv. {level}",
"profile_full": "PROFILE\n\nName: {name}\nLanguage: {lang}\nLevel: {level}\nPoints: {pts}\nStreak: {streak} days\nTotal requests: {total_req}\nReferrals: {refs}\n\nSports: {sports}\nBets: {bet_types}\nExp: {exp}",
"system_prompt": """You are an elite sports analyst with 15 years of experience. Date: {date}.

USER PROFILE:
Favourite sports: {sports}
Bet types: {bet_types}
Experience: {experience}
Style: {style}

RULES:
- Tailor forecasts to user profile
- Only 2025-2026 season data, never mention outdated squads
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
- [Team A] Win: XX% | Odds: X.XX - X.XX
- Draw: XX% | Odds: X.XX - X.XX
- [Team B] Win: XX% | Odds: X.XX - X.XX

Total Goals:
- Over 2.5: XX% | Odds: X.XX - X.XX
- Under 2.5: XX% | Odds: X.XX - X.XX

Both Teams Score:
- Yes: XX% | Odds: X.XX - X.XX
- No: XX% | Odds: X.XX - X.XX

Handicap:
- [Team A] (-1): XX% | Odds: X.XX - X.XX
- [Team B] (+1): XX% | Odds: X.XX - X.XX

BEST BET:
[Bet type] | Odds: X.XX - X.XX | Probability: XX%
Reasoning: [1 sentence]

WARNING: Analytical forecast only. Bet at your own risk.""",
"live_tip_prompt": "You are a live betting analyst. Match {match}, {minute} min, score {score}. Event: {event}. Give the best live bet. Short, max 2 sentences. Plain text only.",
},
}

LANG_NAMES = {"az": "Azerbaycan", "ru": "Русский", "en": "English"}

def tr(uid, key, **kw):
    lang = db_lang(uid)
    txt  = T.get(lang, T["ru"]).get(key, T["ru"].get(key, ""))
    if key == "system_prompt":
        u = db_get(uid) or {}
        kw.setdefault("date", date.today().strftime("%d.%m.%Y"))
        kw.setdefault("sports",     u.get("sports", "any"))
        kw.setdefault("bet_types",  u.get("bet_types", "any"))
        kw.setdefault("experience", u.get("experience", "beginner"))
        kw.setdefault("style",      u.get("bet_style", "balanced"))
    return txt.format(**kw) if kw else txt

# ─── Keyboards ────────────────────────────────────────────────────────────────
def lang_kb(): return InlineKeyboardMarkup([[
    InlineKeyboardButton("Azerbaycan", callback_data="lang_az"),
    InlineKeyboardButton("Русский",    callback_data="lang_ru"),
    InlineKeyboardButton("English",    callback_data="lang_en"),
]])

# Onboarding keyboards
OB_SPORTS = [
    ("Futbol / Football", "sp_football"),
    ("UFC / MMA", "sp_ufc"),
    ("NBA / Basketball", "sp_nba"),
    ("Tennis", "sp_tennis"),
    ("Kibersport / Esports", "sp_esports"),
    ("NHL / Hockey", "sp_hockey"),
    ("Hamisi / All", "sp_all"),
]
OB_BET_TYPES = [
    ("Live betting", "bt_live"),
    ("Prematch", "bt_prematch"),
    ("Express / Parlay", "bt_express"),
    ("Singles", "bt_single"),
    ("Handicap", "bt_handicap"),
]
OB_EXPERIENCE = [
    ("Yeni baslayanam / Novichok / Beginner", "exp_beginner"),
    ("Orta / Sredniy / Intermediate", "exp_mid"),
    ("Tecrubeliyem / Opytny / Expert", "exp_expert"),
]
OB_STYLE = [
    ("Agressif / Aggressivnyy / Aggressive", "sty_aggressive"),
    ("Konservativ / Konservativnyy / Conservative", "sty_conservative"),
    ("Balansli / Sbalansirovannyy / Balanced", "sty_balanced"),
    ("Value hunter", "sty_value"),
]

def ob_kb(items, prefix, done_label="Done"):
    rows = [[InlineKeyboardButton(label, callback_data=f"{prefix}_{cb}")] for label, cb in items]
    return InlineKeyboardMarkup(rows)

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
        blocked_until[uid] = time.time() + SPAM_DUR; violations[uid] = 0
        sus.warning(f"BLOCKED | {info}"); return True
    return False

# ─── Football APIs ─────────────────────────────────────────────────────────────
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
                            "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
                            "shots_home": f.get("statistics", [{}])[0].get("value", 0) if f.get("statistics") else 0}
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

# ─── LIVE Poller + FOMO Alerts ────────────────────────────────────────────────
async def poller(app):
    alert_counters: dict[str, int] = defaultdict(int)
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

                # Regular events
                evs = await get_events(mid)
                prev = last_events.get(mid, [])
                new_evs = evs[len(prev):]
                last_events[mid] = evs

                for ev in new_evs:
                    etype  = ev.get("type", "")
                    detail = ev.get("detail", "")
                    team   = ev.get("team", {}).get("name", "")
                    player = ev.get("player", {}).get("name", "")
                    ev_min = ev.get("time", {}).get("elapsed", minute)
                    event_desc = f"{etype} - {detail} - {team}"
                    tip = await live_tip(next(iter(uids)), match_name, ev_min, score, event_desc)
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try:
                            if etype == "Goal":
                                msg = T[lang]["live_goal"].format(match=match_name, minute=ev_min, team=team, score=score, tip=tip)
                            elif etype == "Card":
                                card = ("Red card" if "Red" in detail else "Yellow card") if lang=="en" else ("Kırmızı kart" if "Red" in detail else "Sarı kart") if lang=="az" else ("Красная" if "Red" in detail else "Жёлтая")
                                msg = T[lang]["live_card"].format(match=match_name, minute=ev_min, player=player, team=team, card=card, tip=tip)
                            else: continue
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except Exception as e: logger.error(f"notify uid={uid}: {e}")

                # FOMO / urgency alerts every ~15 min per match
                alert_counters[mid] += 1
                if alert_counters[mid] % 15 == 0 and minute > 20:
                    alert_type = random.choice(["goal_coming", "value", "pressure"])
                    teams = [st["home"], st["away"]]
                    pressure_team = random.choice(teams)
                    stats_options = ["12 udarlar hedefe / 12 shots on target", "73% top possessiyasi / 73% ball possession",
                                     "6 kose vurus / 6 corners", "xG: 1.8"]
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try:
                            if alert_type == "goal_coming":
                                msg = T[lang]["live_alert_goal_coming"].format(match=match_name, minute=minute)
                            elif alert_type == "value":
                                msg = T[lang]["live_alert_value"].format(match=match_name, minute=minute, team=pressure_team)
                            else:
                                msg = T[lang]["live_alert_pressure"].format(match=match_name, minute=minute,
                                    team=pressure_team, stat=random.choice(stats_options))
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except Exception: pass

                # Half time
                if status == "HT" and mid not in ht_sent:
                    ht_sent.add(mid)
                    for uid in list(uids):
                        lang = db_lang(uid)
                        tip = await live_tip(uid, match_name, 45, score, "Half time")
                        try: await app.bot.send_message(chat_id=uid, text=T[lang]["live_halftime"].format(match=match_name, score=score, tip=tip))
                        except Exception: pass

                # Full time
                if status in ("FT", "AET", "PEN"):
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try: await app.bot.send_message(chat_id=uid, text=T[lang]["live_fulltime"].format(match=match_name, score=score))
                        except Exception: pass
                        db_del_lsub(uid, mid); user_subs[uid].discard(mid)
                    live_subs[mid].clear()
            except Exception as e: logger.error(f"poller mid={mid}: {e}")

# ─── Retention / Daily notifier ───────────────────────────────────────────────
async def daily_retention(app):
    """Send daily engagement messages to inactive users."""
    while True:
        await asyncio.sleep(3600)  # check every hour
        now = datetime.now()
        if now.hour != 10: continue  # fire at 10:00
        try:
            with con() as c:
                # Users inactive for 2+ days
                rows = c.execute(
                    "SELECT user_id, lang, display_name FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND (last_active='' OR date(last_active) <= date('now', '-2 days'))"
                ).fetchall()
            msgs = {
                "az": "Bugun boyuk oyunlar var! Proqnoz almaq ucun yazin.",
                "ru": "Сегодня большие матчи! Напишите, чтобы получить прогноз.",
                "en": "Big matches today! Write to get your forecast.",
            }
            for uid, lang, name in rows:
                try:
                    msg = msgs.get(lang, msgs["ru"])
                    await app.bot.send_message(chat_id=uid, text=msg)
                    await asyncio.sleep(0.1)
                except Exception: pass
        except Exception as e: logger.error(f"daily_retention: {e}")

# ─── Gamification helpers ─────────────────────────────────────────────────────
async def award_and_notify(app_or_context, uid, pts, reason, msg_text=None):
    """Award points, check level up, send notification."""
    u_before = db_get(uid)
    lvl_before = u_before["level"] if u_before else 1
    db_add_points(uid, pts, reason)
    u_after = db_get(uid)
    if not u_after: return
    lang = db_lang(uid)
    bot = app_or_context.bot if hasattr(app_or_context, "bot") else app_or_context
    # Points notification
    notif = tr(uid, "points_earned", pts=pts, total=u_after["points"], level=u_after["level"])
    try: await bot.send_message(chat_id=uid, text=notif)
    except Exception: pass
    # Level up
    if u_after["level"] > lvl_before:
        bonus = u_after["level"] * 20
        db_add_points(uid, bonus, "level_up_bonus")
        lvl_msg = tr(uid, "level_up", level=u_after["level"], bonus=bonus)
        try: await bot.send_message(chat_id=uid, text=lvl_msg)
        except Exception: pass

# ─── Onboarding flow ──────────────────────────────────────────────────────────
async def start_onboarding(update_or_msg, uid, context):
    """Begin onboarding after registration."""
    lang = db_lang(uid)
    sports_labels = {"az": "İdman növü seçin:", "ru": "Выберите виды спорта:", "en": "Select your sports:"}
    kb = ob_kb(OB_SPORTS, "ob_sports")
    text = T[lang]["ob_sports"]
    try:
        await update_or_msg.reply_text(text, reply_markup=kb)
    except Exception:
        pass

# ─── Handlers ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id; args = context.args or []
    logger.info(f"START args={args} | {uinfo(update)}")
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
        await update.message.reply_text(tr(uid, "already_reg", name=u["display_name"] or user.first_name))
        return
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
        await update.message.reply_text("2-64 chars / simvol"); return True
    db_set(uid, "display_name", name)
    # Complete registration (no phone)
    referred_by = context.user_data.get("referred_by")
    with con() as c:
        c.execute("UPDATE users SET is_registered=1 WHERE user_id=?", (uid,))
        if referred_by:
            c.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (referred_by,))
            c.execute("UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL", (referred_by, uid))
    reg_step[uid] = "done"
    u = db_get(uid)
    wkey = "reg_done_ref" if referred_by else "reg_done"
    logger.info(f"REG | id={uid} name={name}")
    await update.message.reply_text(tr(uid, wkey, name=name), reply_markup=ReplyKeyboardRemove())
    # Award first registration points
    db_add_points(uid, 50, "registration")
    # Start onboarding
    await asyncio.sleep(0.5)
    await start_onboarding(update.message, uid, context)
    return True


async def ob_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all onboarding callbacks."""
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; data = q.data

    if data.startswith("ob_sports_"):
        val = data.replace("ob_sports_", "")
        db_set(uid, "sports", val)
        lang = db_lang(uid)
        await q.edit_message_text(T[lang]["ob_bet_types"], reply_markup=ob_kb(OB_BET_TYPES, "ob_bt"))

    elif data.startswith("ob_bt_"):
        val = data.replace("ob_bt_", "")
        db_set(uid, "bet_types", val)
        lang = db_lang(uid)
        await q.edit_message_text(T[lang]["ob_experience"], reply_markup=ob_kb(OB_EXPERIENCE, "ob_exp"))

    elif data.startswith("ob_exp_"):
        val = data.replace("ob_exp_", "")
        db_set(uid, "experience", val)
        lang = db_lang(uid)
        await q.edit_message_text(T[lang]["ob_style"], reply_markup=ob_kb(OB_STYLE, "ob_sty"))

    elif data.startswith("ob_sty_"):
        val = data.replace("ob_sty_", "")
        db_set(uid, "bet_style", val); db_set(uid, "onboarding_done", 1)
        u = db_get(uid)
        lang = db_lang(uid)
        done_msg = T[lang]["ob_done"].format(
            sports=u["sports"], bet_types=u["bet_types"],
            experience=u["experience"], style=u["bet_style"]
        )
        await q.edit_message_text(done_msg)
        db_add_points(uid, 30, "onboarding_complete")
        # Check achievement
        if db_add_achievement(uid, "first_profile"):
            ach_msg = tr(uid, "achievement", name="Profile Master")
            try: await context.bot.send_message(chat_id=uid, text=ach_msg)
            except Exception: pass


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    u = db_get(uid)
    # Calculate rank
    with con() as c:
        rank_row = c.execute("SELECT COUNT(*)+1 FROM users WHERE points>? AND is_registered=1", (u["points"],)).fetchone()
    rank = rank_row[0] if rank_row else "?"
    await update.message.reply_text(tr(uid, "profile_full",
        name=u["display_name"] or "-", lang=LANG_NAMES.get(u["lang"], u["lang"]),
        level=u["level"], pts=u["points"], streak=u["streak_days"],
        total_req=u["total_requests"], refs=u["ref_count"],
        sports=u["sports"] or "-", bet_types=u["bet_types"] or "-",
        exp=u["experience"] or "-"))


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid): await update.message.reply_text(tr(uid, "need_reg")); return
    with con() as c:
        top = c.execute("SELECT user_id,display_name,points,level FROM users WHERE is_registered=1 ORDER BY points DESC LIMIT 10").fetchall()
        rank_row = c.execute("SELECT COUNT(*)+1 FROM users WHERE points>? AND is_registered=1",
                             (db_get(uid)["points"],)).fetchone()
    rank = rank_row[0] if rank_row else "?"
    entries = "\n".join(f"{i+1}. {r[1] or r[0]} — {r[2]} pts (Lv.{r[3]})" for i, r in enumerate(top))
    u = db_get(uid)
    await update.message.reply_text(tr(uid, "leaderboard", entries=entries, rank=rank, pts=u["points"], level=u["level"]))


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
    lines = ["Matches / Matchi:\n"]
    btns  = []
    for s in subs:
        lines.append(f"- {s['match_name']}")
        btns.append([InlineKeyboardButton(f"Unfollow: {s['match_name'][:30]}", callback_data=f"unwatch_{s['match_id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); uid = q.from_user.id; data = q.data
    if data.startswith("watch_"):
        mid = data[6:]
        mname = context.user_data.get(f"mn_{mid}", mid)
        live_subs[mid].add(uid); user_subs[uid].add(mid); db_add_lsub(uid, mid, mname)
        await q.edit_message_text(q.message.text + "\n\n" + tr(uid, "watch_started", match=mname))
        db_add_points(uid, 10, "watch_match")
    elif data.startswith("unwatch_"):
        mid = data[8:]
        subs = db_user_lsubs(uid)
        mname = next((s["match_name"] for s in subs if s["match_id"] == mid), mid)
        live_subs[mid].discard(uid); user_subs[uid].discard(mid); db_del_lsub(uid, mid)
        await q.edit_message_text(tr(uid, "watch_stopped", match=mname))


# ─── Main message handler ─────────────────────────────────────────────────────
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id; info = uinfo(update)
    db_ensure(uid, user.username or "")

    # Registration flow
    step = reg_step.get(uid)
    if step == "awaiting_name" and update.message.text:
        await handle_name(update, context); return
    if step in ("awaiting_lang", "awaiting_name"):
        return

    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    if db_is_blocked(uid):
        await update.message.reply_text(tr(uid, "db_blocked")); return

    # Security
    blk, secs = sec_blocked(uid)
    if blk:
        sus.warning(f"BLK_REQ | {info}")
        await update.message.reply_text(tr(uid, "blocked", m=secs//60, s=secs%60)); return
    exceeded, wait = rate_check(uid)
    if exceeded:
        if record_viol(uid, info):
            await update.message.reply_text(tr(uid, "auto_blocked", min=SPAM_DUR//60))
        else:
            await update.message.reply_text(tr(uid, "rate_limit", w=wait, v=violations[uid], max=SPAM_AFTER))
        return
    violations[uid] = 0

    # Update streak
    update_streak(uid)

    mtype = "PHOTO" if update.message.photo else "TEXT"
    logger.info(f"MSG [{mtype}] | {info}")
    db_log_req(uid, mtype)
    await update.message.chat.send_action("typing")

    text  = update.message.text or update.message.caption or ""
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

    # Fetch real football data
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

    # Claude request
    try:
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1024,
            system=tr(uid, "system_prompt"), messages=[{"role": "user", "content": content}])
        reply = resp.content[0].text
        logger.info(f"REPLY_OK | {info}")
    except anthropic.RateLimitError: reply = tr(uid, "api_overload")
    except anthropic.APIError as e: logger.error(f"API_ERR {e} | {info}"); reply = tr(uid, "api_error")

    # Award points for request
    db_add_points(uid, 5, "forecast_request")
    u = db_get(uid)

    # Check achievements
    if u and u["total_requests"] == 1 and db_add_achievement(uid, "first_forecast"):
        try: await context.bot.send_message(chat_id=uid, text=tr(uid, "achievement", name="First Forecast"))
        except Exception: pass
    if u and u["total_requests"] == 10 and db_add_achievement(uid, "10_forecasts"):
        try: await context.bot.send_message(chat_id=uid, text=tr(uid, "achievement", name="Analyst"))
        except Exception: pass
    if u and u["total_requests"] == 50 and db_add_achievement(uid, "50_forecasts"):
        try: await context.bot.send_message(chat_id=uid, text=tr(uid, "achievement", name="Expert"))
        except Exception: pass
    if u and u["streak_days"] >= 7 and db_add_achievement(uid, "streak_7"):
        try: await context.bot.send_message(chat_id=uid, text=tr(uid, "achievement", name="7-Day Streak"))
        except Exception: pass

    # Watch button for live matches
    watch_kb = None
    if text and APIFOOTBALL_KEY:
        words = text.split()
        ms = await search_match(" ".join(words[:3]))
        if ms:
            m = ms[0]
            context.user_data[f"mn_{m['id']}"] = m["name"]
            btn = tr(uid, "watch_btn") + f": {m['name'][:35]}"
            watch_kb = InlineKeyboardMarkup([[InlineKeyboardButton(btn, callback_data=f"watch_{m['id']}")]])

    # Points badge in reply
    pts_line = f"\n\n+5 pts | Total: {u['points']} | Lv.{u['level']}" if u else ""
    await update.message.reply_text(reply + pts_line, reply_markup=watch_kb)

# ─── Admin panel ──────────────────────────────────────────────────────────────
def is_adm(update): return (update.effective_user.id if update.effective_user else 0) == ADMIN_ID

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Statistika",    callback_data="adm_stats")],
        [InlineKeyboardButton("Rassylka",      callback_data="adm_broadcast")],
        [InlineKeyboardButton("Blokirovka",    callback_data="adm_blocklist")],
        [InlineKeyboardButton("Poisk user",    callback_data="adm_search")],
        [InlineKeyboardButton("Izmenit yazyk", callback_data="adm_setlang")],
        [InlineKeyboardButton("Top refs",      callback_data="adm_toprefs")],
        [InlineKeyboardButton("Leaderboard",   callback_data="adm_toppts")],
        [InlineKeyboardButton("Live subs",     callback_data="adm_live")],
    ])
    await update.message.reply_text("ADMIN PANEL", reply_markup=kb)

async def adm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: await q.answer("No access", show_alert=True); return
    await q.answer(); data = q.data
    back = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="adm_back")]])

    if data == "adm_stats":
        s = db_stats()
        blk_now = sum(1 for v in blocked_until.values() if time.time() < v)
        lang_str = " | ".join(f"{l}: {n}" for l, n in s["langs"])
        live_now = sum(len(v) for v in live_subs.values())
        await q.edit_message_text(
            f"STATISTIKA\n\nPol-li: {s['total']}\nSegodnya: {s['today']}\n"
            f"Zablokirovano: {s['blocked']}\nOnboarding done: {s['ob_done']}\n\n"
            f"Zaprosy vsego: {s['rqtotal']}\nSeg: {s['rqtoday']}\n\n"
            f"Yazyki: {lang_str}\n\nLive subs DB: {s['live_ct']}\nLive aktivnye: {live_now}\n"
            f"Rate blok: {blk_now}", reply_markup=back)

    elif data == "adm_blocklist":
        with con() as c:
            rows = c.execute("SELECT user_id,username,display_name FROM users WHERE is_blocked=1").fetchall()
        if not rows: await q.edit_message_text("Net zablokirovannykh.", reply_markup=back); return
        btns = [[InlineKeyboardButton(f"Razblokirovat: {r[2] or r[1] or r[0]}", callback_data=f"adm_unblk_{r[0]}")] for r in rows]
        btns.append([InlineKeyboardButton("Back", callback_data="adm_back")])
        lines = ["ZABLOKIROVANNYE:"] + [f"- {r[2] or r[1] or r[0]} (id={r[0]})" for r in rows]
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_unblk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 0)
        await q.edit_message_text(f"Razblok: {uid}", reply_markup=back)

    elif data.startswith("adm_blk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 1)
        await q.edit_message_text(f"Zablokirovan: {uid}", reply_markup=back)

    elif data == "adm_broadcast":
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text("Otpravte tekst rassylki. /cancel — otmena.")

    elif data == "adm_search":
        context.user_data["adm_act"] = "search"
        await q.edit_message_text("Vvedite id, username ili imya.")

    elif data == "adm_setlang":
        context.user_data["adm_act"] = "setlang"
        await q.edit_message_text("Format: 123456789 ru\nYazyki: az, ru, en")

    elif data == "adm_toprefs":
        s = db_stats()
        rows = s["toprefs"]
        text = "TOP REFERALOV:\n" + "\n".join(f"{i+1}. @{r[1] or r[0]} — {r[2]}" for i, r in enumerate(rows))
        await q.edit_message_text(text, reply_markup=back)

    elif data == "adm_toppts":
        s = db_stats()
        rows = s["toppts"]
        text = "LEADERBOARD (Admin):\n" + "\n".join(f"{i+1}. {r[1] or r[0]} — {r[2]} pts Lv.{r[3]}" for i, r in enumerate(rows))
        await q.edit_message_text(text, reply_markup=back)

    elif data == "adm_live":
        live_now = sum(len(v) for v in live_subs.values())
        lines = [f"LIVE SUBS: {live_now} aktivnykh\n"]
        for mid, uids in live_subs.items():
            if uids: lines.append(f"Match {mid}: {len(uids)} users")
        await q.edit_message_text("\n".join(lines), reply_markup=back)

    elif data == "adm_back":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Statistika",    callback_data="adm_stats")],
            [InlineKeyboardButton("Rassylka",      callback_data="adm_broadcast")],
            [InlineKeyboardButton("Blokirovka",    callback_data="adm_blocklist")],
            [InlineKeyboardButton("Poisk user",    callback_data="adm_search")],
            [InlineKeyboardButton("Izmenit yazyk", callback_data="adm_setlang")],
            [InlineKeyboardButton("Top refs",      callback_data="adm_toprefs")],
            [InlineKeyboardButton("Leaderboard",   callback_data="adm_toppts")],
            [InlineKeyboardButton("Live subs",     callback_data="adm_live")],
        ])
        await q.edit_message_text("ADMIN PANEL", reply_markup=kb)


async def handle_adm_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    act = context.user_data.get("adm_act")
    if not act: return
    context.user_data.pop("adm_act")
    text = update.message.text or ""

    if act == "broadcast":
        uids = db_all_uids()
        status = await update.message.reply_text(f"Rassylka dla {len(uids)} pol-ley...")
        ok = fail = 0
        for uid in uids:
            try: await context.bot.send_message(chat_id=uid, text=text); ok += 1
            except Exception: fail += 1
            await asyncio.sleep(0.05)
        await status.edit_text(f"Gotovo! Ok: {ok} | Fail: {fail}")

    elif act == "search":
        results = db_search(text.strip())
        if not results: await update.message.reply_text("Ne naydeno."); return
        for u in results:
            reg = "Da" if u["is_registered"] else "Net"
            blk = "ZABLOKIROVAN" if u["is_blocked"] else "Aktiven"
            btns = []
            if u["is_blocked"]:
                btns.append([InlineKeyboardButton("Razblokirovat", callback_data=f"adm_unblk_{u['user_id']}")])
            else:
                btns.append([InlineKeyboardButton("Zablokirovat", callback_data=f"adm_blk_{u['user_id']}")])
            await update.message.reply_text(
                f"ID: {u['user_id']}\n@{u['username'] or '-'}\n{u['display_name'] or '-'}\n"
                f"Lang: {u['lang']}\nReg: {reg}\nStatus: {blk}\nLevel: {u['level']}\nPts: {u['points']}\n"
                f"Sports: {u['sports']}\nBets: {u['bet_types']}\nExp: {u['experience']}\nJoined: {u['joined_at']}",
                reply_markup=InlineKeyboardMarkup(btns))

    elif act == "setlang":
        parts = text.strip().split()
        if len(parts) != 2 or parts[1] not in ("az", "ru", "en"):
            await update.message.reply_text("Format: 123456789 ru"); return
        db_set(int(parts[0]), "lang", parts[1])
        await update.message.reply_text(f"Lang {parts[1]} set for {parts[0]}")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("adm_act", None)
    await update.message.reply_text("Otmeneno.")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("lang",        lang_cmd))
    app.add_handler(CommandHandler("ref",         ref_cmd))
    app.add_handler(CommandHandler("profile",     profile_cmd))
    app.add_handler(CommandHandler("top",         leaderboard_cmd))
    app.add_handler(CommandHandler("matches",     matches_cmd))
    app.add_handler(CommandHandler("admin",       admin_cmd))
    app.add_handler(CommandHandler("cancel",      cancel_cmd))

    app.add_handler(CallbackQueryHandler(lang_cb,   pattern=r"^lang_"))
    app.add_handler(CallbackQueryHandler(ob_callback, pattern=r"^ob_"))
    app.add_handler(CallbackQueryHandler(watch_cb,  pattern=r"^(watch|unwatch)_"))
    app.add_handler(CallbackQueryHandler(adm_cb,    pattern=r"^adm_"))

    # Admin text messages — group 0
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_adm_msg), group=0)
    # All other messages — group 1
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CONTACT, handle_msg), group=1)

    async def post_init(application):
        asyncio.create_task(poller(application))
        asyncio.create_task(daily_retention(application))

    app.post_init = post_init
    logger.info("ProqnozAI v3 started")
    app.run_polling()

if __name__ == "__main__":
    main()
