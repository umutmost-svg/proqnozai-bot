"""Offline tests of deploy/ops/offsite_backup.py: a fake `rclone` maps "<remote>:<path>" to a local directory
(lsf -R --files-only, copyto [--immutable]); real openssl (skipped when absent); passphrase and healthchecks are
stubbed. Run: pytest deploy/ops/tests/"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("offsite_backup", HERE.parent / "offsite_backup.py")
ob = importlib.util.module_from_spec(spec)
sys.modules["offsite_backup"] = ob
spec.loader.exec_module(ob)
pb = ob.pb

OPENSSL = shutil.which("openssl")
pytestmark = pytest.mark.skipif(OPENSSL is None, reason="openssl CLI not available")
PW = b"p" * 32
FAKE_RCLONE = """#!{py}
import os, shutil, sys
root = {root!r}
args = [a for a in sys.argv[1:] if not a.startswith("-") and a not in ("3", "10", "30s", "5m")]
def loc(s):
    if ":" in s and not s.startswith("/"):
        return os.path.join(root, s.split(":", 1)[1]), True
    return s, False
if os.path.exists(os.path.join(root, ".down")):
    print("Failed to create file system: couldn't connect", file=sys.stderr); sys.exit(1)
cmd = args[0]
if cmd == "lsf":
    base, _ = loc(args[1])
    if not os.path.isdir(base):
        print("error listing: directory not found", file=sys.stderr); sys.exit(3)
    for d, _, fs in os.walk(base):
        for f in fs:
            print(os.path.relpath(os.path.join(d, f), base))
elif cmd == "copyto":
    src, _ = loc(args[1]); dst, remote_dst = loc(args[2])
    if not os.path.exists(src):
        print("error: object not found", file=sys.stderr); sys.exit(3)
    if "--immutable" in sys.argv and os.path.exists(dst):
        if open(src, "rb").read() != open(dst, "rb").read():
            print("ERROR : immutable file modified", file=sys.stderr); sys.exit(1)
        sys.exit(0)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    with open(os.path.join(root, ".log"), "a") as log:
        log.write(("up " if remote_dst else "down ") + os.path.basename(dst) + "\\n")
else:
    sys.exit(2)
"""


def make_db(path: Path, tables: list[str]) -> None:
    con = sqlite3.connect(path)
    for t in tables:
        con.execute(f'CREATE TABLE "{t}" (id INTEGER PRIMARY KEY, v TEXT)')
        con.execute(f'INSERT INTO "{t}" (v) VALUES (?), (?)', ("a", "b"))
    con.commit()
    con.close()


def encrypt(src: Path, dst: Path) -> None:
    subprocess.run([OPENSSL, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "300000", "-md", "sha256", "-salt",
                    "-in", str(src), "-out", str(dst), "-pass", f"pass:{PW.decode()}"], check=True)


def manual_archive(d: Path, stamp: str) -> Path:
    db = d / "plain.db"
    make_db(db, ["users", "requests"])
    enc = d / f"bot-backup-{stamp}.db.enc"
    encrypt(db, enc)
    (d / f"bot-backup-{stamp}.db.manifest").write_text(
        f"{pb.sha256_file(enc)}  {enc.name}\nplaintext_sha256 {pb.sha256_file(db)}\n"
        "cipher aes-256-cbc pbkdf2 iter=300000; key: macOS Keychain service=proqnozai-bot-backup account=proqnozai\n")
    db.unlink()
    return enc


def auto_archive(cfg: pb.Config, kind: str, stamp: str, fx: Path) -> Path:
    """Made the way pull_backups.py makes them: through pull_one with a fake ssh."""
    tar = fx / f"{kind}.tar"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        if kind == "bot":
            make_db(fx / "bot.db", ["users"])
            t.add(fx / "bot.db", arcname=f"bot-backup-{stamp}.db")
        else:
            make_db(fx / "miniapp.db", ["users", "sessions"])
            make_db(fx / "miniapp.forecasts.db", ["ai_forecasts"])
            t.add(fx / "miniapp.db", arcname=f"{stamp}/miniapp.db")
            t.add(fx / "miniapp.forecasts.db", arcname=f"{stamp}/miniapp.forecasts.db")
    tar.write_bytes(buf.getvalue())
    for p in fx.glob("*.db"):
        p.unlink()
    ssh = fx / "fake-ssh"
    ssh.write_text(f"#!/bin/sh\ncat '{tar}'\n")
    ssh.chmod(0o700)
    c = pb.Config({**cfg.values, "SSH_BIN": str(ssh)})
    pb.pull_one(c, kind, PW, now=datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC))
    return cfg.backup_dir / kind / f"{kind}-{stamp}.tar.enc"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "bucket-root"
    (root / "bucket").mkdir(parents=True)
    fake = tmp_path / "rclone"
    fake.write_text(FAKE_RCLONE.format(py=sys.executable, root=str(root)))
    fake.chmod(0o700)
    keys: list[str] = []
    pings: list[tuple[str, bool, str]] = []
    monkeypatch.setattr(pb, "passphrase", lambda c: keys.append(c["KEYCHAIN_SERVICE"]) or PW)
    monkeypatch.setattr(pb, "DRILL_SCRIPT", tmp_path / "no-drill.sh")
    monkeypatch.setattr(pb, "hc_ping", lambda c, kind, ok, body: pings.append((kind, ok, body)) or True)
    cfg = pb.load_config({"PROQNOZAI_BACKUP_CONFIG": str(tmp_path / "none.env"), "OPENSSL": OPENSSL,
                          "BACKUP_DIR": str(tmp_path / "auto"), "HC_DIR": str(tmp_path / "hc")})
    manual = tmp_path / "manual"
    manual.mkdir()
    ocfg = ob.load_offsite({"PROQNOZAI_OFFSITE_CONFIG": str(tmp_path / "none.env"), "RCLONE_BIN": str(fake),
                            "REMOTE": "b2:bucket", "RCLONE_CONFIG_KEYCHAIN": "", "MANUAL_DIR": str(manual),
                            "LOCK_WAIT": "0", "MAX_AGE_HOURS": "100000"})
    fx = tmp_path / "fx"
    fx.mkdir()
    return cfg, ocfg, manual, root / "bucket", fx, keys, pings


def no_plaintext(root: Path) -> bool:
    return not [p for p in root.rglob("*") if p.is_file() and p.read_bytes()[:16] == b"SQLite format 3\x00"]


def test_sync_uploads_verifies_and_pings(env, tmp_path):
    cfg, ocfg, manual, bucket, fx, keys, pings = env
    auto_archive(cfg, "bot", "20261113T054000Z", fx)
    auto_archive(cfg, "miniapp", "20261113T054000Z", fx)
    manual_archive(manual, "20261113T080743Z")
    assert ob.cmd_sync(cfg, ocfg) == 0
    stored = sorted(str(p.relative_to(bucket)) for p in bucket.rglob("*") if p.is_file())
    assert stored == ["auto/bot/bot-20261113T054000Z.manifest.json", "auto/bot/bot-20261113T054000Z.tar.enc",
                      "auto/miniapp/miniapp-20261113T054000Z.manifest.json",
                      "auto/miniapp/miniapp-20261113T054000Z.tar.enc",
                      "manual/bot-backup-20261113T080743Z.db.enc", "manual/bot-backup-20261113T080743Z.db.manifest"]
    assert no_plaintext(bucket) and no_plaintext(cfg.backup_dir) and no_plaintext(manual)
    # each archive decrypted with the Keychain item its manifest names
    assert "proqnozai-bot-backup" in keys and "proqnozai-auto-backup" in keys
    assert pings[-1][:2] == ("offsite", True) and "3 uploaded and verified" in pings[-1][2]
    # second run: nothing uploaded; only the newest archive of each kind is downloaded and proven again
    log = (bucket.parent / ".log").read_text()
    assert ob.cmd_sync(cfg, ocfg) == 0
    again = (bucket.parent / ".log").read_text()[len(log):].split()
    assert "up" not in again
    assert sorted(again[1::2]) == ["bot-20261113T054000Z.manifest.json", "bot-20261113T054000Z.tar.enc",
                                   "miniapp-20261113T054000Z.manifest.json", "miniapp-20261113T054000Z.tar.enc"]
    assert pings[-1][1] and "0 uploaded" in pings[-1][2] and "re-verified" in pings[-1][2]
    assert no_plaintext(cfg.backup_dir)


def test_corrupted_remote_copy_fails_and_alerts(env):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    auto_archive(cfg, "bot", "20261113T054000Z", fx)
    auto_archive(cfg, "miniapp", "20261113T054000Z", fx)
    real_copy = ob.rclone

    def corrupting(o, e, args):     # the provider stores something else than was sent
        out = real_copy(o, e, args)
        if args[0] == "copyto" and args[-1].endswith("bot-20261113T054000Z.tar.enc") and ":" in args[-1]:
            p = bucket / "auto/bot/bot-20261113T054000Z.tar.enc"
            p.write_bytes(p.read_bytes()[:-16] + b"\0" * 16)
        return out
    ob.rclone = corrupting
    try:
        assert ob.cmd_sync(cfg, ocfg) == 1
    finally:
        ob.rclone = real_copy
    assert pings[-1][:2] == ("offsite", False) and "bot-20261113T054000Z.tar.enc" in pings[-1][2]
    assert "auto/bot/bot-20261113T054000Z.tar.enc" not in ob._load_state(cfg)
    # the bad copy is never replaced by this script: the alert stays until a human looks
    assert ob.cmd_sync(cfg, ocfg) == 1 and "ciphertext SHA-256" in pings[-1][2]


def test_remote_manifest_must_equal_local(env):
    cfg, ocfg, manual, bucket, fx, _, _ = env
    enc = manual_archive(manual, "20261113T080743Z")
    item = ob.item_for(enc)
    env_ = ob.rclone_env(ocfg)
    ob.upload(ocfg, env_, item)
    (bucket / "manual/bot-backup-20261113T080743Z.db.manifest").write_text("tampered\n")
    with pytest.raises(ob.OffsiteError, match="differs from the local"):
        ob.verify_remote(cfg, ocfg, env_, item.rel, local_manifest=item.manifest)


def test_manual_wrong_plaintext_hash_fails(env, capsys):
    cfg, ocfg, manual, bucket, fx, _, _ = env
    enc = manual_archive(manual, "20261113T080743Z")
    man = enc.with_name(ob.manifest_name(enc.name))
    lines = man.read_text().splitlines()
    lines[1] = "plaintext_sha256 " + "0" * 64
    man.write_text("\n".join(lines) + "\n")
    assert ob.cmd_push(cfg, ocfg, [str(enc)]) == 1
    assert "plaintext SHA-256" in capsys.readouterr().out
    assert no_plaintext(cfg.backup_dir)


def test_push_and_verify_commands(env, capsys):
    cfg, ocfg, manual, bucket, fx, _, _ = env
    enc = manual_archive(manual, "20261113T080743Z")
    assert ob.cmd_push(cfg, ocfg, [str(enc)]) == 0
    assert "PASS: b2:bucket/manual/bot-backup-20261113T080743Z.db.enc" in capsys.readouterr().out
    assert ob.cmd_verify(cfg, ocfg, "manual/bot-backup-20261113T080743Z.db.enc") == 0
    assert "integrity ok" in capsys.readouterr().out
    assert ob.cmd_verify(cfg, ocfg, "../etc/passwd") == 1


def test_missing_kind_or_unreachable_remote_is_a_failure(env):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    auto_archive(cfg, "bot", "20261113T054000Z", fx)
    assert ob.cmd_sync(cfg, ocfg) == 1                       # no miniapp archive offsite yet
    assert pings[-1][1] is False and "none younger than" in pings[-1][2]
    (bucket.parent / ".down").write_text("")
    assert ob.cmd_sync(cfg, ocfg) == 1
    assert pings[-1][1] is False and "couldn't connect" in pings[-1][2]


def test_archive_without_manifest_is_skipped_until_complete(env):
    cfg, ocfg, manual, bucket, fx, _, _ = env
    a = auto_archive(cfg, "bot", "20261113T054000Z", fx)
    man = a.with_name(ob.manifest_name(a.name))
    man.rename(man.with_suffix(".hold"))
    assert ob.local_items(cfg, ocfg) == ([], [])                  # a pull in flight: skipped, not reported
    os.utime(a, (1, 1))                                           # ... but one left like that for hours is
    assert ob.local_items(cfg, ocfg) == ([], [a.name])
    assert ob.cmd_sync(cfg, ocfg) == 1 and "archive without a manifest" in env[6][-1][2]
    man.with_suffix(".hold").rename(man)
    assert [i.rel for i in ob.local_items(cfg, ocfg)[0]] == ["auto/bot/bot-20261113T054000Z.tar.enc"]


def test_fetch_gives_ciphertext_for_the_drill(env, tmp_path):
    cfg, ocfg, manual, bucket, fx, _, _ = env
    a = auto_archive(cfg, "bot", "20261113T054000Z", fx)
    assert ob.cmd_push(cfg, ocfg, [str(a)]) == 0
    dest = tmp_path / "fetched"
    assert ob.cmd_fetch(ocfg, "auto/bot/bot-20261113T054000Z.tar.enc", dest) == 0
    assert sorted(p.name for p in dest.iterdir()) == ["bot-20261113T054000Z.manifest.json",
                                                      "bot-20261113T054000Z.tar.enc"]
    assert oct(dest.stat().st_mode & 0o777) == "0o700"
    # the fetched copy passes pull_backups' own verification (what restore-drill-local.sh runs first)
    assert pb.cmd_verify(cfg, dest / "bot-20261113T054000Z.tar.enc") == 0
    assert ob.cmd_fetch(ocfg, "auto/bot/bot-20261113T054000Z.tar.enc", dest) == 1   # never into an existing dir


def test_config_requires_remote(tmp_path):
    with pytest.raises(ob.OffsiteError, match="REMOTE"):
        ob.load_offsite({"PROQNOZAI_OFFSITE_CONFIG": str(tmp_path / "none.env")})
    (tmp_path / "bad.env").write_text("REMOTE=x:y\nSECRET=1\n")
    with pytest.raises(ob.OffsiteError, match="unknown"):
        ob.load_offsite({"PROQNOZAI_OFFSITE_CONFIG": str(tmp_path / "bad.env")})


def test_offsite_drill_script_fetches_newest_and_cleans_up(env, tmp_path):
    cfg, ocfg, manual, bucket, fx, _, _ = env
    for stamp in ("20261112T174000Z", "20261113T054000Z"):
        assert ob.cmd_push(cfg, ocfg, [str(auto_archive(cfg, "bot", stamp, fx))]) == 0
    stub = tmp_path / "drill-stub.sh"           # stands in for restore-drill-local.sh (needs Docker)
    stub.write_text('#!/bin/sh\n[ "$1" = bot ] && [ -f "$2" ] && echo "PASS: drilled $(basename "$2")"\n')
    run_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PY": sys.executable,
               "PROQNOZAI_BACKUP_CONFIG": str(tmp_path / "none.env"),
               "PROQNOZAI_OFFSITE_CONFIG": str(tmp_path / "none.env"), "BACKUP_DIR": str(cfg.backup_dir),
               "RCLONE_BIN": ocfg["RCLONE_BIN"], "REMOTE": ocfg["REMOTE"], "RCLONE_CONFIG_KEYCHAIN": "",
               "LOCAL_DRILL": str(stub)}
    r = subprocess.run(["/bin/sh", str(HERE.parent / "restore-drill-offsite.sh"), "bot"], env=run_env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS: drilled bot-20261113T054000Z.tar.enc" in r.stdout
    assert r.stdout.strip().splitlines()[-1].startswith("PASS: off-site auto/bot/bot-20261113T054000Z.tar.enc")
    assert not list((cfg.backup_dir / ".work").glob("offsite-fetch*"))
    stub.write_text("#!/bin/sh\necho 'FAIL: integrity'; exit 1\n")
    r = subprocess.run(["/bin/sh", str(HERE.parent / "restore-drill-offsite.sh"), "bot"], env=run_env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and r.stdout.strip().splitlines()[-1].startswith("FAIL:")
    assert not list((cfg.backup_dir / ".work").glob("offsite-fetch*"))


def test_stale_offsite_copies_alert(env):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    auto_archive(cfg, "bot", datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), fx)
    auto_archive(cfg, "miniapp", "20200101T054000Z", fx)       # pulls stopped long ago
    assert ob.cmd_sync(cfg, {**ocfg, "MAX_AGE_HOURS": "36"}) == 1
    assert pings[-1][1] is False and "['miniapp']" in pings[-1][2]


def test_lost_remote_manifest_is_rechecked(env):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    enc = manual_archive(manual, "20261113T080743Z")
    auto_archive(cfg, "bot", datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), fx)
    auto_archive(cfg, "miniapp", datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), fx)
    assert ob.cmd_sync(cfg, ocfg) == 0
    (bucket / "manual" / ob.manifest_name(enc.name)).unlink()     # the provider lost it
    assert ob.cmd_sync(cfg, ocfg) == 0                              # uploaded again and verified
    assert (bucket / "manual" / ob.manifest_name(enc.name)).is_file()
    assert "1 uploaded and verified" in pings[-1][2]


def test_remote_copy_changed_after_verification_turns_red(env):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    auto_archive(cfg, "bot", stamp, fx)
    auto_archive(cfg, "miniapp", stamp, fx)
    assert ob.cmd_sync(cfg, ocfg) == 0
    p = bucket / f"auto/miniapp/miniapp-{stamp}.tar.enc"
    p.write_bytes(p.read_bytes()[:-1] + bytes([p.read_bytes()[-1] ^ 1]))   # damaged at rest
    assert ob.cmd_sync(cfg, ocfg) == 1
    assert pings[-1][1] is False and "(re-check)" in pings[-1][2]
    assert no_plaintext(cfg.backup_dir)


@pytest.mark.parametrize("line", ["plaintext_sha256 ", "plaintext_sha256", "plaintext_sha256 zz"])
def test_malformed_manual_manifest_is_a_reported_failure(env, line):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    enc = manual_archive(manual, "20261113T080743Z")
    man = enc.with_name(ob.manifest_name(enc.name))
    lines = man.read_text().splitlines()
    lines[1] = line
    man.write_text("\n".join(lines) + "\n")
    assert ob.cmd_sync(cfg, ocfg) == 1
    assert pings[-1][1] is False and "plaintext_sha256" in pings[-1][2]


@pytest.mark.parametrize("body", ["[]", '"x"', "{}"])
def test_malformed_auto_manifest_is_a_reported_failure(env, body):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    a = auto_archive(cfg, "bot", datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), fx)
    a.with_name(ob.manifest_name(a.name)).write_text(body)
    assert ob.cmd_sync(cfg, ocfg) == 1
    assert pings[-1][1] is False and a.name in pings[-1][2]


def test_sync_waits_for_a_running_pull_and_takes_its_archive(env):
    import threading
    import time as _time
    cfg, ocfg, manual, bucket, fx, _, pings = env
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    auto_archive(cfg, "bot", stamp, fx)
    pull_lock = pb._lock(cfg, "miniapp")              # a miniapp pull is running when sync starts
    rc: list[int] = []
    t = threading.Thread(target=lambda: rc.append(ob.cmd_sync(cfg, {**ocfg, "LOCK_WAIT": "60"})))
    t.start()
    _time.sleep(1.5)
    name = auto_archive(cfg, "miniapp", stamp, fx).name   # ... and publishes its archive, then finishes
    pull_lock.close()
    t.join(120)
    assert rc == [0]
    assert "2 uploaded and verified" in pings[-1][2] and name in pings[-1][2]


def test_pull_stuck_longer_than_lock_wait_is_a_failure(env):
    cfg, ocfg, manual, bucket, fx, _, pings = env
    held = pb._lock(cfg, "bot")
    try:
        assert ob.cmd_sync(cfg, ocfg) == 1                 # LOCK_WAIT=0 in the fixture
    finally:
        held.close()
    assert pings[-1][1] is False and "pull is still running" in pings[-1][2]
