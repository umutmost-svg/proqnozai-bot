"""In-process cache in front of the providers.

- One upstream load per key at a time (single flight): a thousand users opening
  the same match cost one request, the rest await the same future.
- Each value carries its own TTL, chosen by the loader from the data (live
  matches expire fast, finished ones slowly).
- When a refresh fails, the last good value is served marked ``stale`` with
  its original fetch time, up to ``max_stale_s``; after that the error shows.
  The next refresh is then held back for ``failure_backoff_s`` for every
  caller, so an outage does not turn each page view into a provider request.
  Without usable stale data the failure itself is remembered for the same
  time: callers get the same error, the provider gets no new request. A refusal by
  our own per-minute limiter (no provider request was made) is remembered only until
  the limiter admits requests again, never longer than the backoff; one that only a
  low-priority caller's own limits caused is not remembered at all.
- A refresh started early (the standings warm-up) that fails leaves a still-fresh
  value exactly as it was: it is neither marked stale nor replaced by the error.
- Errors carry ``retry_after`` (seconds) when the wait is known.
- Bounded size: least recently stored keys are dropped first.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .client import LOW_PRIORITY_DAY_SHARE, LOW_PRIORITY_RESERVE, UpstreamError

T = TypeVar("T")


def _low_priority() -> bool:
    """Is the current caller a background one (limited below what users get)?"""
    return LOW_PRIORITY_RESERVE.get() > 0 or LOW_PRIORITY_DAY_SHARE.get() < 1.0


@dataclass(frozen=True)
class Cached(Generic[T]):
    value: T
    fetched_at: float
    expires_at: float
    stale: bool = False


class SportsCache:
    def __init__(self, *, max_entries: int = 2000, max_stale_s: float = 6 * 3600,
                 failure_backoff_s: float = 30.0, clock: Callable[[], float] = time.time) -> None:
        self._failure_backoff_s = failure_backoff_s
        self._entries: OrderedDict[str, Cached] = OrderedDict()
        # key -> (error, held until, the moment the error's own retry_after ends)
        self._failures: dict[str, tuple[UpstreamError, float, float]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._max_entries = max_entries
        self._max_stale_s = max_stale_s
        self._clock = clock

    def peek(self, key: str) -> Cached | None:
        return self._entries.get(key)

    async def get(self, key: str, load: Callable[[], Awaitable[tuple[T, float]]]) -> Cached[T]:
        """``load`` returns (value, ttl_seconds). Errors propagate unless stale data exists."""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now < entry.expires_at:
            return entry
        self._raise_remembered(key, now)
        running = self.start(key, load)
        try:
            return await asyncio.shield(running)
        except UpstreamError as exc:
            # This caller may have joined a background load that only the background's own
            # limits refused: an interactive caller loads it again under its own limits (once).
            if not exc.low_priority_only or _low_priority():
                raise
            if self._inflight.get(key) is running:
                del self._inflight[key]
            return await asyncio.shield(self.start(key, load))

    def existing(self, key: str) -> Cached | asyncio.Future | None:
        """Without starting anything: the fresh value, the running load, or None. A remembered
        failure is raised, like get(). Lets a caller decide (and pay) before start()."""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now < entry.expires_at:
            return entry
        self._raise_remembered(key, now)
        return self._inflight.get(key)

    def _raise_remembered(self, key: str, now: float) -> None:
        failure = self._failures.get(key)
        if failure is None:
            return
        exc, until, ready_at = failure
        if now < until:
            raise UpstreamError(exc.code, status=exc.status,
                                retry_after=max(until, ready_at) - now if exc.code == "rate_limited" else exc.retry_after)
        del self._failures[key]

    def start(self, key: str, load: Callable[[], Awaitable[tuple[T, float]]]) -> asyncio.Future:
        """Starts the load unless one is running (synchronous: no await between check and start)."""
        running = self._inflight.get(key)
        if running is None:
            # The refresh runs as its own task: a caller that goes away (cancelled
            # request) must not cancel the load other callers are waiting for.
            running = asyncio.ensure_future(self._refresh(key, load, self._entries.get(key)))
            self._inflight[key] = running
            running.add_done_callback(lambda task, k=key: self._finished(k, task))
        return running

    def _finished(self, key: str, task: asyncio.Future) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # retrieved here, so a load nobody awaits any more logs no warning

    async def _refresh(self, key: str, load: Callable[[], Awaitable[tuple[T, float]]],
                       previous: Cached | None) -> Cached[T]:
        try:
            value, ttl = await load()
        except UpstreamError as exc:
            now = self._clock()
            if exc.code == "not_found":
                raise
            if previous is not None and not previous.stale and now < previous.expires_at:
                # An early refresh: the value still in date stays as it is.
                return previous
            if exc.low_priority_only:
                # Only a background caller's own limits said no: nothing is held or remembered,
                # so other callers still load the data under their own limits.
                raise
            hold = self._failure_backoff_s
            if exc.code == "rate_limited" and exc.status is None and exc.retry_after is not None:
                # Our own limiter said no (nothing reached the provider): held, as stale data or as
                # the error, only until it admits again.
                hold = min(hold, max(exc.retry_after, 0.0))
            if previous is not None and now - previous.fetched_at <= self._max_stale_s:
                # Held for the backoff, but never past the stale limit of the original fetch.
                until = min(now + hold, previous.fetched_at + self._max_stale_s)
                held = Cached(previous.value, previous.fetched_at, until, stale=True)
                self._entries[key] = held
                return held
            # Nothing we may still serve: data past the stale limit is dropped, never shown.
            self._entries.pop(key, None)
            ready_at = now + (exc.retry_after or 0.0)
            self._failures[key] = (exc, now + hold, ready_at)
            if len(self._failures) > self._max_entries:
                self._failures.pop(next(iter(self._failures)))
            raise
        now = self._clock()
        self._failures.pop(key, None)
        entry = Cached(value, now, now + max(ttl, 0.0))
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return entry
