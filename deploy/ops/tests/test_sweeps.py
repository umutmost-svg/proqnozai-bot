"""Offline tests of the `sweep` modes: restore-drill-local.sh (fake docker on PATH) and backup-export.sh.
Run: pytest deploy/ops/tests/"""
from __future__ import annotations

import json
import os
import subprocess
import time
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


def test_drill_sweep_removes_dead_runs_and_keeps_a_live_one(tmp_path):
    env, sf = _drill_env(tmp_path, {
        "containers": {"c-dead": "drillDEAD01"},
        "volumes": {"v-dead": "drillDEAD01", "v-stale": "drillOLD001", "v-live": "drillLIVE01"},
    })
    work = Path(env["BACKUP_DIR"]) / ".work"
    (work / "drill.LIVE01").mkdir()
    stale = work / "drill.OLD001"
    stale.mkdir()
    past = time.time() - 7 * 3600
    os.utime(stale, (past, past))
    r = _drill_sweep(env)
    assert r.returncode == 0, r.stdout
    st = json.loads(sf.read_text())
    assert st["containers"] == {} and list(st["volumes"]) == ["v-live"]


def test_drill_sweep_fails_when_a_leftover_stays(tmp_path):
    env, _ = _drill_env(tmp_path, {"containers": {}, "volumes": {"v-dead": "drillDEAD01"}, "busy": ["v-dead"]})
    r = _drill_sweep(env)
    assert r.returncode == 1 and "FAIL" in r.stdout


def test_drill_sweep_skips_quietly_without_docker(tmp_path):
    env, _ = _drill_env(tmp_path, {"running": False, "containers": {}, "volumes": {}})
    r = _drill_sweep(env)
    assert r.returncode == 0 and "skipped" in r.stdout


def test_export_sweep_is_refused_through_the_ssh_key(tmp_path):
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": "sweep", "STATE_DIR": str(tmp_path / "s"),
           "LOCK_FILE": str(tmp_path / "lock")}
    r = subprocess.run(["sh", str(OPS / "backup-export.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "local only" in r.stderr
    assert not (tmp_path / "s").exists()                     # refused before touching anything
