"""Post-deploy smoke test, run INSIDE the Mini App container (stdlib only, nothing to install):

    docker exec -i -u miniapp <container> python - < deploy/miniapp/smoke.py                       # ed25519
    docker exec -i -u miniapp -e SMOKE_USER_ID=<owner's own Telegram id> <container> python - < ...  # hmac

It reads its settings from the container's own environment and never prints secrets. It talks to the
app on 127.0.0.1 with the edge secret and makes no paid AI call.

- MINIAPP_INITDATA_MODE=ed25519 (production): there is no bot token, and only Telegram can sign a launch,
  so the smoke checks that unsigned data is refused and that no token is present, and runs the data paths
  in-process. The positive login check is the owner's real Telegram launch (production.md, step 9).
- hmac: it signs initData for the OWNER'S OWN Telegram account (SMOKE_USER_ID, required): an id the
  operator controls, never a guessed one. If that user already exists the run stops unless
  SMOKE_USER_EXISTS_OK=1. It always ends exactly the one session it created (also after a failure).

Exit code 0 = PASS.
"""
import collections
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

PORT = os.environ.get("PORT", "8000")
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = os.environ["MINIAPP_TRUSTED_ORIGINS"].split(",")[0].strip()
HOST = urllib.parse.urlsplit(ORIGIN).netloc
EDGE = os.environ.get("MINIAPP_EDGE_SECRET", "")
MODE = (os.environ.get("MINIAPP_INITDATA_MODE") or "hmac").strip().lower()
# Ed25519 deployments hold no bot token: a signed launch can only come from a real Telegram client, so the
# smoke checks that unsigned data is refused and exercises the data paths in-process instead.
TOKEN = os.environ.get("MINIAPP_BOT_TOKEN", "")
UID = int(os.environ["SMOKE_USER_ID"]) if MODE != "ed25519" else 0

cookies: dict[str, str] = {}
csrf: str | None = None
statuses: collections.Counter = collections.Counter()
results: list[tuple[bool, str]] = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def call(method: str, path: str, body: dict | None = None, *, edge: bool = True, timeout: float = 60):
    headers = {"Host": HOST, "Origin": ORIGIN, "Accept": "application/json"}
    if edge:
        headers["X-Edge-Auth"] = EDGE
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if csrf and method != "GET":
        headers["X-CSRF-Token"] = csrf
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    t0 = time.monotonic()
    try:
        with _opener.open(req, timeout=timeout) as r:
            status, raw, hdrs = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read(), e.headers
    statuses[status] += 1
    for line in hdrs.get_all("Set-Cookie") or []:  # Secure cookies over loopback http: kept by hand
        name, _, rest = line.partition("=")
        cookies[name.strip()] = rest.split(";", 1)[0]
    try:
        payload = json.loads(raw) if raw[:1] in (b"{", b"[") else None
    except ValueError:
        payload = None
    return status, payload, hdrs, round(time.monotonic() - t0, 2), raw


def check(ok: object, label: str) -> None:
    results.append((bool(ok), label))
    print(("PASS " if ok else "FAIL ") + label, flush=True)


def init_data(uid: int) -> str:
    fields = {"query_id": f"AAH{uid}", "auth_date": str(int(time.time())),
              "user": json.dumps({"id": uid, "first_name": "Smoke", "username": f"smoke{uid}", "language_code": "ru"})}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    return urllib.parse.urlencode({**fields, "hash": hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()})


import sqlite3  # noqa: E402 — read-only look at the live users table before signing in

_db = sqlite3.connect(f"file:{os.environ['MINIAPP_DB_PATH']}?mode=ro", uri=True)
try:
    _has_users = _db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone() is not None
    _known = _has_users and _db.execute("SELECT 1 FROM users WHERE telegram_user_id = ?", (UID,)).fetchone() is not None
finally:
    _db.close()
if MODE != "ed25519" and _known and os.environ.get("SMOKE_USER_EXISTS_OK") != "1":
    print("STOP: SMOKE_USER_ID already has an account here; rerun with SMOKE_USER_EXISTS_OK=1 only if it is yours")
    sys.exit(2)

st, _, _, dt, _ = call("GET", "/api/health", edge=False)
check(st == 200, f"health {st} ({dt}s)")
st, _, _, _, _ = call("GET", "/", edge=False)
check(st == 403, f"page without the edge secret is refused ({st})")
st, _, h, _, raw = call("GET", "/")
check(st == 200 and b'id="root"' in raw, f"page with the edge secret {st}")
check("default-src 'none'" in (h.get("Content-Security-Policy") or ""), "CSP enforced on the page")
check((h.get("Strict-Transport-Security") or "").startswith("max-age="), "HSTS on the page")
def in_process_checks() -> None:
    """Ed25519 mode: the data paths without a session (same code and settings as the app; a few provider
    requests). The real login is checked by the owner from Telegram."""
    import asyncio

    from miniapp.settings import Settings
    from miniapp.sports.service import build_service

    async def run() -> None:
        svc = build_service(Settings.from_env(os.environ), warm_emblems=False)
        tournaments = svc.tournaments("football")
        check(len(tournaments) >= 100, f"tournaments {len(tournaments)} (in-process)")
        cal = await svc.calendar("football", "fd:PL")
        check(cal is not None and cal.events, "Premier League calendar (in-process)")
        table = await svc.standings("fd:PL")
        check(len(table) >= 18, f"Premier League table rows {len(table)} (in-process)")
        print("  quota:", json.dumps(svc.football.client.usage()) if svc.football else None)

    asyncio.run(run())


if MODE == "ed25519":
    st, js, _, _, _ = call("POST", "/api/auth/session", {"init_data": "query_id=x&auth_date=1&user=%7B%7D&signature=AAAA"})
    check(st == 401, f"unsigned initData refused in ed25519 mode ({st})")
    check(not os.environ.get("MINIAPP_BOT_TOKEN"), "no bot token in the container")
    in_process_checks()
else:
    try:
        st, js, _, _, _ = call("POST", "/api/auth/session", {"init_data": init_data(UID)})
        check(st == 200, f"session for the synthetic tester {st}")
        csrf = ((js or {}).get("session") or {}).get("csrf_token")
        st, cov, _, _, _ = call("GET", "/api/sports/coverage")
        football = ((cov or {}).get("sports") or {}).get("football") or {}
        check(st == 200 and football.get("status") == "connected", f"football connected ({football.get('status')})")
        print("  quota:", json.dumps(football.get("quota")), "ai:", (cov or {}).get("ai"))
        st, js, _, dt, _ = call("GET", "/api/sports/football/tournaments")
        tournaments = (js or {}).get("tournaments", [])
        check(st == 200 and len(tournaments) >= 100, f"tournaments {len(tournaments)} ({dt}s)")
        check(sum(1 for t in tournaments if t.get("featured")) == 12, "12 featured tournaments")
        for _ in range(20):  # competition emblems arrive in the background
            if sum(1 for t in tournaments if t.get("emblem")) > 100:
                break
            time.sleep(1.5)
            tournaments = (call("GET", "/api/sports/football/tournaments")[1] or {}).get("tournaments", [])
        crests = collections.Counter(call("GET", t["emblem"])[0] for t in tournaments if t.get("emblem"))
        check(crests.get(200, 0) >= 100, f"competition crests {dict(crests)}")
        day = datetime.now(UTC).date() + timedelta(days=1)
        st, js, _, dt, _ = call("GET", "/api/sports/football/events?" + urllib.parse.urlencode(
            {"from": f"{day}T00:00:00Z", "to": f"{day + timedelta(days=1)}T00:00:00Z"}))
        check(st == 200, f"events of {day}: {len((js or {}).get('events', []))} ({dt}s)")
        st, cal, _, dt, _ = call("GET", "/api/sports/football/tournaments/fd:PL/calendar")
        check(st == 200 and (cal or {}).get("events"), f"Premier League calendar {st} ({dt}s)")
        st, _, _, _, _ = call("GET", "/api/sports/football/tournaments/fd:PL/standings")
        check(st == 200, f"Premier League standings {st}")
        upcoming = [e for e in (cal or {}).get("events", []) if e.get("status") == "upcoming"][:1]
        for e in upcoming:  # read-only: never reserves a limit, never calls the model
            st, js, _, dt, _ = call("GET", "/api/ai/analysis?event_id=" + urllib.parse.quote(e["id"]))
            reason = (js or {}).get("reason") or (js or {}).get("error") or (js or {}).get("kind")
            check(st in (200, 404) and reason != "timeout", f"AI read-only {e['id']} -> {st} {reason} ({dt}s)")
    finally:
        if cookies:  # only the session this run created; never the account's other sessions
            st, _, _, _, _ = call("POST", "/api/auth/logout")
            check(st == 200, f"smoke session ended ({st})")
check(not any(s >= 500 for s in statuses), f"no 5xx ({dict(statuses)})")
passed = all(ok for ok, _ in results)
print("RESULT", "PASS" if passed else "FAIL", f"{sum(ok for ok, _ in results)}/{len(results)}")
sys.exit(0 if passed else 1)
