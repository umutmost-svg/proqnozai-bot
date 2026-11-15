"""Create or update the admin panel (server mode) in Dokploy through its API and deploy it (runs ON the server, as root).

    python3 - <IMAGE_TAG> [--deploy] < deploy/admin/dokploy_deploy.py

Reads, never prints: /root/.dokploy-api-key and /root/admin-secrets/edge (the edge secret Cloudflare injects as
X-Admin-Edge-Auth on admin.proqnozai.com). Talks only to http://127.0.0.1:3000. Idempotent: finds the project and
application by exact name, refuses one that is not ours or publishes host ports, re-saves image, environment,
resources and Swarm update config, mounts the Mini App's data volume (/data) and the admin's OWN state volume
(/state, accounts and 2FA secrets) once each, adds the domain once. See docs/ops/admin-server.md.
Errors print the procedure, HTTP status and the API's error code only — never a body or a value.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:3000/api/"
PROJECT = "proqnozai-admin"
APP = "proqnozai-admin"
DOMAIN = "admin.proqnozai.com"
DATA_VOLUME = "proqnozai-miniapp-data"   # the Mini App's (read by the admin)
STATE_VOLUME = "proqnozai-admin-state"   # the admin's own; never mounted into the Mini App
REGISTRY = "127.0.0.1:5000/proqnozai-admin"
PORT = 8795


def secret(name: str) -> str:
    return Path(name).read_text().strip()


KEY = secret("/root/.dokploy-api-key")
EDGE = secret("/root/admin-secrets/edge")
# Same rule as miniapp.admin_web.config (else the app would refuse to start after the change).
if len(EDGE) < 32 or not EDGE.isascii() or not EDGE.isprintable() or " " in EDGE:
    sys.exit("STOP: /root/admin-secrets/edge must hold at least 32 printable characters, no spaces")


def call(proc: str, body: dict | None = None, method: str = "POST"):
    url = API + proc
    data = None
    if method == "GET" and body:
        url += "?" + urllib.parse.urlencode(body)
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


def publishes(app: dict) -> bool:
    return bool(app.get("ports") or ((app.get("endpointSpecSwarm") or {}).get("Ports")))


if len(sys.argv) < 2 or not sys.argv[1].isalnum():
    sys.exit("usage: python3 - <IMAGE_TAG> [--deploy] < dokploy_deploy.py")
tag, deploy = sys.argv[1], "--deploy" in sys.argv[2:]

projects = call("project.all", method="GET")
project = next((p for p in projects if p.get("name") == PROJECT), None)
if project is None:
    call("project.create", {"name": PROJECT, "description": "SporBot admin panel (server mode)"})
    projects = call("project.all", method="GET")
    project = next(p for p in projects if p.get("name") == PROJECT)
env = (project.get("environments") or [None])[0]
if not env:
    sys.exit("STOP: the project has no environment")

def preflight(cur: dict) -> None:
    """Everything about an existing application is checked BEFORE any write: identity, image source, ports,
    mounts, domains. A foreign or differently configured application is never touched."""
    if cur.get("name") != APP or not str(cur.get("appName", "")).startswith(APP) or cur.get("sourceType") != "docker":
        sys.exit("STOP: an application with this name is not the admin panel")
    image = str(cur.get("dockerImage") or "")
    if image and not image.startswith(REGISTRY + ":"):
        sys.exit("STOP: the application runs another image")
    if publishes(cur):
        sys.exit("STOP: the application publishes host ports")
    allowed = {"/data": DATA_VOLUME, "/state": STATE_VOLUME}
    for m in cur.get("mounts", []):
        if m.get("type") != "volume" or allowed.get(m.get("mountPath")) != m.get("volumeName"):
            sys.exit("STOP: the application has a mount this script does not manage")
    for d in cur.get("domains", []):
        if d.get("host") != DOMAIN or int(d.get("port") or 0) != PORT or d.get("path") not in ("/", None, ""):
            sys.exit("STOP: the application has a domain this script does not manage")


apps = [a for e in project.get("environments", []) for a in e.get("applications", [])]
app = next((a for a in apps if a.get("name") == APP), None)
if app is not None:
    preflight(call("application.one", {"applicationId": app["applicationId"]}, method="GET"))
else:
    app = call("application.create", {"name": APP, "appName": APP, "environmentId": env["environmentId"],
                                       "description": "SporBot admin panel (Cloudflare Access + edge secret)"})
app_id = app["applicationId"]
print("application:", app.get("appName"), app_id)

env_vars = {
    "ADMIN_WEB_MODE": "server",
    "ADMIN_DATA_DIR": "/data",
    "ADMIN_STATE_DIR": "/state",
    "ADMIN_PUBLIC_HOST": DOMAIN,
    "ADMIN_EDGE_SECRET": EDGE,
    "ADMIN_WEB_PORT": str(PORT),
}
for k, v in env_vars.items():
    if "\n" in v or not v:
        sys.exit(f"STOP: bad value for {k}")
call("application.saveDockerProvider", {"applicationId": app_id, "dockerImage": f"{REGISTRY}:{tag}",
                                         "username": "", "password": "", "registryUrl": ""})
call("application.saveEnvironment", {"applicationId": app_id, "env": "\n".join(f"{k}={v}" for k, v in env_vars.items()),
                                      "buildArgs": "", "buildSecrets": "", "createEnvFile": False})
print("environment: saved", len(env_vars), "variables (values not shown)")

call("application.update", {
    "applicationId": app_id, "replicas": 1, "autoDeploy": False,
    "memoryLimit": str(384 * 2**20), "memoryReservation": str(128 * 2**20),
    "cpuLimit": str(500_000_000), "cpuReservation": str(100_000_000),
    "updateConfigSwarm": {"Parallelism": 1, "Order": "stop-first", "FailureAction": "rollback", "Monitor": 120_000_000_000},  # healthcheck: start 15 s, every 30 s, 3 retries
    "rollbackConfigSwarm": {"Parallelism": 1, "Order": "stop-first"},
})
print("resources: 384 MiB / 0.5 CPU, stop-first, rollback on failure")

full = call("application.one", {"applicationId": app_id}, method="GET")
mounts = {m.get("mountPath"): m for m in full.get("mounts", [])}
for path, volume in (("/data", DATA_VOLUME), ("/state", STATE_VOLUME)):
    if path not in mounts:
        call("mounts.create", {"type": "volume", "volumeName": volume, "mountPath": path,
                               "serviceId": app_id, "serviceType": "application"})
    print("volume:", volume, "->", path)

if not any(d.get("host") == DOMAIN for d in full.get("domains", [])):
    call("domain.create", {"host": DOMAIN, "path": "/", "port": PORT, "https": False, "certificateType": "none",
                           "applicationId": app_id, "domainType": "application"})
print("domain:", DOMAIN, f"-> :{PORT} (http, entrypoint web; requests without the edge secret get 403)")
full = call("application.one", {"applicationId": app_id}, method="GET")
if publishes(full):
    sys.exit("STOP: the application publishes host ports")

if deploy:
    call("application.deploy", {"applicationId": app_id})
    print("deploy: started")
print("OK")
