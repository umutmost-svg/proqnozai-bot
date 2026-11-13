"""Offline tests of deploy/ops/docker-user-3000.sh against a stateful fake iptables/ip6tables on PATH.
Run: pytest deploy/ops/tests/"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker-user-3000.sh"
TAG = "proqnozai-block-3000"

# Rules are kept as their argument strings; -S prints them the way iptables does ("-A CHAIN <args>").
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
state_file = os.path.join(os.environ["FAKE_IPT_DIR"], os.path.basename(sys.argv[0]) + ".json")
st = json.load(open(state_file)) if os.path.exists(state_file) else {"FORWARD": ["-j DOCKER-USER"]}
a = [x for x in sys.argv[1:] if x != "-n"]
op, chain, rest = a[0], a[1], a[2:]
def save():
    json.dump(st, open(state_file, "w"))
if op == "-L":
    sys.exit(0 if chain in st else 1)
if op == "-N":
    st.setdefault(chain, []); save(); sys.exit(0)
if chain not in st:
    sys.exit(1)
rules = st[chain]
if op == "-S":
    print(f"-P {chain} ACCEPT" if chain == "FORWARD" else f"-N {chain}")
    for r in rules:
        print(f"-A {chain} {r}")
elif op == "-C":
    sys.exit(0 if " ".join(rest) in rules else 1)
elif op == "-I":
    rules.insert(int(rest[0]) - 1, " ".join(rest[1:])); save()
elif op == "-A":
    rules.append(" ".join(rest)); save()
elif op == "-D":
    if len(rest) == 1 and rest[0].isdigit():
        del rules[int(rest[0]) - 1]
    else:
        rules.remove(" ".join(rest))
    save()
'''


@pytest.fixture
def ipt(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("iptables", "ip6tables"):
        p = bindir / name
        p.write_text(FAKE)
        p.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_IPT_DIR": str(tmp_path)}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True)

    def rules(name: str = "iptables") -> list[str]:
        return json.loads((tmp_path / f"{name}.json").read_text())["DOCKER-USER"]

    def insert_top(rule: str) -> None:
        for name in ("iptables", "ip6tables"):
            subprocess.run([str(bindir / name), "-I", "DOCKER-USER", "1", *rule.split()], env=env, check=True)

    return run, rules, insert_top


def test_apply_puts_drops_first_and_is_idempotent(ipt):
    run, rules, _ = ipt
    assert run("status").returncode == 1
    assert run("apply").returncode == 0
    assert run("status").returncode == 0
    first = rules()
    assert len(first) == 2 and all(TAG in r and r.endswith("-j DROP") for r in first)
    assert run("apply").returncode == 0
    assert rules() == first                                   # no duplicates on a second apply


def test_rule_inserted_above_the_drops_is_reported_and_repaired(ipt):
    run, rules, insert_top = ipt
    assert run("apply").returncode == 0
    insert_top("-i eth0 -p tcp -j ACCEPT")                    # would let :3000 through before the DROPs
    st = run("status")
    assert st.returncode == 1 and "not the first rules" in st.stdout
    assert run("apply").returncode == 0
    after = rules()
    assert all(TAG in r for r in after[:2]) and after[2] == "-i eth0 -p tcp -j ACCEPT"
    assert sum(TAG in r for r in after) == 2                  # the lower copies were removed
    assert run("status").returncode == 0


def test_remove_deletes_every_copy(ipt):
    run, rules, _ = ipt
    assert run("apply").returncode == 0
    assert run("remove").returncode == 0
    assert rules() == [] and rules("ip6tables") == []
    assert run("status").returncode == 1
