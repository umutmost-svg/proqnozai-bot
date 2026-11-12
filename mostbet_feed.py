"""Odds export to the Mini App backend: a signed push, never an inbound route.

Off unless both MINIAPP_ODDS_URL and MINIAPP_ODDS_FEED_SECRET are set (MINIAPP_ODDS_MAX_LINES
optionally caps lines per cycle, 1-40; the Mini App can switch the feed off with a 410, see
_send); then a background
task (main.post_init) runs one cycle every CYCLE_S seconds:

1. the match list comes from the bot's cache only (mostbet.cached_matches — no list
   request of its own; the bot's preload refreshes it every 15 minutes);
2. football lines of the competitions in miniapp/odds/leagues.json (exact country +
   league pair), not live, starting within WINDOW_S; skipped football pairs are logged
   hourly so the table can be checked against the live feed;
3. prices of at most MAX_LINES_PER_CYCLE lines whose last push is older than their
   refresh interval (REFRESH_NEAR_S within NEAR_S of kickoff, REFRESH_FAR_S otherwise),
   nearest kickoff first, one request at a time with SPACING_S between them, through
   mostbet.mostbet_get_odds and its shared 2-minute cache (a line a bot user just
   opened costs nothing);
4. any 429 from the API — the bot's or this task's — pauses the export for PAUSE_AFTER_429_S,
   so the bot's own requests keep the API's budget first;
5. every line of a served competition that the list shows live (started ≤ 3 h ago) is sent as a
   live marker without prices (no API request), again after a lost answer or a restart (at most
   every 10 minutes once accepted), so the Mini App drops its pre-match odds at once;
6. one HTTPS POST of the refreshed lines, signed with HMAC-SHA256 (see
   miniapp/odds/feed.py for the receiving side). The Mini App answers only counts.

A failing cycle is logged and retried at the next one; it never stops the bot.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

import httpx

import mostbet
from event_list import parse_kickoff_utc

logger = logging.getLogger(__name__)

CYCLE_S = 300
WINDOW_S = 48 * 3600
NEAR_S = 3 * 3600
REFRESH_NEAR_S = 300
# The agreed cadence: 5 min near kickoff, 10 min further out — both inside the Mini App's
# 15-minute serving limit (miniapp/odds/store.py MAX_AGE_S), within the per-cycle cap.
REFRESH_FAR_S = 600
# A line is priced only if it was present in a page of the match list fetched at most this long
# ago (mostbet.line_seen_at, per line: a partial refresh merges an older tail): the list is what
# says a match has gone live (it refreshes every 15 minutes; a failing refresh stops prices).
LIST_MAX_AGE_S = 20 * 60
# No prices are fetched for the last minutes before kickoff: the Mini App never serves them
# then (miniapp/odds/store.py CUTOFF_BEFORE_KICKOFF_S, kept equal by a test).
STOP_BEFORE_KICKOFF_S = 15 * 60
MAX_LINES_PER_CYCLE = 40  # the ceiling; MINIAPP_ODDS_MAX_LINES may set fewer (first rollout: 5-10)
# Live markers: every known line the list shows live, started at most MARKER_LOOKBACK_S ago (the
# Mini App keeps lines that long after kickoff), sent again at most every MARKER_REPEAT_S after a
# push that was accepted. They are idempotent (a newer marker changes nothing) and cost no API
# request, so a lost answer or a restart of the bot only means the next cycle sends them again.
MARKER_LOOKBACK_S = 3 * 3600
MARKER_REPEAT_S = 600
MAX_MARKERS = 200
# The Mini App answers 410 when the feed is switched off there (MINIAPP_ODDS_FEED_ENABLED=false):
# the export then asks for no prices at all and only re-checks, with an empty push, this often.
OFF_RECHECK_S = 1800
SPACING_S = 2.0
PAUSE_AFTER_429_S = 120
PUSH_TIMEOUT_S = 10
SKIPPED_LOG_EVERY_S = 3600
LEAGUES_PATH = Path(__file__).with_name("miniapp") / "odds" / "leagues.json"


def normalize(text: str) -> str:
    """The same normalization as miniapp.sports.search.normalize (kept in sync by a test)."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    plain = plain.replace("ø", "o").replace("ß", "ss").replace("ı", "i")
    return re.sub(r"[^0-9a-z]+", " ", plain).strip()


def load_table(path: Path = LEAGUES_PATH) -> frozenset[tuple[str, str]]:
    """The exact (country, league) pairs to price (no wildcards)."""
    raw = json.loads(path.read_text())
    return frozenset((normalize(country), normalize(league))
                     for entry in raw["competitions"] for country in entry["country"] for league in entry["league"])


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class OddsExport:
    def __init__(self, url: str, secret: bytes, *, clock=time.time, sleep=asyncio.sleep,
                 post=None, table=None, max_lines: int = MAX_LINES_PER_CYCLE) -> None:
        if not 1 <= max_lines <= MAX_LINES_PER_CYCLE:
            raise ValueError("max_lines must be 1..40")
        self.url = url
        self.secret = secret
        self.max_lines = max_lines
        self._clock = clock
        self._sleep = sleep
        self._post = post or self._http_post
        self.pairs = table or load_table()
        self._pushed: dict[int, float] = {}  # line id -> when its prices were last pushed
        self._marked: dict[int, float] = {}  # line id -> when its live marker was last accepted
        self._off_until: float | None = None  # switched off at the Mini App: re-check after this
        self._skipped: Counter = Counter()
        self._stale_lines = 0
        self._skipped_logged_at = 0.0

    # --- which lines -------------------------------------------------------------------

    def _known(self, m: dict) -> bool:
        country = normalize(str(m.get("lineSuperCategory") or ""))
        league = normalize(str(m.get("lineSubCategory") or ""))
        return (country, league) in self.pairs

    def candidates(self, matches: list[dict]) -> list[tuple[float, dict]]:
        """(kickoff, line) of the lines due now, nearest first, at most max_lines."""
        now = self._clock()
        due: list[tuple[float, dict]] = []
        for m in matches:
            if str(m.get("lineCategory") or "").strip().lower() != "football":
                continue
            if mostbet._is_virtual_match(m) or mostbet._is_outright_market(m) or m.get("isLive"):
                continue
            if not self._known(m):
                self._skipped[(str(m.get("lineSuperCategory") or ""), str(m.get("lineSubCategory") or ""))] += 1
                continue
            kickoff = parse_kickoff_utc(m.get("matchBeginAt"))
            line_id = m.get("id")
            if kickoff is None or not isinstance(line_id, int) or isinstance(line_id, bool):
                continue
            start = kickoff.timestamp()
            if not now + STOP_BEFORE_KICKOFF_S < start <= now + WINDOW_S:
                continue
            if now - mostbet.line_seen_at.get(line_id, float("-inf")) > LIST_MAX_AGE_S:
                self._stale_lines += 1
                continue  # not confirmed by a recent list page: its live state is unknown
            refresh = REFRESH_NEAR_S if start - now <= NEAR_S else REFRESH_FAR_S
            if now - self._pushed.get(line_id, 0.0) >= refresh:
                due.append((start, m))
        due.sort(key=lambda x: (x[0], x[1]["id"]))
        return due[: self.max_lines]

    def went_live(self, matches: list[dict]) -> list[dict]:
        """Live markers (no prices) for every line of a served competition that the list shows
        live and that started at most MARKER_LOOKBACK_S ago, whether or not this process priced
        it (after a restart it cannot know what the Mini App still holds), unless one was
        accepted less than MARKER_REPEAT_S ago. The Mini App then stops serving its pre-match
        odds at once. Costs no API request."""
        now = self._clock()
        markers = []
        for m in matches:
            line_id = m.get("id")
            if not m.get("isLive") or not isinstance(line_id, int) or isinstance(line_id, bool):
                continue
            if str(m.get("lineCategory") or "").strip().lower() != "football" or not self._known(m):
                continue
            if mostbet._is_virtual_match(m) or mostbet._is_outright_market(m):
                continue
            kickoff = parse_kickoff_utc(m.get("matchBeginAt"))
            # No lower bound on purpose: a line live BEFORE its scheduled kickoff is exactly the
            # early start these markers exist for. A wrong live flag only hides one match's prices.
            if kickoff is None or now - kickoff.timestamp() > MARKER_LOOKBACK_S:
                continue
            if now - self._marked.get(line_id, float("-inf")) < MARKER_REPEAT_S:
                continue
            markers.append(self._payload(m, kickoff.timestamp(), live=True, fetched_at=now, odds={}))
        return markers[:MAX_MARKERS]

    @staticmethod
    def _payload(m: dict, kickoff: float, *, live: bool, fetched_at: float, odds: dict) -> dict:
        return {
            "id": m["id"], "country": str(m.get("lineSuperCategory") or "").strip(),
            "league": str(m.get("lineSubCategory") or "").strip(),
            "home": str(m.get("team1Title") or "").strip(), "away": str(m.get("team2Title") or "").strip(),
            "kickoff": _iso(kickoff), "live": live, "fetchedAt": _iso(fetched_at), "odds": odds,
        }

    def _paused(self) -> bool:
        return self._clock() - mostbet.last_rate_limited_at < PAUSE_AFTER_429_S

    # --- one cycle ---------------------------------------------------------------------

    async def cycle(self) -> int:
        """Lines pushed in this cycle (0: nothing due, paused or the push failed)."""
        self._log_skipped()
        if self._off_until is not None:
            if self._clock() < self._off_until:
                return 0
            # Still switched off at the Mini App? Asked with an empty push: no API request.
            if not await self._send([]):
                return 0
        matches = mostbet.cached_matches()
        # Live markers need no API request: sent even while prices are paused.
        markers = self.went_live(matches)
        lines: list[dict] = []
        if self._paused():
            logger.info("odds_export.paused reason=rate_limited")
            due = []
        else:
            self._stale_lines = 0
            due = self.candidates(matches)
            if self._stale_lines:
                logger.info("odds_export.skipped_unconfirmed lines=%d", self._stale_lines)
        for i, (kickoff, m) in enumerate(due):
            if i:
                await self._sleep(SPACING_S)
            if self._paused():
                break
            # Stops at the first 429 (no retries of its own) and leaves the bot's cache untouched.
            odds = await mostbet.mostbet_get_odds(m["id"], retry_on_429=False, page_spacing_s=SPACING_S)
            entry = mostbet.mostbet_cache.get(f"odds_{m['id']}")
            # Only a set read in full (all pages): a partial one could miss or misplace prices.
            if not entry or not mostbet.odds_complete(m["id"]) or not any(v is not None for v in odds.values()):
                continue
            lines.append(self._payload(m, kickoff, live=False, fetched_at=entry[0],
                                       odds={k: v for k, v in odds.items() if isinstance(v, (int, float))}))
        if not lines and not markers:
            return 0
        if not await self._send(lines + markers):
            return 0  # nothing recorded: prices and markers go again next cycle
        now = self._clock()
        for ln in lines:
            self._pushed[ln["id"]] = now
        for ln in markers:
            self._marked[ln["id"]] = now
        # Forget lines long past: bounded memory.
        for line_id in [i for i, t in self._pushed.items() if now - t > WINDOW_S]:
            del self._pushed[line_id]
        for line_id in [i for i, t in self._marked.items() if now - t > MARKER_LOOKBACK_S + MARKER_REPEAT_S]:
            del self._marked[line_id]
        logger.info("odds_export.pushed lines=%d live_markers=%d", len(lines), len(markers))
        return len(lines) + len(markers)

    async def _send(self, lines: list[dict]) -> bool:
        """One signed push; True when the Mini App accepted it. A 410 means the feed is switched
        off there: no prices are asked for until a later empty push is accepted again."""
        body = json.dumps({"v": 1, "nonce": secrets.token_hex(16), "lines": lines},
                          ensure_ascii=False, separators=(",", ":")).encode()
        stamp = str(int(self._clock()))
        headers = {"Content-Type": "application/json", "X-Odds-Timestamp": stamp,
                   "X-Odds-Signature": sign(self.secret, stamp, body)}
        try:
            status = await self._post(self.url, body, headers)
        except Exception as exc:  # network: retried next cycle
            logger.warning("odds_export.push_failed type=%s", type(exc).__name__)
            return False
        if status == 410:
            self._off_until = self._clock() + OFF_RECHECK_S
            logger.warning("odds_export.switched_off_at_receiver recheck_in_s=%d", OFF_RECHECK_S)
            return False
        if status != 200:
            logger.warning("odds_export.push_refused status=%s", status)
            return False
        if self._off_until is not None:
            logger.info("odds_export.switched_on_at_receiver")
        self._off_until = None
        return True

    def _log_skipped(self) -> None:
        now = self._clock()
        if self._skipped and now - self._skipped_logged_at >= SKIPPED_LOG_EVERY_S:
            top = ", ".join(f"{c} / {lg}: {n}" for (c, lg), n in self._skipped.most_common(15))
            logger.info("odds_export.skipped_competitions %s", top)
            self._skipped.clear()
            self._skipped_logged_at = now

    @staticmethod
    async def _http_post(url: str, body: bytes, headers: dict) -> int:
        # No redirects: the signature and body go to the configured address only.
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT_S, follow_redirects=False) as h:
            r = await h.post(url, content=body, headers=headers)
            return r.status_code


def _safe_url(url: str) -> bool:
    """https to a real host, or plain http to this machine's loopback only (local tests); never
    user info in the URL (http://127.0.0.1:80@other.host is a request to other.host)."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname == "127.0.0.1" and port is not None


def from_env(env=os.environ) -> OddsExport | None:
    url = (env.get("MINIAPP_ODDS_URL") or "").strip()
    secret = (env.get("MINIAPP_ODDS_FEED_SECRET") or "").strip()
    if not url or not secret:
        return None
    if len(secret) < 32:
        logger.error("odds_export.disabled reason=short_secret")
        return None
    if not _safe_url(url):
        logger.error("odds_export.disabled reason=insecure_url")
        return None
    raw_max = (env.get("MINIAPP_ODDS_MAX_LINES") or "").strip()
    if raw_max and not (raw_max.isdigit() and 1 <= int(raw_max) <= MAX_LINES_PER_CYCLE):
        logger.error("odds_export.disabled reason=bad_max_lines")
        return None
    max_lines = int(raw_max) if raw_max else MAX_LINES_PER_CYCLE
    logger.info("odds_export.enabled max_lines=%d", max_lines)
    return OddsExport(url, secret.encode(), max_lines=max_lines)


def next_delay(started: float, finished: float) -> float:
    """Cycles start every CYCLE_S from the START of the previous one (a cycle of 40 lines takes
    over a minute); one that overran starts the next at once — never two at a time."""
    return max(0.0, CYCLE_S - (finished - started))


async def run_odds_export(export: OddsExport, *, clock=time.monotonic, sleep=asyncio.sleep) -> None:
    """The background loop: one failing cycle never ends it."""
    await sleep(60)  # after the bot's first list preload
    while True:
        started = clock()
        try:
            await export.cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("odds_export.cycle_failed")
        await sleep(next_delay(started, clock()))
