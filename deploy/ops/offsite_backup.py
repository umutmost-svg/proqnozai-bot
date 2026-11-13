"""Third copy of the backups, off the server and off the Mac (docs/ops/offsite-backup.md). Stdlib + rclone.

    python3 offsite_backup.py sync                  # launchd after each pull: upload + verify what is missing
    python3 offsite_backup.py push <file.enc> ...   # one archive now (e.g. a manual bot-backup-*.db.enc)
    python3 offsite_backup.py verify <remote path>  # download, check, decrypt, integrity_check: PASS/FAIL
    python3 offsite_backup.py fetch <remote path> <new dir>   # ciphertext + manifest only, for restore drills
    python3 offsite_backup.py list

Only ciphertext leaves the Mac: the archives are already encrypted by pull_backups.py (or by hand for the manual
copies) with a passphrase from the macOS Keychain; the storage provider never sees a key. An upload counts only
after a verification of the copy *as stored remotely*: downloaded into a private temp dir, ciphertext SHA-256 equal
to the manifest (and the remote manifest byte-equal to the local one), decrypted, plaintext checked against the
manifest, PRAGMA integrity_check and the expected tables; the temp dir is deleted. This script never deletes or
overwrites anything remotely (rclone --immutable): retention is the bucket's lifecycle rule, and the storage key
should not be allowed to delete at all — a stolen Mac cannot wipe the third copy.

Config: KEY=VALUE lines in ~/.config/proqnozai-backup/offsite.env (or $PROQNOZAI_OFFSITE_CONFIG); the environment
overrides the file. Archive locations, openssl and healthchecks come from pull_backups.py's config.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pull_backups", HERE / "pull_backups.py")
pb = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("pull_backups", pb)
_spec.loader.exec_module(pb)

HOME = Path.home()
DEFAULTS: dict[str, str] = {
    "RCLONE_BIN": "/opt/homebrew/bin/rclone",
    "REMOTE": "",                       # rclone remote + bucket, e.g. proqnozai-offsite:proqnozai-backups-x7k2
    "RCLONE_CONFIG_KEYCHAIN": "proqnozai-offsite-rclone",   # Keychain item with the rclone config password; "" = none
    "MANUAL_DIR": str(HOME / ".proqnozai-backups"),         # hand-made bot-backup-*.db.enc + .db.manifest
    "RCLONE_TIMEOUT": "900",
    "LOCK_WAIT": "1200",                # seconds to wait for a running pull of the same kind
    "MAX_AGE_HOURS": "36",              # the newest off-site archive of each kind must be younger: else /fail
}
MANUAL_RE = re.compile(r"bot-backup-\d{8}T\d{6}Z\.db\.enc")
SERVICE_RE = re.compile(r"service=(proqnozai-[a-z0-9-]+) account=([A-Za-z0-9._-]+)")
REMOTE_PATH_RE = re.compile(r"(auto/(bot|miniapp)/(bot|miniapp)-\d{8}T\d{6}Z\.tar\.enc|manual/bot-backup-\d{8}T\d{6}Z\.db\.enc)")
STATE = ".offsite-verified.json"
ERRORS = (pb.BackupError, OSError, subprocess.SubprocessError, sqlite3.Error, tarfile.TarError, ValueError,
          KeyError, IndexError, AttributeError, TypeError)   # a malformed manifest is a reported failure, never an unpinged crash


class OffsiteError(pb.BackupError):
    pass


def load_offsite(env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    values = dict(DEFAULTS)
    path = Path(env.get("PROQNOZAI_OFFSITE_CONFIG", str(HOME / ".config/proqnozai-backup/offsite.env"))).expanduser()
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, val = line.partition("=")
            if not sep or key.strip() not in DEFAULTS:
                raise OffsiteError(f"config {path}: unknown or malformed line {line[:40]!r}")
            values[key.strip()] = val.strip()
    for key in DEFAULTS:
        if key in env:
            values[key] = env[key]
    if not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9._/-]+", values["REMOTE"]):
        raise OffsiteError("REMOTE must be set to <rclone remote>:<bucket>[/prefix]")
    for key in ("RCLONE_TIMEOUT", "LOCK_WAIT", "MAX_AGE_HOURS"):
        if not values[key].isdigit():
            raise OffsiteError(f"{key} must be a whole number")
    return values


@dataclass(frozen=True)
class Item:
    local: Path
    manifest: Path
    rel: str            # path under REMOTE: auto/<kind>/<name> or manual/<name>

    @property
    def kind(self) -> str:
        return self.rel.split("/")[1] if self.rel.startswith("auto/") else "manual"


def manifest_name(name: str) -> str:
    return name.replace(".tar.enc", ".manifest.json") if name.endswith(".tar.enc") else name[: -len(".enc")] + ".manifest"


def item_for(path: Path) -> Item:
    path = path.expanduser()
    if pb.ARCHIVE_RE.fullmatch(path.name):
        rel = f"auto/{pb.ARCHIVE_RE.fullmatch(path.name).group(1)}/{path.name}"
    elif MANUAL_RE.fullmatch(path.name):
        rel = f"manual/{path.name}"
    else:
        raise OffsiteError(f"not a backup archive name: {path.name[:80]}")
    man = path.with_name(manifest_name(path.name))
    if not path.is_file() or not man.is_file():
        raise OffsiteError(f"{path.name}: archive or its manifest is missing")
    return Item(path, man, rel)


ORPHAN_SECONDS = 3600   # an archive without its manifest for longer than this is a broken pull, not one in flight


def local_items(cfg: pb.Config, ocfg: dict[str, str]) -> tuple[list[Item], list[str]]:
    """Complete local archives, and the names of archives that stayed without a manifest (a pull renames the
    archive just before its manifest: a fresh one is in flight and skipped, an old one is reported)."""
    found: list[Path] = []
    for kind in pb.KINDS:
        found += sorted((cfg.backup_dir / kind).glob(f"{kind}-*.tar.enc"))
    found += sorted(Path(ocfg["MANUAL_DIR"]).expanduser().glob("bot-backup-*.db.enc"))
    items, orphans = [], []
    for p in found:
        try:
            items.append(item_for(p))
        except OffsiteError:
            try:
                if p.stat().st_mtime < time.time() - ORPHAN_SECONDS:
                    orphans.append(p.name)
            except OSError:
                continue            # removed by retention meanwhile
    return items, orphans


def _keychain(service: str) -> str:
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", "proqnozai", "-w"],
                       capture_output=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        raise OffsiteError(f"no rclone config password in the Keychain (service {service})")
    return r.stdout.decode().rstrip("\n")


def rclone_env(ocfg: dict[str, str]) -> dict[str, str]:
    """rclone's config (it holds the storage key) is encrypted; its password goes to the child via the environment,
    readable only by this user's processes — never argv, never a file."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}
    if ocfg["RCLONE_CONFIG_KEYCHAIN"]:
        env["RCLONE_CONFIG_PASS"] = _keychain(ocfg["RCLONE_CONFIG_KEYCHAIN"])
    return env


def rclone(ocfg: dict[str, str], env: dict[str, str], args: list[str]) -> str:
    r = subprocess.run([ocfg["RCLONE_BIN"], *args, "--retries", "3", "--low-level-retries", "10",
                        "--contimeout", "30s", "--timeout", "5m", "--ask-password=false"],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=int(ocfg["RCLONE_TIMEOUT"]))
    if r.returncode != 0:
        lines = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
        raise OffsiteError(f"rclone {args[0]} failed: {(lines[-1] if lines else f'exit {r.returncode}')[-160:]}")
    return r.stdout


def remote(ocfg: dict[str, str], rel: str) -> str:
    return f"{ocfg['REMOTE'].rstrip('/')}/{rel}"


def remote_files(ocfg: dict[str, str], env: dict[str, str]) -> set[str]:
    """Every file under REMOTE as a relative path (one recursive listing)."""
    try:
        out = rclone(ocfg, env, ["lsf", "-R", "--files-only", ocfg["REMOTE"]])
    except OffsiteError as exc:
        if "directory not found" in str(exc):
            return set()
        raise
    return {line.strip() for line in out.splitlines() if line.strip()}


def upload(ocfg: dict[str, str], env: dict[str, str], item: Item) -> None:
    """Archive first, manifest second: a remote manifest means the archive beside it was fully stored. --immutable:
    an existing remote file with other content is an error, never replaced (the same content is a no-op)."""
    for src, name in ((item.local, Path(item.rel).name), (item.manifest, manifest_name(Path(item.rel).name))):
        rclone(ocfg, env, ["copyto", "--immutable", str(src), remote(ocfg, f"{Path(item.rel).parent}/{name}")])


def _with_key(cfg: pb.Config, manifest_text: str) -> pb.Config:
    """The Keychain item named by the manifest (auto and manual archives use different passphrases)."""
    m = SERVICE_RE.search(manifest_text)
    if not m:
        raise OffsiteError("manifest does not name a proqnozai Keychain item")
    return pb.Config({**cfg.values, "KEYCHAIN_SERVICE": m.group(1), "KEYCHAIN_ACCOUNT": m.group(2)})


def _check_manual(cfg: pb.Config, archive: Path, manifest: Path, tmp: Path) -> str:
    lines = manifest.read_text().splitlines()
    first = lines[0].split() if lines else []
    if len(first) != 2 or first[1] != archive.name or not re.fullmatch(r"[0-9a-f]{64}", first[0]):
        raise OffsiteError("manual manifest: first line must be '<sha256>  <archive name>'")
    plain = next((ln.split(maxsplit=1)[1].strip() for ln in lines
                  if ln.startswith("plaintext_sha256 ") and len(ln.split(maxsplit=1)) == 2), "")
    if not re.fullmatch(r"[0-9a-f]{64}", plain):
        raise OffsiteError("manual manifest: no plaintext_sha256")
    if pb.sha256_file(archive) != first[0]:
        raise OffsiteError("archive does not match its manifest (ciphertext SHA-256)")
    kcfg = _with_key(cfg, manifest.read_text())
    db = tmp / "restored.db"
    proc = pb._openssl(kcfg, pb.passphrase(kcfg), ["-d", "-in", str(archive), "-out", str(db)], stderr=subprocess.PIPE)
    try:
        _, err = proc.communicate(timeout=600)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        raise OffsiteError(f"decrypt failed: {err.decode(errors='replace').strip()[:120]}")
    if pb.sha256_file(db) != plain:
        raise OffsiteError("decrypted database does not match the manifest (plaintext SHA-256)")
    info = pb.check_db(db, pb.LAYOUT["bot"][1])
    return f"integrity={info['integrity']} rows={sum(info['tables'].values())}"


def _check_auto(cfg: pb.Config, archive: Path, manifest: Path, tmp: Path) -> str:
    man = pb._manifest(archive)                    # ciphertext SHA-256 against the downloaded manifest
    kcfg = _with_key(cfg, man.get("key", ""))
    dest = tmp / "d"
    dest.mkdir(mode=0o700)
    _, files = pb.decrypt_and_check(kcfg, pb.passphrase(kcfg), pb._kind_of(archive), archive, dest,
                                    expect_sha=man["plaintext_tar_sha256"])
    pb._check_against(man, files)
    return "; ".join(f"{Path(n).name} integrity={v['integrity']} rows={sum(v['tables'].values())}"
                     for n, v in files.items())


def verify_remote(cfg: pb.Config, ocfg: dict[str, str], env: dict[str, str], rel: str,
                  local_manifest: Path | None = None) -> str:
    """Downloads the stored copy and proves it restorable; plaintext only in a private temp dir under .work
    (removed here, and by pull_backups' stale sweep after a SIGKILL)."""
    if not REMOTE_PATH_RE.fullmatch(rel):
        raise OffsiteError(f"not a backup path: {rel[:80]}")
    work = pb._private_dir(cfg.backup_dir / ".work")
    name = Path(rel).name
    with tempfile.TemporaryDirectory(dir=work, prefix="offsite-") as t:
        tmp = Path(t)
        archive, manifest = tmp / name, tmp / manifest_name(name)
        rclone(ocfg, env, ["copyto", remote(ocfg, rel), str(archive)])
        rclone(ocfg, env, ["copyto", remote(ocfg, f"{Path(rel).parent}/{manifest.name}"), str(manifest)])
        if local_manifest is not None and manifest.read_bytes() != local_manifest.read_bytes():
            raise OffsiteError("remote manifest differs from the local one")
        check = _check_auto if rel.startswith("auto/") else _check_manual
        return check(cfg, archive, manifest, tmp)


def _load_state(cfg: pb.Config) -> set[str]:
    try:
        return set(json.loads((cfg.backup_dir / STATE).read_text()))
    except (OSError, ValueError):
        return set()


def _save_state(cfg: pb.Config, names: set[str]) -> None:
    tmp = cfg.backup_dir / f".{STATE}.partial"
    tmp.write_text(json.dumps(sorted(names), indent=0))
    tmp.chmod(0o600)
    os.rename(tmp, cfg.backup_dir / STATE)


def _kind_lock(cfg: pb.Config, kind: str, wait: int):
    """The pull's own lock of this kind: retention never deletes an archive while it is being uploaded."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return pb._lock(cfg, kind)
        except pb.BackupError:
            if time.monotonic() >= deadline:
                raise OffsiteError(f"a {kind} pull is still running")
            time.sleep(5)


def push_one(cfg: pb.Config, ocfg: dict[str, str], env: dict[str, str], item: Item, present: set[str]) -> str:
    lock = _kind_lock(cfg, item.kind, int(ocfg["LOCK_WAIT"])) if item.kind != "manual" else None
    try:
        if not item.local.is_file():
            raise OffsiteError(f"{item.local.name} disappeared (retention)")
        mrel = f"{Path(item.rel).parent}/{item.manifest.name}"
        if not (item.rel in present and mrel in present):
            upload(ocfg, env, item)
        return verify_remote(cfg, ocfg, env, item.rel, local_manifest=item.manifest)
    finally:
        if lock is not None:
            lock.close()


def cmd_sync(cfg: pb.Config, ocfg: dict[str, str]) -> int:
    ts = time.strftime("%FT%TZ", time.gmtime())
    done, failed = 0, []
    try:
        lock = pb._lock(cfg, "offsite")
        try:
            if err := pb.sweep_stale(cfg):
                raise OffsiteError(err)
            env = rclone_env(ocfg)
            present = remote_files(ocfg, env)
            # Verified earlier counts only while both files are still in the bucket (a lost manifest = re-check).
            verified = {r for r in _load_state(cfg)
                        if r in present and f"{Path(r).parent}/{manifest_name(Path(r).name)}" in present}
            # A pull running right now (e.g. both jobs fired at wake-up) is waited for before the scan, so its
            # archive goes off site in this run. A pull that starts after this point is the next run's business
            # (its own healthchecks ping reports it meanwhile).
            for kind in pb.KINDS:
                _kind_lock(cfg, kind, int(ocfg["LOCK_WAIT"])).close()
            checked: set[str] = set()
            items, orphans = local_items(cfg, ocfg)
            for item in items:
                if item.rel in verified:
                    continue
                try:
                    push_one(cfg, ocfg, env, item, present)
                except ERRORS as exc:
                    failed.append(f"{item.local.name}: {exc}")
                    continue
                verified.add(item.rel)
                checked.add(item.rel)
                _save_state(cfg, verified)
                done += 1
            failed += [f"{name}: archive without a manifest (interrupted pull?)" for name in orphans]
            newest = {k: max((r for r in verified if r.startswith(f"auto/{k}/")), default="") for k in pb.KINDS}
            # The copies a restore would use are proven again on every run, not trusted from the local record:
            # a stored file changed or damaged after its first check turns the check red.
            for rel in newest.values():
                if rel and rel not in checked:
                    local = next((i.manifest for i in items if i.rel == rel), None)
                    try:
                        verify_remote(cfg, ocfg, env, rel, local_manifest=local)
                    except ERRORS as exc:
                        failed.append(f"{Path(rel).name} (re-check): {exc}")
        finally:
            lock.close()
    except ERRORS as exc:
        failed.append(str(exc))
        newest = {}
    if failed:
        reason = "; ".join(failed)[:900]
        print(f"{ts} offsite FAIL: {reason}")
        pb.hc_ping(cfg, "offsite", False, f"offsite backup failed: {reason}")
        return 1
    cutoff = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - int(ocfg["MAX_AGE_HOURS"]) * 3600))
    # Names carry the UTC stamp: a pull that keeps failing (or a Mac that stopped pulling) shows here as well.
    missing = [k for k in pb.KINDS if not newest.get(k) or Path(newest[k]).name.split("-", 1)[1][:16] < cutoff]
    summary = f"{done} uploaded and verified; newest offsite (re-verified): " + ", ".join(
        Path(v).name for v in newest.values() if v) + (
        f"; none younger than {ocfg['MAX_AGE_HOURS']} h for {missing}" if missing else "")
    if missing:
        print(f"{ts} offsite FAIL: {summary}")
        pb.hc_ping(cfg, "offsite", False, summary)
        return 1
    if not pb.hc_ping(cfg, "offsite", True, summary):
        print(f"{ts} offsite FAIL: {summary}, but the healthchecks ping was not delivered")
        return 1
    print(f"{ts} offsite OK: {summary}")
    return 0


def cmd_push(cfg: pb.Config, ocfg: dict[str, str], paths: list[str]) -> int:
    rc = 0
    try:
        env = rclone_env(ocfg)
        present = remote_files(ocfg, env)
    except ERRORS as exc:
        print(f"FAIL: {exc}")
        return 1
    state = _load_state(cfg)
    for p in paths:
        try:
            item = item_for(Path(p))
            result = push_one(cfg, ocfg, env, item, present)
        except ERRORS as exc:
            print(f"FAIL: {Path(p).name}: {exc}")
            rc = 1
            continue
        state.add(item.rel)
        _save_state(cfg, state)
        print(f"PASS: {remote(ocfg, item.rel)} stored and verified from the remote copy ({result})")
    return rc


def cmd_verify(cfg: pb.Config, ocfg: dict[str, str], rel: str) -> int:
    try:
        if err := pb.sweep_stale(cfg):
            raise OffsiteError(err)
        result = verify_remote(cfg, ocfg, rclone_env(ocfg), rel)
    except ERRORS as exc:
        print(f"FAIL: {rel[:80]}: {exc}")
        return 1
    print(f"PASS: {rel} downloads, matches its manifest, decrypts, integrity ok ({result})")
    return 0


def cmd_fetch(ocfg: dict[str, str], rel: str, dest: Path) -> int:
    """Ciphertext + manifest into a new 0700 dir — e.g. to run restore-drill-local.sh on the offsite copy."""
    try:
        if not REMOTE_PATH_RE.fullmatch(rel):
            raise OffsiteError(f"not a backup path: {rel[:80]}")
        env = rclone_env(ocfg)
        dest.mkdir(mode=0o700)
        name = Path(rel).name
        rclone(ocfg, env, ["copyto", remote(ocfg, rel), str(dest / name)])
        rclone(ocfg, env, ["copyto", remote(ocfg, f"{Path(rel).parent}/{manifest_name(name)}"),
                           str(dest / manifest_name(name))])
    except ERRORS as exc:
        print(f"FAIL: {exc}")
        return 1
    print(dest / Path(rel).name)
    return 0


def cmd_list(ocfg: dict[str, str]) -> int:
    try:
        files = remote_files(ocfg, rclone_env(ocfg))
    except ERRORS as exc:
        print(f"FAIL: {exc}")
        return 1
    for rel in sorted(files):
        if REMOTE_PATH_RE.fullmatch(rel):
            print(rel)
    return 0


def _terminate(signum, _frame) -> None:
    raise SystemExit(128 + signum)      # unwinds: temp dirs with plaintext are removed by their context managers


def main(argv: list[str]) -> int:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    try:
        cfg = pb.load_config()
        ocfg = load_offsite()
    except pb.BackupError as exc:
        print(f"FAIL: {exc}")
        return 2
    if argv == ["sync"]:
        return cmd_sync(cfg, ocfg)
    if argv == ["list"]:
        return cmd_list(ocfg)
    if len(argv) >= 2 and argv[0] == "push":
        return cmd_push(cfg, ocfg, argv[1:])
    if len(argv) == 2 and argv[0] == "verify":
        return cmd_verify(cfg, ocfg, argv[1])
    if len(argv) == 3 and argv[0] == "fetch":
        return cmd_fetch(ocfg, argv[1], Path(argv[2]))
    print(__doc__.split("\n\n")[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
