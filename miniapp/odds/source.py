"""The Mini App's own odds source (docs/miniapp/odds-catalog.md): Mostbet's list every LIST_EVERY_S, matched
against the Mini App's catalogue (OddsStore.match_catalog: strict, any competition), then prices of the matched
lines — nearest kickoff first, near ones every 10 minutes and far ones every 45 — within a small budget: at most
`max_per_cycle` price REQUESTS (pages included) per CYCLE_S, every request SPACING_S after the previous one, and a PAUSE_S stop after any 429 (the bot, still
running from the same IP, keeps the API's budget first). Lines the list shows live become live markers (their
pre-match prices are dropped at once). Never more than one loop; one failing step never ends it.

The priced lines are kept in a snapshot file next to the database (`snapshot`), so a restart (a deploy) does not
empty the odds for the half hour a full refill takes: the snapshot is read once, right after the first complete list,
and only the lines that list still shows upcoming come back (served once assigned; the usual age limits apply).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path

from miniapp.sports.models import FootballEvent

from . import mostbet
from .book import FEED_KEYS, read_book
from .feed import TRANSIT_ALLOWANCE_S, CatalogLine, FeedLine
from .store import CATALOG_WINDOW_S, CUTOFF_BEFORE_KICKOFF_S, NEAR_S, WEEK_MAX_AGE_S, OddsStore, max_age

logger = logging.getLogger("miniapp.odds")

CYCLE_S = 300.0
BUDGET_WINDOW_S = 300.0
SAMPLES_EVERY_S = 15 * 60.0
LIST_EVERY_S = 15 * 60.0
SPACING_S = 2.0
PAUSE_S = 120.0
LIVE_LOOKBACK_S = 3 * 3600.0
FAR_SHARE = 4  # every 4th price request of a cycle goes to a match beyond NEAR_S when one is due
SNAPSHOT_MAX_LINES = 2000
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024


class OddsSource:
    def __init__(self, store: OddsStore, events: Callable[[], Iterable[FootballEvent]], *, max_per_cycle: int = 20,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep,
                 fetch: mostbet.Fetch = mostbet.http_get, snapshot: Path | None = None,
                 warm: Callable[[], object] | None = None) -> None:
        if not 1 <= max_per_cycle <= 40:
            raise ValueError("max_per_cycle must be 1..40")
        self.store = store
        self.events = events
        self.max_per_cycle = max_per_cycle
        self._clock = clock
        self._sleep = sleep
        self._fetch = fetch
        self._listed_at = float("-inf")
        self._list: dict[int, dict] = {}  # line id -> the list's entry (country, league, names, kickoff)
        self._priced: dict[int, float] = {}  # line id -> when its prices were last stored
        self._paused_until = float("-inf")
        self._task: asyncio.Task | None = None
        self._requests: deque[float] = deque()  # price requests of the last BUDGET_WINDOW_S (pages included)
        self._samples_at = float("-inf")
        self._empty: set[int] = set()  # lines whose last price read had no servable market
        self._snapshot = snapshot
        # Keeps the Mini App's season calendars (the matches the catalogue is matched against) loaded while nobody
        # uses the app: started at boot, the source would otherwise have nothing to match.
        self._warm = warm
        self._restored = False

    def ensure_running(self) -> asyncio.Task | None:
        """Starts the loop once (from a request: the Mini App is in use)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())
        return self._task

    async def _run(self) -> None:
        while True:
            started = self._clock()
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("odds.source_cycle_failed")
            await self._sleep(max(0.0, CYCLE_S - (self._clock() - started)))

    def _paused(self) -> bool:
        return self._clock() < self._paused_until

    def _pause(self, where: str) -> None:
        self._paused_until = self._clock() + PAUSE_S
        logger.info("odds.source_paused reason=rate_limited at=%s pause_s=%d", where, int(PAUSE_S))

    async def cycle(self) -> int:
        """One step: the list when due, then up to max_per_cycle prices. Returns how many lines were priced."""
        if self._warm is not None:
            try:
                self._warm()
            except Exception:  # noqa: BLE001 — warming is best effort; the cycle goes on with what is loaded
                logger.exception("odds.source_warm_failed")
        if self._paused():
            logger.info("odds.source_cycle skipped=paused")
            return 0
        now = self._clock()
        listed = False
        if now - self._listed_at >= LIST_EVERY_S:
            try:
                raw = await mostbet.fetch_list(self._fetch, self._sleep, SPACING_S)
            except mostbet.RateLimited:
                self._pause("list")
                logger.info("odds.source_cycle skipped=rate_limited")
                return 0
            except mostbet.SourceError as exc:
                logger.info("odds.source_list_failed reason=%s", exc)
                logger.info("odds.source_cycle skipped=list_failed")
                return 0
            self._take_list(raw)
            listed = True
            if not self._restored:
                # Only after a complete list: it governs from now on, and only lines it still lists come back.
                self._restored = True
                self.restore()
        report = self.store.match_catalog(self.events())
        due = [(line_id, refresh) for line_id, refresh in report.wanted
               if self._clock() - self._priced.get(line_id, float("-inf")) >= refresh and line_id in self._list]
        # Coverage policy within the budget: the line whose shown price would go stale soonest comes first (never
        # priced: at once, nearest kickoff first). Near matches still lead — their limit is 15 min — but a match days
        # ahead is never starved by an endless queue of nearer ones: its 4 h limit makes it urgent in its turn.
        clock_now = self._clock()

        def urgency(item: tuple[int, float]) -> tuple[float, float]:
            kickoff = self._list[item[0]]["kickoff"]
            return self._priced.get(item[0], float("-inf")) + max_age(kickoff, clock_now, True), kickoff
        # A share of the REQUESTS (pages included) is reserved for matches further than NEAR_S while any of them is
        # due: near lines stop at the budget minus that share, so a long queue of near matches (or near lines with
        # several pages) never starves the rest of the week. Within that, the soonest-to-go-stale line goes first.
        due.sort(key=urgency)
        far_ids = {d[0] for d in due if self._list[d[0]]["kickoff"] - clock_now > NEAR_S}
        # At least two requests: a line takes one or two outcome pages (measured on production lines, 15.11), so a
        # far line always fits its share whole; at the production budget (20) the share is 5.
        reserve = min(self.max_per_cycle, max(2, self.max_per_cycle // FAR_SHARE))
        priced = 0
        spent = 0

        cap = self.max_per_cycle  # requests this line may bring the window to (lower for near lines, see below)

        def room() -> bool:
            # A rolling five-minute window, not a per-cycle count: a cycle's late requests and the next one's early
            # ones never add up beyond the budget.
            now = self._clock()
            while self._requests and now - self._requests[0] >= BUDGET_WINDOW_S:
                self._requests.popleft()
            return len(self._requests) < cap

        async def budgeted(url: str) -> tuple[int, bytes]:
            # Every price request counts, pages included; a line whose pages would pass the budget is dropped
            # (a partial set is never used) and the cycle ends.
            nonlocal spent
            if not room():
                raise mostbet.SourceError("budget")
            self._requests.append(self._clock())
            spent += 1
            return await self._fetch(url)

        queue = deque(due)
        deferred: set[int] = set()  # near lines held back once while a far line was due
        waiting: list[tuple[int, float]] = []  # those lines, in their urgency order: back at the FRONT afterwards
        attempts = 0
        while queue:
            line_id, refresh = queue.popleft()
            # A near line may use the budget only down to the far matches' share while any of them is due —
            # every page of it counts (budgeted() checks the cap on each request).
            held = line_id not in far_ids and bool(far_ids)
            cap = self.max_per_cycle - reserve if held else self.max_per_cycle
            if not room():
                if held and line_id not in deferred:
                    deferred.add(line_id)
                    waiting.append((line_id, refresh))  # after the far ones: the rest of the budget, if any
                    continue
                if held:
                    continue
                break
            far_ids.discard(line_id)  # attempted: its share is not held for it any more
            if not far_ids and waiting:
                # Every far line has had its turn: the held near lines come next, ahead of less urgent ones.
                queue.extendleft(reversed(waiting))
                waiting.clear()
            attempts += 1
            if attempts > 1 or listed:
                await self._sleep(SPACING_S)  # also between the list's last page and the first price
            try:
                odds = await mostbet.fetch_odds(line_id, budgeted, self._sleep, SPACING_S)
            except mostbet.RateLimited:
                self._pause("odds")
                break
            except mostbet.SourceError as exc:
                if str(exc) == "budget":
                    if held:
                        if line_id not in deferred:
                            deferred.add(line_id)
                            waiting.append((line_id, refresh))  # its next page would take the far share: later
                        continue
                    break
                logger.info("odds.source_odds_failed line=%d reason=%s", line_id, exc)
                continue
            book = read_book({k: v for k, v in odds.items() if isinstance(v, float)})
            if not book:
                self._empty.add(line_id)  # priced, but nothing servable: counted as stale, not covered
            else:
                self._empty.discard(line_id)
            m = self._list[line_id]
            report_in = self.store.ingest([FeedLine(line_id, m["country"], m["league"], m["home"], m["away"],
                                                    m["kickoff"], False, self._clock(), book)], own=True)
            if not report_in.accepted:
                self._empty.add(line_id)  # refused by the store (e.g. the line went live): not covered
            self._priced[line_id] = self._clock()
            priced += 1
        if priced:
            self.save()
        if report.samples and self._clock() - self._samples_at >= SAMPLES_EVERY_S:
            # The names behind unknown_names / ambiguous / reversed (public team names, no user data), so the alias
            # table can be extended.
            self._samples_at = self._clock()
            for reason, items in sorted(report.samples.items()):
                logger.info("odds.unmatched_examples reason=%s examples=%s", reason, " | ".join(items))
        # Coverage: wanted lines whose last own price is older than its serving limit (shown as "no odds" now).
        stale = sum(1 for line_id, _ in report.wanted
                    if line_id in self._list and (line_id in self._empty or clock_now - self._priced.get(
                        line_id, float("-inf")) >= max_age(self._list[line_id]["kickoff"], clock_now, True)
                        - TRANSIT_ALLOWANCE_S))  # the same boundary as serving
        logger.info("odds.source_cycle listed=%d wanted=%d due=%d priced=%d requests=%d stale=%d %s", len(self._list),
                    len(report.wanted), len(due), priced, spent, stale, " ".join(f"{k}={v}" for k, v in sorted(report.reasons.items())))
        return priced

    def _take_list(self, raw: list[dict]) -> None:
        """The list's real football lines: upcoming ones into the catalogue, live ones as markers."""
        now = self._clock()
        self._listed_at = now
        catalog: list[CatalogLine] = []
        markers: list[FeedLine] = []
        listed: dict[int, dict] = {}
        for m in raw:
            line_id = m.get("id")
            if not isinstance(line_id, int) or isinstance(line_id, bool) or not mostbet.is_real_match(m):
                continue
            kickoff = mostbet.parse_kickoff(m.get("matchBeginAt"))
            texts = {k: str(m.get(f) or "").strip()[:120] for k, f in (("country", "lineSuperCategory"),
                     ("league", "lineSubCategory"), ("home", "team1Title"), ("away", "team2Title"))}
            if kickoff is None or not texts["country"] or not texts["home"] or not texts["away"]:
                continue
            entry = {**texts, "kickoff": kickoff}
            if m.get("isLive"):
                if now - kickoff <= LIVE_LOOKBACK_S:
                    markers.append(FeedLine(line_id, texts["country"], texts["league"], texts["home"], texts["away"],
                                            kickoff, True, now, {}))
                continue
            if now + CUTOFF_BEFORE_KICKOFF_S < kickoff <= now + CATALOG_WINDOW_S:
                listed[line_id] = entry
                catalog.append(CatalogLine(line_id, texts["country"], texts["league"], texts["home"], texts["away"],
                                           kickoff))
        self._list = listed
        self.store.ingest_catalog(catalog)  # fetch_list returns only a list read to its end
        if markers:
            self.store.ingest(markers, own=True)
        for line_id in [i for i in self._priced if i not in listed]:
            del self._priced[line_id]
        logger.info("odds.source_list lines=%d upcoming=%d live=%d", len(raw), len(listed), len(markers))

    def save(self) -> None:
        """Writes the own priced lines (atomically: a crash leaves the previous snapshot). Never raises."""
        if self._snapshot is None:
            return
        lines = sorted(self.store.own_lines(), key=lambda line: line.kickoff)[:SNAPSHOT_MAX_LINES]
        data = {"version": 1, "lines": [{"id": line.line_id, "country": line.country, "league": line.league,
                                         "home": line.home, "away": line.away, "kickoff": line.kickoff,
                                         "fetchedAt": line.fetched_at, "book": line.book} for line in lines]}
        tmp: str | None = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=self._snapshot.name + ".", suffix=".tmp", dir=self._snapshot.parent)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data, separators=(",", ":")))
            os.replace(tmp, self._snapshot)
            tmp = None
        except OSError as exc:
            logger.info("odds.snapshot_save_failed reason=%s", type(exc).__name__)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def restore(self) -> int:
        """Reads the snapshot into the store as own prices (fresh, not yet started lines only). Never raises."""
        if self._snapshot is None:
            return 0
        try:
            if self._snapshot.stat().st_size > SNAPSHOT_MAX_BYTES:
                raise ValueError("too_big")
            raw = json.loads(self._snapshot.read_text())
            items = raw["lines"] if isinstance(raw, dict) and raw.get("version") == 1 else None
            if not isinstance(items, list):
                raise ValueError("bad_format")
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, KeyError) as exc:
            logger.info("odds.snapshot_restore_failed reason=%s", type(exc).__name__)
            return 0
        now = self._clock()
        lines: list[FeedLine] = []
        for item in items[:SNAPSHOT_MAX_LINES]:
            line = _snapshot_line(item)
            if line is None or not now - WEEK_MAX_AGE_S < line.fetched_at <= now or line.kickoff <= now:
                continue
            listed = self._list.get(line.line_id)
            # The same line as the fresh list shows it (teams, competition, kickoff), not just the same id.
            if listed is None or any(listed.get(k) != getattr(line, k) for k in ("country", "league", "home", "away",
                                                                                  "kickoff")):
                continue
            lines.append(line)
        if lines:
            self.store.ingest(lines, own=True)
            for line in lines:
                self._priced[line.line_id] = line.fetched_at
        logger.info("odds.snapshot_restored lines=%d", len(lines))
        return len(lines)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _snapshot_line(item: object) -> FeedLine | None:
    if not isinstance(item, dict):
        return None
    line_id, kickoff, fetched_at, book = item.get("id"), _number(item.get("kickoff")), _number(item.get("fetchedAt")), item.get("book")
    texts = [item.get(k) for k in ("country", "league", "home", "away")]
    if (not isinstance(line_id, int) or isinstance(line_id, bool) or kickoff is None or fetched_at is None
            or not all(isinstance(t, str) for t in texts) or not isinstance(book, dict)):
        return None
    # The stored book is in our keys: it is checked again by the same rule as a fresh read.
    prices = read_book({feed: book[ours] for feed, ours in FEED_KEYS.items() if _number(book.get(ours)) is not None})
    if not prices:
        return None
    return FeedLine(line_id, *texts, kickoff, False, fetched_at, prices)
