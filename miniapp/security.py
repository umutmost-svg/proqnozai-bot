"""Request-level protections: Origin allowlist, CSRF tokens, login rate limit."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_logger = logging.getLogger("miniapp")


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


# ---- admin access policy (block = no Mini App access; nothing is deleted) ---------------------------------

ACCESS_POLICY_FILE = "access-policy.json"
ACCESS_RELOAD_S = 30
MAX_BLOCKED = 100_000


class AccessPolicyError(ValueError):
    pass


def parse_access_policy(raw: bytes, public_key: Ed25519PublicKey) -> tuple[int, frozenset[int]]:
    """(version, blocked Telegram ids) of a snapshot signed by the admin's Ed25519 key; anything else raises.

    The signature covers the canonical JSON of every field except `signature` and `sha256` (the scheme of
    ads-policy.json)."""
    if len(raw) > 4 * 1024 * 1024:
        raise AccessPolicyError("too_large")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise AccessPolicyError("bad_json") from None
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("kind") != "access":
        raise AccessPolicyError("bad_schema")
    body = {k: v for k, v in data.items() if k not in ("signature", "sha256")}
    content = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    signature = str(data.get("signature"))
    try:
        public_key.verify(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)), content)
    except (InvalidSignature, ValueError, TypeError):
        raise AccessPolicyError("bad_signature") from None
    version, blocked = data.get("version"), data.get("blocked")
    if isinstance(version, bool) or not isinstance(version, int) or not 0 < version < 2**62:
        raise AccessPolicyError("bad_version")
    if not isinstance(blocked, list) or len(blocked) > MAX_BLOCKED or not all(
            isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2**53 for i in blocked):
        raise AccessPolicyError("bad_blocked")
    return version, frozenset(blocked)


class AccessPolicy:
    """Users blocked in the admin panel, from the signed `access-policy.json` next to the Mini App database.

    Re-read when the file changed (checked at most every ACCESS_RELOAD_S). A missing, unsigned, invalid or older
    file keeps the last good list, so deleting or replaying a file cannot unblock anybody while the process
    runs; MINIAPP_BLOCKED_USER_IDS applies independently. Blocking ends access only: no data is deleted."""

    def __init__(self, path: Path, public_key_hex: str, clock: Callable[[], float]) -> None:
        self.path = path
        # The applied version survives restarts: an older (replayed) file is refused after a restart too.
        self.mark = path.with_name(path.name + ".applied")
        self.public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        self.clock = clock
        self._lock = threading.Lock()
        self._version = self._read_mark()
        self._blocked: frozenset[int] = frozenset()
        self._stamp: tuple[int, int] | None = None
        self._stamp_applied: tuple[int, int] | None = None
        self._checked_at: float | None = None

    def _read_mark(self) -> int:
        try:
            value = int(self.mark.read_text().strip())
        except (OSError, ValueError):
            return 0  # a lost mark accepts the current file (the only one the admin keeps writing)
        # Versions are wall-clock milliseconds: a mark far in the future would refuse every real file.
        return value if 0 < value <= int(self.clock() * 1000) + 86_400_000 else 0

    def _write_mark(self, version: int) -> None:
        tmp = self.mark.with_name(self.mark.name + ".tmp")
        try:
            tmp.write_text(str(version))
            tmp.replace(self.mark)
        except OSError as exc:
            _logger.warning("event=access_mark_failed type=%s", type(exc).__name__)

    def blocked(self) -> frozenset[int]:
        with self._lock:
            now = self.clock()
            if self._checked_at is not None and 0 <= now - self._checked_at < ACCESS_RELOAD_S:
                return self._blocked
            self._checked_at = now
            try:
                st = self.path.stat()
                stamp = (st.st_mtime_ns, st.st_size)
                if stamp != self._stamp:
                    self._stamp = stamp
                    version, blocked = parse_access_policy(self.path.read_bytes(), self.public_key)
                    if version > self._version or (version == self._version and self._stamp_applied is None):
                        self._version, self._blocked, self._stamp_applied = version, blocked, stamp
                        self._write_mark(version)
                        _logger.info("event=access_policy_applied version=%s blocked=%s", version, len(blocked))
            except FileNotFoundError:
                pass
            except (OSError, AccessPolicyError) as exc:
                _logger.warning("event=access_policy_rejected reason=%s", exc)
            return self._blocked
