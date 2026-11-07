"""Quota-aware HTTP client for one sports data provider.

Every upstream request goes through ``ProviderClient.get_json``. It enforces, per
provider: a fixed base URL (paths only, no caller-supplied hosts), a per-minute
budget, a daily budget, limited concurrency, a timeout, retries with backoff for
transient failures only (timeouts, 5xx), and a cooldown after HTTP 429 that is
never retried immediately. Counters live in this process (the stand runs one
worker); they reset on restart, which the daily budget's margin accounts for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger("miniapp.sports")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class TransportError(Exception):
    """Network-level failure (timeout, connection reset, DNS)."""


# (url, headers, timeout_s) -> response. Injected so tests never touch the network.
Transport = Callable[[str, Mapping[str, str], float], Awaitable[HttpResponse]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would resend our headers (the provider key)
    to whatever host — even plain HTTP — the redirect names. A 30x surfaces as an
    error response instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


MAX_BODY_BYTES = 2 * 1024 * 1024
# Real connections to providers are capped here, not only by the async semaphore:
# a worker can outlive its caller's deadline in a socket read (each read is bounded
# by the socket timeout and the deadline check), but never adds a third connection.
_UPSTREAM_THREADS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sports-upstream")


def _read_with_deadline(stream, deadline: float, limit: int) -> bytes:
    """Read the body in chunks; the socket timeout only bounds each wait, so a
    response that trickles bytes would otherwise never end."""
    chunks: list[bytes] = []
    size = 0
    while size < limit:
        if time.monotonic() > deadline:
            raise TimeoutError("response deadline")
        # read1 returns what has arrived instead of waiting to fill the whole chunk.
        reader = getattr(stream, "read1", None) or stream.read
        chunk = reader(min(16 * 1024, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


async def urllib_transport(url: str, headers: Mapping[str, str], timeout_s: float) -> HttpResponse:
    deadline = time.monotonic() + timeout_s

    def call() -> HttpResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with _OPENER.open(request, timeout=timeout_s) as response:
                return HttpResponse(response.status, {k.lower(): v for k, v in response.headers.items()},
                                    _read_with_deadline(response, deadline, MAX_BODY_BYTES))
        except urllib.error.HTTPError as exc:
            # Status and headers only (429 needs Retry-After): the error body is never used,
            # and reading it could time out and turn a 429 into an unhandled 500.
            exc.close()
            return HttpResponse(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, b"")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(type(exc).__name__) from None

    # The absolute deadline also on the async side: the caller (and its concurrency
    # slot and the shared cache load) is released even if the thread is still blocked.
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(_UPSTREAM_THREADS, call), timeout_s + 1.0)
    except TimeoutError:
        raise TransportError("deadline") from None


class UpstreamError(Exception):
    """The provider could not answer; ``code`` is safe to show and log."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code  # quota_exhausted | rate_limited | timeout | upstream_error | bad_response | not_found
        self.status = status


@dataclass(frozen=True)
class ClientLimits:
    per_minute: int
    per_day: int
    concurrency: int = 2
    timeout_s: float = 8.0
    retries: int = 2
    backoff_s: float = 0.5
    max_cooldown_s: float = 300.0


class ProviderClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        auth_headers: Mapping[str, str],
        limits: ClientLimits,
        *,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("provider base URL must be https")
        self.name = name
        self._base = base_url.rstrip("/")
        self._auth = dict(auth_headers)
        self._limits = limits
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(limits.concurrency)
        self._minute: deque[float] = deque()
        self._day_key = ""
        self._day_used = 0
        self._cooldown_until = 0.0
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    # --- quota ---------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")

    def usage(self) -> dict:
        now = self._clock()
        self._trim(now)
        if self._day_key != self._today():
            day_used = 0
        else:
            day_used = self._day_used
        return {
            "usedToday": day_used,
            "dailyLimit": self._limits.per_day,
            "usedLastMinute": len(self._minute),
            "perMinuteLimit": self._limits.per_minute,
            "coolingDownSeconds": max(0, int(self._cooldown_until - now)),
        }

    def _trim(self, now: float) -> None:
        while self._minute and now - self._minute[0] >= 60:
            self._minute.popleft()

    def _reserve(self) -> None:
        """Count one upstream request against the budgets or refuse it."""
        now = self._clock()
        if now < self._cooldown_until:
            raise UpstreamError("rate_limited")
        today = self._today()
        if self._day_key != today:
            self._day_key, self._day_used = today, 0
        if self._day_used >= self._limits.per_day:
            self.last_error = "quota_exhausted"  # coverage must not keep saying "connected"
            raise UpstreamError("quota_exhausted")
        self._trim(now)
        if len(self._minute) >= self._limits.per_minute:
            raise UpstreamError("rate_limited")
        self._day_used += 1
        self._minute.append(now)

    def _cool_down(self, headers: Mapping[str, str]) -> None:
        wait = 60.0
        for key in ("retry-after", "x-requestcounter-reset"):
            raw = headers.get(key)
            if raw and raw.strip().isdigit():
                wait = float(raw.strip())
                break
        self._cooldown_until = self._clock() + min(max(wait, 1.0), self._limits.max_cooldown_s)

    def _respect_remaining(self, headers: Mapping[str, str]) -> None:
        # The docs name this header two ways; when the provider says the minute is
        # used up, pause until its reset instead of collecting a 429.
        for key in ("x-requests-available-minute", "x-requestsavailable"):
            raw = headers.get(key)
            if raw is not None and raw.strip() == "0":
                self._cool_down(headers)
                return

    # --- requests ------------------------------------------------------------

    async def get_json(self, path: str, params: Mapping[str, str] | None = None) -> tuple[dict, Mapping[str, str]]:
        if not path.startswith("/") or "//" in path or ".." in path:
            raise ValueError("path must be a plain absolute path")
        url = self._base + urllib.parse.quote(path, safe="/,")
        if params:
            url += "?" + urllib.parse.urlencode(sorted(params.items()))
        headers = {"Accept": "application/json", **self._auth}
        async with self._semaphore:
            attempt = 0
            while True:
                self._reserve()
                try:
                    response = await self._transport(url, headers, self._limits.timeout_s)
                except TransportError:
                    error = UpstreamError("timeout")
                else:
                    if response.status == 200:
                        try:
                            data = json.loads(response.body)
                        except ValueError:
                            raise self._fail(UpstreamError("bad_response", status=200)) from None
                        if not isinstance(data, dict):
                            raise self._fail(UpstreamError("bad_response", status=200))
                        self.last_error = None
                        self.last_success_at = self._clock()
                        self._respect_remaining(response.headers)
                        return data, response.headers
                    if response.status == 429:
                        self._cool_down(response.headers)
                        raise self._fail(UpstreamError("rate_limited", status=429))
                    if response.status == 404:
                        raise UpstreamError("not_found", status=404)
                    if response.status in (401, 403):
                        # Wrong key or plan: retrying only burns quota.
                        raise self._fail(UpstreamError("upstream_error", status=response.status))
                    error = UpstreamError("upstream_error", status=response.status)
                    if response.status < 500:
                        raise self._fail(error)
                if attempt >= self._limits.retries:
                    raise self._fail(error)
                attempt += 1
                await self._sleep(self._limits.backoff_s * (2 ** (attempt - 1)))

    def _fail(self, error: UpstreamError) -> UpstreamError:
        self.last_error = error.code
        # Status and code only: the URL may carry query values, the key lives in headers.
        logger.warning("event=sports_upstream_error provider=%s code=%s status=%s", self.name, error.code, error.status)
        return error
