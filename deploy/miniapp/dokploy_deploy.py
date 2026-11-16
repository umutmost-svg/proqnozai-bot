"""Create or update the Mini App in Dokploy through its API and deploy it (runs ON the server, as root).

    python3 - <IMAGE_TAG> [--deploy] < deploy/miniapp/dokploy_deploy.py

Reads, never prints: /root/.dokploy-api-key (Dokploy API key), /root/miniapp-secrets/{edge,csrf,
football-data-key,owner-id} and, when present, {odds-feed,anthropic-key}. Talks only to http://127.0.0.1:3000 (Dokploy on this host).
Idempotent configuration: finds the project/application by exact name, checks it BEFORE any change (no
published ports), re-saves image, environment, resources, Swarm update config, the data volume and the domain
(each only once). Without --deploy it configures and stops; every run WITH --deploy starts one Dokploy
deployment. Closed mode (unless /root/miniapp-secrets/public exists): MINIAPP_ALLOWED_USER_IDS = the bot's ADMIN_ID, re-read from the running bot and
compared with the stored value. On the FIRST deployment there is no previous revision to roll back to: health is
verified explicitly afterwards (release-same-server.md, B3); later updates roll back automatically.
Errors print the procedure, HTTP status and the API's error code only — never a body or a value.
Guards (audit wave 2): one run at a time (a lock); a feature the running app has (public access, AI, odds feed, admin
policy, blocked list) is never switched off because its file went missing — that needs --allow-off; with --deploy
the run waits for the new image to be healthy and for the public health check, and exits non-zero otherwise (Swarm
rolls a failed update back by itself).
See docs/miniapp/release-same-server.md.
"""
import fcntl
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:3000/api/"
PROJECT = "proqnozai-miniapp"
APP = "proqnozai-miniapp"
DOMAIN = "app.proqnozai.com"
VOLUME = "proqnozai-miniapp-data"
REGISTRY = "127.0.0.1:5000/proqnozai-miniapp"
BOT_ID = "8727724516"


def secret(name: str) -> str:
    return Path(name).read_text().strip()


KEY = secret("/root/.dokploy-api-key")

# One deploy at a time: two runs would interleave saveEnvironment/deploy of different tags.
_LOCK = open("/run/proqnozai-miniapp-deploy.lock", "w")
try:
    fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit("STOP: another Mini App deploy is running")


def call(proc: str, body: dict | None = None, method: str = "POST"):
    url = API + proc
    data = None
    if method == "GET" and body:
        url += "?" + urllib.parse.urlencode(body)  # Dokploy's OpenAPI: flat query parameters
    elif body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-api-key": KEY, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            code = json.loads(e.read() or b"{}").get("code", "")
        except ValueError:
            code = ""
        code = code if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) < 40 else "?"
        sys.exit(f"STOP: {proc} -> HTTP {e.code} {code}")
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()


# The closed list is the bot's admin: re-read from the running bot and compared with the stored copy.
_bot = subprocess.run(["docker", "ps", "-q", "-f", "name=proqnozai-proqnozaibot"], capture_output=True, text=True).stdout.split()
if len(_bot) != 1:
    sys.exit("STOP: cannot identify the running bot container")
_admin = subprocess.run(["docker", "exec", _bot[0], "printenv", "ADMIN_ID"], capture_output=True, text=True).stdout.strip()
OWNER = secret("/root/miniapp-secrets/owner-id")
if not _admin or _admin != OWNER:
    sys.exit("STOP: the stored owner id differs from the bot's ADMIN_ID")

# Odds feed (signed push from the bot): validated BEFORE any change; absent file = feed off, said so.
_ODDS_FILE = Path("/root/miniapp-secrets/odds-feed")
ODDS_SECRET = secret(str(_ODDS_FILE)) if _ODDS_FILE.exists() else ""
if _ODDS_FILE.exists() and not ODDS_SECRET:
    sys.exit("STOP: the odds feed secret file exists but is empty")
if ODDS_SECRET and len(ODDS_SECRET) < 32:
    sys.exit("STOP: the odds feed secret is shorter than 32 characters")
print("odds feed:", "on (secret present)" if ODDS_SECRET else "off (no /root/miniapp-secrets/odds-feed)")

# AI forecasts: on once its key file exists (the bot's Anthropic key, copied server-side); validated BEFORE any change.
_AI_FILE = Path("/root/miniapp-secrets/anthropic-key")
AI_KEY = secret(str(_AI_FILE)) if _AI_FILE.exists() else ""
# Same syntax rule as miniapp.settings (else the app would refuse to start after the change).
if _AI_FILE.exists() and not (AI_KEY.startswith("sk-ant-") and AI_KEY.isascii() and AI_KEY.isprintable()
                              and " " not in AI_KEY and 20 <= len(AI_KEY) <= 256):
    sys.exit("STOP: the Anthropic key file exists but does not hold a valid Anthropic key")
print("ai forecasts:", "on (key present)" if AI_KEY else "off (no /root/miniapp-secrets/anthropic-key)")

# The admin panel's policy key (public half, hex; not a secret): ads snapshots and the access policy (users blocked
# in the admin) are signed by the admin and verified here. Present = telemetry, ads, referrals and blocking on.
_POLICY_FILE = Path("/root/miniapp-secrets/admin-policy-public-key")
POLICY_KEY = secret(str(_POLICY_FILE)) if _POLICY_FILE.exists() else ""
if _POLICY_FILE.exists() and not (len(POLICY_KEY) == 64 and all(c in "0123456789abcdef" for c in POLICY_KEY)):
    sys.exit("STOP: the admin policy public key must be 64 lower-case hex characters")
print("admin features (telemetry, ads, referrals, blocking):", "on" if POLICY_KEY else "off (no admin policy key)")

if len(sys.argv) < 2 or not sys.argv[1].isalnum():
    sys.exit("usage: python3 - <IMAGE_TAG> [--deploy] < dokploy_deploy.py")
tag, deploy = sys.argv[1], "--deploy" in sys.argv[2:]
allow_off = "--allow-off" in sys.argv[2:]

# 1) project + its production environment
projects = call("project.all", method="GET")
project = next((p for p in projects if p.get("name") == PROJECT), None)
if project is None:
    call("project.create", {"name": PROJECT, "description": "ProqnozAI Mini App"})
    projects = call("project.all", method="GET")
    project = next(p for p in projects if p.get("name") == PROJECT)
env = (project.get("environments") or [None])[0]
if not env:
    sys.exit("STOP: the project has no environment")
environment_id = env["environmentId"]

# 2) application (found by appName prefix; Dokploy may add a suffix)
apps = [a for e in project.get("environments", []) for a in e.get("applications", [])]
app = next((a for a in apps if a.get("name") == APP), None)
if app is not None:
    # An existing app is changed only if it is exactly ours and publishes nothing on the host.
    cur = call("application.one", {"applicationId": app["applicationId"]}, method="GET")
    if cur.get("name") != APP or not str(cur.get("appName", "")).startswith(APP):
        sys.exit("STOP: an application with this name is not the Mini App")
    if cur.get("sourceType") != "docker":
        sys.exit("STOP: the existing application is not image-based")
    if cur.get("ports") or ((cur.get("endpointSpecSwarm") or {}).get("Ports")):
        sys.exit("STOP: the application publishes host ports")
    # Names only (values are never read into anything printed).
    RUNNING_KEYS = {line.split("=", 1)[0] for line in str(cur.get("env") or "").splitlines() if "=" in line}
else:
    RUNNING_KEYS = set()
if app is None:
    app = call("application.create", {"name": APP, "appName": APP, "environmentId": environment_id,
                                       "description": "ProqnozAI Mini App (closed mode)"})
app_id = app["applicationId"]
print("application:", app.get("appName"), app_id)

# 4) environment (secrets from files; never printed)
env_vars = {
    "MINIAPP_ENV": "production",
    "MINIAPP_INITDATA_MODE": "ed25519",
    "MINIAPP_BOT_ID": BOT_ID,
    "MINIAPP_TELEGRAM_ENV": "production",
    "MINIAPP_CSRF_SECRET": secret("/root/miniapp-secrets/csrf"),
    "MINIAPP_EDGE_SECRET": secret("/root/miniapp-secrets/edge"),
    "MINIAPP_TRUSTED_ORIGINS": f"https://{DOMAIN}",
    "MINIAPP_DB_PATH": "/data/miniapp.db",
    "MINIAPP_CSP_MODE": "enforce",
    "MINIAPP_COOKIE_SAMESITE": "none",
    "MINIAPP_FOOTBALL_DATA_KEY": secret("/root/miniapp-secrets/football-data-key"),
    "MINIAPP_FOOTBALL_DATA_LIVE_PLAN": "true",
    "MINIAPP_FOOTBALL_DATA_PER_MINUTE": "60",
    "MINIAPP_FOOTBALL_DATA_PER_DAY": "14400",
    "MINIAPP_FOOTBALL_DATA_SHARED_RESERVE": "40",
    "MINIAPP_FOOTBALL_DATA_COMPETITIONS": "all",
    # Odds from the bot (signed push): on once its shared secret exists on this host (same value in the bot).
    # Odds are served from the Mini App's own Mostbet source (catalogue mode, docs/miniapp/odds-catalog.md) and,
    # while the bot runs, also from its signed feed: serving no longer depends on the bot's secret.
    "MINIAPP_ODDS_FEED_ENABLED": "true",
    "MINIAPP_ODDS_CATALOG": "true",
    "MINIAPP_ODDS_SOURCE_MAX_PER_CYCLE": "40",  # the bot no longer reads Mostbet (launcher only, 15.11): the full budget
    "MINIAPP_LOGINS_ENABLED": "true",
}
if not OWNER.isdigit():
    sys.exit("STOP: owner id is not a Telegram id")
# Public launch (owner 15.11): the presence of /root/miniapp-secrets/public opens the Mini App to everyone; without it
# the closed mode stays (only the owner). The blocked list (MINIAPP_BLOCKED_USER_IDS, file blocked-ids) applies always.
PUBLIC = Path("/root/miniapp-secrets/public").exists()
if not PUBLIC:
    env_vars["MINIAPP_ALLOWED_USER_IDS"] = OWNER
_BLOCKED = Path("/root/miniapp-secrets/blocked-ids")
if _BLOCKED.exists():
    ids = [x for x in secret(str(_BLOCKED)).replace("\n", ",").split(",") if x.strip()]
    if not all(x.strip().isdigit() and 1 <= int(x) < 2**53 for x in ids):  # the app's own rule (settings.py)
        sys.exit("STOP: blocked-ids holds something that is not a Telegram id")
    if ids:
        env_vars["MINIAPP_BLOCKED_USER_IDS"] = ",".join(x.strip() for x in ids)
print("access:", "PUBLIC (everyone)" if PUBLIC else "closed (owner only)",
      f"blocked={len(env_vars.get('MINIAPP_BLOCKED_USER_IDS', '').split(',')) if env_vars.get('MINIAPP_BLOCKED_USER_IDS') else 0}")
if ODDS_SECRET:
    env_vars["MINIAPP_ODDS_FEED_SECRET"] = ODDS_SECRET
if POLICY_KEY:
    # Telemetry (activity, retention) exists only with ads on; no offer is shown until the admin publishes one.
    env_vars |= {"MINIAPP_ADS_ENABLED": "true", "MINIAPP_ADS_POLICY_PUBLIC_KEY": POLICY_KEY,
                 "MINIAPP_ACCESS_POLICY_PUBLIC_KEY": POLICY_KEY, "MINIAPP_REFERRALS_ENABLED": "true"}
if AI_KEY:
    # Model calls per UTC day (cache hits are free). Owner 16.11: 10 per Telegram user and no app-wide cap — the budget
    # is set to the settings' maximum (100000), i.e. only the per-user limit applies.
    env_vars |= {"MINIAPP_ANTHROPIC_API_KEY": AI_KEY, "MINIAPP_AI_PER_USER_DAILY": "10", "MINIAPP_AI_DAILY_BUDGET": "100000"}
for k, v in env_vars.items():
    if "\n" in v or not v:
        sys.exit(f"STOP: bad value for {k}")
# A missing secrets file must not silently switch off what the running app has (audit wave 2).
if RUNNING_KEYS:
    lost = [name for name, was, now in (
        ("public access (/root/miniapp-secrets/public)", "MINIAPP_ALLOWED_USER_IDS" not in RUNNING_KEYS, PUBLIC),
        ("AI forecasts (anthropic-key)", "MINIAPP_ANTHROPIC_API_KEY" in RUNNING_KEYS, bool(AI_KEY)),
        ("odds feed (odds-feed)", "MINIAPP_ODDS_FEED_SECRET" in RUNNING_KEYS, bool(ODDS_SECRET)),
        ("admin features (admin-policy-public-key)", "MINIAPP_ADS_POLICY_PUBLIC_KEY" in RUNNING_KEYS, bool(POLICY_KEY)),
        ("blocked users (blocked-ids)", "MINIAPP_BLOCKED_USER_IDS" in RUNNING_KEYS,
         "MINIAPP_BLOCKED_USER_IDS" in env_vars),
    ) if was and not now]
    if lost and not allow_off:
        sys.exit("STOP: this run would switch off " + "; ".join(lost) + " (its file is missing). "
                 "Restore the file, or pass --allow-off to switch it off on purpose")
    for name in lost:
        print("switching OFF on purpose:", name)
# 3) image from the loopback registry (no login)
call("application.saveDockerProvider", {"applicationId": app_id, "dockerImage": f"{REGISTRY}:{tag}",
                                         "username": "", "password": "", "registryUrl": ""})

call("application.saveEnvironment", {"applicationId": app_id, "env": "\n".join(f"{k}={v}" for k, v in env_vars.items()),
                                      "buildArgs": "", "buildSecrets": "", "createEnvFile": False})
print("environment: saved", len(env_vars), "variables (values not shown)")

# 5) resources, one replica, stop-first with automatic rollback, no autodeploy
call("application.update", {
    "applicationId": app_id, "replicas": 1, "autoDeploy": False,
    "memoryLimit": str(512 * 2**20), "memoryReservation": str(256 * 2**20),
    "cpuLimit": str(1_000_000_000), "cpuReservation": str(250_000_000),
    # Monitor 150 s: the image's healthcheck (start 10 s, every 30 s, 3 retries) needs ~100 s to call a task unhealthy.
    "updateConfigSwarm": {"Parallelism": 1, "Order": "stop-first", "FailureAction": "rollback", "Monitor": 150_000_000_000},
    "rollbackConfigSwarm": {"Parallelism": 1, "Order": "stop-first"},
})
print("resources: 512 MiB / 1 CPU, stop-first, rollback on failure of later updates")

# 6) data volume (once)
full = call("application.one", {"applicationId": app_id}, method="GET")
if not any(m.get("mountPath") == "/data" for m in full.get("mounts", [])):
    call("mounts.create", {"type": "volume", "volumeName": VOLUME, "mountPath": "/data",
                           "serviceId": app_id, "serviceType": "application"})
print("volume:", VOLUME, "-> /data")

# 7) domain (once): HTTP inside the host; TLS is Cloudflare's, ingress is the tunnel to 127.0.0.1:80
if not any(d.get("host") == DOMAIN for d in full.get("domains", [])):
    call("domain.create", {"host": DOMAIN, "path": "/", "port": 8000, "https": False, "certificateType": "none",
                           "applicationId": app_id, "domainType": "application"})
print("domain:", DOMAIN, "-> :8000 (http, entrypoint web)")
full = call("application.one", {"applicationId": app_id}, method="GET")
if full.get("ports") or ((full.get("endpointSpecSwarm") or {}).get("Ports")):
    sys.exit("STOP: the application publishes host ports")

if deploy:
    name = str(full.get("appName") or APP)
    # Containers before the deploy: success needs a NEW one (the same tag again would match the old task).
    before = set(subprocess.run(["docker", "ps", "-aq", "--no-trunc", "-f", f"name={name}"],
                                capture_output=True, text=True).stdout.split())
    call("application.deploy", {"applicationId": app_id})
    print("deploy: started")
    # Done only when the NEW image runs healthy and the public address answers (through Cloudflare and Traefik).
    image = f"{REGISTRY}:{tag}"
    deadline, healthy = time.monotonic() + 360, False
    while time.monotonic() < deadline:
        rows = subprocess.run(["docker", "ps", "--no-trunc", "-f", f"name={name}", "--format", "{{.ID}} {{.Image}} {{.Status}}"],
                              capture_output=True, text=True).stdout.splitlines()
        # Swarm's own verdict on this update: a rollback also starts a new healthy container of the same tag.
        state = subprocess.run(["docker", "service", "inspect", name, "--format",
                                "{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}"],
                               capture_output=True, text=True).stdout.strip()
        if state.startswith("rollback") or state == "paused":
            sys.exit(f"STOP: Swarm update state '{state}': the new configuration was rolled back or paused")
        if state in ("completed", "") and any(
                r.split(" ", 1)[0] not in before and f" {image} " in r and "(healthy)" in r for r in rows):
            healthy = True
            break
        time.sleep(5)
    if not healthy:
        sys.exit(f"STOP: {image} is not running healthy with the update completed after 6 minutes (Swarm rolls a failed update back)")
    print("health:", image, "healthy")
    for attempt in range(6):
        try:
            # Cloudflare answers Python's default User-Agent with 403.
            probe = urllib.request.Request(f"https://{DOMAIN}/api/health", headers={"User-Agent": "proqnozai-deploy-check"})
            with urllib.request.urlopen(probe, timeout=15) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(5)
    else:
        sys.exit(f"STOP: https://{DOMAIN}/api/health does not answer 200 (container healthy: check the tunnel)")
    print("public health: ok")
print("OK")
