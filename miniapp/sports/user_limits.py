"""Per-user share of the shared football-data budget.

The provider allows a few requests a minute for the whole app (and the key is shared with the bot);
the client caps the total, but without a per-user share one signed-in account could take all of it
(walking the calendar, inventing match ids, AI analyses or quick picks over cold data) and every
other user would see "source busy".

How it is charged: a user request runs with a tally (client.USER_TALLY) that charges every provider
request it really makes to the user's minute window at once — retries, AI evidence and quick-pick
loads included, also from a cache load that outlives the request; cached answers and loads another
user already started cost nothing; background work is never charged. A request is admitted with its
expected cost (an AI analysis: every evidence key still cold) reserved until it ends; each request it
really makes uses up its own reservation. It is admitted only while the window plus every reservation
of this user still in progress leave room for that cost, so parallel requests (several cold analyses
at once) cannot run past the share. A request costing more than the whole share runs only when the
user has nothing else in the window or in progress. The reservation lasts until the request AND every
cache load it started have ended. A request that chooses its steps by what it can afford (the quick
pick) reserves each step with `take()` and skips steps that do not fit.
Nothing here refuses inside a load, so the shared cache and its remembered failures never see a
per-user refusal. Never-listed match ids are counted separately per hour, before the lookup.
The same charges also count against a per-user UTC-day cap (audit wave 2): within the minute share alone a
script could still spend more than the app's whole daily budget (15/min x 1440 > 14 400).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .client import USER_TALLY, UpstreamTally

MAX_USERS = 10_000


IN_FLIGHT_MAX_AGE_S = 300   # an admission never released (a crash between gate and call) stops counting
DAY_S = 86400
DAY_USERS_MAX = 4 * MAX_USERS   # users with a day count kept at once


class UserUpstreamLimiter:
    """Sliding windows per user: provider requests per minute (plus what admitted requests in progress
    still reserve), never-listed match ids per hour; provider requests per UTC day (`per_day`, None: no cap)."""

    def __init__(self, per_minute: int, unknown_per_hour: int, clock: Callable[[], float],
                 per_day: int | None = None) -> None:
        self.per_minute = per_minute
        self.unknown_per_hour = unknown_per_hour
        self.per_day = per_day
        self._clock = clock
        self._minute: dict[int, deque[float]] = {}
        self._unknown: dict[int, deque[float]] = {}
        # user -> [UTC day number, requests charged that day]; earlier days are dropped once a day starts.
        self._day: dict[int, list[int]] = {}
        self._day_number = 0
        # user -> {ticket: [admitted at, requests still reserved]}
        self._in_flight: dict[int, dict[object, list[float]]] = {}
        self._lock = threading.Lock()

    def admit(self, user_id: int, unknown: bool = False, ticket: object | None = None, cost: int = 1) -> float | None:
        """None when the user's share has room for `cost` requests (they stay reserved until
        `release(ticket)`; an unknown id is charged here), else seconds to wait."""
        now = self._clock()
        cost = max(1, cost)
        with self._lock:
            if len(self._minute) + len(self._unknown) + len(self._in_flight) > MAX_USERS:
                self._prune(now)
            minute = self._window(self._minute, user_id, now, 60)
            running = self._in_flight.setdefault(user_id, {})
            for stale in [t for t, (at, _) in running.items() if at <= now - IN_FLIGHT_MAX_AGE_S]:
                del running[stale]
            reserved = int(sum(left for _, left in running.values()))
            used = len(minute) + reserved
            waits = []
            if used and used + cost > self.per_minute:
                if reserved:
                    # Requests in progress free their reservation when they end: a short wait, then ask again.
                    waits.append(5.0)
                else:
                    # The oldest requests that must leave the window before this cost fits (all of them
                    # for a cost above the whole share).
                    k = min(len(minute), used + cost - self.per_minute) if cost <= self.per_minute else len(minute)
                    waits.append(minute[k - 1] + 60 - now if k > 0 else 5.0)
            if self.per_day is not None:
                today = self._day_used(user_id, now)
                if today and today + cost > self.per_day:
                    waits.append(DAY_S - now % DAY_S)       # the day's share is spent: until 00:00 UTC
                elif reserved and today + reserved + cost > self.per_day:
                    waits.append(5.0)                        # what is in progress may still leave room
            if unknown:
                hour = self._window(self._unknown, user_id, now, 3600)
                if len(hour) >= self.unknown_per_hour:
                    waits.append(hour[0] + 3600 - now if hour else 3600.0)
            if waits:
                return max(1.0, max(waits))
            running[ticket if ticket is not None else object()] = [now, float(cost)]
            if unknown:
                self._unknown[user_id].append(now)
            return None

    def reserve(self, user_id: int, ticket: object, cost: int) -> bool:
        """Adds `cost` to this request's reservation if the share has room for it (no overshoot), else False.
        For a request that decides step by step what it can afford (the quick pick)."""
        if cost <= 0:
            return True
        now = self._clock()
        with self._lock:
            minute = self._window(self._minute, user_id, now, 60)
            running = self._in_flight.setdefault(user_id, {})
            reserved = sum(left for _, left in running.values())
            if len(minute) + reserved + cost > self.per_minute:
                return False
            if self.per_day is not None and self._day_used(user_id, now) + reserved + cost > self.per_day:
                return False
            admission = running.setdefault(ticket, [now, 0.0])
            admission[1] += cost
            return True

    def release(self, user_id: int, ticket: object) -> None:
        """Ends exactly that admission (an expired one is already gone and frees nothing else)."""
        with self._lock:
            running = self._in_flight.get(user_id)
            if running is not None:
                running.pop(ticket, None)

    def charge(self, user_id: int, requests: int = 1, ticket: object | None = None) -> None:
        """Adds provider requests a request of this user really made (out of its reservation first)."""
        if requests <= 0:
            return
        now = self._clock()
        with self._lock:
            minute = self._window(self._minute, user_id, now, 60)
            # More than two shares in the window never changes the wait: the memory stays bounded.
            minute.extend([now] * min(requests, 2 * self.per_minute - len(minute)))
            if self.per_day is not None:
                self._day_used(user_id, now)
                self._day[user_id][1] += requests
            admission = self._in_flight.get(user_id, {}).get(ticket) if ticket is not None else None
            if admission is not None:
                admission[1] = max(0.0, admission[1] - requests)

    def _day_used(self, user_id: int, now: float) -> int:
        day = int(now // DAY_S)
        if day != self._day_number:
            self._day_number = day
            self._day = {k: v for k, v in self._day.items() if v[0] == day}
        entry = self._day.get(user_id)
        if entry is None:
            if len(self._day) >= DAY_USERS_MAX:
                # Bounded memory: the lighter half is forgotten (their count restarts — never a refusal).
                keep = sorted(self._day.items(), key=lambda kv: kv[1][1], reverse=True)[: DAY_USERS_MAX // 2]
                self._day = dict(keep)
            entry = self._day[user_id] = [day, 0]
        if entry[0] != day:
            entry[:] = [day, 0]
        return entry[1]

    @staticmethod
    def _window(store: dict[int, deque[float]], user_id: int, now: float, span: float) -> deque[float]:
        hits = store.setdefault(user_id, deque())
        while hits and hits[0] <= now - span:
            hits.popleft()
        return hits

    def _prune(self, now: float) -> None:
        for store, span in ((self._minute, 60), (self._unknown, 3600)):
            for key in [k for k, v in store.items() if not v or v[-1] <= now - span]:
                del store[key]
        for key in [k for k, v in self._in_flight.items() if not v or max(at for at, _ in v.values()) <= now - IN_FLIGHT_MAX_AGE_S]:
            del self._in_flight[key]

        # Still crowded (that many users active within the hour): forget the oldest-active ones.
        for store in (self._minute, self._unknown):
            if len(store) > MAX_USERS // 2:
                by_age = sorted(store, key=lambda k: store[k][-1] if store[k] else 0.0)
                for key in by_age[: len(store) - MAX_USERS // 2]:
                    del store[key]


class UserShare:
    """One request's use of a user's share: `gate()` before the call, `with tally():` around it."""

    def __init__(self, limiter: UserUpstreamLimiter | None, user_id: int) -> None:
        self._limiter = limiter
        self._user = user_id
        self._ticket = object()
        self._admitted = False
        self._credit = 0  # reserved at admission, not yet claimed by take()

    def gate(self, cost: int = 1, unknown: bool = False) -> float | None:
        """None to go on, else seconds to wait. `cost` 0 (answered from the cache) always passes."""
        if self._limiter is None or cost <= 0:
            return None
        wait = self._limiter.admit(self._user, unknown, self._ticket, cost)
        self._admitted = wait is None
        self._credit = max(1, cost) if self._admitted else 0
        return wait

    def take(self, cost: int) -> bool:
        """During the request: reserve `cost` more requests if the share has room (see Limiter.reserve)."""
        if self._limiter is None or cost <= 0:
            return True
        used = min(self._credit, cost)  # what the admission already reserved is used first
        if used == cost or self._limiter.reserve(self._user, self._ticket, cost - used):
            self._credit -= used          # spent only by a step that really goes ahead
            self._admitted = True
            return True
        return False

    def _reserve_retry(self) -> bool:
        assert self._limiter is not None
        if self._limiter.reserve(self._user, self._ticket, 1):
            self._admitted = True
            return True
        return False

    @contextmanager
    def tally(self) -> Iterator[UpstreamTally]:
        limiter, user = self._limiter, self._user
        ticket = self._ticket
        tally = UpstreamTally(None if limiter is None else (lambda: limiter.charge(user, ticket=ticket)),
                              None if limiter is None else (lambda: self._reserve_retry()))
        token = USER_TALLY.set(tally)
        try:
            yield tally
        finally:
            USER_TALLY.reset(token)
            # The reservation ends with the request AND every load it started (a load outliving the
            # response still makes its requests against it).
            def release() -> None:
                if self._admitted and limiter is not None:
                    self._admitted = False
                    limiter.release(user, ticket)
            tally.close(release)
