"""Offline tests of deploy/ops/pull_backups.py: a fake `ssh` replays a tar stream shaped like `docker cp ... -`;
real openssl (skipped when absent); passphrase and healthchecks are stubbed. Run: pytest deploy/ops/tests/"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pull_backups", HERE.parent / "pull_backups.py")
pb = importlib.util.module_from_spec(spec)
sys.modules["pull_backups"] = pb
spec.loader.exec_module(pb)

OPENSSL = shutil.which("openssl")
pytestmark = pytest.mark.skipif(OPENSSL is None, reason="openssl CLI not available")
STAMP = "20261112T030000Z"


def make_db(path: Path, tables: list[str]) -> None:
    con = sqlite3.connect(path)
    for t in tables:
        con.execute(f'CREATE TABLE "{t}" (id INTEGER PRIMARY KEY, v TEXT)')
        con.execute(f'INSERT INTO "{t}" (v) VALUES (?), (?)', ("a", "b"))
    con.commit()
    con.close()


def tar_of(entries: list[tuple[str, Path | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, src in entries:
            if src is None:
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                tar.addfile(info)
            else:
                tar.add(src, arcname=name)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    fx = tmp_path / "fx"
    fx.mkdir()
    make_db(fx / "bot.db", ["users", "requests"])
    make_db(fx / "miniapp.db", ["users", "sessions"])
    make_db(fx / "miniapp.forecasts.db", ["ai_forecasts"])
    (fx / "bot.tar").write_bytes(tar_of([(f"bot-backup-{STAMP}.db", fx / "bot.db")]))
    (fx / "miniapp.tar").write_bytes(tar_of([
        (STAMP, None), (f"{STAMP}/miniapp.db", fx / "miniapp.db"), (f"{STAMP}/miniapp.forecasts.db", fx / "miniapp.forecasts.db"),
    ]))
    fake_ssh = tmp_path / "fake-ssh"
    # The last argument is the requested kind, as with the forced-command key.
    fake_ssh.write_text(
        "#!/bin/sh\nfor a; do kind=$a; done\n"
        f'[ -f "{fx}/fail" ] && {{ echo "export: FAIL: no single healthy container" >&2; exit 1; }}\n'
        f'cat "{fx}/$kind.tar"\n'
    )
    fake_ssh.chmod(0o700)
    pings: list[tuple[str, bool, str]] = []
    monkeypatch.setattr(pb, "passphrase", lambda cfg: b"x" * 32)
    monkeypatch.setattr(pb, "DRILL_SCRIPT", tmp_path / "no-drill.sh")   # never the real Docker of this machine
    monkeypatch.setattr(pb, "hc_ping", lambda cfg, kind, ok, body: pings.append((kind, ok, body)) or True)
    cfg = pb.load_config({
        "PROQNOZAI_BACKUP_CONFIG": str(tmp_path / "none.env"), "SSH_BIN": str(fake_ssh), "OPENSSL": OPENSSL,
        "BACKUP_DIR": str(tmp_path / "backups"), "HC_DIR": str(tmp_path / "hc"),
    })
    return cfg, fx, pings


def plaintext_dbs(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and p.read_bytes()[:16] == b"SQLite format 3\x00"]


@pytest.mark.parametrize("kind", ["bot", "miniapp"])
def test_pull_writes_only_verified_ciphertext(env, kind):
    cfg, _, pings = env
    assert pb.cmd_pull(cfg, [kind]) == 0
    base = cfg.backup_dir / kind
    archives = list(base.glob(f"{kind}-*.tar.enc"))
    assert len(archives) == 1
    archive = archives[0]
    man = json.loads(archive.with_name(archive.name.replace(".tar.enc", ".manifest.json")).read_text())
    assert man["ciphertext_sha256"] == pb.sha256_file(archive)
    assert all(info["integrity"] == "ok" for info in man["files"].values())
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert plaintext_dbs(cfg.backup_dir) == []          # nothing decrypted is left behind
    assert not list(base.glob(".*partial"))
    assert pings == [(kind, True, pings[0][2])]
    assert pb.cmd_verify(cfg, archive) == 0
    assert plaintext_dbs(cfg.backup_dir) == []


def test_server_failure_keeps_nothing_and_alerts(env):
    cfg, fx, pings = env
    (fx / "fail").write_text("1")
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    assert list((cfg.backup_dir / "bot").iterdir()) == []
    kind, ok, body = pings[0]
    assert (kind, ok) == ("bot", False) and "no single healthy container" in body


def test_database_without_expected_tables_is_rejected(env):
    cfg, fx, pings = env
    other = fx / "other.db"
    make_db(other, ["something_else"])
    (fx / "bot.tar").write_bytes(tar_of([(f"bot-backup-{STAMP}.db", other)]))
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    assert list((cfg.backup_dir / "bot").iterdir()) == []
    assert "missing tables" in pings[0][2]


def test_corrupt_database_is_rejected(env):
    cfg, fx, pings = env
    bad = fx / "bad.db"
    data = bytearray((fx / "bot.db").read_bytes())
    data[4096:4096 + 12] = b"\xff" * 12                     # garbage page header of the first table page
    bad.write_bytes(bytes(data))
    (fx / "bot.tar").write_bytes(tar_of([(f"bot-backup-{STAMP}.db", bad)]))
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    assert list((cfg.backup_dir / "bot").iterdir()) == []
    assert pings[0][1] is False


def test_unexpected_tar_entries_are_rejected(env):
    cfg, fx, pings = env
    (fx / "miniapp.tar").write_bytes(tar_of([(f"{STAMP}/miniapp.db", fx / "miniapp.db"), ("../evil.db", fx / "bot.db")]))
    assert pb.cmd_pull(cfg, ["miniapp"]) == 1
    assert "unexpected entry" in pings[0][2]
    assert not (cfg.backup_dir / "evil.db").exists() and not (cfg.backup_dir.parent / "evil.db").exists()


def test_tampered_archive_fails_verify(env):
    cfg, _, _ = env
    assert pb.cmd_pull(cfg, ["bot"]) == 0
    archive = next((cfg.backup_dir / "bot").glob("*.tar.enc"))
    data = bytearray(archive.read_bytes())
    data[-40] ^= 0x01
    archive.write_bytes(bytes(data))
    assert pb.cmd_verify(cfg, archive) == 1


def test_extract_gives_databases_and_cleans_up_on_failure(env, tmp_path):
    cfg, _, _ = env
    assert pb.cmd_pull(cfg, ["miniapp"]) == 0
    archive = next((cfg.backup_dir / "miniapp").glob("*.tar.enc"))
    dest = cfg.backup_dir / ".work" / "drill"
    assert pb.cmd_extract(cfg, archive, dest) == 0
    assert sorted(p.name for p in (dest / "x" / STAMP).iterdir()) == ["miniapp.db", "miniapp.forecasts.db"]
    archive.write_bytes(archive.read_bytes()[:-64])          # truncated archive
    dest2 = cfg.backup_dir / ".work" / "drill2"
    assert pb.cmd_extract(cfg, archive, dest2) == 1
    assert not dest2.exists()


def test_retention_by_name_keeps_newest_and_ignores_strangers(tmp_path):
    d = tmp_path
    stamps = ["20260101T000000Z", "20260201T000000Z", "20261001T000000Z", "20261110T000000Z", "20261111T000000Z"]
    for s in stamps:
        (d / f"bot-{s}.tar.enc").write_text("x")
        (d / f"bot-{s}.manifest.json").write_text("{}")
    (d / "miniapp-20250101T000000Z.tar.enc").write_text("x")   # other kind: untouched
    (d / "notes.txt").write_text("x")
    now = datetime(2026, 11, 12, tzinfo=UTC)
    assert pb.retention(d, "bot", keep_days=30, min_keep=2, now=now) == 3
    left = sorted(p.name for p in d.iterdir())
    assert "bot-20261111T000000Z.tar.enc" in left and "bot-20261110T000000Z.manifest.json" in left
    assert "bot-20261001T000000Z.tar.enc" not in left
    assert "miniapp-20250101T000000Z.tar.enc" in left and "notes.txt" in left
    # A long gap: everything is old, the newest min_keep still stay.
    assert pb.retention(d, "bot", keep_days=1, min_keep=2, now=datetime(2027, 6, 1, tzinfo=UTC)) == 0


def test_mirror_requires_marker_and_copies_ciphertext(env, tmp_path):
    cfg, _, pings = env
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    cfg.values["MIRROR_DIR"] = str(mirror)
    assert pb.cmd_pull(cfg, ["bot"]) == 1                    # no marker: alert ...
    assert "marker" in pings[-1][2]
    local = list((cfg.backup_dir / "bot").glob("*.tar.enc"))
    assert len(local) == 1                                   # ... but the verified local archive is kept
    for p in (cfg.backup_dir / "bot").iterdir():             # a second run within the same second would collide
        p.unlink()
    (mirror / pb.MIRROR_MARKER).write_text("")
    assert pb.cmd_pull(cfg, ["bot"]) == 0
    archives = list((mirror / "bot").glob("*.tar.enc"))
    assert archives and plaintext_dbs(mirror) == []
    src = cfg.backup_dir / "bot" / archives[0].name
    assert pb.sha256_file(src) == pb.sha256_file(archives[0])


def test_config_rejects_unknown_keys_and_bad_numbers(tmp_path):
    f = tmp_path / "c.env"
    f.write_text("# comment\nKEEP_DAYS=10\n")
    assert pb.load_config({"PROQNOZAI_BACKUP_CONFIG": str(f)})["KEEP_DAYS"] == "10"
    f.write_text("SSH_TARGETX=root@x\n")
    with pytest.raises(pb.BackupError):
        pb.load_config({"PROQNOZAI_BACKUP_CONFIG": str(f)})
    with pytest.raises(pb.BackupError):
        pb.load_config({"PROQNOZAI_BACKUP_CONFIG": str(tmp_path / "none"), "KEEP_DAYS": "0"})


def test_passphrase_never_in_argv(env, monkeypatch):
    cfg, _, _ = env
    seen: list[list[str]] = []
    real = pb.subprocess.Popen

    def spy(args, *a, **kw):
        seen.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(pb.subprocess, "Popen", spy)
    assert pb.cmd_pull(cfg, ["bot"]) == 0
    assert seen and all("x" * 32 not in " ".join(a) for a in seen)
    assert all(os.environ.get(k, "") != "x" * 32 for k in os.environ)


def test_sweep_removes_only_stale_leftovers_of_killed_runs(env):
    cfg, _, _ = env
    work = cfg.backup_dir / ".work"
    old_verify, old_drill, fresh, kind_dir = (work / "verify-abc", work / "drill.XYZ", work / "verify-new", work / "bot")
    for d in (old_verify, old_drill, fresh, kind_dir):
        d.mkdir(parents=True)
        (d / "stream.tar").write_bytes(b"plaintext")
    past = pb.time.time() - pb.STALE_SECONDS - 60
    for d in (old_verify, old_drill, kind_dir):
        os.utime(d, (past, past))
    pb.sweep_stale(cfg)
    assert not old_verify.exists() and not old_drill.exists()
    assert fresh.exists()          # may belong to a drill running right now
    assert kind_dir.exists()       # emptied by the pull of that kind under its own lock


def test_killed_pull_leftovers_removed_even_when_keychain_fails(env, monkeypatch):
    cfg, _, pings = env
    work = cfg.backup_dir / ".work" / "bot"
    work.mkdir(parents=True)
    (work / "stream.tar").write_bytes(b"decrypted leftover")
    base = cfg.backup_dir / "bot"
    base.mkdir(parents=True, exist_ok=True)
    (base / ".bot-x.tar.enc.partial").write_bytes(b"partial")

    def locked(cfg):
        raise pb.BackupError("no usable passphrase in the Keychain")

    monkeypatch.setattr(pb, "passphrase", locked)
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    assert list(work.iterdir()) == [] and list(base.iterdir()) == []
    assert "Keychain" in pings[-1][2]


def test_verify_and_extract_require_a_matching_manifest(env, tmp_path):
    cfg, _, _ = env
    assert pb.cmd_pull(cfg, ["bot"]) == 0
    archive = next((cfg.backup_dir / "bot").glob("*.tar.enc"))
    man = archive.with_name(archive.name.replace(".tar.enc", ".manifest.json"))
    data = json.loads(man.read_text())
    name = next(iter(data["files"]))
    data["files"][name]["tables"]["users"] += 1          # a wrong row count in an otherwise valid manifest
    man.write_text(json.dumps(data))
    assert pb.cmd_verify(cfg, archive) == 1
    d1 = cfg.backup_dir / ".work" / "d1"
    assert pb.cmd_extract(cfg, archive, d1) == 1 and not d1.exists()
    man.unlink()                                        # no manifest at all
    assert pb.cmd_verify(cfg, archive) == 1


def test_interrupted_extract_leaves_no_plaintext(env, tmp_path, monkeypatch):
    cfg, _, _ = env
    assert pb.cmd_pull(cfg, ["bot"]) == 0
    archive = next((cfg.backup_dir / "bot").glob("*.tar.enc"))
    real = pb.check_db

    def interrupted(path, required):
        real(path, required)
        raise SystemExit(143)                           # what the SIGTERM handler raises mid-way

    monkeypatch.setattr(pb, "check_db", interrupted)
    dest = cfg.backup_dir / ".work" / "d"
    with pytest.raises(SystemExit):
        pb.cmd_extract(cfg, archive, dest)
    assert not dest.exists()


def test_decrypt_timeout_kills_openssl(env, monkeypatch, tmp_path):
    cfg, _, _ = env

    class Hung:
        returncode = None
        killed = False

        def communicate(self, timeout=None):
            raise pb.subprocess.TimeoutExpired("openssl", timeout)

        def poll(self):
            return None if not self.killed else -9

        def kill(self):
            self.killed = True

        def wait(self):
            return -9

    hung = Hung()
    monkeypatch.setattr(pb, "_openssl", lambda *a, **k: hung)
    with pytest.raises(pb.subprocess.TimeoutExpired):
        pb.decrypt_and_check(cfg, b"x" * 32, "bot", tmp_path / "a.tar.enc", tmp_path)
    assert hung.killed                                   # no child keeps writing plaintext after cleanup


def test_verify_without_manifest_still_sweeps_stale_plaintext(env, tmp_path):
    cfg, _, _ = env
    old = cfg.backup_dir / ".work" / "verify-old"
    old.mkdir(parents=True)
    (old / "stream.tar").write_bytes(b"plaintext")
    past = pb.time.time() - pb.STALE_SECONDS - 60
    os.utime(old, (past, past))
    bogus = cfg.backup_dir / "bot" / f"bot-{STAMP}.tar.enc"
    bogus.parent.mkdir(parents=True, exist_ok=True)
    bogus.write_bytes(b"x")
    assert pb.cmd_verify(cfg, bogus) == 1
    assert not old.exists()


def test_drill_leftovers_fail_every_kind_check(env, monkeypatch, tmp_path):
    cfg, _, pings = env
    script = tmp_path / "drill.sh"
    script.write_text('[ "$1" = sweep ] && [ -n "$BACKUP_DIR" ] || exit 9\necho "cannot remove orphan drill volume v1"\nexit 1\n')
    monkeypatch.setattr(pb, "DRILL_SCRIPT", script)
    assert pb.cmd_pull(cfg, ["bot", "miniapp"]) == 1
    assert [(k, ok) for k, ok, _ in pings] == [("bot", False), ("miniapp", False)]
    assert all("cannot remove orphan drill volume v1" in body for _, _, body in pings)
    assert len(list((cfg.backup_dir / "bot").glob("*.tar.enc"))) == 1     # the backup itself still succeeded


def test_successful_drill_sweep_keeps_checks_green(env, monkeypatch, tmp_path):
    cfg, _, pings = env
    script = tmp_path / "drill.sh"
    script.write_text('echo "removed orphan drill volume v1"\n')
    monkeypatch.setattr(pb, "DRILL_SCRIPT", script)
    assert pb.cmd_pull(cfg, ["bot"]) == 0 and pings[-1][1] is True


def test_encrypt_failure_mid_stream_does_not_wait_for_the_watchdog(env, monkeypatch):
    cfg, fx, pings = env
    big = fx / "bot.tar"
    big.write_bytes(big.read_bytes() + os.urandom(8 << 20))          # more than a pipe buffer
    real = pb._openssl

    def dying(cfg_, pw, args, **kw):
        if "-salt" in args:
            args = ["-salt", "-out", "/nonexistent-dir/x"]               # openssl fails at once
        return real(cfg_, pw, args, **kw)

    monkeypatch.setattr(pb, "_openssl", dying)
    cfg.values["SSH_TIMEOUT"] = "60"
    t0 = pb.time.time()
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    assert pb.time.time() - t0 < 30
    assert "encrypt failed" in pings[-1][2]


def test_stale_plaintext_that_cannot_be_removed_fails_pull_and_verify(env):
    cfg, _, pings = env
    stuck = cfg.backup_dir / ".work" / "verify-old"
    stuck.mkdir(parents=True)
    (stuck / "stream.tar").write_bytes(b"decrypted leftover")
    past = pb.time.time() - pb.STALE_SECONDS - 60
    os.utime(stuck, (past, past))
    stuck.chmod(0o500)                                   # its file cannot be deleted
    try:
        assert pb.cmd_pull(cfg, ["bot"]) == 1
        kind, ok, body = pings[-1]
        assert (kind, ok) == ("bot", False) and "stale decrypted data not removed" in body
        archive = next((cfg.backup_dir / "bot").glob("*.tar.enc"))
        assert pb.cmd_verify(cfg, archive) == 1
    finally:
        stuck.chmod(0o700)
    assert pb.cmd_verify(cfg, archive) == 0              # removable again: the next run cleans and passes
    assert not stuck.exists()


def test_backup_is_not_ok_without_a_delivered_ping(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.setattr(pb, "hc_ping", lambda cfg, kind, ok, body: False)   # no URL file / network down
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    assert len(list((cfg.backup_dir / "bot").glob("*.tar.enc"))) == 1      # the archive itself is kept


def test_failed_mirror_copy_leaves_no_partial_and_no_orphan_archive(env, tmp_path, monkeypatch):
    cfg, _, pings = env
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / pb.MIRROR_MARKER).write_text("")
    cfg.values["MIRROR_DIR"] = str(mirror)
    real = pb.shutil.copyfile
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        real(src, dst)
        if calls["n"] == 2:                            # the archive copy breaks after writing
            raise OSError("disk full")

    monkeypatch.setattr(pb.shutil, "copyfile", flaky)
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    assert list((mirror / "bot").iterdir()) == []       # no .partial, no manifest without its archive


def test_extract_refuses_a_destination_outside_the_swept_work_dir(env, tmp_path):
    cfg, _, _ = env
    assert pb.cmd_pull(cfg, ["bot"]) == 0
    archive = next((cfg.backup_dir / "bot").glob("*.tar.enc"))
    outside = tmp_path / "restore"
    assert pb.cmd_extract(cfg, archive, outside) == 1 and not outside.exists()
    inside = cfg.backup_dir / ".work" / "restore-x"
    assert pb.cmd_extract(cfg, archive, inside) == 0 and (inside / "x").is_dir()


def test_ok_line_only_after_a_delivered_ping(env, monkeypatch, capsys):
    cfg, _, _ = env
    monkeypatch.setattr(pb, "hc_ping", lambda cfg, kind, ok, body: False)
    assert pb.cmd_pull(cfg, ["bot"]) == 1
    out = capsys.readouterr().out
    assert " OK:" not in out and "archive made" in out


def test_mirror_drops_a_manifest_orphaned_by_a_killed_publish(env, tmp_path):
    cfg, _, _ = env
    mirror = tmp_path / "mirror"
    (mirror / "bot").mkdir(parents=True)
    (mirror / pb.MIRROR_MARKER).write_text("")
    orphan = mirror / "bot" / "bot-20260101T000000Z.manifest.json"   # its archive never got renamed in
    orphan.write_text("{}")
    cfg.values["MIRROR_DIR"] = str(mirror)
    assert pb.cmd_pull(cfg, ["bot"]) == 0
    assert not orphan.exists()
    assert len(list((mirror / "bot").glob("*.tar.enc"))) == 1 == len(list((mirror / "bot").glob("*.manifest.json")))
