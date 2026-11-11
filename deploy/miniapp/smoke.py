"""Post-deploy smoke test, run INSIDE the Mini App container (stdlib only, nothing to install):

    docker exec -i <container> python - < deploy/miniapp/smoke.py

It reads the bot token, edge secret and trusted origin from the container's own environment and never
prints them. It talks to the app on 127.0.0.1 with the edge secret, signs Telegram initData for ONE
synthetic tester id (SMOKE_USER_ID, default 999000001), makes no paid AI call (read-only AI path only),
and ends the tester's sessions at the end. Exit code 0 = PASS.
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
TOKEN = os.environ["MINIAPP_BOT_TOKEN"]
UID = int(os.environ.get("SMOKE_USER_ID", "999000001"))

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


st, _, _, dt, _ = call("GET", "/api/health", edge=False)
check(st == 200, f"health {st} ({dt}s)")
st, _, _, _, _ = call("GET", "/", edge=False)
check(st == 403, f"page without the edge secret is refused ({st})")
st, _, h, _, raw = call("GET", "/")
check(st == 200 and b'id="root"' in raw, f"page with the edge secret {st}")
check("default-src 'none'" in (h.get("Content-Security-Policy") or ""), "CSP enforced on the page")
check((h.get("Strict-Transport-Security") or "").startswith("max-age="), "HSTS on the page")
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
st, _, _, _, _ = call("POST", "/api/auth/logout-all")
check(st == 200, f"synthetic tester signed out everywhere ({st})")
check(not any(s >= 500 for s in statuses), f"no 5xx ({dict(statuses)})")
passed = all(ok for ok, _ in results)
print("RESULT", "PASS" if passed else "FAIL", f"{sum(ok for ok, _ in results)}/{len(results)}")
sys.exit(0 if passed else 1)
