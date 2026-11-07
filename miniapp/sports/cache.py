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
  time: callers get the same error, the provider gets no new request.
- Bounded size: least recently stored keys are dropped first.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .client import UpstreamError

T = TypeVar("T")


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
        self._failures: dict[str, tuple[UpstreamError, float]] = {}
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
        failure = self._failures.get(key)
        if failure is not None:
            if now < failure[1]:
                raise UpstreamError(failure[0].code, status=failure[0].status)
            del self._failures[key]
        running = self._inflight.get(key)
        if running is None:
            # The refresh runs as its own task: a caller that goes away (cancelled
            # request) must not cancel the load other callers are waiting for.
            running = asyncio.ensure_future(self._refresh(key, load, entry))
            self._inflight[key] = running
            running.add_done_callback(lambda task, k=key: self._finished(k, task))
        return await asyncio.shield(running)

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
            if previous is not None and now - previous.fetched_at <= self._max_stale_s:
                # Held for the backoff, but never past the stale limit of the original fetch.
                until = min(now + self._failure_backoff_s, previous.fetched_at + self._max_stale_s)
                held = Cached(previous.value, previous.fetched_at, until, stale=True)
                self._entries[key] = held
                return held
            # Nothing we may still serve: data past the stale limit is dropped, never shown.
            self._entries.pop(key, None)
            self._failures[key] = (exc, now + self._failure_backoff_s)
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
