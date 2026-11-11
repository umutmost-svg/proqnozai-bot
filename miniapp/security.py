"""Request-level protections: Origin allowlist, CSRF tokens, login rate limit."""
from __future__ import annotations

import hashlib
import hmac
import threading
from collections import deque
from collections.abc import Callable


def origin_allowed(origin: str | None, trusted: frozenset[str]) -> bool:
    """Exact match of the full serialized origin (scheme, host, port).

    A missing header and the opaque origin `null` are never trusted.
    """
    if not origin or origin == "null":
        return False
    return origin in trusted


class CsrfTokens:
    """Per-session CSRF token derived from the raw session token.

    Not stored anywhere: it is recomputed from the cookie, so it changes with
    every session and is useless without the matching HttpOnly cookie.
    """

    def __init__(self, key_material: str) -> None:
        # MINIAPP_CSRF_SECRET when set (always in Ed25519 mode), else the bot token as before.
        self._key = hmac.new(b"SporAI-MiniApp-CSRF", key_material.encode(), hashlib.sha256).digest()

    def for_session(self, session_token: str) -> str:
        return hmac.new(self._key, session_token.encode(), hashlib.sha256).hexdigest()

    def verify(self, session_token: str, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(self.for_session(session_token), presented)


class LoginRateLimiter:
    """Sliding-window limiter, in memory of ONE process.

    With several workers/replicas each keeps its own counters; this is not a
    distributed protection.
    """

    def __init__(self, limit: int, window_s: int, clock: Callable[[], float]) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Records an attempt. Returns None if allowed, else seconds to wait."""
        now = self._clock()
        with self._lock:
            if len(self._hits) > 10_000:
                self._prune(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1.0, hits[0] + self.window_s - now)
            hits.append(now)
            return None

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window_s]:
            del self._hits[key]
