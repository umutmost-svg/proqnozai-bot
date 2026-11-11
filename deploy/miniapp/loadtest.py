"""Load test of the Mini App API: N simulated Telegram users opening the app and browsing like real ones.

LOCAL ONLY. It signs launches with a TEST bot token (HMAC mode) for synthetic user ids, so it is run
against a throw-away container on this machine, never against production (which has no token and real
users). The target must be a loopback address; anything else is refused.

    LOADTEST_BOT_TOKEN=<test bot token> LOADTEST_EDGE_SECRET=<container's edge secret> \
      python deploy/miniapp/loadtest.py --url http://127.0.0.1:8899 --origin https://miniapp.example \
        --users 200 --ramp 120 --duration 600

Every user: signs in, then repeats a visit (home: coverage, tournaments, top and events of today;
then random actions: a tournament calendar and table, a match, a quick prediction, the free read-only AI
answer, a search) with 2-6 s of thinking between requests. No paid AI request is ever made (only GET
/api/ai/analysis, which never calls the model). Reports per-endpoint p50/p95/p99, statuses, errors.
Exit 0 when no 5xx, no transport errors and p95 of every endpoint is below --p95-limit seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import hmac
import json
import os
import random
import statistics
import sys
import time
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx

FEATURED = ("fd:PL", "fd:PD", "fd:BL1", "fd:SA", "fd:FL1", "fd:CL", "fd:DED", "fd:PPL", "fd:ELC", "fd:BSA")
SEARCHES = ("real", "bayern", "arsenal", "milan", "porto", "ajax", "liverpool", "barcelona")
UID_BASE = 7_700_000_000  # synthetic ids, local throw-away database only

# Statuses that are a correct answer, per endpoint. AI read: 404 = no analysis made yet (no key at launch),
# 503 = the AI's own temporary reasons. Quick: 429 = the app's deliberate back-pressure (rate printed).
EXPECTED = {"ai_read": {200, 404, 503}, "quick": {200, 429}}
lat: dict[str, list[float]] = collections.defaultdict(list)
codes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
errors: collections.Counter = collections.Counter()
reasons: collections.Counter = collections.Counter()


def init_data(token: str, uid: int) -> str:
    fields = {"query_id": f"AAL{uid}", "auth_date": str(int(time.time())),
              "user": json.dumps({"id": uid, "first_name": "Load", "username": f"load{uid}", "language_code": "ru"})}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urllib.parse.urlencode({**fields, "hash": hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()})


class User:
    def __init__(self, client: httpx.AsyncClient, args: argparse.Namespace, uid: int) -> None:
        self.c, self.a, self.uid = client, args, uid
        self.cookies: dict[str, str] = {}
        self.csrf: str | None = None
        self.events: list[str] = []

    async def call(self, name: str, method: str, path: str, body: dict | None = None) -> dict | None:
        headers = {"Host": self.a.host, "Origin": self.a.origin, "X-Edge-Auth": self.a.edge, "Accept": "application/json"}
        if self.cookies:  # Secure cookies over loopback http: kept by hand, as the smoke does
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        t0 = time.monotonic()
        try:
            r = await self.c.request(method, self.a.url + path, json=body, headers=headers, timeout=60)
        except httpx.HTTPError as exc:
            errors[f"{name}:{type(exc).__name__}"] += 1
            return None
        lat[name].append(time.monotonic() - t0)
        codes[name][r.status_code] += 1
        if r.status_code >= 400:
            try:
                reasons[f"{name} {r.status_code} {r.json().get('error')}/{r.json().get('reason')}"] += 1
            except ValueError:
                reasons[f"{name} {r.status_code}"] += 1
        for line in r.headers.get_list("set-cookie"):
            k, _, rest = line.partition("=")
            self.cookies[k.strip()] = rest.split(";", 1)[0]
        try:
            return r.json() if r.content[:1] in (b"{", b"[") else None
        except ValueError:
            return None

    async def think(self) -> None:
        await asyncio.sleep(random.uniform(self.a.think_min, self.a.think_max))

    async def login(self) -> bool:
        js = await self.call("login", "POST", "/api/auth/session", {"init_data": init_data(self.a.token, self.uid)})
        self.csrf = ((js or {}).get("session") or {}).get("csrf_token")
        return self.csrf is not None

    async def visit(self) -> None:
        now = datetime.now(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rng = {"from": day.isoformat().replace("+00:00", "Z"), "to": (day + timedelta(days=1)).isoformat().replace("+00:00", "Z")}
        await self.call("coverage", "GET", "/api/sports/coverage")
        await self.call("tournaments", "GET", "/api/sports/football/tournaments")
        await self.call("top", "GET", "/api/sports/football/top?" + urllib.parse.urlencode(rng))
        js = await self.call("events", "GET", "/api/sports/football/events?" + urllib.parse.urlencode(rng))
        self.events = [e["id"] for e in (js or {}).get("events", [])][:30] or self.events
        for _ in range(random.randint(3, 8)):
            await self.think()
            action = random.choices(("calendar", "standings", "event", "quick", "ai_read", "search", "week"),
                                    weights=(25, 15, 20, 15, 10, 10, 5))[0]
            t = random.choice(FEATURED)
            if action == "calendar":
                cal = await self.call("calendar", "GET", f"/api/sports/football/tournaments/{t}/calendar")
                ups = [e["id"] for e in (cal or {}).get("events", []) if e.get("status") == "upcoming"][:10]
                self.events = ups or self.events
            elif action == "standings":
                await self.call("standings", "GET", f"/api/sports/football/tournaments/{t}/standings")
            elif action == "event" and self.events:
                await self.call("event", "GET", "/api/sports/event/" + urllib.parse.quote(random.choice(self.events), safe=""))
            elif action == "quick":
                t0 = datetime.now(UTC)  # the backend accepts a period starting now (as the app sends it)
                q = {"mode": random.choice(("single", "coupon")), "from": t0.isoformat().replace("+00:00", "Z"),
                     "to": (t0 + timedelta(days=3)).isoformat().replace("+00:00", "Z"), "market": "any", "risk": "balanced"}
                if q["mode"] == "coupon":
                    q["size"] = "3"
                await self.call("quick", "GET", "/api/quick/pick?" + urllib.parse.urlencode(q))
            elif action == "ai_read" and self.events:  # free: never reserves a limit, never calls the model
                await self.call("ai_read", "GET", "/api/ai/analysis?event_id=" + urllib.parse.quote(random.choice(self.events)))
            elif action == "search":
                await self.call("search", "GET", "/api/sports/football/search?q=" + random.choice(SEARCHES))
            elif action == "week":
                wk = {"from": rng["from"], "to": (day + timedelta(days=2)).isoformat().replace("+00:00", "Z")}  # 48 h max
                await self.call("week", "GET", "/api/sports/football/events?" + urllib.parse.urlencode(wk))

    async def run(self, deadline: float, signed_in: bool = False) -> None:
        if not signed_in and not await self.login():
            errors["login_failed"] += 1
            return
        while time.monotonic() < deadline:
            await self.visit()
            await asyncio.sleep(random.uniform(self.a.pause_min, self.a.pause_max))  # app closed, reopened later
        await self.call("logout", "POST", "/api/auth/logout")


async def probe_run(user: "User", deadline: float) -> None:
    await user.run(deadline, signed_in=True)


def pct(xs: list[float], p: float) -> float:
    return statistics.quantiles(xs, n=100, method="inclusive")[int(p) - 1] if len(xs) > 1 else (xs[0] if xs else 0.0)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--origin", required=True)
    ap.add_argument("--users", type=int, default=100)
    ap.add_argument("--ramp", type=float, default=60, help="seconds over which users arrive")
    ap.add_argument("--duration", type=float, default=300)
    ap.add_argument("--think-min", type=float, default=2)
    ap.add_argument("--think-max", type=float, default=6)
    ap.add_argument("--pause-min", type=float, default=20)
    ap.add_argument("--pause-max", type=float, default=90)
    ap.add_argument("--p95-limit", type=float, default=2.0)
    ap.add_argument("--json", help="write the summary here")
    a = ap.parse_args()
    host = urllib.parse.urlsplit(a.url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"REFUSED: {a.url} is not a loopback address — this test never runs against a real deployment")
        return 2
    a.host = urllib.parse.urlsplit(a.origin).netloc
    a.token = os.environ["LOADTEST_BOT_TOKEN"]
    a.edge = os.environ.get("LOADTEST_EDGE_SECRET", "")
    limits = httpx.Limits(max_connections=a.users + 10, max_keepalive_connections=a.users + 10)
    started = time.monotonic()
    deadline = started + a.duration
    async with httpx.AsyncClient(limits=limits, trust_env=False) as client:  # no proxy: loopback means loopback
        probe = User(client, a, UID_BASE)  # one login first: a target that refuses test launches stops everything
        if not await probe.login():
            print("STOP: the first test login failed — is this really the local test container?")
            return 2

        async def delayed(i: int) -> None:
            await asyncio.sleep(a.ramp * i / max(1, a.users))
            await User(client, a, UID_BASE + i).run(deadline)
        await asyncio.gather(*(delayed(i) for i in range(1, a.users)), probe_run(probe, deadline))
    elapsed = time.monotonic() - started
    total = sum(len(v) for v in lat.values())
    summary = {"users": a.users, "seconds": round(elapsed), "requests": total, "rps": round(total / elapsed, 1),
               "errors": dict(errors), "endpoints": {}}
    ok = not errors
    print(f"users={a.users} duration={elapsed:.0f}s requests={total} rps={total / elapsed:.1f} transport_errors={dict(errors)}")
    print(f"{'endpoint':12} {'n':>6} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7}  statuses")
    for name in sorted(lat):
        xs = lat[name]
        row = {"n": len(xs), "p50": round(pct(xs, 50), 3), "p95": round(pct(xs, 95), 3), "p99": round(pct(xs, 99), 3),
               "max": round(max(xs), 3), "statuses": dict(codes[name])}
        summary["endpoints"][name] = row
        print(f"{name:12} {row['n']:>6} {row['p50']:>7} {row['p95']:>7} {row['p99']:>7} {row['max']:>7}  {row['statuses']}")
        allowed = EXPECTED.get(name, {200})
        if set(codes[name]) - allowed or (name not in ("quick",) and row["p95"] > a.p95_limit):
            ok = False  # any status the app should not give here (401/403/5xx …) fails the run
    for k, v in reasons.most_common(15):
        print(f"  {v:>6}  {k}")
    summary["reasons"] = dict(reasons)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(summary, f, indent=1)
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
