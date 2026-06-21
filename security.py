import time
import logging

from config import msg_times, violations, blocked_until, RATE_WINDOW, RATE_MAX, SPAM_AFTER, SPAM_DUR

sus = logging.getLogger("suspicious")


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
