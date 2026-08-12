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

# httpx logs every request URL at INFO — that includes the Telegram bot TOKEN in
# the getUpdates URL, plus one line every few seconds. Quiet these to WARNING so
# secrets never reach the logs and long-polling doesn't spam them.
for _noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

sus = logging.getLogger("suspicious")
_sh = RotatingFileHandler("suspicious.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
sus.addHandler(_sh); sus.setLevel(logging.WARNING)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))
# Promo gate: channel the user must be subscribed to before claiming a code.
# PROMO_CHANNEL is the getChatMember target ("@name" or a "-100…" id); the bot
# MUST be an admin/member of it. PROMO_CHANNEL_URL is the public link for the
# "open channel" button (derived from @name when omitted). Empty channel ⇒ the
# promo feature stays hidden.
PROMO_CHANNEL     = os.environ.get("PROMO_CHANNEL", "").strip()
PROMO_CHANNEL_URL = os.environ.get("PROMO_CHANNEL_URL", "").strip()
# "Our partners" menu button. Configure via PARTNERS as "Label | https://url"
# entries separated by ';' (newlines also work), e.g.
#   PARTNERS=Mostbet | https://mostbet.com;1xBet | https://1xbet.com
# Legacy single-link PARTNERS_URL is still accepted (labeled at render time).
# No partners configured ⇒ the button stays hidden.
PARTNERS_URL      = os.environ.get("PARTNERS_URL", "").strip()


def _parse_partners(raw: str, legacy_url: str) -> list[tuple[str, str]]:
    """List of (label, url). Each entry is "Label | url"; a bare url gets an
    empty label (the UI then uses a generic 'open partner' caption). Only
    http(s) links are kept, so a malformed env can't inject junk buttons."""
    out: list[tuple[str, str]] = []
    for chunk in (raw or "").replace(";", "\n").split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, _, url = chunk.partition("|")
        url = (url or label).strip()
        label = label.strip() if url != label.strip() else ""
        if url.startswith(("http://", "https://")):
            out.append((label, url))
    if not out and legacy_url.startswith(("http://", "https://")):
        out.append(("", legacy_url))
    return out


PARTNERS = _parse_partners(os.environ.get("PARTNERS", ""), PARTNERS_URL)

# Public base URL of the dashboard service, used to count partner clicks:
# buttons point at <base>/r/<partner>?u=<uid> which records the click and
# redirects on. Telegram reports nothing about taps on a plain URL button, so
# without this there is no click data at all.
#
# LEAVING IT UNSET IS THE SAFE DEFAULT: buttons then link straight to the
# partner exactly as before. Set it only once the dashboard is reliably
# reachable — while it is set, a dashboard outage breaks partner links.
PARTNER_REDIRECT_BASE = os.environ.get("PARTNER_REDIRECT_BASE", "").rstrip("/")
# Shared with the dashboard (same env var it authenticates with). Used to sign
# the user id on partner links so a click cannot be attributed to someone else,
# or invented wholesale by hitting the redirect URL.
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
# Accept either name: FOOTBALL_KEY (preferred) or legacy FOOTBALL_API_KEY.
FOOTBALL_KEY    = os.environ.get("FOOTBALL_KEY") or os.environ.get("FOOTBALL_API_KEY", "")
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
MOSTBET_BASE    = "https://mostbet2.com"   # Odds Checker API (IP whitelisted)
# Mostbet returns its "DD.MM.YYYY HH:MM" times in Moscow time (UTC+3).
# Used by both the date-window filter and display formatting — keep in sync.
MOSTBET_SRC_TZ  = 3

RATE_WINDOW = 60; RATE_MAX = 5; SPAM_AFTER = 3; SPAM_DUR = 600
# Pure menu navigation (sport/day/country/league/pagination/back) is cheap and
# clicked in quick bursts, so it gets its OWN, far more generous budget — a
# short sliding window with a high cap — instead of sharing the strict text /
# expensive-call budget above. Exceeding it only soft-throttles (a toast); it
# never accrues violations toward the SPAM_DUR auto-block. Expensive callbacks
# (fm_match_cb → Claude) keep the strict RATE_MAX budget via cb_guard.
NAV_RATE_WINDOW = 10; NAV_RATE_MAX = 15
MOSTBET_CACHE_TTL = 900           # match LIST cache (15 min — list moves slowly)
# Odds move much faster than the match list: a 15-min snapshot visibly diverges
# from the live site. Keep odds fresh, and never pin a failed/empty fetch for
# long — one network hiccup must not mean "no odds" until the next TTL.
MOSTBET_ODDS_TTL = 120            # per-line odds cache (2 min)
MOSTBET_ODDS_EMPTY_TTL = 45       # cache for a fetch that yielded no values

# ─── In-memory ────────────────────────────────────────────────────────────────
msg_times:     dict[int, deque] = defaultdict(deque)
nav_times:     dict[int, deque] = defaultdict(deque)   # separate budget for menu navigation
violations:    dict[int, int]   = defaultdict(int)
blocked_until: dict[int, float] = {}
reg_step:      dict[int, str]   = {}
live_subs:     dict[str, set]   = defaultdict(set)
mostbet_cache: dict              = {}   # cache: key -> (timestamp, data)
demand_cache:  dict              = {}   # cache: days -> (timestamp, demand dict)
winrate_cache: dict              = {}   # cache: days -> (timestamp, bot-winrate dict|None)
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
