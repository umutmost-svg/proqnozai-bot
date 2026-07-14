import asyncio
import os
import logging
from logging.handlers import RotatingFileHandler
from collections import defaultdict, deque

# ─── Logging ──────────────────────────────────────────────────────────────────
# Rotating handlers cap disk usage: bot.log 5MB×3, suspicious.log 2MB×3.
_bot_fh = RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_bot_fh, logging.StreamHandler()])
logger = logging.getLogger(__name__)
sus = logging.getLogger("suspicious")
_sh = RotatingFileHandler("suspicious.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
sus.addHandler(_sh); sus.setLevel(logging.WARNING)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))
# Accept either name: FOOTBALL_KEY (preferred) or legacy FOOTBALL_API_KEY.
FOOTBALL_KEY    = os.environ.get("FOOTBALL_KEY") or os.environ.get("FOOTBALL_API_KEY", "")
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
MOSTBET_BASE    = "https://mostbet2.com"   # Odds Checker API (IP whitelisted)
# Mostbet returns its "DD.MM.YYYY HH:MM" times in Moscow time (UTC+3).
# Used by both the date-window filter and display formatting — keep in sync.
MOSTBET_SRC_TZ  = 3

RATE_WINDOW = 60; RATE_MAX = 5; SPAM_AFTER = 3; SPAM_DUR = 600
MOSTBET_CACHE_TTL = 900           # match LIST cache (15 min — list moves slowly)
# Odds move much faster than the match list: a 15-min snapshot visibly diverges
# from the live site. Keep odds fresh, and never pin a failed/empty fetch for
# long — one network hiccup must not mean "no odds" until the next TTL.
MOSTBET_ODDS_TTL = 120            # per-line odds cache (2 min)
MOSTBET_ODDS_EMPTY_TTL = 45       # cache for a fetch that yielded no values

# ─── In-memory ────────────────────────────────────────────────────────────────
msg_times:     dict[int, deque] = defaultdict(deque)
violations:    dict[int, int]   = defaultdict(int)
blocked_until: dict[int, float] = {}
reg_step:      dict[int, str]   = {}
live_subs:     dict[str, set]   = defaultdict(set)
mostbet_cache: dict              = {}   # cache: key -> (timestamp, data)
last_events:   dict[str, list]  = {}
ht_sent:       set              = set()
_mostbet_lock: asyncio.Lock     = asyncio.Lock()

UNIVERSAL_WELCOME = """ProqnozAI

Azərbaycan: Dil seçin aşağıda
Русский: Выберите язык ниже
English: Choose language below
Türkçe: Aşağıdan dil seçin
Қазақша: Төменде тілді таңдаңыз
O'zbek: Quyida tilni tanlang
العربية: اختر اللغة أدناه
"""
