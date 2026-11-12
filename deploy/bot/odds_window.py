"""Bot update window: turn on the odds export to the Mini App (runs ON the server, as root).

    python3 - check                < deploy/bot/odds_window.py   # read-only preflight
    python3 - apply --confirm      < deploy/bot/odds_window.py   # the window (owner-confirmed only)
    python3 - rollback --confirm   < deploy/bot/odds_window.py   # back to the pre-window image, env and config

apply: snapshots the Dokploy config and the Swarm service spec to /root/bot-window (0700/0600), tags the
running image :pre-odds, turns autoDeploy off for the window, sets stop-first (one instance at a time: no
getUpdates conflict), adds MINIAPP_ODDS_URL / _FEED_SECRET / _MAX_LINES, switches the source branch to
release/bot-odds and deploys. The build runs while the old bot keeps working; the downtime is the stop-first
swap only. Any exception or failed check after the first change -> verified rollback: the Swarm service gets
the :pre-odds image and loses the odds variables, Dokploy gets its env, branch and autoDeploy back.
Rollback triggers: deployment error, Swarm update not completed, the new task is not the release build,
more than one running task at any sample, a new task after the release one (restart), a getUpdates conflict,
a traceback, the export disabled at startup, no accepted push (odds_export.pushed = HTTP 200) in PUSH_WAIT_S.
Dokploy keeps stop-first after a rollback on purpose (the owner's rule for this bot).
Prints names, states and counts only — never an env value, token or secret.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:3000/api/"
APP_ID = "kgfev211JBqtmsVjrPVwZ"
APP_NAME = "proqnozai-bot"
SERVICE = "proqnozai-proqnozaibot-uejbpm"
IMAGE = f"{SERVICE}:latest"
PRE = f"{SERVICE}:pre-odds"
BRANCH_FROM, BRANCH_TO = "main", "release/bot-odds"
RELEASE_MARKER = "/app/mostbet_feed.py"  # exists only in the release build (not on main)
ODDS_VARS = ("MINIAPP_ODDS_URL", "MINIAPP_ODDS_FEED_SECRET", "MINIAPP_ODDS_MAX_LINES")
ODDS_URL = "https://app.proqnozai.com/api/internal/odds"
MAX_LINES = "10"
ODDS_FILE = Path("/root/miniapp-secrets/odds-feed")
STATE = Path("/root/bot-window")
SNAPSHOT = STATE / "pre-odds.json"
BUILD_WAIT_S, WATCH_S, PUSH_WAIT_S, SAMPLE_S = 20 * 60, 240, 8 * 60, 5
JOB_DRAIN_S = 25 * 60  # a rollback first waits out a Dokploy job still running (it would redeploy after us)
CMD_TIMEOUT_S, CONVERGE_S = 60, 180
UPDATE_CFG = {"Parallelism": 1, "Order": "stop-first", "FailureAction": "rollback", "Monitor": 60_000_000_000}
ROLLBACK_CFG = {"Parallelism": 1, "Order": "stop-first"}

KEY = Path("/root/.dokploy-api-key").read_text().strip()


class WindowError(Exception):
    pass


def call(proc: str, body: dict | None = None, method: str = "POST"):
    url, data = API + proc, None
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
        raise WindowError(f"{proc} -> HTTP {e.code}") from None
    except OSError as e:
        raise WindowError(f"{proc} -> {type(e).__name__}") from None
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()


def run(*args: str, timeout: int = CMD_TIMEOUT_S) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WindowError(f"timeout: {' '.join(args[:3])}") from None


def sh(*args: str) -> str:
    return run(*args).stdout.strip()


def app() -> dict:
    return call("application.one", {"applicationId": APP_ID}, method="GET")


def env_map(env: str) -> dict[str, str]:
    return {l.split("=", 1)[0]: l.split("=", 1)[1] for l in env.splitlines() if "=" in l and not l.startswith("#")}


def service() -> dict:
    out = sh("docker", "service", "inspect", SERVICE)
    return json.loads(out)[0] if out.startswith("[") else {}


def spec_env_names(svc: dict) -> set[str]:
    env = svc.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Env") or []
    return {e.split("=", 1)[0] for e in env}


def running() -> list[str]:
    return sh("docker", "ps", "-q", "--no-trunc", "-f", f"label=com.docker.swarm.service.name={SERVICE}").split()


def all_task_ids() -> set[str]:
    return set(sh("docker", "service", "ps", "-q", "--no-trunc", SERVICE).split())


def task_of(container: str) -> str:
    return sh("docker", "inspect", "-f", '{{index .Config.Labels "com.docker.swarm.task.id"}}', container)


def image_id(ref: str) -> str:
    return sh("docker", "image", "inspect", "-f", "{{.Id}}", ref)


def running_image() -> str:
    c = running()
    return sh("docker", "inspect", "-f", "{{.Image}}", c[0]) if len(c) == 1 else ""


def service_logs(since: str) -> str:
    r = run("docker", "service", "logs", "--raw", "--since", since, SERVICE, timeout=90)
    return r.stdout + r.stderr


def webhook_url(token: str) -> str | None:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=20) as r:
            return json.loads(r.read())["result"].get("url", "")
    except Exception:
        return None


def check() -> bool:
    a, svc, ok = app(), service(), True

    def need(cond: bool, what: str) -> None:
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + what)
        ok = ok and cond

    env = env_map(a.get("env") or "")
    need(a.get("name") == APP_NAME and a.get("appName") == SERVICE, "Dokploy app is the bot")
    need(a.get("sourceType") == "github" and a.get("branch") == BRANCH_FROM, f"source github@{BRANCH_FROM}")
    need(not job_active(), "no Dokploy deployment of the bot queued or running")
    need(svc.get("Spec", {}).get("Mode", {}).get("Replicated", {}).get("Replicas") == 1, "service is 1 replica")
    need(len(running()) == 1, "exactly one bot task running")
    need(not any(k in env for k in ODDS_VARS) and not (spec_env_names(svc) & set(ODDS_VARS)), "odds variables not set yet")
    sec = ODDS_FILE.read_text().strip() if ODDS_FILE.exists() else ""
    need(len(sec) >= 32, "odds secret file present (>= 32 chars)")
    need(ODDS_FILE.exists() and (ODDS_FILE.stat().st_mode & 0o077) == 0, "odds secret file mode 0600")
    need(webhook_url(env.get("TELEGRAM_TOKEN", "")) == "", "no Telegram webhook (polling)")
    need(bool(image_id(IMAGE)) and image_id(IMAGE) == running_image(), "running image is :latest")
    mini = sh("docker", "ps", "-q", "-f", "label=com.docker.swarm.service.name=proqnozai-miniapp-olj3ry").split()
    need(len(mini) == 1 and sh("docker", "inspect", "-f", "{{.State.Health.Status}}", mini[0]) == "healthy", "Mini App healthy")
    need(len(mini) == 1 and sh("docker", "exec", mini[0], "printenv", "MINIAPP_ODDS_FEED_ENABLED") == "true",
         "Mini App odds feed enabled")
    st = sh("df", "--output=avail", "-B1G", "/").splitlines()[-1].strip()
    need(st.isdigit() and int(st) >= 5, f"disk free {st} GB (>= 5)")
    need(not STATE.is_symlink() and (not STATE.exists() or STATE.is_dir()), "state directory is a plain directory")
    print("PREFLIGHT", "PASS" if ok else "FAIL")
    return ok


def save_snapshot(snap: dict) -> None:
    if STATE.is_symlink():
        raise WindowError("state directory is a symlink")
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)
    tmp = STATE / f".pre-odds.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, SNAPSHOT)


def restore_dokploy(snap: dict) -> bool:
    """Env, build settings and the GitHub source back to the snapshot; autoDeploy stays off here."""
    call("application.saveEnvironment", {"applicationId": APP_ID, "env": snap["env"], "buildArgs": snap["buildArgs"],
                                         "buildSecrets": snap["buildSecrets"], "createEnvFile": snap["createEnvFile"]})
    call("application.saveGithubProvider", {**snap["github"], "applicationId": APP_ID})
    a = app()
    cur = {"env": a.get("env") or "", "buildArgs": a.get("buildArgs") or "", "buildSecrets": a.get("buildSecrets") or "",
           "createEnvFile": bool(a.get("createEnvFile")),
           "github": {k: a.get(k) for k in ("repository", "owner", "buildPath", "githubId", "branch", "triggerType")}
           | {"watchPaths": a.get("watchPaths") or [], "enableSubmodules": bool(a.get("enableSubmodules"))}}
    bad = [k for k in cur if cur[k] != snap[k]]
    print("dokploy: env, build settings, source restored" if not bad else f"dokploy: RESTORE MISMATCH in {bad}")
    return not bad


def job_active() -> bool:
    """A bot deployment queued or running in Dokploy (applicationStatus alone misses queued jobs)."""
    queued = APP_ID in json.dumps(call("deployment.queueList", method="GET"))
    deps = call("deployment.all", {"applicationId": APP_ID}, method="GET")
    return queued or any(d.get("status") in ("running", "pending", "queued") for d in deps)


def drain_job() -> bool:
    """Wait until no bot job is queued or running (it would redeploy the release after us); on timeout kill it."""
    deadline, quiet = time.time() + JOB_DRAIN_S, 0
    while time.time() < deadline:
        quiet = quiet + 1 if not job_active() else 0
        if quiet >= 2:
            return True
        print("waiting: a Dokploy deployment of the bot is queued or running")
        time.sleep(30)
    print("drain timeout: killing the build and cleaning the bot's queue")
    for proc in ("application.killBuild", "application.cleanQueues"):
        try:
            call(proc, {"applicationId": APP_ID})
        except WindowError as e:
            print(proc, "failed:", e)
    time.sleep(60)
    return not job_active() and (time.sleep(30) or not job_active())


def autodeploy_off() -> bool:
    for _ in range(3):
        try:
            call("application.update", {"applicationId": APP_ID, "autoDeploy": False})
            if app().get("autoDeploy") is False:
                return True
        except WindowError as e:
            print("autoDeploy off failed:", e)
        time.sleep(5)
    return False


def rollback(reason: str) -> None:
    print("ROLLBACK:", reason)
    snap = json.loads(SNAPSHOT.read_text())
    off = autodeploy_off()  # nothing re-deploys meanwhile
    print("autoDeploy:", "off (verified)" if off else "COULD NOT VERIFY OFF")
    if not off:
        # With autoDeploy possibly on, a push could redeploy during the rollback: stop for a human.
        print("ROLLBACK INCOMPLETE — autoDeploy not verified off; Swarm NOT touched.",
              "Turn autoDeploy off in Dokploy, then: python3 - rollback --confirm")
        sys.exit(2)
    drained = drain_job()
    if not drained:
        # Rolling Swarm back now would race the job (it redeploys the release after us): stop for a human.
        print("ROLLBACK INCOMPLETE — a Dokploy job of the bot is still active after kill; Swarm NOT touched.",
              "Run again when it ends: python3 - rollback --confirm")
        sys.exit(2)
    svc = service()
    changed = bool(spec_env_names(svc) & set(ODDS_VARS)) or running_image() != snap["image_id"] or len(running()) != 1
    if changed:
        args = ["docker", "service", "update", "--detach=false", "--quiet", "--update-order", "stop-first",
                "--image", PRE]
        for k in ODDS_VARS:
            args += ["--env-rm", k]
        r = run(*args, SERVICE, timeout=CONVERGE_S)
        print("swarm update:", "ok" if r.returncode == 0 else f"exit {r.returncode}")
    else:
        print("swarm: service never changed — no restart")
    restored = restore_dokploy(snap)
    deadline = time.time() + CONVERGE_S
    good = False
    while time.time() < deadline and not good:
        svc = service()
        state = (svc.get("UpdateStatus") or {}).get("State", "completed")
        good = (len(running()) == 1 and running_image() == snap["image_id"]
                and not (spec_env_names(svc) & set(ODDS_VARS)) and state in ("completed", "rollback_completed"))
        if not good:
            time.sleep(SAMPLE_S)
    if good and restored and off and not job_active():
        call("application.update", {"applicationId": APP_ID, "autoDeploy": snap["autoDeploy"]})
        print("ROLLBACK PASS: pre-odds image, one task, no odds variables, Dokploy restored, autoDeploy back")
    else:
        print("ROLLBACK INCOMPLETE — autoDeploy left OFF; check manually",
              {"swarm": good, "dokploy": restored, "no_job": not job_active()})
    sys.exit(1)


def watch(pre_id: str, pre_tasks: set[str], t0: str) -> str:
    deadline = time.time() + BUILD_WAIT_S
    while True:
        time.sleep(15)
        if time.time() > deadline:
            raise WindowError("no completed deployment within the build window")
        if len(running()) > 1:
            raise WindowError("two bot tasks running")
        status = app().get("applicationStatus")
        if status == "error":
            raise WindowError("Dokploy deployment error")
        svc = service()
        if status == "done" and (spec_env_names(svc) & set(ODDS_VARS)):
            break
    while True:  # Swarm convergence of the release task
        state = (svc.get("UpdateStatus") or {}).get("State", "completed")
        if state in ("rollback_started", "rollback_completed", "rollback_paused", "paused"):
            raise WindowError(f"swarm update {state}")
        c = running()
        if state == "completed" and len(c) == 1 and running_image() != pre_id:
            break
        if len(c) > 1:
            raise WindowError("two bot tasks running")
        if time.time() > deadline:
            raise WindowError(f"swarm update not completed ({state})")
        time.sleep(SAMPLE_S)
        svc = service()
    spec = svc.get("Spec", {})
    if spec.get("Mode", {}).get("Replicated", {}).get("Replicas") != 1:
        raise WindowError("service is not 1 replica after deploy")
    if (spec.get("UpdateConfig") or {}).get("Order") != "stop-first":
        raise WindowError("service update order is not stop-first")
    release_task = task_of(running()[0])
    if release_task in pre_tasks or run("docker", "exec", running()[0], "test", "-f", RELEASE_MARKER).returncode != 0:
        raise WindowError("running task is not the release build")
    print("swap: release build running (stop-first), watching")

    swap, pushed, n = time.time(), False, 0
    known = pre_tasks | {release_task}
    while time.time() - swap < PUSH_WAIT_S and not (pushed and time.time() - swap >= WATCH_S):
        time.sleep(SAMPLE_S)
        c = running()
        if len(c) > 1:
            raise WindowError("two bot tasks running")
        new = all_task_ids() - known
        if new:
            raise WindowError("bot task restarted")
        if not c or task_of(c[0]) != release_task:
            time.sleep(SAMPLE_S)
            if not running() or task_of(running()[0]) != release_task:
                raise WindowError("release task is not running")
        n += 1
        if n % 6 == 0:  # logs every ~30 s (all tasks of the service, dead ones too)
            out = service_logs(t0)
            if "terminated by other getUpdates" in out or "telegram.error.Conflict" in out:
                raise WindowError("getUpdates conflict")
            if "Traceback" in out:
                raise WindowError("traceback in bot log")
            if "odds_export.disabled" in out:
                raise WindowError("odds export disabled at startup")
            pushed = pushed or "odds_export.pushed" in out
    out = service_logs(t0)
    if "terminated by other getUpdates" in out or "telegram.error.Conflict" in out or "Traceback" in out:
        raise WindowError("conflict or traceback in bot log")
    if "odds_export.pushed" not in out:
        raise WindowError("no accepted push to the Mini App")
    print("pushes accepted:", out.count("odds_export.pushed"), "| refused:", out.count("odds_export.push_refused"),
          "| failed:", out.count("odds_export.push_failed"))
    return release_task


def still_healthy(release_task: str, pre_id: str, known: set[str], t0: str) -> None:
    """Re-check after any wait: the same release task alone, nothing new, no conflict or traceback."""
    c = running()
    if len(c) != 1 or task_of(c[0]) != release_task or running_image() == pre_id:
        raise WindowError("release task is not the only running task")
    if all_task_ids() - known:
        raise WindowError("bot task restarted")
    out = service_logs(t0)
    if "terminated by other getUpdates" in out or "telegram.error.Conflict" in out or "Traceback" in out:
        raise WindowError("conflict or traceback in bot log")


def apply() -> None:
    if not check():
        sys.exit("STOP: preflight failed, nothing changed")
    a, svc = app(), service()
    pre_id = image_id(IMAGE)
    snap = {"image_id": pre_id, "env": a.get("env") or "", "buildArgs": a.get("buildArgs") or "",
            "buildSecrets": a.get("buildSecrets") or "", "createEnvFile": bool(a.get("createEnvFile")),
            "autoDeploy": bool(a.get("autoDeploy")),
            "github": {k: a.get(k) for k in ("repository", "owner", "buildPath", "githubId", "branch", "triggerType")}
            | {"watchPaths": a.get("watchPaths") or [], "enableSubmodules": bool(a.get("enableSubmodules"))},
            "service_spec": svc.get("Spec")}
    save_snapshot(snap)
    if run("docker", "tag", IMAGE, PRE).returncode != 0 or image_id(PRE) != pre_id:
        sys.exit("STOP: could not tag :pre-odds, nothing changed")
    print("saved: image :pre-odds, config + service spec snapshot (0600)")
    pre_tasks = all_task_ids()
    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        call("application.update", {"applicationId": APP_ID, "autoDeploy": False,
                                    "updateConfigSwarm": UPDATE_CFG, "rollbackConfigSwarm": ROLLBACK_CFG})
        env = snap["env"].rstrip("\n") + "\n" + "\n".join([
            f"MINIAPP_ODDS_URL={ODDS_URL}", f"MINIAPP_ODDS_FEED_SECRET={ODDS_FILE.read_text().strip()}",
            f"MINIAPP_ODDS_MAX_LINES={MAX_LINES}"])
        call("application.saveEnvironment", {"applicationId": APP_ID, "env": env, "buildArgs": snap["buildArgs"],
                                             "buildSecrets": snap["buildSecrets"], "createEnvFile": snap["createEnvFile"]})
        call("application.saveGithubProvider", {**snap["github"], "applicationId": APP_ID, "branch": BRANCH_TO})
        print(f"config: autoDeploy off, stop-first + rollback, 3 odds variables, branch {BRANCH_TO}")
        call("application.deploy", {"applicationId": APP_ID, "title": "odds export window"})
        print("deploy: started", t0, "(build runs, the old bot keeps working)")
        release_task = watch(pre_id, pre_tasks, t0)
        if not drain_job():
            raise WindowError("a Dokploy job of the bot is still active after the window")
        still_healthy(release_task, pre_id, pre_tasks | {release_task}, t0)
        call("application.update", {"applicationId": APP_ID, "autoDeploy": snap["autoDeploy"]})
        still_healthy(release_task, pre_id, pre_tasks | {release_task}, t0)
    except (WindowError, Exception, KeyboardInterrupt) as e:
        rollback(str(e) if isinstance(e, WindowError) else f"unexpected {type(e).__name__}")
    print(f"WINDOW PASS — autoDeploy restored (branch {BRANCH_TO}); rollback stays available: python3 - rollback --confirm")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    confirmed = "--confirm" in sys.argv[2:]
    try:
        if cmd == "check":
            sys.exit(0 if check() else 1)
        if cmd == "apply" and confirmed:
            apply()
        elif cmd == "rollback" and confirmed:
            rollback("requested")
        else:
            sys.exit("usage: python3 - check | apply --confirm | rollback --confirm")
    except WindowError as e:
        sys.exit(f"STOP: {e}")


main()
