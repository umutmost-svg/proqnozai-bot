import os
import logging
from collections import defaultdict, deque

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
MOSTBET_CACHE_TTL = 900           # 15 minutes cache

# ─── In-memory ────────────────────────────────────────────────────────────────
msg_times:     dict[int, deque] = defaultdict(deque)
violations:    dict[int, int]   = defaultdict(int)
blocked_until: dict[int, float] = {}
reg_step:      dict[int, str]   = {}
live_subs:     dict[str, set]   = defaultdict(set)
mostbet_cache: dict              = {}   # cache: key -> (timestamp, data)
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
