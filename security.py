import re
import time
import logging

from config import msg_times, violations, blocked_until, RATE_WINDOW, RATE_MAX, SPAM_AFTER, SPAM_DUR

sus = logging.getLogger("suspicious")

# ─── Prompt-injection detection (multilingual) ────────────────────────────────
# Patterns matched case-insensitively against incoming text/captions.
_INJECTION_PATTERNS = [
    # English
    r"ignore (all |the |your )?(previous|prior|above)",
    r"forget (all |your |the )?(instructions|context|everything)",
    r"disregard (all |the |your )?(previous|prior|above|instructions)",
    # NOTE: no bare \bDAN\b here. With IGNORECASE it matched the ordinary
    # Turkish ablative suffix after an apostrophe ("Trabzonspor'dan haber…"),
    # auto-blocking legitimate users in a primary market. The jailbreak is
    # caught by its explicit phrasings instead.
    r"system\s*prompt", r"\bjailbreak\b", r"\bDAN mode\b", r"\bdo anything now\b",
    r"act as (a |an )?(dan|jailbroken|unrestricted)",
    r"you are (now )?(a |an )?(dan|unrestricted|developer mode)",
    r"developer mode", r"reveal (your |the )?(prompt|instructions|system)",
    r"print (your |the )?(prompt|instructions|system)",
    r"repeat (the |your )?(prompt|instructions|words above)",
    r"new instructions?:", r"override",
    # Russian
    r"игнорир\w* (все |предыдущ\w*|выше)",
    r"забудь (все |инструкц\w*|контекст|предыдущ\w*)",
    r"систем\w* промпт", r"систем\w* инструкц",
    r"покажи (свой |системн\w*)?(промпт|инструкц)",
    r"новые инструкц", r"режим разработчика",
    # Turkish
    r"önceki (talimat|komut)\w*\s*(yoksay|unut)",
    r"sistem (promptu|talimat)",
    # Arabic
    r"تجاهل (التعليمات|السابق)", r"انس (التعليمات|كل)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def detect_injection(text: str) -> bool:
    """Return True if text looks like a prompt-injection / jailbreak attempt."""
    if not text:
        return False
    return bool(_INJECTION_RE.search(text))


def uinfo(update):
    u = update.effective_user
    return f"id={u.id} @{u.username or '-'} {u.full_name}"


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
