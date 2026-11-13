"""Offline tests of the `sweep` modes: restore-drill-local.sh (fake docker on PATH) and backup-export.sh.
Run: pytest deploy/ops/tests/"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent

# Volumes and containers with their proqnozai.drill label; `rm` deletes them from the state file.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
f = os.environ["FAKE_DOCKER_STATE"]
st = json.load(open(f))
a = sys.argv[1:]
if a[0] == "info":
    sys.exit(0 if st.get("running", True) else 1)
if a[:2] == ["ps", "-aq"]:
    print("\n".join(st["containers"]))
elif a[:2] == ["volume", "ls"]:
    print("\n".join(st["volumes"]))
elif a[0] == "inspect":
    print(st["containers"][a[-1]])
elif a[:2] == ["volume", "inspect"]:
    print(st["volumes"][a[-1]])
elif a[:2] == ["rm", "-f"]:
    st["containers"].pop(a[-1]); json.dump(st, open(f, "w"))
elif a[:2] == ["volume", "rm"]:
    if a[-1] in st.get("busy", []):
        sys.exit(1)
    st["volumes"].pop(a[-1]); json.dump(st, open(f, "w"))
'''


def _drill_env(tmp_path: Path, state: dict) -> tuple[dict, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    sf = tmp_path / "docker.json"
    sf.write_text(json.dumps(state))
    backups = tmp_path / "backups"
    (backups / ".work").mkdir(parents=True)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_DOCKER_STATE": str(sf),
           "BACKUP_DIR": str(backups)}
    return env, sf


def _drill_sweep(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(OPS / "restore-drill-local.sh"), "sweep"], env=env, capture_output=True, text=True)


def _live_drill_process() -> subprocess.Popen:
    """A process whose command line names restore-drill-local.sh, like a running drill's shell."""
    return subprocess.Popen(["sh", "-c", "sleep 60; :", "restore-drill-local.sh"])   # "; :" stops sh exec-ing sleep


def _record(env: dict, token: str, pid: int) -> Path:
    pending = Path(env["BACKUP_DIR"]) / ".drill-pending"
    pending.mkdir(exist_ok=True)
    (pending / token).write_text(f"{pid}\n")
    return pending / token


def test_drill_sweep_removes_dead_runs_and_keeps_a_live_one(tmp_path):
    env, sf = _drill_env(tmp_path, {
        "containers": {"c-dead": "drillDEAD01"},
        "volumes": {"v-dead": "drillDEAD01", "v-norecord": "drillOLD001", "v-live": "drillLIVE01"},
    })
    live = _live_drill_process()
    try:
        live_rec = _record(env, "drillLIVE01", live.pid)
        dead_rec = _record(env, "drillDEAD01", 999999)             # no such process
        (Path(env["BACKUP_DIR"]) / ".work" / "drill.DEAD01").mkdir()   # a fresh work dir proves nothing
        r = _drill_sweep(env)
        assert r.returncode == 0, r.stdout
        st = json.loads(sf.read_text())
        assert st["containers"] == {} and list(st["volumes"]) == ["v-live"]
        assert live_rec.exists() and not dead_rec.exists()
    finally:
        live.kill()
        live.wait()


def test_drill_sweep_fails_when_a_leftover_stays(tmp_path):
    env, _ = _drill_env(tmp_path, {"containers": {}, "volumes": {"v-dead": "drillDEAD01"}, "busy": ["v-dead"]})
    r = _drill_sweep(env)
    assert r.returncode == 1 and "FAIL" in r.stdout


def test_drill_sweep_skips_quietly_without_docker_when_no_drill_is_on_record(tmp_path):
    env, _ = _drill_env(tmp_path, {"running": False, "containers": {}, "volumes": {}})
    r = _drill_sweep(env)
    assert r.returncode == 0 and "skipped" in r.stdout


def test_drill_sweep_fails_without_docker_while_a_killed_drill_is_on_record(tmp_path):
    env, sf = _drill_env(tmp_path, {"running": False, "containers": {}, "volumes": {"v-dead": "drillDEAD01"}})
    rec = _record(env, "drillDEAD01", 999999)
    (Path(env["BACKUP_DIR"]) / ".work" / "drill.DEAD01").mkdir()   # fresh dir, but the process is gone
    r = _drill_sweep(env)
    assert r.returncode == 1 and "drillDEAD01" in r.stdout and "FAIL" in r.stdout
    # Docker back: the leftover is removed and only then the record goes.
    st = json.loads(sf.read_text())
    st["running"] = True
    sf.write_text(json.dumps(st))
    r = _drill_sweep(env)
    assert r.returncode == 0, r.stdout
    assert json.loads(sf.read_text())["volumes"] == {} and not rec.exists()


def test_drill_record_with_a_reused_pid_is_dead(tmp_path):
    env, _ = _drill_env(tmp_path, {"running": False, "containers": {}, "volumes": {}})
    other = subprocess.Popen(["sleep", "60"])                     # alive, but not a drill
    try:
        _record(env, "drillREUSE1", other.pid)
        r = _drill_sweep(env)
        assert r.returncode == 1 and "drillREUSE1" in r.stdout
    finally:
        other.kill()
        other.wait()


def test_drill_record_of_a_running_drill_is_kept(tmp_path):
    env, _ = _drill_env(tmp_path, {"running": False, "containers": {}, "volumes": {}})
    live = _live_drill_process()
    try:
        rec = _record(env, "drillLIVE01", live.pid)
        assert _drill_sweep(env).returncode == 0              # a running drill is not a leftover
        assert rec.exists()
    finally:
        live.kill()
        live.wait()


def test_export_sweep_is_refused_through_the_ssh_key(tmp_path):
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": "sweep", "STATE_DIR": str(tmp_path / "s"),
           "LOCK_FILE": str(tmp_path / "lock")}
    r = subprocess.run(["sh", str(OPS / "backup-export.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "local only" in r.stderr
    assert not (tmp_path / "s").exists()                     # refused before touching anything
