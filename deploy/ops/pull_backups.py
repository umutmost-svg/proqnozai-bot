"""Mac half of the off-server backups (docs/ops/infra-hardening.md, section 3). Stdlib only.

    python3 pull_backups.py pull bot|miniapp|all     # launchd, twice a day
    python3 pull_backups.py verify <archive.tar.enc>  # decrypt in a temp dir, check, delete: PASS/FAIL
    python3 pull_backups.py extract <archive.tar.enc> <new dir>   # for restore drills (restore-drill-local.sh)

`pull` asks the server's forced-command key for a fresh consistent copy (backup-export.sh), encrypts the stream
on the fly (openssl enc, AES-256 + PBKDF2, passphrase from the macOS Keychain) so plaintext never touches this
disk on the way in, then proves the archive restorable: decrypts it into a private temp dir, checks the
SHA-256 against the stream, the tar layout, PRAGMA integrity_check and the expected tables of every database,
and deletes the temp dir. Only then the archive gets its final name, a manifest (hashes, row counts — no row
contents), an optional mirror copy (e.g. an iCloud Drive folder: ciphertext only) and retention. Result goes to
healthchecks.io: ping on success, /fail with the reason otherwise (URL files, 0600, never printed).

Config: KEY=VALUE lines in ~/.config/proqnozai-backup/config.env (or $PROQNOZAI_BACKUP_CONFIG); the
environment overrides the file. Keys and defaults: see DEFAULTS.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

HOME = Path.home()
DEFAULTS: dict[str, str] = {
    "SSH_TARGET": "root@164.90.180.208",
    "SSH_KEY": str(HOME / ".ssh/proqnozai_backup_ed25519"),
    "SSH_BIN": "/usr/bin/ssh",
    "OPENSSL": "/opt/homebrew/bin/openssl",
    "KEYCHAIN_SERVICE": "proqnozai-auto-backup",
    "KEYCHAIN_ACCOUNT": "proqnozai",
    "BACKUP_DIR": str(HOME / ".proqnozai-backups/auto"),
    "MIRROR_DIR": "",          # optional second place for the ciphertext; must hold the MIRROR_MARKER file
    "KEEP_DAYS": "30",
    "MIN_KEEP": "14",          # the newest N archives of a kind are never deleted, whatever their age
    "HC_DIR": str(HOME / ".config/proqnozai-backup"),   # hc-bot-backup, hc-miniapp-backup
    "SSH_TIMEOUT": "900",
}
CIPHER = ["-aes-256-cbc", "-pbkdf2", "-iter", "300000", "-md", "sha256"]
CIPHER_DESC = "openssl enc aes-256-cbc pbkdf2 iter=300000 md=sha256 salt"
MIRROR_MARKER = ".proqnozai-backup-mirror"
KINDS = ("bot", "miniapp")
DIR_RE = re.compile(r"\d{8}T\d{6}Z(-\d+)?")   # backup.py's directory name inside the Mini App volume
ARCHIVE_RE = re.compile(r"(bot|miniapp)-(\d{8}T\d{6}Z)\.tar\.enc")
# Expected tar layout per kind and the tables each database must have (a restored file without them is not ours).
LAYOUT = {
    "bot": (re.compile(r"bot-backup-\d{8}T\d{6}Z\.db"), {"users"}),
    "miniapp": (re.compile(r"\d{8}T\d{6}Z(-\d+)?/(miniapp\.db|miniapp\.forecasts\.db)"), None),
}
MINIAPP_TABLES = {"miniapp.db": {"users", "sessions"}, "miniapp.forecasts.db": {"ai_forecasts"}}
CHUNK = 1 << 16


class BackupError(Exception):
    pass


@dataclass
class Config:
    values: dict[str, str]

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    @property
    def backup_dir(self) -> Path:
        return Path(self["BACKUP_DIR"]).expanduser()


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    values = dict(DEFAULTS)
    path = Path(env.get("PROQNOZAI_BACKUP_CONFIG", str(HOME / ".config/proqnozai-backup/config.env"))).expanduser()
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, val = line.partition("=")
            if not sep or key.strip() not in DEFAULTS:
                raise BackupError(f"config {path}: unknown or malformed line {line[:40]!r}")
            values[key.strip()] = val.strip()
    for key in DEFAULTS:
        if key in env:
            values[key] = env[key]
    for key in ("KEEP_DAYS", "MIN_KEEP", "SSH_TIMEOUT"):
        if not values[key].isdigit() or int(values[key]) < 1:
            raise BackupError(f"{key} must be a positive whole number")
    return Config(values)


def passphrase(cfg: Config) -> bytes:
    """The archive passphrase from the login Keychain; never written anywhere, handed to openssl over a pipe."""
    r = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", cfg["KEYCHAIN_SERVICE"], "-a", cfg["KEYCHAIN_ACCOUNT"], "-w"],
        capture_output=True, timeout=30,
    )
    pw = r.stdout.rstrip(b"\n")
    if r.returncode != 0 or len(pw) < 20:
        raise BackupError("no usable passphrase in the Keychain (service %s)" % cfg["KEYCHAIN_SERVICE"])
    return pw


def _openssl(cfg: Config, pw: bytes, args: list[str], **kw) -> subprocess.Popen:
    """openssl reading the passphrase from an inherited pipe (fd:N): not in argv, env or any file."""
    r, w = os.pipe()
    try:
        proc = subprocess.Popen([cfg["OPENSSL"], "enc", *args, *CIPHER, "-pass", f"fd:{r}"], pass_fds=(r,), **kw)
    finally:
        os.close(r)
    with os.fdopen(w, "wb") as f:
        f.write(pw + b"\n")
    return proc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def check_db(path: Path, required: set[str]) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1")]
        counts = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()
    if integrity != "ok":
        raise BackupError(f"{path.name}: integrity_check = {integrity[:80]}")
    missing = required - set(tables)
    if missing:
        raise BackupError(f"{path.name}: missing tables {sorted(missing)}")
    return {"integrity": integrity, "tables": counts}


def decrypt_and_check(cfg: Config, pw: bytes, kind: str, archive: Path, dest: Path,
                      expect_sha: str | None = None) -> tuple[str, dict]:
    """Decrypts `archive` into the existing private dir `dest`, validates layout and databases.
    Returns (plaintext tar sha256, {member: {size, sha256, integrity, tables}})."""
    tar_path = dest / "stream.tar"
    proc = _openssl(cfg, pw, ["-d", "-in", str(archive), "-out", str(tar_path)], stderr=subprocess.PIPE)
    _, err = proc.communicate(timeout=600)
    if proc.returncode != 0:
        raise BackupError(f"decrypt failed: {err.decode(errors='replace').strip()[:120]}")
    tar_sha = sha256_file(tar_path)
    if expect_sha and tar_sha != expect_sha:
        raise BackupError("decrypted stream does not match the recorded SHA-256")
    pattern, _ = LAYOUT[kind]
    files: dict[str, dict] = {}
    out = dest / "x"
    out.mkdir(mode=0o700)
    with tarfile.open(tar_path, "r:") as tar:
        members = tar.getmembers()
        for m in members:
            name = m.name.rstrip("/")
            if m.isdir() and kind == "miniapp" and DIR_RE.fullmatch(name):
                continue
            if not m.isfile() or not pattern.fullmatch(name):
                raise BackupError(f"unexpected entry in the archive: {name[:80]!r}")
        tar.extractall(out, filter="data")
    tar_path.unlink()
    names = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    if kind == "bot" and len(names) != 1:
        raise BackupError(f"expected one bot database, found {len(names)}")
    if kind == "miniapp" and sorted(Path(n).name for n in names) != sorted(MINIAPP_TABLES):
        raise BackupError(f"expected {sorted(MINIAPP_TABLES)}, found {names}")
    for name in names:
        p = out / name
        required = LAYOUT["bot"][1] if kind == "bot" else MINIAPP_TABLES[p.name]
        files[name] = {"size": p.stat().st_size, "sha256": sha256_file(p), **check_db(p, required)}
    return tar_sha, files


def clean_kind(cfg: Config, kind: str) -> tuple[Path, Path]:
    """A pull killed mid-way may have left a decrypted copy or a partial archive: removed under the kind's lock,
    before anything that can fail (Keychain, network) — a broken run never keeps plaintext waiting."""
    base = _private_dir(cfg.backup_dir / kind)
    work = _private_dir(cfg.backup_dir / ".work" / kind)
    for leftover in list(work.iterdir()) + list(base.glob(".*.partial")):
        shutil.rmtree(leftover) if leftover.is_dir() and not leftover.is_symlink() else leftover.unlink()
    return base, work


def pull_one(cfg: Config, kind: str, pw: bytes, now: datetime | None = None) -> str:
    base, work = clean_kind(cfg, kind)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    final = base / f"{kind}-{stamp}.tar.enc"
    manifest = base / f"{kind}-{stamp}.manifest.json"
    if final.exists() or manifest.exists():
        raise BackupError(f"{final.name} already exists")
    partial = base / f".{final.name}.partial"
    ssh = enc = None
    try:
        # stderr of both children goes to unnamed temp files: a chatty child can never block on a full pipe.
        ssh_log, enc_log = tempfile.TemporaryFile(dir=work), tempfile.TemporaryFile(dir=work)
        ssh = subprocess.Popen(
            [cfg["SSH_BIN"], "-i", cfg["SSH_KEY"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=4", cfg["SSH_TARGET"], kind],
            stdout=subprocess.PIPE, stderr=ssh_log, stdin=subprocess.DEVNULL,
        )
        watchdog = threading.Timer(int(cfg["SSH_TIMEOUT"]), ssh.kill)   # a hung server end never hangs launchd
        watchdog.start()
        enc = _openssl(cfg, pw, ["-salt", "-out", str(partial)], stdin=subprocess.PIPE, stderr=enc_log)
        h, size = hashlib.sha256(), 0
        try:
            for chunk in iter(lambda: ssh.stdout.read(CHUNK), b""):
                h.update(chunk)
                size += len(chunk)
                enc.stdin.write(chunk)
        finally:
            enc.stdin.close()
            ssh_rc = ssh.wait()
            watchdog.cancel()
        enc_rc = enc.wait(timeout=120)
        ssh_log.seek(0)
        enc_log.seek(0)
        ssh_err = ssh_log.read().decode(errors="replace").strip()
        enc_err = enc_log.read().decode(errors="replace").strip()
        if enc_rc != 0:
            raise BackupError(f"encrypt failed: {enc_err[:120]}")
        if ssh_rc != 0:
            last = ssh_err.splitlines()[-1] if ssh_err else f"ssh exit {ssh_rc}"
            raise BackupError(f"server export failed: {last[:160]}")
        if size == 0:
            raise BackupError("server sent an empty stream")
        tar_sha = h.hexdigest()
        tar_sha2, files = decrypt_and_check(cfg, pw, kind, partial, work, expect_sha=tar_sha)
        data = {
            "kind": kind, "stamp": stamp, "created_utc": stamp, "cipher": CIPHER_DESC,
            "key": f"macOS Keychain service={cfg['KEYCHAIN_SERVICE']} account={cfg['KEYCHAIN_ACCOUNT']}",
            "ciphertext_sha256": sha256_file(partial), "plaintext_tar_sha256": tar_sha2,
            "plaintext_tar_bytes": size, "files": files,
        }
        man_tmp = base / f".{manifest.name}.partial"
        man_tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        man_tmp.chmod(0o600)
        partial.chmod(0o600)
        os.rename(partial, final)
        os.rename(man_tmp, manifest)
    finally:
        for proc in (ssh, enc):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
        for p in list(work.iterdir()):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        for p in base.glob(".*.partial"):
            p.unlink()
    mirror(cfg, kind, final, manifest)
    removed = retention(base, kind, int(cfg["KEEP_DAYS"]), int(cfg["MIN_KEEP"]), now)
    rows = ", ".join(f"{Path(n).name}:{sum(v['tables'].values())}rows" for n, v in data["files"].items())
    return f"{final.name} ({size} bytes; {rows}; integrity ok; {removed} expired removed)"


def mirror(cfg: Config, kind: str, archive: Path, manifest: Path) -> None:
    if not cfg["MIRROR_DIR"]:
        return
    root = Path(cfg["MIRROR_DIR"]).expanduser()
    if not (root / MIRROR_MARKER).is_file():
        raise BackupError(f"mirror dir has no {MIRROR_MARKER} marker (not mounted / not dedicated)")
    dest = root / kind
    dest.mkdir(exist_ok=True)
    for src in (archive, manifest):
        tmp = dest / f".{src.name}.partial"
        shutil.copyfile(src, tmp)
        if sha256_file(tmp) != sha256_file(src):
            tmp.unlink()
            raise BackupError(f"mirror copy of {src.name} differs")
        os.rename(tmp, dest / src.name)
    retention(dest, kind, int(cfg["KEEP_DAYS"]), int(cfg["MIN_KEEP"]))


def retention(directory: Path, kind: str, keep_days: int, min_keep: int, now: datetime | None = None) -> int:
    """Deletes archives (+ manifests) of `kind` older than keep_days by the stamp in the NAME; the newest
    min_keep always stay (a long gap — a Mac off for weeks — never wipes the history). Touches nothing else."""
    stamps = sorted(
        (m.group(2) for p in directory.iterdir() if (m := ARCHIVE_RE.fullmatch(p.name)) and m.group(1) == kind),
        reverse=True,
    )
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=keep_days)).strftime("%Y%m%dT%H%M%SZ")
    removed = 0
    for s in stamps[min_keep:]:
        if s < cutoff:
            for p in (directory / f"{kind}-{s}.tar.enc", directory / f"{kind}-{s}.manifest.json"):
                p.unlink(missing_ok=True)
            removed += 1
    return removed


def hc_ping(cfg: Config, kind: str, ok: bool, body: str) -> None:
    url_file = Path(cfg["HC_DIR"]).expanduser() / f"hc-{kind}-backup"
    try:
        url = url_file.read_text().strip()
    except OSError:
        print(f"warning: no healthchecks URL file {url_file}", file=sys.stderr)
        return
    if not url.startswith("https://"):
        print(f"warning: {url_file} does not hold an https URL", file=sys.stderr)
        return
    req = urllib.request.Request(url if ok else url + "/fail", data=body[:1000].encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as exc:
        print(f"warning: healthchecks ping failed ({type(exc).__name__})", file=sys.stderr)


def _lock(cfg: Config, kind: str):
    _private_dir(cfg.backup_dir)
    f = open(cfg.backup_dir / f".lock-{kind}", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise BackupError("another pull of this kind is running")
    return f


STALE_SECONDS = 6 * 3600   # a verify/drill work dir older than this belongs to a killed run (a drill takes minutes)


def sweep_stale(cfg: Config) -> None:
    """Removes decrypted leftovers of killed `verify`/`extract`/drill runs (SIGKILL, power loss) under .work.
    Per-kind pull dirs are emptied by the pull itself under its lock."""
    work = cfg.backup_dir / ".work"
    if not work.is_dir():
        return
    cutoff = time.time() - STALE_SECONDS
    for p in work.iterdir():
        if p.name in KINDS:
            continue
        try:
            if p.lstat().st_mtime < cutoff:
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        except OSError as exc:
            print(f"warning: cannot remove stale {p.name}: {exc}", file=sys.stderr)


def cmd_pull(cfg: Config, kinds: list[str]) -> int:
    rc = 0
    sweep_stale(cfg)
    for kind in kinds:
        ts = datetime.now(UTC).strftime("%FT%TZ")
        try:
            lock = _lock(cfg, kind)
            try:
                clean_kind(cfg, kind)
                summary = pull_one(cfg, kind, passphrase(cfg))
            finally:
                lock.close()
        except (BackupError, OSError, subprocess.SubprocessError, sqlite3.Error, tarfile.TarError) as exc:
            reason = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
            print(f"{ts} {kind} FAIL: {reason}")
            hc_ping(cfg, kind, False, f"{kind} backup failed: {reason}")
            rc = 1
            continue
        print(f"{ts} {kind} OK: {summary}")
        hc_ping(cfg, kind, True, summary)
    return rc


def _kind_of(archive: Path) -> str:
    m = ARCHIVE_RE.fullmatch(archive.name)
    if not m:
        raise BackupError(f"not an archive name: {archive.name}")
    return m.group(1)


def _manifest(archive: Path) -> dict:
    """The archive's manifest (required) after checking the ciphertext against it."""
    man = archive.with_name(archive.name.replace(".tar.enc", ".manifest.json"))
    if not man.is_file():
        raise BackupError(f"no manifest {man.name} next to the archive")
    data = json.loads(man.read_text())
    if data.get("ciphertext_sha256") != sha256_file(archive):
        raise BackupError("archive does not match its manifest (ciphertext SHA-256)")
    if not data.get("plaintext_tar_sha256") or not isinstance(data.get("files"), dict):
        raise BackupError("manifest is incomplete")
    return data


def _check_against(manifest: dict, files: dict) -> None:
    """Every decrypted database must equal its manifest entry: name, size, SHA-256, integrity, row counts."""
    if files != manifest["files"]:
        raise BackupError("decrypted databases differ from the manifest (files, hashes or row counts)")


def cmd_verify(cfg: Config, archive: Path) -> int:
    try:
        kind = _kind_of(archive)
        man = _manifest(archive)
        sweep_stale(cfg)
        work = _private_dir(cfg.backup_dir / ".work")
        with tempfile.TemporaryDirectory(dir=work, prefix="verify-") as tmp:
            _, files = decrypt_and_check(cfg, passphrase(cfg), kind, archive, Path(tmp),
                                         expect_sha=man["plaintext_tar_sha256"])
        _check_against(man, files)
    except (BackupError, OSError, subprocess.SubprocessError, sqlite3.Error, tarfile.TarError, ValueError) as exc:
        print(f"FAIL: {archive.name}: {exc}")
        return 1
    for name, info in files.items():
        print(f"  {name}: integrity={info['integrity']} rows={sum(info['tables'].values())}")
    print(f"PASS: {archive.name} decrypts, matches its manifest, every database passes integrity_check")
    return 0


def cmd_extract(cfg: Config, archive: Path, dest: Path) -> int:
    """For drills: verified databases into `dest` (must not exist; created 0700). The caller deletes it."""
    created = done = False
    try:
        kind = _kind_of(archive)
        man = _manifest(archive)
        dest.mkdir(mode=0o700)
        created = True
        _, files = decrypt_and_check(cfg, passphrase(cfg), kind, archive, dest, expect_sha=man["plaintext_tar_sha256"])
        _check_against(man, files)
        done = True
    except (BackupError, OSError, subprocess.SubprocessError, sqlite3.Error, tarfile.TarError, ValueError) as exc:
        print(f"FAIL: {archive.name}: {exc}")
        return 1
    finally:
        if created and not done:   # any unsuccessful exit, also SIGTERM/SIGHUP/Ctrl-C: no partial plaintext stays
            shutil.rmtree(dest, ignore_errors=True)
    print(f"extracted to {dest / 'x'}")
    return 0


def _terminate(signum, _frame) -> None:
    # SIGTERM/SIGHUP (launchd stop, logout, a closed terminal) unwind through the finally/with blocks that
    # delete decrypted data; only SIGKILL skips them — sweep_stale() collects that case on the next run.
    raise SystemExit(128 + signum)


def main(argv: list[str]) -> int:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    try:
        cfg = load_config()
    except BackupError as exc:
        print(f"FAIL: {exc}")
        return 2
    if len(argv) == 2 and argv[0] == "pull" and argv[1] in (*KINDS, "all"):
        return cmd_pull(cfg, list(KINDS) if argv[1] == "all" else [argv[1]])
    if len(argv) == 2 and argv[0] == "verify":
        return cmd_verify(cfg, Path(argv[1]))
    if len(argv) == 3 and argv[0] == "extract":
        return cmd_extract(cfg, Path(argv[1]), Path(argv[2]))
    print(__doc__.split("\n\n")[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
