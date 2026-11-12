"""Create or update the Mini App in Dokploy through its API and deploy it (runs ON the server, as root).

    python3 - <IMAGE_TAG> [--deploy] < deploy/miniapp/dokploy_deploy.py

Reads, never prints: /root/.dokploy-api-key (Dokploy API key), /root/miniapp-secrets/{edge,csrf,
football-data-key,owner-id}. Talks only to http://127.0.0.1:3000 (Dokploy on this host).
Idempotent configuration: finds the project/application by exact name, checks it BEFORE any change (no
published ports), re-saves image, environment, resources, Swarm update config, the data volume and the domain
(each only once). Without --deploy it configures and stops; every run WITH --deploy starts one Dokploy
deployment. Closed mode: MINIAPP_ALLOWED_USER_IDS = the bot's ADMIN_ID, re-read from the running bot and
compared with the stored value. On the FIRST deployment there is no previous revision to roll back to: health is
verified explicitly afterwards (release-same-server.md, B3); later updates roll back automatically.
Errors print the procedure, HTTP status and the API's error code only — never a body or a value.
See docs/miniapp/release-same-server.md.
"""
import json
import subprocess
import sys
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

if len(sys.argv) < 2 or not sys.argv[1].isalnum():
    sys.exit("usage: python3 - <IMAGE_TAG> [--deploy] < dokploy_deploy.py")
tag, deploy = sys.argv[1], "--deploy" in sys.argv[2:]

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
    if cur.get("sourceType") not in ("docker",) and cur.get("dockerImage"):
        sys.exit("STOP: unexpected source type")
    if cur.get("ports") or ((cur.get("endpointSpecSwarm") or {}).get("Ports")):
        sys.exit("STOP: the application publishes host ports")
if app is None:
    app = call("application.create", {"name": APP, "appName": APP, "environmentId": environment_id,
                                       "description": "ProqnozAI Mini App (closed mode)"})
app_id = app["applicationId"]
print("application:", app.get("appName"), app_id)

# 3) image from the loopback registry (no login)
call("application.saveDockerProvider", {"applicationId": app_id, "dockerImage": f"{REGISTRY}:{tag}",
                                         "username": "", "password": "", "registryUrl": ""})

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
    "MINIAPP_ALLOWED_USER_IDS": OWNER,
    "MINIAPP_FOOTBALL_DATA_KEY": secret("/root/miniapp-secrets/football-data-key"),
    "MINIAPP_FOOTBALL_DATA_LIVE_PLAN": "true",
    "MINIAPP_FOOTBALL_DATA_PER_MINUTE": "60",
    "MINIAPP_FOOTBALL_DATA_PER_DAY": "14400",
    "MINIAPP_FOOTBALL_DATA_SHARED_RESERVE": "40",
    "MINIAPP_FOOTBALL_DATA_COMPETITIONS": "all",
    "MINIAPP_ODDS_FEED_ENABLED": "false",
    "MINIAPP_LOGINS_ENABLED": "true",
}
if not OWNER.isdigit():
    sys.exit("STOP: owner id is not a Telegram id")
for k, v in env_vars.items():
    if "\n" in v or not v:
        sys.exit(f"STOP: bad value for {k}")
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
    call("application.deploy", {"applicationId": app_id})
    print("deploy: started")
print("OK")
